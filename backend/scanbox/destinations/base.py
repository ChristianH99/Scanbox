"""Destination plugin interface.

A destination receives a finished document (artifacts on disk + metadata) and
delivers it somewhere. Each delivery is a tracked, retryable step; destinations
must be *idempotent-aware*: ``recover`` is called instead of ``deliver`` when a
previous attempt was interrupted (process crash) so a destination can decide
whether re-sending is safe.
"""

from __future__ import annotations

import enum
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar, Protocol

import httpx
from pydantic import BaseModel


class DeliveryError(Exception):
    """Raised by destinations. ``retryable`` decides whether the queue backs off and retries."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


@dataclass(frozen=True)
class Artifact:
    kind: str  # "pdf" | "txt" | "image"
    path: Path
    filename: str  # final, sanitised filename (with extension) for this artifact
    content_type: str
    size: int
    sha256: str


@dataclass(frozen=True)
class DeliveryJob:
    """Everything a destination needs to know about the document to deliver."""

    delivery_id: int
    document_id: int
    idempotency_key: str
    attempt: int
    title: str
    filename: str  # primary artifact filename
    qr_value: str | None
    created_at: datetime
    page_count: int
    profile_name: str
    user_id: int
    username: str
    user_email: str | None
    user_display_name: str
    artifacts: tuple[Artifact, ...]
    ocr_text: str | None = None
    # Lazily (re)builds a PDF with stronger compression; returns new artifact. Optional.
    recompress: Callable[[str], Awaitable[Artifact]] | None = None
    # An alternate PDF without our OCR layer (for Paperless skip_ocr); may be None.
    image_only_pdf: Artifact | None = None

    @property
    def primary(self) -> Artifact:
        return self.artifacts[0]

    def template_vars(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "filename": self.filename,
            "qr": self.qr_value or "",
            "date": self.created_at.strftime("%Y-%m-%d"),
            "time": self.created_at.strftime("%H:%M"),
            "pages": self.page_count,
            "profile": self.profile_name,
            "user": self.user_display_name or self.username,
        }


class SettingsAccessor(Protocol):
    """Read access to admin (global) and per-user settings. Secrets come back decrypted."""

    def admin(self, key: str, default: Any = None) -> Any: ...

    def user(self, key: str, default: Any = None) -> Any: ...


@dataclass
class DeliveryEnv:
    settings: SettingsAccessor
    tmp_dir: Path
    http_client_factory: Callable[..., httpx.AsyncClient] = field(
        default=lambda **kw: httpx.AsyncClient(**kw)
    )
    # Extra context (e.g. output roots mapping) for destinations that need it.
    extras: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DeliveryResult:
    """Successful delivery. ``detail`` is stored in deliveries.result (JSON) and shown in the UI."""

    message: str
    detail: dict[str, Any] = field(default_factory=dict)


class RecoverAction(enum.StrEnum):
    REDELIVER = "redeliver"  # safe to call deliver() again
    DONE = "done"  # verified already delivered
    NEEDS_ATTENTION = "needs_attention"  # unknown; a human must decide (e.g. email may have been sent)


class Destination(Protocol):
    type: ClassVar[str]
    Config: ClassVar[type[BaseModel]]

    async def validate(self, cfg: BaseModel, env: DeliveryEnv) -> list[str]:
        """Return human-readable problems with the configuration (empty list = ok)."""
        ...

    async def deliver(self, job: DeliveryJob, cfg: BaseModel, env: DeliveryEnv) -> DeliveryResult:
        """Deliver; raise DeliveryError on failure."""
        ...

    async def recover(
        self, job: DeliveryJob, cfg: BaseModel, env: DeliveryEnv, previous: dict[str, Any] | None
    ) -> RecoverAction:
        """Called when a previous attempt was interrupted mid-delivery."""
        ...
