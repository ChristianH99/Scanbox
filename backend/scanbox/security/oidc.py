"""OIDC login (authorization code + PKCE) against Synology SSO Server or any OIDC provider."""

from __future__ import annotations

import base64
import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx
import structlog
from authlib.jose import JsonWebKey, JsonWebToken
from authlib.jose.errors import JoseError
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from .crypto import random_token

log = structlog.get_logger(__name__)

STATE_COOKIE = "scanbox_oidc"
STATE_MAX_AGE = 600
_jwt = JsonWebToken(["RS256", "RS384", "RS512", "ES256", "ES384", "PS256", "HS256"])


class OidcError(Exception):
    pass


@dataclass
class OidcSettings:
    issuer: str
    client_id: str
    client_secret: str | None
    scopes: str
    admin_claim: str
    admin_value: str
    redirect_uri: str

    @property
    def configured(self) -> bool:
        return bool(self.issuer and self.client_id)


def settings_from_store(ctx) -> OidcSettings | None:  # noqa: ANN001
    st = ctx.settings
    issuer = st.get("oidc.issuer")
    client_id = st.get("oidc.client_id")
    if not issuer or not client_id:
        return None
    return OidcSettings(
        issuer=str(issuer).rstrip("/"),
        client_id=str(client_id),
        client_secret=st.get("oidc.client_secret"),
        scopes=st.get("oidc.scopes") or "openid profile email",
        admin_claim=st.get("oidc.admin_claim") or "groups",
        admin_value=st.get("oidc.admin_value") or "",
        redirect_uri=f"{ctx.config.base_url}/api/auth/oidc/callback",
    )


class OidcClient:
    """Discovery + JWKS cache; stateless otherwise (state lives in a signed cookie)."""

    def __init__(self, http_factory, secret: str) -> None:  # noqa: ANN001
        self.http_factory = http_factory
        self.serializer = URLSafeTimedSerializer(secret, salt="scanbox-oidc-state")
        self._discovery: dict[str, tuple[float, dict[str, Any]]] = {}
        self._jwks: dict[str, tuple[float, Any]] = {}

    async def discovery(self, issuer: str) -> dict[str, Any]:
        cached = self._discovery.get(issuer)
        if cached and time.monotonic() - cached[0] < 3600:
            return cached[1]
        url = issuer.rstrip("/") + "/.well-known/openid-configuration"
        async with self.http_factory(timeout=httpx.Timeout(10.0)) as c:
            r = await c.get(url)
        if r.status_code != 200:
            raise OidcError(f"OIDC discovery failed: HTTP {r.status_code} at {url}")
        doc = r.json()
        for k in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            if k not in doc:
                raise OidcError(f"OIDC discovery document lacks {k}")
        self._discovery[issuer] = (time.monotonic(), doc)
        return doc

    async def jwks(self, uri: str, force: bool = False) -> Any:
        cached = self._jwks.get(uri)
        if cached and not force and time.monotonic() - cached[0] < 3600:
            return cached[1]
        async with self.http_factory(timeout=httpx.Timeout(10.0)) as c:
            r = await c.get(uri)
        if r.status_code != 200:
            raise OidcError(f"JWKS fetch failed: HTTP {r.status_code}")
        keys = JsonWebKey.import_key_set(r.json())
        self._jwks[uri] = (time.monotonic(), keys)
        return keys

    async def start(self, cfg: OidcSettings, next_path: str = "/") -> tuple[str, str]:
        """Returns (authorization URL, signed state cookie value)."""
        disc = await self.discovery(cfg.issuer)
        state = random_token(16)
        nonce = random_token(16)
        verifier = random_token(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        params = {
            "response_type": "code",
            "client_id": cfg.client_id,
            "redirect_uri": cfg.redirect_uri,
            "scope": cfg.scopes,
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        cookie = self.serializer.dumps({"s": state, "n": nonce, "v": verifier, "next": _safe_next(next_path)})
        sep = "&" if "?" in disc["authorization_endpoint"] else "?"
        return disc["authorization_endpoint"] + sep + urlencode(params), cookie

    async def finish(self, cfg: OidcSettings, code: str, state: str, cookie: str | None) -> tuple[dict[str, Any], str]:
        """Validate callback, exchange code, verify ID token. Returns (claims, next_path)."""
        if not cookie:
            raise OidcError("login state missing (cookie expired?) – please try again")
        try:
            data = self.serializer.loads(cookie, max_age=STATE_MAX_AGE)
        except SignatureExpired as e:
            raise OidcError("login took too long – please try again") from e
        except BadSignature as e:
            raise OidcError("invalid login state") from e
        if not state or state != data.get("s"):
            raise OidcError("state mismatch")
        disc = await self.discovery(cfg.issuer)
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": cfg.redirect_uri,
            "client_id": cfg.client_id,
            "code_verifier": data["v"],
        }
        auth = None
        methods = disc.get("token_endpoint_auth_methods_supported") or ["client_secret_basic"]
        if cfg.client_secret:
            if "client_secret_basic" in methods:
                auth = (cfg.client_id, cfg.client_secret)
            else:
                form["client_secret"] = cfg.client_secret
        async with self.http_factory(timeout=httpx.Timeout(15.0)) as c:
            r = await c.post(disc["token_endpoint"], data=form, auth=auth, headers={"Accept": "application/json"})
        if r.status_code != 200:
            raise OidcError(f"token exchange failed: HTTP {r.status_code} {r.text[:300]}")
        tok = r.json()
        id_token = tok.get("id_token")
        if not id_token:
            raise OidcError("provider returned no id_token (is the 'openid' scope enabled?)")
        claims = await self._verify(cfg, disc, id_token, data["n"])
        # Merge userinfo (some providers – incl. Synology – put groups/email there).
        if disc.get("userinfo_endpoint") and tok.get("access_token"):
            try:
                async with self.http_factory(timeout=httpx.Timeout(10.0)) as c:
                    ui = await c.get(disc["userinfo_endpoint"],
                                     headers={"Authorization": f"Bearer {tok['access_token']}"})
                if ui.status_code == 200:
                    info = ui.json()
                    if info.get("sub") in (None, claims.get("sub")):
                        for k, v in info.items():
                            claims.setdefault(k, v)
            except (httpx.HTTPError, json.JSONDecodeError) as e:
                log.info("oidc.userinfo_failed", error=str(e))
        return claims, data.get("next") or "/"

    async def _verify(self, cfg: OidcSettings, disc: dict[str, Any], id_token: str, nonce: str) -> dict[str, Any]:
        options = {
            "iss": {"essential": True, "values": [disc.get("issuer", cfg.issuer), cfg.issuer]},
            "aud": {"essential": True, "value": cfg.client_id},
            "exp": {"essential": True},
            "nonce": {"essential": True, "value": nonce},
        }
        for force in (False, True):
            keys = await self.jwks(disc["jwks_uri"], force=force)
            try:
                claims = _jwt.decode(id_token, keys, claims_options=options)
                claims.validate(leeway=60)
                return dict(claims)
            except JoseError as e:
                if force or "key" not in str(e).lower():
                    raise OidcError(f"invalid id_token: {e}") from e
        raise OidcError("invalid id_token")


def is_admin_from_claims(claims: dict[str, Any], claim: str, values: str) -> bool:
    if not claim or not values:
        return False
    wanted = {v.strip().lower() for v in values.split(",") if v.strip()}
    got = claims.get(claim)
    if got is None:
        return False
    items = got if isinstance(got, list) else str(got).replace(";", ",").split(",") if isinstance(got, str) else [got]
    return any(str(i).strip().lower() in wanted for i in items)


def _safe_next(p: str) -> str:
    if not p or not p.startswith("/") or p.startswith("//") or "\\" in p:
        return "/"
    return p[:500]
