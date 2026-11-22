"""Background worker: lane loops over the persistent queue, startup recovery, graceful shutdown."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from pathlib import Path

import structlog
from sqlalchemy import func, select, update

from ..db import session_scope, utcnow
from ..models import Batch, Delivery, Document, Page, Task
from . import queue
from . import states as st

log = structlog.get_logger(__name__)


class Worker:
    def __init__(self, ctx) -> None:  # noqa: ANN001
        from ..destinations.service import DeliveryRunner
        from ..processing.pipeline import Processor
        from ..scanner.runner import ScanRunner

        self.ctx = ctx
        self.scan_runner = ScanRunner(ctx)
        self.processor = Processor(ctx)
        self.deliveries = DeliveryRunner(ctx)
        self._tasks: list[asyncio.Task] = []
        self._wake: dict[str, asyncio.Event] = {}
        self._stopping = False
        self._running: dict[int, asyncio.Task] = {}
        # Deliveries interrupted mid-flight by a restart: go through Destination.recover() first.
        self.recovering_deliveries: set[int] = set()

    # ---------------------------------------------------------------- lifecycle
    def start(self) -> None:
        conc = {
            "scanner": 1,
            "process": max(1, int(self.ctx.settings.get("jobs.ocr_concurrency") or 1)),
            "deliver": 2,
        }
        for lane, n in conc.items():
            self._wake[lane] = asyncio.Event()
            for i in range(n):
                self._tasks.append(asyncio.create_task(self._loop(lane), name=f"worker-{lane}-{i}"))
        log.info("worker.started", lanes=conc)

    @property
    def alive(self) -> bool:
        return bool(self._tasks) and all(not t.done() for t in self._tasks)

    def notify(self, lane: str | None = None) -> None:
        for name, ev in self._wake.items():
            if lane is None or lane == name:
                ev.set()

    async def stop(self, timeout: float = 20.0) -> None:
        self._stopping = True
        self.notify()
        running = list(self._running.values())
        if running:
            log.info("worker.draining", tasks=len(running))
            _done, pending = await asyncio.wait(running, timeout=timeout)
            for t in pending:
                t.cancel()
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        self._tasks.clear()

    # ---------------------------------------------------------------- loop
    async def _loop(self, lane: str) -> None:
        ev = self._wake[lane]
        while not self._stopping:
            try:
                task = await asyncio.to_thread(queue.claim_next, lane)
            except Exception as e:  # noqa: BLE001
                log.warning("worker.claim_failed", lane=lane, error=str(e))
                task = None
            if task is None:
                ev.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(ev.wait(), timeout=2.0)
                continue
            runner = asyncio.create_task(self._execute(task))
            self._running[task.id] = runner
            try:
                await asyncio.shield(runner)
            except asyncio.CancelledError:
                if not runner.done():
                    runner.cancel()
                raise
            finally:
                self._running.pop(task.id, None)

    async def _execute(self, task: Task) -> None:
        structlog.contextvars.bind_contextvars(task_id=task.id, task_kind=task.kind)
        try:
            ok = True
            if task.kind == "scan":
                await self.scan_runner.run(task)
            elif task.kind == "process":
                await self.processor.run(task)
            elif task.kind == "deliver":
                recovering = task.delivery_id in self.recovering_deliveries
                self.recovering_deliveries.discard(task.delivery_id or 0)
                ok = await self.deliveries.run(task, recovering=recovering)
            elif task.kind == "webhook":
                from ..destinations.service import send_webhook

                ok = await send_webhook(self.ctx, task)
            else:
                log.error("worker.unknown_kind")
            if ok:
                await asyncio.to_thread(queue.complete, task.id)
            else:
                rescheduled = await asyncio.to_thread(queue.fail, task.id, "retryable failure", retryable=True)
                if rescheduled:
                    self.notify(task.lane)
        except asyncio.CancelledError:
            # Shutdown: leave the task 'running'; startup recovery handles it.
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("worker.task_crashed")
            await asyncio.to_thread(queue.fail, task.id, f"{type(e).__name__}: {e}", retryable=False)
            await asyncio.to_thread(self._mark_doc_after_crash, task, e)
        finally:
            structlog.contextvars.unbind_contextvars("task_id", "task_kind")
            # Follow-up tasks may have been enqueued.
            self.notify()

    def _mark_doc_after_crash(self, task: Task, e: Exception) -> None:
        if task.document_id is None:
            return
        with session_scope() as s:
            doc = s.get(Document, task.document_id)
            if doc and doc.status in (st.SCANNING, st.PROCESSING, st.QUEUED, st.DELIVERING):
                with contextlib.suppress(st.InvalidTransition):
                    st.transition(s, doc, st.NEEDS_ATTENTION, code="internal_error", level="error",
                                  message=f"Interner Fehler: {e}"[:500], error_code="internal_error")


# -------------------------------------------------------------------- recovery


def recover_on_startup(ctx) -> tuple[dict[str, int], set[int]]:  # noqa: ANN001
    """Bring the persistent state back to consistency after a crash/restart.

    Returns (stats, ids of deliveries interrupted mid-flight that must go through ``recover()``).
    """
    recovering: set[int] = set()
    stats = {"scan": 0, "process": 0, "deliver": 0, "orphans": 0, "tmp": 0}
    stats["tmp"] = ctx.storage.clean_tmp()
    with session_scope() as s:
        running = s.scalars(select(Task).where(Task.status == "running")).all()
        for t in running:
            t.locked_at = None
            if t.kind == "scan":
                stats["scan"] += 1
                t.status = "failed"
                t.last_error = "interrupted by restart"
                batch = s.get(Batch, int(t.payload.get("batch_id", 0)))
                if batch and batch.status in ("queued", "scanning"):
                    batch.status = "interrupted"
                    batch.error_code = "interrupted"
                    batch.error_message = "Neustart während des Scans"
                    batch.finished_at = utcnow()
                doc = s.get(Document, t.document_id) if t.document_id else None
                if doc and doc.status in (st.SCANNING, st.QUEUED):
                    n = s.scalar(select(func.count(Page.id)).where(Page.document_id == doc.id,
                                                                    Page.deleted.is_(False))) or 0
                    dst = st.NEEDS_ATTENTION if n else st.FAILED
                    st.transition(s, doc, dst, code="scan.interrupted", level="error", error_code="interrupted",
                                  message=f"Scan durch Neustart unterbrochen – {n} Seite(n) gesichert")
            elif t.kind == "process":
                stats["process"] += 1
                t.status = "pending"
                t.next_run_at = utcnow()
            elif t.kind == "deliver":
                stats["deliver"] += 1
                t.status = "pending"
                t.next_run_at = utcnow()
                if t.delivery_id:
                    recovering.add(t.delivery_id)
                    dl = s.get(Delivery, t.delivery_id)
                    if dl and dl.status == "running":
                        dl.status = "pending"
            else:
                t.status = "pending"
        s.flush()
        # Batches stuck in 'scanning' without a running task (should not happen, but be safe).
        s.execute(update(Batch).where(Batch.status == "scanning").values(status="interrupted",
                                                                         error_code="interrupted"))
        for doc in s.scalars(select(Document).where(Document.status == st.SCANNING)).all():
            n = s.scalar(select(func.count(Page.id)).where(Page.document_id == doc.id)) or 0
            st.transition(s, doc, st.NEEDS_ATTENTION if n else st.FAILED, code="scan.interrupted",
                          error_code="interrupted", level="error", message="Scan unterbrochen")
        stats["orphans"] = _adopt_orphan_pages(ctx, s)
    if any(v for k, v in stats.items() if k != "tmp"):
        log.warning("recovery.done", **stats)
    return stats, recovering


def _adopt_orphan_pages(ctx, s) -> int:  # noqa: ANN001
    """A page file written to disk but not yet in the DB (crash in between) is adopted, never lost."""
    from ..storage import sha256_file

    raw_dir: Path = ctx.config.raw_dir
    if not raw_dir.exists():
        return 0
    known = set(s.scalars(select(Page.raw_path)).all())
    adopted = 0
    for d in raw_dir.iterdir():
        if not d.is_dir() or not d.name.isdigit():
            continue
        doc = s.get(Document, int(d.name))
        if doc is None or doc.status in st.TERMINAL:
            continue
        for f in sorted(d.iterdir(), key=lambda p: p.stat().st_mtime):
            if f.name.startswith(".") or not f.is_file():
                continue
            rel = ctx.storage.rel(f)
            if rel in known:
                continue
            try:
                uuid.UUID(f.stem)
            except ValueError:
                continue
            maxpos = s.scalar(select(func.max(Page.position)).where(Page.document_id == doc.id)) or 0
            s.add(Page(id=f.stem, document_id=doc.id, position=float(maxpos) + 1, raw_path=rel,
                       raw_sha256=sha256_file(f), raw_size=f.stat().st_size,
                       content_type="image/jpeg" if f.suffix == ".jpg" else "application/octet-stream",
                       dpi=int(doc.profile_snapshot.get("resolution", 300))))
            s.flush()
            doc.page_count = s.scalar(select(func.count(Page.id)).where(Page.document_id == doc.id,
                                                                        Page.deleted.is_(False))) or 0
            st.add_event(s, doc, "recovery.page_adopted", "Seite nach Neustart wiederhergestellt", "warning")
            adopted += 1
    return adopted
