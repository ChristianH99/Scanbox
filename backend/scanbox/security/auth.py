"""Sessions, CSRF, local admin, login rate limiting, API tokens and request authentication."""

from __future__ import annotations

import secrets
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import timedelta

import structlog
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..config import AppConfig
from ..db import get_session, session_scope, utcnow
from ..models import ApiToken, SessionRow, User
from .crypto import constant_time_eq, random_token, sha256_hex

log = structlog.get_logger(__name__)

SESSION_COOKIE = "scanbox_session"
CSRF_COOKIE = "scanbox_csrf"
CSRF_HEADER = "x-csrf-token"
IDLE_TIMEOUT = timedelta(days=7)
ABSOLUTE_TIMEOUT = timedelta(days=30)
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

_ph = PasswordHasher()


def hash_password(pw: str) -> str:
    return _ph.hash(pw)


def verify_password(hash_: str | None, pw: str) -> bool:
    if not hash_:
        # Burn comparable time to avoid user enumeration by timing.
        try:
            _ph.verify(_DUMMY_HASH, pw)
        except (VerificationError, InvalidHashError):
            pass
        return False
    try:
        return _ph.verify(hash_, pw)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


_DUMMY_HASH = _ph.hash("scanbox-dummy-password")


# ------------------------------------------------------------------ rate limiting
class LoginRateLimiter:
    """Sliding-window limiter: N failures per (ip, username) and per ip."""

    def __init__(self, per_user: int = 5, per_ip: int = 20, per_name: int = 20, window_s: float = 900) -> None:
        self.per_user = per_user
        self.per_ip = per_ip
        self.per_name = per_name  # across all IPs – defence in depth against forged client IPs
        self.window = window_s
        self._fails: dict[str, deque[float]] = defaultdict(deque)

    def _prune(self, key: str, now: float) -> deque[float]:
        q = self._fails[key]
        while q and q[0] <= now - self.window:
            q.popleft()
        if not q:
            self._fails.pop(key, None)
            return deque()
        return q

    def retry_after(self, ip: str, username: str) -> float:
        now = time.monotonic()
        for key, limit in ((f"u:{ip}:{username.lower()}", self.per_user), (f"i:{ip}", self.per_ip),
                           (f"n:{username.lower()}", self.per_name)):
            q = self._prune(key, now)
            if len(q) >= limit:
                return max(1.0, q[0] + self.window - now)
        return 0.0

    def failure(self, ip: str, username: str) -> None:
        now = time.monotonic()
        self._fails[f"u:{ip}:{username.lower()}"].append(now)
        self._fails[f"i:{ip}"].append(now)
        self._fails[f"n:{username.lower()}"].append(now)

    def success(self, ip: str, username: str) -> None:
        self._fails.pop(f"u:{ip}:{username.lower()}", None)

    def reset(self) -> None:
        self._fails.clear()


login_limiter = LoginRateLimiter()
# Setup-code attempts: few per IP, and a global cap so a forged/rotating IP cannot brute-force the code.
setup_limiter = LoginRateLimiter(per_user=10, per_ip=10, per_name=30)

MIN_PASSWORD_LENGTH = 8


# ------------------------------------------------------------------ local admin
def ensure_local_admin(username: str, password: str | None) -> None:
    """Create/update the local admin from env. Password is re-hashed only if it changed."""
    if not password:
        return
    if len(password) < MIN_PASSWORD_LENGTH:
        log.error("auth.admin_password_too_short")
        return
    with session_scope() as s:
        u = s.scalar(select(User).where(User.username == username))
        if u is not None and (u.sub is not None or not u.is_local):
            # An SSO account already uses this name: never convert it into the local admin.
            log.error("auth.local_admin_name_taken", username=username,
                      hint="set SCANBOX_ADMIN_USERNAME to a name not used by SSO accounts")
            return
        if u is None:
            s.add(User(username=username, display_name="Administrator", is_admin=True, is_local=True,
                       password_hash=hash_password(password)))
            log.info("auth.local_admin_created", username=username)
            return
        u.is_local = True
        u.is_admin = True
        u.disabled = False
        if not verify_password(u.password_hash, password):
            u.password_hash = hash_password(password)
            log.info("auth.local_admin_password_updated", username=username)


def admin_password_locked(config: AppConfig) -> bool:
    """The local admin's password comes from the environment: no setup page, no change in the app."""
    return bool(config.resolved_admin_password())


def local_admin_exists(s: Session) -> bool:
    return s.scalar(select(func.count(User.id)).where(
        User.is_local.is_(True), User.is_admin.is_(True), User.password_hash.is_not(None))) > 0


# ------------------------------------------------------------------ first-start setup
# Without a configured admin password, the first start writes a one-time code to the container log.
# Whoever can read that log (the same people who could read a password file) claims the admin account.
_SETUP_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I
setup_lock = threading.Lock()


def generate_setup_code() -> str:
    raw = "".join(secrets.choice(_SETUP_ALPHABET) for _ in range(12))  # 60 bits
    return "-".join(raw[i:i + 4] for i in range(0, 12, 4))


def normalize_setup_code(code: str) -> str:
    return "".join(ch for ch in code.upper() if ch.isalnum())


def check_setup_code(expected: str | None, given: str) -> bool:
    if not expected:
        return False
    return constant_time_eq(normalize_setup_code(expected), normalize_setup_code(given))


def init_setup(config: AppConfig) -> str | None:
    """Return a fresh setup code if setup is pending, else None. A new code is made on every start."""
    if admin_password_locked(config):
        return None
    with session_scope() as s:
        if local_admin_exists(s):
            return None
    code = generate_setup_code()
    log.warning("auth.setup_required", setup_code=code,
                message=f"No admin account yet. Open Scanbox in the browser and enter the setup code {code}. "
                        "The code is valid until setup is completed or the container restarts.")
    return code


# ------------------------------------------------------------------ sessions
def client_ip(request: Request) -> str:
    # DSM reverse proxy sets X-Forwarded-For; uvicorn's --proxy-headers already applied it.
    return request.client.host if request.client else "unknown"


def create_session(s: Session, user: User, request: Request, response: Response, method: str,
                   secure: bool) -> tuple[str, str]:
    """Create a session; returns (raw session id, csrf token)."""
    raw = random_token(32)
    csrf = random_token(24)
    now = utcnow()
    s.add(SessionRow(
        id=sha256_hex(raw), user_id=user.id, csrf_token=csrf, created_at=now, last_seen_at=now,
        expires_at=now + ABSOLUTE_TIMEOUT, ip=client_ip(request),
        user_agent=(request.headers.get("user-agent") or "")[:300], auth_method=method,
    ))
    user.last_login_at = now
    set_session_cookies(response, raw, csrf, secure)
    return raw, csrf


def set_session_cookies(response: Response, raw: str, csrf: str, secure: bool) -> None:
    max_age = int(ABSOLUTE_TIMEOUT.total_seconds())
    response.set_cookie(SESSION_COOKIE, raw, max_age=max_age, httponly=True, secure=secure, samesite="lax", path="/")
    response.set_cookie(CSRF_COOKIE, csrf, max_age=max_age, httponly=False, secure=secure, samesite="lax", path="/")


def clear_session_cookies(response: Response, secure: bool) -> None:
    for name in (SESSION_COOKIE, CSRF_COOKIE):
        response.delete_cookie(name, path="/", secure=secure, samesite="lax")


def delete_session(s: Session, raw: str | None) -> None:
    if raw:
        s.execute(delete(SessionRow).where(SessionRow.id == sha256_hex(raw)))


def delete_other_sessions(s: Session, user_id: int, keep_sid: str | None) -> int:
    q = delete(SessionRow).where(SessionRow.user_id == user_id)
    if keep_sid:
        q = q.where(SessionRow.id != keep_sid)
    return s.execute(q).rowcount or 0


def purge_expired_sessions() -> None:
    with session_scope() as s:
        now = utcnow()
        s.execute(delete(SessionRow).where(
            (SessionRow.expires_at < now) | (SessionRow.last_seen_at < now - IDLE_TIMEOUT)))


# ------------------------------------------------------------------ API tokens
TOKEN_PREFIX = "sbx_"


def create_api_token(s: Session, user: User, name: str, expires_days: int | None) -> tuple[ApiToken, str]:
    prefix = random_token(6).replace("-", "x").replace("_", "y")[:8]
    secret = random_token(32)
    token = f"{TOKEN_PREFIX}{prefix}_{secret}"
    row = ApiToken(user_id=user.id, name=name[:100], prefix=prefix, token_hash=sha256_hex(token),
                   expires_at=utcnow() + timedelta(days=expires_days) if expires_days else None)
    s.add(row)
    s.flush()
    return row, token


def _lookup_token(s: Session, token: str) -> User | None:
    if not token.startswith(TOKEN_PREFIX):
        return None
    try:
        prefix = token[len(TOKEN_PREFIX):].split("_", 1)[0]
    except IndexError:
        return None
    row = s.scalar(select(ApiToken).where(ApiToken.prefix == prefix))
    if row is None or row.revoked or not constant_time_eq(row.token_hash, sha256_hex(token)):
        return None
    if row.expires_at and row.expires_at < utcnow():
        return None
    now = utcnow()
    if row.last_used_at is None or now - row.last_used_at > timedelta(minutes=1):
        row.last_used_at = now
    return row.user


# ------------------------------------------------------------------ request auth
@dataclass
class Principal:
    user: User
    via: str  # session | token
    session_id: str | None = None

    @property
    def id(self) -> int:
        return self.user.id

    @property
    def is_admin(self) -> bool:
        return self.user.is_admin


def authenticate(request: Request, s: Session) -> Principal | None:
    authz = request.headers.get("authorization", "")
    if authz.lower().startswith("bearer "):
        user = _lookup_token(s, authz[7:].strip())
        if user is None or user.disabled:
            raise HTTPException(401, detail={"code": "invalid_token", "message": "Ungültiges API-Token"})
        return Principal(user, "token")
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        return None
    sid = sha256_hex(raw)
    row = s.get(SessionRow, sid)
    now = utcnow()
    if row is None or row.expires_at < now or row.last_seen_at < now - IDLE_TIMEOUT:
        return None
    user = row.user
    if user is None or user.disabled:
        return None
    if request.method not in SAFE_METHODS:
        header = request.headers.get(CSRF_HEADER, "")
        if not header or not constant_time_eq(header, row.csrf_token):
            raise HTTPException(403, detail={"code": "csrf_failed", "message": "CSRF-Prüfung fehlgeschlagen"})
    if now - row.last_seen_at > timedelta(minutes=5):
        row.last_seen_at = now
    request.state.csrf_token = row.csrf_token
    return Principal(user, "session", sid)


def current_principal(request: Request, s: Session = Depends(get_session)) -> Principal:
    p = authenticate(request, s)
    if p is None:
        raise HTTPException(401, detail={"code": "not_authenticated", "message": "Bitte anmelden"})
    request.state.user_id = p.id
    structlog.contextvars.bind_contextvars(user_id=p.id)
    # End the auth transaction now: handlers start their own, and long-lived responses
    # (SSE) must not keep the write lock.
    s.commit()
    return p


def require_admin(p: Principal = Depends(current_principal)) -> Principal:
    if not p.is_admin:
        raise HTTPException(403, detail={"code": "forbidden", "message": "Nur für Administratoren"})
    return p
