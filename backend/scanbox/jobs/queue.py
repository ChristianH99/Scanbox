"""Persistent task queue in SQLite with lanes, leases, backoff and idempotency keys."""

from __future__ import annotations

import random
from datetime import timedelta
from typing import Any

import structlog
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..db import session_scope, utcnow
from ..models import Task

log = structlog.get_logger(__name__)

LANES = {"scan": "scanner", "process": "process", "deliver": "deliver", "webhook": "deliver"}

# Backoff schedule (seconds) for retryable failures.
BACKOFF = [30, 120, 600, 1800, 3600]


def backoff_delay(attempt: int) -> float:
    base = BACKOFF[min(attempt - 1, len(BACKOFF) - 1)] if attempt > 0 else BACKOFF[0]
    return base * (0.8 + random.random() * 0.4)


def enqueue(
    s: Session,
    kind: str,
    *,
    idempotency_key: str,
    document_id: int | None = None,
    delivery_id: int | None = None,
    payload: dict[str, Any] | None = None,
    max_attempts: int = 1,
    delay_s: float = 0,
) -> Task:
    """Insert a task unless one with the same idempotency key exists (then return it)."""
    existing = s.scalar(select(Task).where(Task.idempotency_key == idempotency_key))
    if existing is not None:
        return existing
    t = Task(
        kind=kind,
        lane=LANES[kind],
        document_id=document_id,
        delivery_id=delivery_id,
        payload=payload or {},
        idempotency_key=idempotency_key,
        max_attempts=max_attempts,
        next_run_at=utcnow() + timedelta(seconds=delay_s),
    )
    try:
        with s.begin_nested():
            s.add(t)
            s.flush()
    except IntegrityError:
        existing = s.scalar(select(Task).where(Task.idempotency_key == idempotency_key))
        assert existing is not None
        return existing
    return t


def claim_next(lane: str) -> Task | None:
    """Atomically claim the next due task in a lane."""
    with session_scope() as s:
        now = utcnow()
        candidate = s.scalar(
            select(Task.id)
            .where(Task.lane == lane, Task.status == "pending", Task.next_run_at <= now)
            .order_by(Task.next_run_at, Task.id)
            .limit(1)
        )
        if candidate is None:
            return None
        res = s.execute(
            update(Task)
            .where(Task.id == candidate, Task.status == "pending")
            .values(status="running", locked_at=now, attempts=Task.attempts + 1, updated_at=now)
        )
        if res.rowcount != 1:
            return None
        task = s.get(Task, candidate)
        s.expunge(task)  # type: ignore[arg-type]
        return task


def complete(task_id: int) -> None:
    with session_scope() as s:
        s.execute(update(Task).where(Task.id == task_id).values(status="done", locked_at=None, updated_at=utcnow()))


def fail(task_id: int, error: str, *, retryable: bool) -> bool:
    """Record a failure. Returns True if the task was rescheduled."""
    with session_scope() as s:
        t = s.get(Task, task_id)
        if t is None:
            return False
        t.last_error = error[:4000]
        t.locked_at = None
        if retryable and t.attempts < t.max_attempts:
            t.status = "pending"
            t.next_run_at = utcnow() + timedelta(seconds=backoff_delay(t.attempts))
            return True
        t.status = "failed"
        return False


def cancel_for_document(s: Session, document_id: int, kinds: tuple[str, ...] | None = None) -> int:
    q = update(Task).where(Task.document_id == document_id, Task.status == "pending")
    if kinds:
        q = q.where(Task.kind.in_(kinds))
    return s.execute(q.values(status="cancelled", updated_at=utcnow())).rowcount


def pending_count(s: Session, lane: str) -> int:
    from sqlalchemy import func

    return s.scalar(select(func.count(Task.id)).where(Task.lane == lane, Task.status.in_(("pending", "running")))) or 0


def queue_position(s: Session, task: Task) -> int:
    from sqlalchemy import func

    return (
        s.scalar(
            select(func.count(Task.id)).where(
                Task.lane == task.lane,
                Task.status.in_(("pending", "running")),
                Task.id < task.id,
            )
        )
        or 0
    )
