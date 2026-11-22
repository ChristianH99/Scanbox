"""Runs one batch (one ADF stack = one eSCL job) for a document.

Reliability rules:
* every page is persisted (fsync + rename) *before* the DB row is written and announced;
* pages already received are kept whatever happens afterwards;
* the scanner is used by one job at a time (``ScannerService.lock``).
"""

from __future__ import annotations

import asyncio
import io
from dataclasses import dataclass

import structlog
from PIL import Image
from sqlalchemy import func, select

from ..db import run_db, session_scope, utcnow
from ..escl.client import EsclError, ScannerBusy, ScanRejected
from ..escl.xml import ScanSettings
from ..jobs import states as st
from ..jobs.queue import enqueue
from ..models import Batch, Document, Page, Task
from ..profiles import ProfileSettings, escl_color_mode

log = structlog.get_logger(__name__)

BUSY_RETRY_S = 60.0


class ScanFailure(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


USER_MESSAGES = {
    "feeder_empty": "Kein Papier im Einzug / feeder is empty",
    "paper_jam": "Papierstau / paper jam",
    "cover_open": "Abdeckung offen / cover open",
    "multipick": "Mehrfacheinzug erkannt / multiple sheets fed at once",
    "scanner_offline": "Scanner nicht erreichbar / scanner offline",
    "scanner_busy": "Scanner belegt / scanner busy",
    "scanner_timeout": "Zeitüberschreitung / scanner timeout",
    "insufficient_disk": "Zu wenig Speicherplatz / not enough disk space",
    "scanner_not_configured": "Kein Scanner konfiguriert / no scanner configured",
    "scan_rejected": "Scanner hat den Auftrag abgelehnt / scanner rejected the job",
    "cancelled": "Scan abgebrochen / scan cancelled",
    "connection_lost": "Verbindung während einer Seite abgebrochen – evtl. fehlt ein Blatt, bitte prüfen "
    "/ connection lost mid-page – a sheet may be missing",
}

ADF_ERRORS = {
    "ScannerAdfEmpty": "feeder_empty",
    "ScannerAdfJam": "paper_jam",
    "ScannerAdfMispick": "paper_jam",
    "ScannerAdfDoorOpen": "cover_open",
    "ScannerAdfHatchOpen": "cover_open",
    "ScannerAdfMultipickDetected": "multipick",
}


def build_escl_settings(profile: ProfileSettings, caps) -> ScanSettings:  # noqa: ANN001
    w, h = profile.scan_region_300(caps)
    s = profile.scanner
    blank = None
    if profile.processing.blank_removal in ("scanner", "both"):
        blank = True
    elif s.blank_page_removal is not None:
        blank = s.blank_page_removal
    return ScanSettings(
        resolution=profile.resolution,
        color_mode=escl_color_mode(profile),
        duplex=profile.source == "adf_duplex",
        input_source="Feeder",
        document_format="image/jpeg",
        width=w,
        height=h,
        intent=profile.intent,
        content_type=profile.content_type,
        blank_page_removal=blank if (caps is None or caps.blank_page_detection) else None,
        blank_page_sensitivity=s.blank_page_sensitivity if blank else None,
        multipick_detection=s.multipick_detection if (caps is None or caps.multipick_actions) else None,
        multipick_action=s.multipick_action,
        auto_crop=s.auto_crop if (caps is None or caps.auto_crop) else None,
        auto_exposure=s.auto_exposure,
        carrier_sheet=s.carrier_sheet,
        brightness=s.brightness,
        contrast=s.contrast,
        gamma=s.gamma,
        highlight=s.highlight,
        shadow=s.shadow,
        sharpen=s.sharpen,
        noise_removal=s.noise_removal,
        threshold=s.threshold,
        compression_factor=s.compression_factor,
    )


@dataclass
class BatchOutcome:
    pages: int
    error_code: str | None = None
    error_message: str | None = None


def _image_meta(data: bytes) -> tuple[int, int, int]:
    try:
        with Image.open(io.BytesIO(data)) as im:
            dpi = im.info.get("dpi", (0, 0))[0] or 0
            return im.width, im.height, int(round(float(dpi))) if dpi else 0
    except Exception:  # noqa: BLE001 - metadata only
        return 0, 0, 0


class ScanRunner:
    def __init__(self, ctx) -> None:  # noqa: ANN001
        self.ctx = ctx
        # Documents whose running scan the user asked to stop (checked between pages).
        self.cancel_requests: set[int] = set()
        self.current_job: tuple[int, str] | None = None  # (document_id, eSCL job URI)

    @property
    def scanner(self):  # noqa: ANN201
        return self.ctx.scanner

    @staticmethod
    def _load(batch_id: int) -> tuple[int, ProfileSettings] | None:
        with session_scope() as s:
            batch = s.get(Batch, batch_id)
            if batch is None or batch.status not in ("queued",):
                log.info("scan.skip", batch_id=batch_id, status=batch.status if batch else None)
                return None
            doc = s.get(Document, batch.document_id)
            assert doc is not None
            if doc.status in st.TERMINAL:
                batch.status = "cancelled"
                return None
            return doc.id, ProfileSettings.model_validate(doc.profile_snapshot)

    @staticmethod
    def _mark_scanning(doc_id: int, batch_id: int) -> None:
        with session_scope() as s:
            doc = s.get(Document, doc_id)
            batch = s.get(Batch, batch_id)
            assert doc and batch
            st.transition(s, doc, st.SCANNING, code="scan.started", message=f"Stapel {batch.seq}")
            batch.status = "scanning"
            batch.started_at = utcnow()

    @staticmethod
    def _update_batch(batch_id: int, **values) -> None:  # noqa: ANN003
        with session_scope() as s:
            b = s.get(Batch, batch_id)
            assert b
            for k, v in values.items():
                setattr(b, k, v)

    async def run(self, task: Task) -> None:
        batch_id = int(task.payload["batch_id"])
        loaded = await run_db(self._load, batch_id)
        if loaded is None:
            return
        doc_id, profile = loaded
        context_id = task.payload.get("context_id")

        structlog.contextvars.bind_contextvars(document_id=doc_id, batch_id=batch_id)
        try:
            outcome = await self._run_locked(doc_id, batch_id, profile, context_id)
        finally:
            structlog.contextvars.unbind_contextvars("document_id", "batch_id")
        await run_db(self._finish, doc_id, batch_id, outcome)

    async def _run_locked(
        self, doc_id: int, batch_id: int, profile: ProfileSettings, context_id: str | None = None
    ) -> BatchOutcome:
        min_free = int(self.ctx.settings.get("disk.min_free_mb") or 0)
        free = self.ctx.storage.free_mb()
        if free < min_free:
            return BatchOutcome(0, "insufficient_disk", f"only {free} MB free, need {min_free} MB")
        async with self.scanner.lock:
            self.scanner.set_active(doc_id)
            await run_db(self._mark_scanning, doc_id, batch_id)
            job_uri: str | None = None
            received = 0
            try:
                client = self.scanner.client()
                status = await client.get_status()
                err = ADF_ERRORS.get(status.adf_state or "")
                if err:
                    raise ScanFailure(err, USER_MESSAGES[err])
                settings = build_escl_settings(profile, self.scanner.capabilities)
                settings.context_id = context_id
                await run_db(self._update_batch, batch_id,
                             settings={k: v for k, v in settings.__dict__.items() if v is not None})
                job_uri = await self._create_job_with_busy_retry(client, settings)
                await run_db(self._update_batch, batch_id, escl_job_uri=job_uri)
                self.current_job = (doc_id, job_uri)
                log.info("scan.job_created", job_uri=job_uri)
                duplex = profile.source == "adf_duplex"
                while doc_id not in self.cancel_requests:
                    try:
                        page = await client.next_document(job_uri)
                    except EsclError:
                        if doc_id in self.cancel_requests:
                            break  # the job was deleted on our request
                        raise
                    if page is None:
                        break
                    await run_db(self._store_page, doc_id, batch_id, page.content, page.content_type, duplex,
                                 received)
                    received += 1
                if doc_id in self.cancel_requests:
                    await client.cancel_job(job_uri)
                    return BatchOutcome(received, "cancelled", USER_MESSAGES["cancelled"])
                # The job ended – find out whether it ended well.
                await self._check_job_end(client, job_uri, received)
                return BatchOutcome(received)
            except ScanFailure as e:
                if job_uri:
                    await client.cancel_job(job_uri)
                return BatchOutcome(received, e.code, e.message)
            except ScanRejected as e:
                code, msg = await self._explain_rejection(e)
                return BatchOutcome(received, code, msg)
            except EsclError as e:
                if job_uri:
                    await self.scanner.client().cancel_job(job_uri)
                code = e.code
                return BatchOutcome(received, code, USER_MESSAGES.get(code, e.message) + f" ({e.message})")
            except Exception as e:  # noqa: BLE001 - DB/disk errors: stop the scanner job, keep pages
                log.exception("scan.internal_error")
                if job_uri:
                    try:
                        await self.scanner.client().cancel_job(job_uri)
                    except Exception:  # noqa: BLE001
                        pass
                try:
                    from ..jobs.worker import adopt_orphans_for

                    await run_db(adopt_orphans_for, self.ctx, doc_id)
                except Exception:  # noqa: BLE001
                    log.exception("scan.adopt_failed")
                return BatchOutcome(received, "internal_error", f"Interner Fehler beim Speichern: {e}"[:500])
            except asyncio.CancelledError:
                if job_uri:
                    try:
                        await asyncio.shield(self.scanner.client().cancel_job(job_uri))
                    except Exception:  # noqa: BLE001
                        pass
                raise
            finally:
                self.current_job = None
                self.cancel_requests.discard(doc_id)
                self.scanner.set_active(None)

    async def _create_job_with_busy_retry(self, client, settings: ScanSettings) -> str:  # noqa: ANN001
        loop = asyncio.get_running_loop()
        deadline = loop.time() + BUSY_RETRY_S
        delay = 2.0
        while True:
            try:
                return await client.create_job(settings)
            except ScannerBusy as e:
                if loop.time() + delay > deadline:
                    raise ScanFailure("scanner_busy", USER_MESSAGES["scanner_busy"]) from e
                log.info("scan.busy_retry", delay=delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 15.0)

    async def _explain_rejection(self, e: ScanRejected) -> tuple[str, str]:
        try:
            status = await self.scanner.client().get_status()
            code = ADF_ERRORS.get(status.adf_state or "")
            if code:
                return code, USER_MESSAGES[code]
        except EsclError:
            pass
        return "scan_rejected", f"{USER_MESSAGES['scan_rejected']} (HTTP {e.status_code})"

    async def _check_job_end(self, client, job_uri: str, received: int) -> None:  # noqa: ANN001
        try:
            status = await client.get_status()
        except EsclError:
            if received == 0:
                raise
            return  # we have pages and the job ended with 404 – accept
        job = status.job(job_uri)
        adf_err = ADF_ERRORS.get(status.adf_state or "")
        reasons = " ".join(job.reasons).lower() if job else ""
        aborted = job is not None and (job.state or "").lower() in ("aborted", "canceled", "cancelled")
        if "multipick" in reasons and not aborted:
            await run_db(self._multipick_warning, job_uri)
        if aborted:
            code = adf_err or ("multipick" if "multipick" in reasons else
                               "paper_jam" if "jam" in reasons else "scanner_error")
            raise ScanFailure(code, USER_MESSAGES.get(code, f"scan aborted by scanner ({reasons or 'no reason'})"))
        if adf_err and adf_err != "feeder_empty":
            raise ScanFailure(adf_err, USER_MESSAGES[adf_err])
        if received == 0:
            raise ScanFailure("feeder_empty", USER_MESSAGES["feeder_empty"])

    def _multipick_warning(self, job_uri: str) -> None:
        with session_scope() as s:
            b = s.get(Batch, self._batch_id_for(s, job_uri))
            if b:
                doc = s.get(Document, b.document_id)
                if doc:
                    st.add_event(s, doc, "scan.multipick_continued",
                                 "Mehrfacheinzug erkannt, Scan wurde fortgesetzt – Seiten prüfen", "warning")

    @staticmethod
    def _batch_id_for(s, job_uri: str) -> int | None:  # noqa: ANN001
        return s.scalar(select(Batch.id).where(Batch.escl_job_uri == job_uri))

    def _store_page(
        self, doc_id: int, batch_id: int, data: bytes, ctype: str, duplex: bool, index: int
    ) -> None:
        ext = "jpg" if "jpeg" in ctype else ("pdf" if "pdf" in ctype else "bin")
        page_id, path, sha = self.ctx.storage.save_raw_page(doc_id, data, ext)  # persisted first
        w, h, dpi = _image_meta(data)
        with session_scope() as s:
            doc = s.get(Document, doc_id)
            assert doc
            maxpos = s.scalar(select(func.max(Page.position)).where(Page.document_id == doc_id)) or 0
            s.add(
                Page(
                    id=page_id,
                    document_id=doc_id,
                    batch_id=batch_id,
                    position=float(maxpos) + 1.0,
                    side=("back" if index % 2 else "front") if duplex else "front",
                    raw_path=self.ctx.storage.rel(path),
                    raw_sha256=sha,
                    raw_size=len(data),
                    content_type=ctype,
                    width=w,
                    height=h,
                    dpi=dpi or int(doc.profile_snapshot.get("resolution", 300)),
                )
            )
            b = s.get(Batch, batch_id)
            assert b
            b.pages_received += 1
            s.flush()
            doc.page_count = (
                s.scalar(select(func.count(Page.id)).where(Page.document_id == doc_id, Page.deleted.is_(False))) or 0
            )
            owner = doc.owner_id
            count = doc.page_count
        self.ctx.bus.publish(
            "page.added",
            {"document_id": doc_id, "page_id": page_id, "batch_id": batch_id, "index": index, "page_count": count},
            audience=owner,
        )
        log.info("scan.page_stored", page_id=page_id, bytes=len(data))

    def _finish(self, doc_id: int, batch_id: int, o: BatchOutcome) -> None:
        with session_scope() as s:
            doc = s.get(Document, doc_id)
            batch = s.get(Batch, batch_id)
            assert doc and batch
            batch.finished_at = utcnow()
            if o.error_code is None:
                batch.status = "done"
                st.transition(s, doc, st.STORED, code="scan.batch_done", message=f"{o.pages} Seite(n)")
                if doc.auto_finish:
                    start_processing(s, doc)
                return
            if o.error_code == "cancelled":
                batch.status = "cancelled"
                total = s.scalar(select(func.count(Page.id)).where(Page.document_id == doc_id,
                                                                   Page.deleted.is_(False))) or 0
                if total:
                    st.transition(s, doc, st.STORED, code="scan.cancelled",
                                  message=f"Scan abgebrochen – {total} Seite(n) vorhanden")
                else:
                    st.transition(s, doc, st.FAILED, code="scan.cancelled", message="Scan abgebrochen",
                                  error_code="cancelled")
                return
            batch.status = "failed"
            batch.error_code = o.error_code
            batch.error_message = o.error_message
            total = s.scalar(select(func.count(Page.id)).where(Page.document_id == doc_id, Page.deleted.is_(False))) or 0
            msg = o.error_message or o.error_code
            if total > 0:
                if o.pages:
                    msg = f"{msg} – {o.pages} Seite(n) dieses Stapels gesichert"
                st.transition(s, doc, st.NEEDS_ATTENTION, code=f"scan.{o.error_code}", message=msg,
                              level="error", error_code=o.error_code)
            else:
                st.transition(s, doc, st.FAILED, code=f"scan.{o.error_code}", message=msg, level="error",
                              error_code=o.error_code)


def start_processing(s, doc: Document) -> Task:  # noqa: ANN001
    """Move a document to processing and enqueue the processing task (idempotent per run)."""
    doc.processing_run += 1
    st.transition(s, doc, st.PROCESSING, code="process.queued")
    return enqueue(
        s,
        "process",
        idempotency_key=f"process:{doc.id}:{doc.processing_run}",
        document_id=doc.id,
        payload={"run": doc.processing_run},
        max_attempts=2,
    )
