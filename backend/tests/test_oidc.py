"""OIDC login (code + PKCE) against a mock provider; admin role from group claim."""

from __future__ import annotations

import base64
import hashlib
import time
from urllib.parse import parse_qs, urlparse

import httpx
from authlib.jose import JsonWebKey, jwt
from fastapi import FastAPI, Form, HTTPException

ISSUER = "https://sso.example.test/webman/sso"
CLIENT_ID = "scanbox"
SECRET = "client-secret-xyz"


def make_provider(claims_for_code: dict, *, tamper_nonce: bool = False) -> FastAPI:
    key = JsonWebKey.generate_key("RSA", 2048, is_private=True)
    key_dict = key.as_dict(is_private=True)
    key_dict["kid"] = "k1"
    pub = key.as_dict()
    pub["kid"] = "k1"
    app = FastAPI()
    pending: dict[str, dict] = {}
    app.state.pending = pending

    @app.get("/webman/sso/.well-known/openid-configuration")
    def disc() -> dict:
        return {
            "issuer": ISSUER,
            "authorization_endpoint": f"{ISSUER}/authorize",
            "token_endpoint": f"{ISSUER}/token",
            "jwks_uri": f"{ISSUER}/jwks",
            "userinfo_endpoint": f"{ISSUER}/userinfo",
            "token_endpoint_auth_methods_supported": ["client_secret_post"],
        }

    @app.get("/webman/sso/jwks")
    def jwks() -> dict:
        return {"keys": [pub]}

    @app.post("/webman/sso/token")
    def token(grant_type: str = Form(...), code: str = Form(...), code_verifier: str = Form(...),
              client_id: str = Form(...), client_secret: str = Form(...), redirect_uri: str = Form(...)) -> dict:
        p = pending.get(code)
        if p is None or client_secret != SECRET or client_id != CLIENT_ID:
            raise HTTPException(400, "invalid_grant")
        challenge = base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest()).rstrip(b"=").decode()
        if challenge != p["code_challenge"]:
            raise HTTPException(400, "pkce mismatch")
        now = int(time.time())
        payload = {"iss": ISSUER, "aud": CLIENT_ID, "iat": now, "exp": now + 300,
                   "nonce": "evil" if tamper_nonce else p["nonce"], **claims_for_code}
        idt = jwt.encode({"alg": "RS256", "kid": "k1"}, payload, key_dict).decode()
        return {"access_token": "at-1", "id_token": idt, "token_type": "Bearer"}

    @app.get("/webman/sso/userinfo")
    def userinfo() -> dict:
        return {"sub": claims_for_code["sub"], "email": "alice@example.test"}

    return app


async def _login(harness, provider: FastAPI) -> tuple[httpx.AsyncClient, httpx.Response]:  # noqa: ANN001
    c = harness.client()
    r = await c.get("/api/auth/oidc/login", params={"next": "/history"})
    assert r.status_code == 303, r.text
    loc = urlparse(r.headers["location"])
    q = {k: v[0] for k, v in parse_qs(loc.query).items()}
    assert q["code_challenge_method"] == "S256" and q["client_id"] == CLIENT_ID
    assert q["redirect_uri"] == "http://scanbox.test/api/auth/oidc/callback"
    provider.state.pending["code-1"] = q
    r = await c.get("/api/auth/oidc/callback", params={"code": "code-1", "state": q["state"]})
    return c, r


async def _harness(harness_factory, provider):  # noqa: ANN001, ANN202
    def customize(ctx) -> None:  # noqa: ANN001
        ctx.http_client_factory = lambda **kw: httpx.AsyncClient(
            transport=httpx.ASGITransport(app=provider), base_url="https://sso.example.test", **kw)

    from scanbox.api.routes_auth import reset_oidc_client

    reset_oidc_client()
    return await harness_factory(customize=customize, oidc_issuer=ISSUER, oidc_client_id=CLIENT_ID,
                                 oidc_client_secret=SECRET, oidc_admin_value="scanbox-admins")


async def test_oidc_login_creates_user_with_role(harness_factory) -> None:  # noqa: ANN001
    provider = make_provider({"sub": "u-123", "preferred_username": "alice", "name": "Alice A",
                              "groups": ["users", "scanbox-admins"]})
    h = await _harness(harness_factory, provider)
    cfg = (await h.client().get("/api/auth/config")).json()
    assert cfg["oidc_enabled"] is True
    c, r = await _login(h, provider)
    assert r.status_code == 303 and r.headers["location"] == "/history"
    me = (await c.get("/api/auth/me")).json()["user"]
    assert me["username"] == "alice" and me["is_admin"] is True and me["email"] == "alice@example.test"


async def test_oidc_non_admin(harness_factory) -> None:  # noqa: ANN001
    provider = make_provider({"sub": "u-2", "preferred_username": "bob", "groups": ["users"]})
    h = await _harness(harness_factory, provider)
    c, r = await _login(h, provider)
    me = (await c.get("/api/auth/me")).json()["user"]
    assert me["is_admin"] is False


async def test_oidc_rejects_bad_nonce_and_state(harness_factory) -> None:  # noqa: ANN001
    provider = make_provider({"sub": "u-3"}, tamper_nonce=True)
    h = await _harness(harness_factory, provider)
    c, r = await _login(h, provider)
    assert r.headers["location"].startswith("/login?error=")
    assert (await c.get("/api/auth/me")).status_code == 401
    # wrong state
    c2 = h.client()
    await c2.get("/api/auth/oidc/login")
    r = await c2.get("/api/auth/oidc/callback", params={"code": "x", "state": "forged"})
    assert r.headers["location"].startswith("/login?error=")


async def test_open_redirect_blocked(harness_factory) -> None:  # noqa: ANN001
    provider = make_provider({"sub": "u-4", "preferred_username": "eve"})
    h = await _harness(harness_factory, provider)
    c = h.client()
    r = await c.get("/api/auth/oidc/login", params={"next": "//evil.example/x"})
    q = {k: v[0] for k, v in parse_qs(urlparse(r.headers["location"]).query).items()}
    provider.state.pending["code-1"] = q
    r = await c.get("/api/auth/oidc/callback", params={"code": "code-1", "state": q["state"]})
    assert r.headers["location"] == "/"
