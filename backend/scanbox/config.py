"""Process configuration from environment variables (prefix ``SCANBOX_``).

Values set here are *locked*: they take precedence over anything stored by the
admin UI (see ``settings_store``). Everything has a safe default so the app can
start with an empty environment for development.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SCANBOX_", env_file=None, extra="ignore")

    # Paths
    data_dir: Path = Path("/data")
    static_dir: Path | None = None  # built frontend; default: package static/ dir

    # Public URL of the app (behind DSM reverse proxy), used for OIDC redirect + links
    base_url: str = "http://localhost:8080"
    # Cookies are Secure unless explicitly disabled (plain-HTTP dev only)
    cookie_secure: bool = True

    # Secret key for session signing + secret encryption at rest
    secret_key: str | None = None
    secret_key_file: Path | None = None

    # Local admin (bootstrap / SSO fallback)
    admin_username: str = "admin"
    admin_password: str | None = None
    admin_password_file: Path | None = None

    # OIDC (Synology SSO Server) — may also be set in admin UI if not given here
    oidc_issuer: str | None = None
    oidc_client_id: str | None = None
    oidc_client_secret: str | None = None
    oidc_client_secret_file: Path | None = None
    oidc_scopes: str | None = None
    oidc_admin_claim: str | None = None
    oidc_admin_value: str | None = None

    # Scanner
    scanner_url: str | None = None  # e.g. http://192.168.1.50:8080 or http://scanner.lan

    # Jobs / resources
    ocr_concurrency: int | None = None
    ocr_jobs: int | None = None
    nice_level: int = 10
    max_queued_scans: int = 5
    min_free_mb: int | None = None  # None → admin setting (default 1024)

    # Retention (None → admin setting / default)
    raw_retention_days: int | None = None
    processed_retention_days: int | None = None

    # Output roots "Name=/path;Other=/path2"
    output_roots: str | None = None
    paperless_url: str | None = None
    paperless_consume_dir: Path | None = None

    # Walk-up scan (HP-specific, experimental)
    walkup_enabled: bool | None = None

    log_level: str = "INFO"
    log_json: bool = True

    # Test / dev knobs
    disable_background: bool = Field(default=False, description="Do not start workers (tests)")

    @field_validator("base_url")
    @classmethod
    def _strip_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @staticmethod
    def _read_file(p: Path | None) -> str | None:
        if p and p.exists():
            return p.read_text(encoding="utf-8").strip()
        return None

    def resolved_secret_key(self) -> str:
        key = self.secret_key or self._read_file(self.secret_key_file)
        if key:
            return key
        # Persist a generated key in the data dir so restarts keep sessions + secrets.
        path = self.data_dir / "secret.key"
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
        import secrets

        path.parent.mkdir(parents=True, exist_ok=True)
        key = secrets.token_urlsafe(48)
        path.write_text(key, encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return key

    def resolved_admin_password(self) -> str | None:
        return self.admin_password or self._read_file(self.admin_password_file)

    def resolved_oidc_client_secret(self) -> str | None:
        return self.oidc_client_secret or self._read_file(self.oidc_client_secret_file)

    # Derived paths
    @property
    def db_path(self) -> Path:
        return self.data_dir / "scanbox.db"

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def processed_dir(self) -> Path:
        return self.data_dir / "processed"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.raw_dir, self.processed_dir, self.cache_dir, self.tmp_dir, self.log_dir):
            d.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_config() -> AppConfig:
    return AppConfig()
