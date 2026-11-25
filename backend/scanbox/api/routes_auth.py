"""Authentication endpoints: first-start setup, local login, password change, OIDC, logout, me."""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..context import AppContext
from ..db import get_session, run_db
from ..models import User
from ..security import auth
from ..security.oidc import STATE_COOKIE, OidcClient, OidcError, is_admin_from_claims, settings_from_store
from .deps import CtxDep, PrincipalDep
from .schemas import (
    AuthConfigOut,
    ErrorResponse,
    LoginIn,
    MeOut,
    PasswordChangeIn,
    SetupIn,
    UserOut,
    UserPrefsIn,
)

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/api/auth", tags=["auth"])

_oidc_client: OidcClient | None = None


def oidc_client(ctx: AppContext) -> OidcClient:
    global _oidc_client
    if _oidc_client is None:
        _oidc_client = OidcClient(ctx.http_client_factory, ctx.box.sign("oidc-state"))
    return _oidc_client


def reset_oidc_client() -> None:
    global _oidc_client
    _oidc_client = None


def can_change_password(ctx: AppContext, user: User) -> bool:
    if not user.is_local or not user.password_hash:
        return False
    # The env-managed admin is locked like every other setting given in the environment.
    return not (auth.admin_password_locked(ctx.config) and user.username == ctx.config.admin_username)


def me_out(ctx: AppContext, user: User, csrf: str | None) -> MeOut:
    return MeOut(user=UserOut.model_validate(user), csrf_token=csrf, can_change_password=can_change_password(ctx, user))


def _rate_limited(wait: float) -> HTTPException:
    return HTTPException(429, detail={"code": "rate_limited",
                                      "message": f"Zu viele Versuche – bitte {int(wait // 60) + 1} min warten"},
                         headers={"Retry-After": str(int(wait))})


@router.get("/config", response_model=AuthConfigOut)
def auth_config(ctx: AppContext = CtxDep) -> AuthConfigOut:
    cfg = settings_from_store(ctx)
    return AuthConfigOut(oidc_enabled=cfg is not None, oidc_label=ctx.settings.get("oidc.button_label") or "SSO",
                         setup_required=ctx.setup_code is not None,
                         default_admin_username=ctx.config.admin_username)


@router.post("/setup", response_model=MeOut, status_code=201,
             responses={403: {"model": ErrorResponse}, 409: {"model": ErrorResponse}, 429: {"model": ErrorResponse}})
def setup(body: SetupIn, request: Request, response: Response, s: Session = Depends(get_session),
          ctx: AppContext = CtxDep) -> MeOut:
    """First-start setup: create the local admin with the one-time code from the container log, then sign in."""
    closed = HTTPException(409, detail={"code": "setup_closed", "message": "Die Einrichtung ist bereits abgeschlossen"})
    if ctx.setup_code is None:
        raise closed
    ip = auth.client_ip(request)
    wait = auth.setup_limiter.retry_after(ip, "setup")
    if wait:
        raise _rate_limited(wait)
    with auth.setup_lock:
        if ctx.setup_code is None:
            raise closed
        if not auth.check_setup_code(ctx.setup_code, body.code):
            auth.setup_limiter.failure(ip, "setup")
            log.warning("auth.setup_code_rejected", ip=ip)
            raise HTTPException(403, detail={"code": "invalid_setup_code", "message": "Einrichtungscode falsch"})
        if auth.local_admin_exists(s):  # e.g. created by another process
            ctx.setup_code = None
            raise closed
        if s.scalar(select(User.id).where(User.username == body.username)) is not None:
            raise HTTPException(409, detail={"code": "username_taken", "message": "Benutzername ist bereits vergeben"})
        user = User(username=body.username, display_name="Administrator", is_admin=True, is_local=True,
                    password_hash=auth.hash_password(body.password))
        s.add(user)
        s.flush()
        _raw, csrf = auth.create_session(s, user, request, response, "local", ctx.config.cookie_secure)
        s.commit()
        # Closed for good: an admin exists now, so no later start generates a code either.
        ctx.setup_code = None
    auth.setup_limiter.reset()
    log.info("auth.setup_completed", user_id=user.id, username=user.username)
    return me_out(ctx, user, csrf)


@router.post("/login", response_model=MeOut, responses={401: {"model": ErrorResponse}, 429: {"model": ErrorResponse}})
def login(body: LoginIn, request: Request, response: Response, s: Session = Depends(get_session),
          ctx: AppContext = CtxDep) -> MeOut:
    ip = auth.client_ip(request)
    wait = auth.login_limiter.retry_after(ip, body.username)
    if wait:
        raise _rate_limited(wait)
    user = s.scalar(select(User).where(User.username == body.username, User.is_local.is_(True)))
    ok = auth.verify_password(user.password_hash if user else None, body.password)
    if not ok or user is None or user.disabled:
        auth.login_limiter.failure(ip, body.username)
        log.warning("auth.login_failed", username=body.username[:50], ip=ip)
        raise HTTPException(401, detail={"code": "invalid_credentials", "message": "Benutzername oder Passwort falsch"})
    auth.login_limiter.success(ip, body.username)
    _raw, csrf = auth.create_session(s, user, request, response, "local", ctx.config.cookie_secure)
    log.info("auth.login", user_id=user.id, method="local")
    return me_out(ctx, user, csrf)


@router.post("/logout", status_code=204)
def logout(request: Request, response: Response, s: Session = Depends(get_session), ctx: AppContext = CtxDep,
           _p: auth.Principal = PrincipalDep) -> Response:
    auth.delete_session(s, request.cookies.get(auth.SESSION_COOKIE))
    response.status_code = 204
    auth.clear_session_cookies(response, ctx.config.cookie_secure)
    return response


@router.get("/me", response_model=MeOut, responses={401: {"model": ErrorResponse}})
def me(request: Request, p: auth.Principal = PrincipalDep, ctx: AppContext = CtxDep) -> MeOut:
    return me_out(ctx, p.user, getattr(request.state, "csrf_token", None))


@router.post("/password", status_code=204,
             responses={403: {"model": ErrorResponse}, 409: {"model": ErrorResponse}, 429: {"model": ErrorResponse}})
def change_password(body: PasswordChangeIn, request: Request, s: Session = Depends(get_session),
                    ctx: AppContext = CtxDep, p: auth.Principal = PrincipalDep) -> Response:
    """Change the own local password. Signs out all other sessions of this account."""
    user = s.get(User, p.id)
    assert user
    if not user.is_local or not user.password_hash:
        raise HTTPException(409, detail={"code": "no_local_password",
                                          "message": "Dieses Konto meldet sich per SSO an und hat kein Passwort"})
    if not can_change_password(ctx, user):
        raise HTTPException(409, detail={"code": "password_locked",
                                          "message": "Das Passwort ist per Umgebungsvariable festgelegt"})
    ip = auth.client_ip(request)
    wait = auth.login_limiter.retry_after(ip, user.username)
    if wait:
        raise _rate_limited(wait)
    if not auth.verify_password(user.password_hash, body.current_password):
        auth.login_limiter.failure(ip, user.username)
        log.warning("auth.password_change_failed", user_id=user.id, ip=ip)
        raise HTTPException(403, detail={"code": "invalid_current_password",
                                          "message": "Aktuelles Passwort ist falsch"})
    user.password_hash = auth.hash_password(body.new_password)
    ended = auth.delete_other_sessions(s, user.id, p.session_id)
    log.info("auth.password_changed", user_id=user.id, other_sessions_ended=ended)
    return Response(status_code=204)


@router.patch("/me", response_model=UserOut)
def update_me(body: UserPrefsIn, s: Session = Depends(get_session), p: auth.Principal = PrincipalDep) -> UserOut:
    u = s.get(User, p.id)
    assert u
    if body.locale:
        u.locale = body.locale
    if body.theme:
        u.theme = body.theme
    if body.display_name is not None:
        u.display_name = body.display_name.strip()[:200]
    return UserOut.model_validate(u)


@router.get("/oidc/login", include_in_schema=True)
async def oidc_login(next: str = Query("/", max_length=500), ctx: AppContext = CtxDep) -> RedirectResponse:  # noqa: A002
    cfg = settings_from_store(ctx)
    if cfg is None:
        raise HTTPException(404, detail={"code": "oidc_disabled", "message": "SSO ist nicht konfiguriert"})
    try:
        url, cookie = await oidc_client(ctx).start(cfg, next)
    except (OidcError, Exception) as e:  # noqa: BLE001
        log.warning("oidc.start_failed", error=str(e))
        return RedirectResponse("/login?error=sso_unavailable", status_code=303)
    resp = RedirectResponse(url, status_code=303)
    resp.set_cookie(STATE_COOKIE, cookie, max_age=600, httponly=True, secure=ctx.config.cookie_secure,
                    samesite="lax", path="/api/auth/oidc")
    return resp


@router.get("/oidc/callback", include_in_schema=False)
async def oidc_callback(request: Request, code: str = "", state: str = "", error: str = "",
                        ctx: AppContext = CtxDep) -> RedirectResponse:
    if error:
        return RedirectResponse(f"/login?error=sso_{error[:30]}", status_code=303)
    cfg = settings_from_store(ctx)
    if cfg is None:
        return RedirectResponse("/login?error=sso_disabled", status_code=303)
    try:
        claims, next_path = await oidc_client(ctx).finish(cfg, code, state, request.cookies.get(STATE_COOKIE))
    except OidcError as e:
        log.warning("oidc.callback_failed", error=str(e))
        return RedirectResponse("/login?error=sso_failed", status_code=303)
    sub = str(claims.get("sub") or "")
    if not sub:
        return RedirectResponse("/login?error=sso_failed", status_code=303)
    is_admin = is_admin_from_claims(claims, cfg.admin_claim, cfg.admin_value)
    resp = RedirectResponse(next_path, status_code=303)
    outcome = await run_db(_upsert_oidc_user, ctx, request, resp, claims, sub, is_admin)
    if outcome is not None:
        return outcome
    resp.delete_cookie(STATE_COOKIE, path="/api/auth/oidc")
    return resp


def _upsert_oidc_user(ctx: AppContext, request: Request, resp: RedirectResponse, claims: dict, sub: str,
                      is_admin: bool) -> RedirectResponse | None:
    from ..db import session_scope

    with session_scope() as s:
        user = s.scalar(select(User).where(User.sub == sub))
        username = str(claims.get("preferred_username") or claims.get("username") or claims.get("email") or sub)[:150]
        if user is None:
            base, n = username, 1
            reserved = {ctx.config.admin_username.lower()}
            while username.lower() in reserved or \
                    s.scalar(select(User.id).where(User.username == username)) is not None:
                n += 1
                username = f"{base}-{n}"[:150]
            user = User(sub=sub, username=username)
            s.add(user)
        if user.disabled:
            return RedirectResponse("/login?error=account_disabled", status_code=303)
        user.display_name = str(claims.get("name") or user.display_name or username)[:200]
        user.email = (str(claims["email"])[:320] if claims.get("email") else user.email)
        user.is_admin = is_admin
        s.flush()
        auth.create_session(s, user, request, resp, "oidc", ctx.config.cookie_secure)
        # Claim *names* only (no values) – helps configuring the admin claim.
        log.info("auth.login", user_id=user.id, method="oidc", admin=is_admin, claims=sorted(claims))
    return None
