"""Delivery runner: executes one Delivery row via its destination plugin, tracks state, retries.

Also sends webhooks for API-triggered scans.
"""

from __future__ import annotations

import inspect
from datetime import datetime
from typing import Any

import httpx
import structlog
from sqlalchemy import select

from ..db import run_db, session_scope, utcnow
from ..jobs import states as st
from ..models import ArtifactRow, Delivery, Document, Task, User
from ..processing.ocr import ghostscript_compress
from ..processing.pipeline import schedule_webhook
from ..settings_store import UserSettingsAccessor
from ..storage import sha256_file
from .base import Artifact, DeliveryEnv, DeliveryError, DeliveryJob, RecoverAction
from .builtin import DownloadDestination, FolderDestination

log = structlog.get_logger(__name__)


def _registry() -> dict[str, Any]:
    reg: dict[str, Any] = {"download": DownloadDestination(), "folder": FolderDestination()}
    from .email import EmailDestination
    from .paperless import PaperlessDestination

    reg["email"] = EmailDestination()
    reg["paperless"] = PaperlessDestination()
    return reg


REGISTRY = _registry()


def get_destination(type_: str):  # noqa: ANN201
    d = REGISTRY.get(type_)
    if d is None:
        raise DeliveryError("unknown_destination", f"Unbekanntes Ziel: {type_}")
    return d


def make_env(ctx, user_id: int, extras: dict[str, Any] | None = None) -> DeliveryEnv:  # noqa: ANN001
    roots = ctx.settings.get("output.roots") or {}
    return DeliveryEnv(
        settings=UserSettingsAccessor(ctx.settings, user_id),
        tmp_dir=ctx.config.tmp_dir,
        http_client_factory=ctx.http_client_factory,
        extras={"output_roots": roots, **(extras or {})},
    )


def _artifact(ctx, row: ArtifactRow) -> Artifact:  # noqa: ANN001
    return Artifact(row.kind, ctx.storage.abs(row.path), row.filename, row.content_type, row.size, row.sha256)


def build_job(ctx, s, delivery: Delivery, doc: Document, user: User) -> DeliveryJob:  # noqa: ANN001
    rows = s.scalars(select(ArtifactRow).where(ArtifactRow.document_id == doc.id).order_by(ArtifactRow.id)).all()
    arts = [_artifact(ctx, r) for r in rows if r.kind != "pdf_image"]
    image_only = next((_artifact(ctx, r) for r in rows if r.kind == "pdf_image"), None)
    if not arts:
        raise DeliveryError("no_artifacts", "Keine Ausgabedateien vorhanden (abgelaufen?) – bitte neu verarbeiten")
    for a in arts:
        if not a.path.exists():
            raise DeliveryError("artifact_missing", "Ausgabedatei fehlt (Cache gelöscht?) – bitte neu verarbeiten")
    primary = arts[0]
    run_dir = primary.path.parent

    async def recompress(level: str) -> Artifact:
        out = run_dir / f"{primary.path.stem}.{level}.pdf"
        if not out.exists():
            await ghostscript_compress(primary.path, out, level)
        return Artifact("pdf", out, primary.filename, primary.content_type, out.stat().st_size, sha256_file(out))

    return DeliveryJob(
        delivery_id=delivery.id,
        document_id=doc.id,
        idempotency_key=f"scanbox-{doc.id}-{delivery.id}-{delivery.run}",
        attempt=delivery.attempts,
        title=doc.title or doc.profile_name,
        filename=doc.filename or primary.filename,
        qr_value=doc.qr_value,
        created_at=_doc_date(doc),
        page_count=doc.page_count,
        profile_name=doc.profile_name,
        user_id=user.id,
        username=user.username,
        user_email=user.email,
        user_display_name=user.display_name or user.username,
        artifacts=tuple(arts),
        ocr_text=doc.ocr_text,
        recompress=recompress if primary.content_type == "application/pdf" else None,
        image_only_pdf=image_only,
    )


def _doc_date(doc: Document) -> datetime:
    if doc.document_date:
        try:
            d = datetime.fromisoformat(doc.document_date)
            return d.replace(hour=doc.created_at.hour, minute=doc.created_at.minute)
        except ValueError:
            pass
    return doc.created_at


class DeliveryRunner:
    def __init__(self, ctx) -> None:  # noqa: ANN001
        self.ctx = ctx

    def _begin(self, task: Task):  # noqa: ANN202
        """Sync (thread): mark running, build the job. Returns None if nothing to do."""
        assert task.delivery_id is not None
        with session_scope() as s:
            dl = s.get(Delivery, task.delivery_id)
            if dl is None or dl.status in ("done", "cancelled"):
                return None
            doc = s.get(Document, dl.document_id)
            assert doc
            if doc.owner_id is None or doc.status not in (st.DELIVERING, st.NEEDS_ATTENTION, st.DONE):
                return None
            user = s.get(User, doc.owner_id)
            assert user
            previous = dict(dl.result or {})
            dl.status = "running"
            dl.attempts += 1
            try:
                job = build_job(self.ctx, s, dl, doc, user)
                dest = get_destination(dl.destination_type)
            except DeliveryError as e:
                self._record_failure(s, dl, doc, e, final=True)
                return None
            try:
                cfg = dest.Config.model_validate(dl.config or {})
            except Exception as e:  # noqa: BLE001
                self._record_failure(s, dl, doc, DeliveryError("invalid_config", f"Ungültige Konfiguration: {e}"),
                                     final=True)
                return None
            self._publish(dl, doc)
            return dl.id, user.id, doc.id, previous, job, dest, cfg

    def _fail(self, task_id: int, delivery_id: int, doc_id: int, e: DeliveryError, *, final: bool | None) -> None:
        with session_scope() as s:
            dl = s.get(Delivery, delivery_id)
            doc = s.get(Document, doc_id)
            assert dl and doc
            if final is None:
                t = s.get(Task, task_id)
                final = not e.retryable or (t is not None and t.attempts >= t.max_attempts)
            self._record_failure(s, dl, doc, e, final=final)

    @staticmethod
    def _save_progress(delivery_id: int, data: dict[str, Any]) -> None:
        with session_scope() as s:
            d2 = s.get(Delivery, delivery_id)
            if d2:
                d2.result = {**(d2.result or {}), **data}

    async def run(self, task: Task, *, recovering: bool = False) -> bool:
        """Returns False if a retryable error occurred (the queue reschedules)."""
        begun = await run_db(self._begin, task)
        if begun is None:
            return True
        delivery_id, user_id, doc_id, previous, job, dest, cfg = begun

        async def progress(data: dict[str, Any]) -> None:
            await run_db(self._save_progress, delivery_id, data)

        env = make_env(self.ctx, user_id, {"progress": progress, "previous_result": previous})
        structlog.contextvars.bind_contextvars(document_id=doc_id, delivery_id=delivery_id)
        try:
            if recovering:
                action = await dest.recover(job, cfg, env, previous)
                if action == RecoverAction.DONE:
                    await run_db(self._success, delivery_id, "Bereits zugestellt (nach Neustart verifiziert)",
                                 previous)
                    return True
                if action == RecoverAction.NEEDS_ATTENTION:
                    await run_db(self._fail, task.id, delivery_id, doc_id, DeliveryError(
                        "interrupted", "Unterbrochen – wurde evtl. schon gesendet. Bitte prüfen und ggf. "
                        "manuell erneut senden."), final=True)
                    return True
            result = await dest.deliver(job, cfg, env)
        except DeliveryError as e:
            await run_db(self._fail, task.id, delivery_id, doc_id, e, final=None)
            return not e.retryable
        except Exception as e:  # noqa: BLE001 - plugin bug must not kill the worker
            log.exception("delivery.crash")
            await run_db(self._fail, task.id, delivery_id, doc_id,
                         DeliveryError("internal_error", f"Interner Fehler: {e}"), final=True)
            return True
        finally:
            structlog.contextvars.unbind_contextvars("document_id", "delivery_id")
        await run_db(self._success, delivery_id, result.message, result.detail)
        return True

    def _success(self, delivery_id: int, message: str, detail: dict[str, Any]) -> None:
        with session_scope() as s:
            dl = s.get(Delivery, delivery_id)
            assert dl
            doc = s.get(Document, dl.document_id)
            assert doc
            dl.status = "done"
            dl.result = {**(dl.result or {}), **(detail or {}), "message": message}
            dl.last_error = None
            dl.error_code = None
            dl.delivered_at = utcnow()
            st.add_event(s, doc, f"deliver.{dl.destination_type}.done", f"{dl.label}: {message}")
            self._publish(dl, doc)
            self._settle(s, doc, dl.run)

    def _record_failure(self, s, dl: Delivery, doc: Document, e: DeliveryError, *, final: bool) -> None:  # noqa: ANN001
        dl.last_error = e.message[:2000]
        dl.error_code = e.code
        dl.status = "needs_attention" if final else "pending"
        level = "error" if final else "warning"
        suffix = "" if final else " – neuer Versuch folgt"
        st.add_event(s, doc, f"deliver.{dl.destination_type}.{e.code}", f"{dl.label}: {e.message}{suffix}", level=level)
        self._publish(dl, doc)
        if final:
            self._settle(s, doc, dl.run)

    def _settle(self, s, doc: Document, run: int) -> None:  # noqa: ANN001
        statuses = [d.status for d in s.scalars(
            select(Delivery).where(Delivery.document_id == doc.id, Delivery.run == run))]
        if any(x in ("pending", "running") for x in statuses):
            return
        if all(x == "done" for x in statuses):
            if doc.status != st.DONE:
                st.transition(s, doc, st.DONE, code="done")
                schedule_webhook(s, doc)
        elif doc.status != st.NEEDS_ATTENTION:
            failed = sum(1 for x in statuses if x != "done")
            st.transition(s, doc, st.NEEDS_ATTENTION, code="deliver.failed", level="error",
                          message=f"{failed} Ziel(e) fehlgeschlagen – erneut senden möglich",
                          error_code="delivery_failed")
            schedule_webhook(s, doc)

    def _publish(self, dl: Delivery, doc: Document) -> None:
        self.ctx.bus.publish(
            "delivery.updated",
            {"id": dl.id, "document_id": doc.id, "status": dl.status, "type": dl.destination_type,
             "error": dl.last_error},
            audience=doc.owner_id,
        )


def _webhook_payload(ctx, task: Task):  # noqa: ANN001, ANN202
    with session_scope() as s:
        doc = s.get(Document, task.document_id)
        if doc is None or not doc.webhook_url:
            return None
        arts = s.scalars(select(ArtifactRow).where(ArtifactRow.document_id == doc.id)).all()
        payload = {
            "event": "scan.finished",
            "document_id": doc.id,
            "status": doc.status,
            "title": doc.title,
            "filename": doc.filename,
            "qr_value": doc.qr_value,
            "pages": doc.page_count,
            "error_code": doc.error_code,
            "error_message": doc.error_message,
            "url": f"{ctx.config.base_url}/documents/{doc.id}",
            "files": [
                {"name": a.filename, "size": a.size,
                 "download_url": f"{ctx.config.base_url}/api/documents/{doc.id}/artifacts/{a.id}"}
                for a in arts if a.kind != "pdf_image"
            ],
        }
        return doc.webhook_url, payload


async def send_webhook(ctx, task: Task) -> bool:  # noqa: ANN001
    loaded = await run_db(_webhook_payload, ctx, task)
    if loaded is None:
        return True
    url, payload = loaded
    try:
        async with ctx.http_client_factory(timeout=httpx.Timeout(15.0)) as client:
            resp = await client.post(url, json=payload, headers={"User-Agent": "Scanbox/1.0"})
        if resp.status_code >= 500 or resp.status_code == 429:
            raise httpx.HTTPStatusError("retry", request=resp.request, response=resp)
        log.info("webhook.sent", status=resp.status_code)
        return True
    except httpx.HTTPError as e:
        log.warning("webhook.failed", error=str(e))
        return False


async def call_maybe_async(fn, *a):  # noqa: ANN001, ANN201
    r = fn(*a)
    if inspect.isawaitable(r):
        return await r
    return r
