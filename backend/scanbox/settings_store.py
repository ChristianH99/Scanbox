"""Admin (global) and per-user settings.

Resolution order for admin settings: environment (locked) > database > default.
Secret values are encrypted at rest and never returned to the browser
(the API shows only whether a secret is set).
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import AppConfig
from .db import session_scope
from .models import Setting, UserSetting
from .security.crypto import SecretBox


@dataclass(frozen=True)
class SettingDef:
    key: str
    default: Any = None
    secret: bool = False
    env: Callable[[AppConfig], Any] | None = None
    type: str = "str"  # str | int | bool | json | path
    internal: bool = False  # not exposed in admin UI


def _parse_roots(raw: str | None) -> dict[str, str] | None:
    if not raw:
        return None
    out: dict[str, str] = {}
    for part in raw.split(";"):
        if "=" in part:
            name, path = part.split("=", 1)
            if name.strip() and path.strip():
                out[name.strip()] = path.strip()
    return out or None


ADMIN_SETTINGS: dict[str, SettingDef] = {
    d.key: d
    for d in [
        SettingDef("scanner.url", None, env=lambda c: c.scanner_url),
        SettingDef("scanner.capabilities", None, type="json", internal=True),
        SettingDef("scanner.capabilities_xml", None, internal=True),
        SettingDef("scanner.capabilities_fetched_at", None, internal=True),
        SettingDef("retention.raw_days", 30, env=lambda c: c.raw_retention_days, type="int"),
        SettingDef("retention.processed_days", 14, env=lambda c: c.processed_retention_days, type="int"),
        SettingDef("jobs.ocr_concurrency", 1, env=lambda c: c.ocr_concurrency, type="int"),
        SettingDef("jobs.ocr_jobs", 2, env=lambda c: c.ocr_jobs, type="int"),
        SettingDef("disk.min_free_mb", 1024, env=lambda c: c.min_free_mb, type="int"),
        SettingDef("oidc.issuer", None, env=lambda c: c.oidc_issuer),
        SettingDef("oidc.client_id", None, env=lambda c: c.oidc_client_id),
        SettingDef("oidc.client_secret", None, secret=True, env=lambda c: c.resolved_oidc_client_secret()),
        SettingDef("oidc.scopes", "openid profile email", env=lambda c: c.oidc_scopes),
        SettingDef("oidc.admin_claim", "groups", env=lambda c: c.oidc_admin_claim),
        SettingDef("oidc.admin_value", "scanbox-admins", env=lambda c: c.oidc_admin_value),
        SettingDef("oidc.button_label", "Synology SSO"),
        SettingDef("smtp.host", None),
        SettingDef("smtp.port", 587, type="int"),
        SettingDef("smtp.security", "starttls"),
        SettingDef("smtp.username", None),
        SettingDef("smtp.password", None, secret=True),
        SettingDef("smtp.from_address", None),
        SettingDef("smtp.from_name", "Scanbox"),
        SettingDef("smtp.max_attachment_mb", 25, type="int"),
        SettingDef("smtp.timeout_s", 30, type="int"),
        SettingDef("smtp.verify_tls", True, type="bool"),
        SettingDef("paperless.url", None, env=lambda c: c.paperless_url),
        SettingDef(
            "paperless.consume_dir",
            None,
            env=lambda c: str(c.paperless_consume_dir) if c.paperless_consume_dir else None,
            type="path",
        ),
        SettingDef("paperless.verify_tls", True, type="bool"),
        SettingDef("output.roots", {}, env=lambda c: _parse_roots(c.output_roots), type="json"),
        SettingDef("walkup.enabled", False, env=lambda c: c.walkup_enabled, type="bool"),
        SettingDef("walkup.default_profile_id", None, type="int"),
        SettingDef("walkup.destination_name", "Scanbox"),
        SettingDef("walkup.hostname", None),
        SettingDef("walkup.state", None, type="json", internal=True),
    ]
}

USER_SECRET_KEYS = {"paperless.token"}
USER_KEYS = {
    "paperless.token",
    "email.default_recipients",
    "email.subject_template",
    "email.body_template",
    "ui.default_profile_id",
}


class SettingsStore:
    """Admin settings are cached in memory (single process → the cache is authoritative and
    updated on every write). Reads therefore never open a DB transaction, which matters because
    they happen inside other transactions (BEGIN IMMEDIATE would otherwise self-deadlock)."""

    def __init__(self, config: AppConfig, box: SecretBox) -> None:
        self.config = config
        self.box = box
        self._cache: dict[str, tuple[Any, bool]] | None = None
        self._lock = threading.RLock()

    def _load(self) -> dict[str, tuple[Any, bool]]:
        with self._lock:
            if self._cache is None:
                with session_scope() as s:
                    self._cache = {r.key: (r.value, r.encrypted) for r in s.scalars(select(Setting))}
            return self._cache

    def invalidate(self) -> None:
        with self._lock:
            self._cache = None

    # ------------------------------------------------------------------ admin
    def env_value(self, key: str) -> Any:
        d = ADMIN_SETTINGS.get(key)
        if d is None or d.env is None:
            return None
        return d.env(self.config)

    def is_locked(self, key: str) -> bool:
        return self.env_value(key) is not None

    def get(self, key: str, default: Any = None, session: Session | None = None) -> Any:
        d = ADMIN_SETTINGS.get(key)
        if d is None:
            raise KeyError(key)
        env = self.env_value(key)
        if env is not None:
            return env
        cached = self._load().get(key)
        if cached is not None and cached[0] is not None:
            value, encrypted = cached
            return self.box.decrypt(value) if encrypted else value
        return d.default if default is None else default

    def set(self, key: str, value: Any, session: Session | None = None) -> None:
        d = ADMIN_SETTINGS.get(key)
        if d is None:
            raise KeyError(key)
        if self.is_locked(key):
            raise PermissionError(f"{key} is set via environment and cannot be changed here")
        value = _coerce(d, value)
        enc = d.secret and value is not None
        stored = self.box.encrypt(str(value)) if enc else value

        def _do(s: Session) -> None:
            row = s.get(Setting, key)
            if row is None:
                s.add(Setting(key=key, value=stored, encrypted=enc))
            else:
                row.value = stored
                row.encrypted = enc

        with self._lock:
            if session is not None:
                _do(session)
            else:
                with session_scope() as s:
                    _do(s)
            self._load()[key] = (stored, enc)

    def admin_view(self) -> dict[str, dict[str, Any]]:
        """All non-internal settings for the admin UI; secrets masked."""
        out = {}
        for key, d in ADMIN_SETTINGS.items():
            if d.internal:
                continue
            val = self.get(key)
            out[key] = {
                "value": None if d.secret else val,
                "is_set": val not in (None, "", {}),
                "secret": d.secret,
                "locked": self.is_locked(key),
                "type": d.type,
            }
        return out

    # ------------------------------------------------------------------ user
    def user_get(self, user_id: int, key: str, default: Any = None, session: Session | None = None) -> Any:
        def _do(s: Session) -> Any:
            row = s.get(UserSetting, (user_id, key))
            if row is None or row.value is None:
                return default
            if row.encrypted:
                return self.box.decrypt(row.value)
            return row.value

        if session is not None:
            return _do(session)
        with session_scope() as s:
            return _do(s)

    def user_set(self, user_id: int, key: str, value: Any, session: Session | None = None) -> None:
        if key not in USER_KEYS and not key.startswith("counter."):
            raise KeyError(key)
        enc = key in USER_SECRET_KEYS and value is not None
        stored = self.box.encrypt(str(value)) if enc else value

        def _do(s: Session) -> None:
            row = s.get(UserSetting, (user_id, key))
            if row is None:
                s.add(UserSetting(user_id=user_id, key=key, value=stored, encrypted=enc))
            else:
                row.value = stored
                row.encrypted = enc

        if session is not None:
            _do(session)
        else:
            with session_scope() as s:
                _do(s)

    def user_all(self, user_id: int, session: Session | None = None) -> dict[str, Any]:
        """All user settings, secrets decrypted (sync – call from a thread)."""

        def _do(s: Session) -> dict[str, Any]:
            out: dict[str, Any] = {}
            for r in s.scalars(select(UserSetting).where(UserSetting.user_id == user_id)):
                out[r.key] = self.box.decrypt(r.value) if r.encrypted and r.value is not None else r.value
            return out

        if session is not None:
            return _do(session)
        with session_scope() as s:
            return _do(s)

    def user_view(self, user_id: int, session: Session) -> dict[str, Any]:
        rows = session.scalars(select(UserSetting).where(UserSetting.user_id == user_id)).all()
        out: dict[str, Any] = {}
        for r in rows:
            if r.key.startswith("counter."):
                continue
            if r.key in USER_SECRET_KEYS:
                out[r.key] = {"is_set": r.value is not None}
            else:
                out[r.key] = r.value
        return out

    def next_counter(self, user_id: int, name: str, session: Session) -> int:
        key = f"counter.{name}"
        row = session.get(UserSetting, (user_id, key))
        if row is None:
            session.add(UserSetting(user_id=user_id, key=key, value=1))
            session.flush()
            return 1
        row.value = int(row.value or 0) + 1
        session.flush()
        return int(row.value)


def _coerce(d: SettingDef, value: Any) -> Any:
    if value is None or value == "":
        return None
    if d.type == "int":
        return int(value)
    if d.type == "bool":
        if isinstance(value, str):
            return value.lower() in ("1", "true", "yes", "on")
        return bool(value)
    if d.type == "path":
        p = Path(str(value))
        if not p.is_absolute():
            raise ValueError(f"{d.key}: path must be absolute")
        return str(p)
    if d.type == "json":
        return value
    return str(value)


class UserSettingsAccessor:
    """Adapter implementing destinations.base.SettingsAccessor for one user.

    User values are preloaded (in a worker thread) so that destinations running on the event loop
    never open a DB transaction. Admin values come from the in-memory cache.
    """

    def __init__(self, store: SettingsStore, user_id: int, user_values: dict[str, Any]) -> None:
        self.store = store
        self.user_id = user_id
        self._user = user_values

    def admin(self, key: str, default: Any = None) -> Any:
        v = self.store.get(key)
        return default if v is None else v

    def user(self, key: str, default: Any = None) -> Any:
        v = self._user.get(key)
        return default if v is None else v
