"""Document state machine (docs/ARCHITECTURE.md §4)."""

from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy.orm import Session

from ..db import utcnow
from ..events import bus
from ..models import Document, JobEvent

log = structlog.get_logger(__name__)

QUEUED = "queued"
SCANNING = "scanning"
STORED = "stored"
PROCESSING = "processing"
DELIVERING = "delivering"
DONE = "done"
FAILED = "failed"
NEEDS_ATTENTION = "needs_attention"
DISCARDED = "discarded"
EXPIRED = "expired"

ALL_STATES = (QUEUED, SCANNING, STORED, PROCESSING, DELIVERING, DONE, FAILED, NEEDS_ATTENTION, DISCARDED, EXPIRED)
TERMINAL = {DISCARDED, EXPIRED}
ACTIVE = {QUEUED, SCANNING, PROCESSING, DELIVERING}
# States in which the user may edit pages / add batches / finish.
EDITABLE = {STORED, NEEDS_ATTENTION, FAILED, DONE}

TRANSITIONS: dict[str, set[str]] = {
    QUEUED: {SCANNING, FAILED, NEEDS_ATTENTION, DISCARDED, STORED},
    SCANNING: {STORED, FAILED, NEEDS_ATTENTION, PROCESSING},
    STORED: {QUEUED, PROCESSING, DISCARDED, EXPIRED, NEEDS_ATTENTION},
    PROCESSING: {DELIVERING, DONE, NEEDS_ATTENTION, FAILED, STORED},
    DELIVERING: {DONE, NEEDS_ATTENTION, FAILED, DELIVERING},
    DONE: {PROCESSING, DELIVERING, QUEUED, DISCARDED, STORED},
    NEEDS_ATTENTION: {QUEUED, STORED, PROCESSING, DELIVERING, DONE, DISCARDED, FAILED, EXPIRED},
    FAILED: {QUEUED, STORED, PROCESSING, DISCARDED, EXPIRED},
    DISCARDED: set(),
    EXPIRED: set(),
}


class InvalidTransition(Exception):
    def __init__(self, src: str, dst: str) -> None:
        super().__init__(f"invalid state transition {src} → {dst}")
        self.src = src
        self.dst = dst


def add_event(
    s: Session,
    doc: Document,
    code: str,
    message: str = "",
    level: str = "info",
    data: dict[str, Any] | None = None,
) -> None:
    s.add(JobEvent(document_id=doc.id, code=code, message=message, level=level, data=data))
    log.info("job.event", document_id=doc.id, code=code, level=level, msg=message)


def transition(
    s: Session,
    doc: Document,
    dst: str,
    *,
    code: str | None = None,
    message: str = "",
    level: str = "info",
    error_code: str | None = None,
) -> None:
    src = doc.status
    if src == dst and dst not in (DELIVERING,):
        return
    if dst not in TRANSITIONS.get(src, set()) and src != dst:
        raise InvalidTransition(src, dst)
    doc.status = dst
    doc.updated_at = utcnow()
    if dst in (DONE,):
        doc.finished_at = utcnow()
    if dst in (FAILED, NEEDS_ATTENTION):
        doc.error_code = error_code or doc.error_code
        doc.error_message = message or doc.error_message
    elif dst in (QUEUED, SCANNING, PROCESSING, DONE, STORED):
        doc.error_code = None
        doc.error_message = None
    add_event(s, doc, code or f"status.{dst}", message, level=level)
    s.flush()
    publish_document(doc)


def publish_document(doc: Document, type_: str = "document.updated") -> None:
    payload = {
        "id": doc.id,
        "status": doc.status,
        "page_count": doc.page_count,
        "title": doc.title,
        "qr_value": doc.qr_value,
        "error_code": doc.error_code,
        "error_message": doc.error_message,
        "origin": doc.origin,
        "owner_id": doc.owner_id,
    }
    bus.publish(type_, payload, audience=doc.owner_id)
    if doc.owner_id is None:
        bus.publish("inbox.changed", {"id": doc.id})
