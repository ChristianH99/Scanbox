"""Cache retention, data usage, disk guard (docs/ARCHITECTURE.md §13)."""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
from datetime import timedelta
from typing import Any

import structlog
from sqlalchemy import select

from .db import session_scope, utcnow
from .documents import delete_document_files
from .jobs import states as st
from .models import Document
from .storage import dir_size, free_bytes

log = structlog.get_logger(__name__)

UNFINISHED = {st.QUEUED, st.SCANNING, st.STORED, st.PROCESSING, st.DELIVERING, st.NEEDS_ATTENTION}
RENDER_CACHE_MAX = 500 * 1024 * 1024


def run_retention(ctx, *, force_all: bool = False) -> dict[str, int]:  # noqa: ANN001
    """Expire cached files. ``force_all`` = admin 'Clear cache now' (keeps unfinished work!)."""
    raw_days = int(ctx.settings.get("retention.raw_days") or 30)
    proc_days = int(ctx.settings.get("retention.processed_days") or 14)
    now = utcnow()
    stats = {"raw_expired": 0, "processed_expired": 0, "inbox_expired": 0, "render_cache_bytes": 0}
    with session_scope() as s:
        # Unclaimed inbox items
        for doc in s.scalars(select(Document).where(
            Document.owner_id.is_(None), Document.origin == "walkup",
            Document.status.not_in((st.DISCARDED, st.EXPIRED, st.SCANNING, st.QUEUED)),
        )):
            if force_all or (doc.inbox_expires_at and doc.inbox_expires_at <= now):
                delete_document_files(s, ctx, doc)
                st.transition(s, doc, st.EXPIRED, code="inbox.expired", message="Nicht übernommen – abgelaufen")
                stats["inbox_expired"] += 1
        # Processed outputs
        cutoff_p = now - timedelta(days=proc_days)
        q = select(Document).where(Document.processed_expired.is_(False), Document.status.not_in(UNFINISHED))
        for doc in s.scalars(q):
            ts = doc.finished_at or doc.updated_at or doc.created_at
            if force_all or ts <= cutoff_p:
                delete_document_files(s, ctx, doc, raw=False, processed=True)
                stats["processed_expired"] += 1
        # Raw pages: finished docs after raw_days; unfinished docs only after 2× (never lose work silently)
        cutoff_r = now - timedelta(days=raw_days)
        cutoff_unfinished = now - timedelta(days=2 * raw_days)
        for doc in s.scalars(select(Document).where(Document.raw_expired.is_(False), Document.owner_id.is_not(None))):
            if doc.status in (st.QUEUED, st.SCANNING, st.PROCESSING, st.DELIVERING):
                continue
            unfinished = doc.status in UNFINISHED
            limit = cutoff_unfinished if unfinished else cutoff_r
            if (force_all and not unfinished) or doc.created_at <= limit:
                delete_document_files(s, ctx, doc, raw=True, processed=False)
                if unfinished:
                    st.add_event(s, doc, "retention.raw_expired", "Originalseiten abgelaufen", "warning")
                stats["raw_expired"] += 1
    stats["render_cache_bytes"] = trim_render_cache(ctx, 0 if force_all else RENDER_CACHE_MAX)
    if any(stats.values()):
        log.info("retention.run", **stats, force=force_all)
    return stats


def trim_render_cache(ctx, max_bytes: int) -> int:  # noqa: ANN001
    root = ctx.config.cache_dir / "render"
    if not root.exists():
        return 0
    files = []
    for dirpath, _d, names in os.walk(root):
        for n in names:
            p = os.path.join(dirpath, n)
            with contextlib.suppress(OSError):
                stt = os.stat(p)
                files.append((stt.st_atime, stt.st_size, p))
    total = sum(f[1] for f in files)
    removed = 0
    if total <= max_bytes:
        return 0
    for _atime, size, p in sorted(files):
        with contextlib.suppress(OSError):
            os.unlink(p)
            removed += size
            total -= size
        if total <= max_bytes:
            break
    if max_bytes == 0:
        shutil.rmtree(root, ignore_errors=True)
    return removed


def usage(ctx) -> dict[str, Any]:  # noqa: ANN001
    c = ctx.config
    db = sum(p.stat().st_size for p in c.data_dir.glob("scanbox.db*") if p.exists())
    return {
        "raw_bytes": dir_size(c.raw_dir),
        "processed_bytes": dir_size(c.processed_dir),
        "cache_bytes": dir_size(c.cache_dir),
        "db_bytes": db,
        "logs_bytes": dir_size(c.log_dir),
        "free_bytes": free_bytes(c.data_dir),
        "total_bytes": shutil.disk_usage(c.data_dir).total if c.data_dir.exists() else 0,
        "min_free_mb": int(ctx.settings.get("disk.min_free_mb") or 0),
    }


async def retention_loop(ctx, interval_s: float = 3600) -> None:  # noqa: ANN001
    await asyncio.sleep(60)
    while True:
        try:
            await asyncio.to_thread(run_retention, ctx)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("retention.failed", error=str(e))
        await asyncio.sleep(interval_s)
