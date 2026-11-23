"""All external services Scanbox talks to, as one networked process – for the system test.

    python -m tests.mocks.servers            # in the scanbox:test image

* SMTP capture on :2525 (AUTH PLAIN/LOGIN user ``scanbox`` / ``mailpass``, no TLS)
* Paperless-ngx mock on :8000 (token ``paperless-token``)
* control API on :9000
    GET/DELETE /smtp/messages            captured mails (subject, from, to, attachments)
    POST /smtp/reply {"data": "451 …"}   make the next DATA commands fail (null = accept)
    GET /paperless/state                 uploads + documents of the Paperless mock
    POST /paperless/toggle {...}         offline / fail_status / consume_error …
    /oidc/…                              OpenID provider (code + PKCE, RS256)
    POST /oidc/next-user {claims}        identity returned by the next authorize call
    POST /hook/{name}, GET /hook         webhook receiver
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import uvicorn
from aiosmtpd.controller import Controller
from aiosmtpd.smtp import AuthResult, LoginPassword
from authlib.jose import JsonWebKey, jwt
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse

from .paperless import create_paperless_mock
from .smtp import CapturingHandler

PUBLIC = os.environ.get("MOCKS_PUBLIC_URL", "http://mocks:9000")
ISSUER = f"{PUBLIC}/oidc"
CLIENT_ID = "scanbox"
CLIENT_SECRET = "oidc-secret"  # noqa: S105
SMTP_USER, SMTP_PASS = "scanbox", "mailpass"
PAPERLESS_TOKEN = "paperless-token"  # noqa: S105


def build_control(smtp: CapturingHandler, paperless: FastAPI) -> FastAPI:
    app = FastAPI(title="Scanbox system-test mocks")
    key = JsonWebKey.generate_key("RSA", 2048, is_private=True)
    priv = {**key.as_dict(is_private=True), "kid": "k1"}
    pub = {**key.as_dict(), "kid": "k1"}
    codes: dict[str, dict] = {}
    next_user: dict[str, Any] = {"sub": "alice", "email": "alice@example.test", "name": "Alice",
                                 "preferred_username": "alice", "groups": ["users"]}
    hooks: list[dict] = []

    # ------------------------------------------------------------------ SMTP
    @app.get("/smtp/messages")
    def smtp_messages() -> list[dict]:
        out = []
        for m in smtp.messages:
            msg = m.message
            atts = [{"filename": p.get_filename(), "size": len(p.get_payload(decode=True) or b""),
                     "type": p.get_content_type()} for p in msg.iter_attachments()]
            body = msg.get_body(("plain",))
            out.append({"from": m.mail_from, "to": m.rcpt_tos, "subject": str(msg["subject"]),
                        "message_id": str(msg["message-id"]), "attachments": atts,
                        "body": body.get_content() if body else ""})
        return out

    @app.delete("/smtp/messages")
    def smtp_clear() -> dict:
        smtp.messages.clear()
        return {"ok": True}

    @app.post("/smtp/reply")
    def smtp_reply(body: dict) -> dict:
        smtp.data_reply = body.get("data")
        return {"ok": True}

    # ------------------------------------------------------------------ Paperless
    @app.get("/paperless/state")
    def pl_state() -> dict:
        st = paperless.state.mock
        return {"uploads": [{k: v for k, v in u.items() if k != "document"} for u in st.uploads],
                "documents": list(st.documents.values()), "tasks": list(st.tasks.values())}

    @app.post("/paperless/toggle")
    def pl_toggle(body: dict) -> dict:
        st = paperless.state.mock
        for k, v in body.items():
            if not hasattr(st, k):
                raise HTTPException(400, f"unknown toggle {k}")
            setattr(st, k, v)
        return {"ok": True}

    # ------------------------------------------------------------------ OIDC
    @app.post("/oidc/next-user")
    def oidc_next(claims: dict) -> dict:
        next_user.clear()
        next_user.update(claims)
        return {"ok": True}

    @app.get("/oidc/.well-known/openid-configuration")
    def oidc_disc() -> dict:
        return {
            "issuer": ISSUER,
            "authorization_endpoint": f"{ISSUER}/authorize",
            "token_endpoint": f"{ISSUER}/token",
            "jwks_uri": f"{ISSUER}/jwks",
            "userinfo_endpoint": f"{ISSUER}/userinfo",
            "response_types_supported": ["code"],
            "id_token_signing_alg_values_supported": ["RS256"],
            "token_endpoint_auth_methods_supported": ["client_secret_post", "client_secret_basic"],
            "code_challenge_methods_supported": ["S256"],
        }

    @app.get("/oidc/jwks")
    def oidc_jwks() -> dict:
        return {"keys": [pub]}

    @app.get("/oidc/authorize")
    def oidc_authorize(request: Request) -> RedirectResponse:
        q = request.query_params
        if q.get("client_id") != CLIENT_ID or q.get("response_type") != "code":
            raise HTTPException(400, "bad client/response_type")
        if q.get("code_challenge_method") != "S256" or not q.get("code_challenge"):
            raise HTTPException(400, "PKCE required")
        code = secrets.token_urlsafe(16)
        codes[code] = {"challenge": q["code_challenge"], "nonce": q.get("nonce"), "redirect_uri": q["redirect_uri"],
                       "claims": dict(next_user)}
        return RedirectResponse(f"{q['redirect_uri']}?{urlencode({'code': code, 'state': q.get('state', '')})}", 302)

    @app.post("/oidc/token")
    async def oidc_token(request: Request) -> dict:
        form = await request.form()
        cid, csec = form.get("client_id"), form.get("client_secret")
        auth = request.headers.get("authorization", "")
        if auth.startswith("Basic "):
            cid, csec = base64.b64decode(auth[6:]).decode().split(":", 1)
        c = codes.pop(str(form.get("code")), None)
        if c is None or cid != CLIENT_ID or csec != CLIENT_SECRET:
            raise HTTPException(400, "invalid_grant")
        if form.get("redirect_uri") != c["redirect_uri"]:
            raise HTTPException(400, "redirect_uri mismatch")
        verifier = str(form.get("code_verifier", ""))
        if base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode() != c["challenge"]:
            raise HTTPException(400, "pkce mismatch")
        now = int(time.time())
        payload = {"iss": ISSUER, "aud": CLIENT_ID, "iat": now, "exp": now + 300, "nonce": c["nonce"], **c["claims"]}
        idt = jwt.encode({"alg": "RS256", "kid": "k1"}, payload, priv).decode()
        return {"access_token": "at-" + secrets.token_hex(4), "id_token": idt, "token_type": "Bearer", "expires_in": 300}

    @app.get("/oidc/userinfo")
    def oidc_userinfo() -> dict:
        return dict(next_user)

    # ------------------------------------------------------------------ webhooks
    @app.post("/hook/{name}")
    async def hook(name: str, request: Request) -> dict:
        hooks.append({"name": name, "headers": dict(request.headers), "body": await request.json()})
        return {"ok": True}

    @app.get("/hook")
    def hook_list() -> list[dict]:
        return hooks

    @app.get("/health")
    def health() -> dict:
        return {"ok": True}

    return app


async def main() -> None:
    handler = CapturingHandler()

    def authenticator(server, session, envelope, mechanism, auth_data):  # noqa: ANN001, ANN202
        if isinstance(auth_data, LoginPassword):
            ok = auth_data.login.decode() == SMTP_USER and auth_data.password.decode() == SMTP_PASS
            return AuthResult(success=ok, handled=False)
        return AuthResult(success=False, handled=False)

    smtp = Controller(handler, hostname="0.0.0.0", port=2525, authenticator=authenticator,  # noqa: S104
                      auth_require_tls=False, data_size_limit=100 * 1024 * 1024)
    smtp.start()
    paperless = create_paperless_mock(token=PAPERLESS_TOKEN)
    control = build_control(handler, paperless)
    servers = [
        uvicorn.Server(uvicorn.Config(paperless, host="0.0.0.0", port=8000, log_level="warning")),  # noqa: S104
        uvicorn.Server(uvicorn.Config(control, host="0.0.0.0", port=9000, log_level="warning")),  # noqa: S104
    ]
    print("mocks: smtp :2525, paperless :8000, control/oidc/hooks :9000", flush=True)  # noqa: T201
    try:
        await asyncio.gather(*(s.serve() for s in servers))
    finally:
        smtp.stop()


if __name__ == "__main__":
    asyncio.run(main())
