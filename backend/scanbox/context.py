"""Application context: one instance per process, holds all services."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import httpx

from .config import AppConfig
from .events import EventBus, bus
from .security.crypto import SecretBox
from .settings_store import SettingsStore
from .storage import Storage

if TYPE_CHECKING:
    from .jobs.worker import Worker
    from .scanner.service import ScannerService
    from .walkup.service import WalkupService


@dataclass
class AppContext:
    config: AppConfig
    box: SecretBox
    settings: SettingsStore
    storage: Storage
    bus: EventBus = field(default_factory=lambda: bus)
    # Factory for outbound HTTP (destinations/webhooks/OIDC). Tests inject ASGI transports.
    http_client_factory: Callable[..., httpx.AsyncClient] = field(
        default=lambda **kw: httpx.AsyncClient(**kw)
    )
    # Factory for the scanner HTTP client (tests route this to the simulator).
    scanner_client_factory: Callable[[], httpx.AsyncClient] | None = None
    scanner: ScannerService | None = None
    worker: Worker | None = None
    walkup: WalkupService | None = None
    # One-time code for first-start setup (only in memory: a restart invalidates it). None = setup closed.
    setup_code: str | None = None


_ctx: AppContext | None = None


def set_context(ctx: AppContext | None) -> None:
    global _ctx
    _ctx = ctx


def get_context() -> AppContext:
    if _ctx is None:
        raise RuntimeError("app context not initialised")
    return _ctx


def build_context(config: AppConfig) -> AppContext:
    config.ensure_dirs()
    box = SecretBox(config.resolved_secret_key())
    return AppContext(config=config, box=box, settings=SettingsStore(config, box), storage=Storage(config))
