"""Scans, documents (history/inbox), pages, editor operations, deliveries, downloads."""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, PlainTextResponse
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from ..context import AppContext
from ..db import get_session
from ..documents import (
    DomainError,
    add_batch,
    claim,
    create_scan,
    discard,
    ensure_editable,
    finish,
    merge_documents,
    move_pages,
    reorder_pages,
    resolve_profile,
    split_into_children,
)
from ..jobs import states as st
from ..jobs.queue import enqueue, queue_position
from ..models import ArtifactRow, Delivery, Document, JobEvent, Page, Task, User
from ..processing import imageops as ops
from ..processing.qr import detect_qr, select_hit
from ..processing.render import PageEdits, render_page
from ..profiles import ZONE_PRESETS, ProfileSettings
from ..security.auth import Principal
from .deps import (
    CtxDep,
    PrincipalDep,
    domain_http,
    get_owned_document,
    get_visible_document,
    get_visible_page,
    visible_condition,
)
from .schemas import (
    DocumentDetail,
    DocumentList,
    DocumentPatch,
    DocumentSummary,
    ErrorResponse,
    MergeIn,
    MovePagesIn,
    PageOut,
    PagePatch,
    PagesActionIn,
    QrDetectIn,
    QrDetectOut,
    QrHitOut,
    ReorderIn,
    ScanIn,
    SplitIn,
    SplitResult,
)

router = APIRouter(prefix="/api", tags=["documents"])
ERR = {404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}}


def _notify(ctx: AppContext) -> None:
    if ctx.worker:
        ctx.worker.notify()


def _summary(doc: Document, s: Session) -> DocumentSummary:
    out = DocumentSummary.model_validate(doc)
    if not doc.raw_expired:
        out.first_page_id = s.scalar(select(Page.id).where(Page.document_id == doc.id, Page.deleted.is_(False))
                                     .order_by(Page.position).limit(1))
    out.destinations = [d.destination_type for d in doc.deliveries] or [
        d.get("type", "") for d in (doc.profile_snapshot or {}).get("destinations", []) if d.get("enabled", True)
    ]
    return out


def _detail(s: Session, doc: Document) -> DocumentDetail:
    events = s.scalars(select(JobEvent).where(JobEvent.document_id == doc.id).order_by(JobEvent.id.desc()).limit(200)).all()
    base = _summary(doc, s).model_dump()
    pos = None
    if doc.status == st.QUEUED:
        t = s.scalar(select(Task).where(Task.document_id == doc.id, Task.kind == "scan", Task.status == "pending"))
        if t:
            pos = queue_position(s, t)
    return DocumentDetail(
        **base,
        profile_snapshot=ProfileSettings.model_validate(doc.profile_snapshot),
        title_suggestion=doc.title_suggestion,
        document_date=doc.document_date,
        qr_source=doc.qr_source,
        qr_page_id=doc.qr_page_id,
        add_qr_to_filename=doc.add_qr_to_filename,
        auto_finish=doc.auto_finish,
        pages=[PageOut.model_validate(p) for p in doc.pages],
        batches=[b for b in doc.batches],  # type: ignore[misc]
        deliveries=sorted(doc.deliveries, key=lambda d: -d.id),  # type: ignore[arg-type]
        artifacts=[a for a in doc.artifacts if a.kind != "pdf_image"],  # type: ignore[misc]
        events=list(events),  # type: ignore[arg-type]
        queue_position=pos,
        has_ocr_text=bool(doc.ocr_text),
    )


def _wrap(fn):  # noqa: ANN001, ANN202
    try:
        return fn()
    except DomainError as e:
        raise domain_http(e) from e


# ------------------------------------------------------------------ scans
@router.post("/scans", response_model=DocumentDetail, status_code=201,
             responses={**ERR, 422: {"model": ErrorResponse}, 429: {"model": ErrorResponse},
                        507: {"model": ErrorResponse}})
def start_scan(body: ScanIn, s: Session = Depends(get_session), ctx: AppContext = CtxDep,
               p: Principal = PrincipalDep,
               idempotency_key: str | None = Header(None, max_length=200)) -> DocumentDetail:
    """Start a scan with a profile. Pages are stored as they arrive; watch `/api/events`."""
    if not ctx.settings.get("scanner.url"):
        raise HTTPException(409, detail={"code": "scanner_not_configured", "message": "Kein Scanner konfiguriert"})

    def run() -> Document:
        user = s.get(User, p.id)
        prof = resolve_profile(s, body.profile_id, p.id)
        return create_scan(
            s, ctx, user=user, profile=prof, overrides=body.overrides, origin="api" if p.via == "token" else "app",
            auto_finish=body.auto_finish if body.auto_finish is not None else (True if p.via == "token" else None),
            webhook_url=body.webhook_url, idempotency_key=idempotency_key,
            add_qr_to_filename=body.add_qr_to_filename, title=body.title,
        )

    doc = _wrap(run)
    s.flush()
    s.refresh(doc)
    s.commit()
    _notify(ctx)
    return _detail(s, doc)


# ------------------------------------------------------------------ listing / search
def _fts_query(q: str) -> str | None:
    words = re.findall(r"[\w\-./]+", q, flags=re.UNICODE)[:12]
    terms = ['"' + w.replace('"', "") + '"*' for w in words if w.strip("-./")]
    return " ".join(terms) or None


@router.get("/documents", response_model=DocumentList)
def list_documents(
    scope: Literal["history", "inbox", "active", "all"] = "history",
    status: list[str] | None = Query(None),
    profile_id: int | None = None,
    destination: str | None = Query(None, max_length=20),
    q: str | None = Query(None, max_length=200),
    date_from: date | None = None,
    date_to: date | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    s: Session = Depends(get_session),
    p: Principal = PrincipalDep,
) -> DocumentList:
    qry = select(Document).where(visible_condition(p.id))
    if scope == "inbox":
        qry = qry.where(Document.owner_id.is_(None))
    elif scope == "history":
        qry = qry.where(Document.owner_id == p.id, Document.status.not_in((st.DISCARDED,)))
    elif scope == "active":
        qry = qry.where(Document.owner_id == p.id, Document.status.in_(
            (st.QUEUED, st.SCANNING, st.STORED, st.PROCESSING, st.DELIVERING, st.NEEDS_ATTENTION)))
    if status:
        qry = qry.where(Document.status.in_(status))
    if profile_id:
        qry = qry.where(Document.profile_id == profile_id)
    if destination:
        qry = qry.where(Document.id.in_(select(Delivery.document_id).where(Delivery.destination_type == destination)))
    if date_from:
        qry = qry.where(Document.created_at >= datetime.combine(date_from, datetime.min.time()))
    if date_to:
        qry = qry.where(Document.created_at < datetime.combine(date_to, datetime.min.time()) + timedelta(days=1))
    if q:
        fts = _fts_query(q)
        if fts:
            qry = qry.where(Document.id.in_(
                select(text("rowid")).select_from(text("documents_fts"))
                .where(text("documents_fts MATCH :q").bindparams(q=fts))
            ))
    total = s.scalar(select(func.count()).select_from(qry.subquery())) or 0
    rows = s.scalars(qry.order_by(Document.created_at.desc(), Document.id.desc()).limit(limit).offset(offset)).all()
    return DocumentList(items=[_summary(d, s) for d in rows], total=total, limit=limit, offset=offset)


@router.get("/documents/{doc_id}", response_model=DocumentDetail, responses=ERR)
def get_document(doc_id: int, s: Session = Depends(get_session), p: Principal = PrincipalDep) -> DocumentDetail:
    return _detail(s, get_visible_document(s, doc_id, p))


@router.patch("/documents/{doc_id}", response_model=DocumentDetail, responses=ERR)
def patch_document(doc_id: int, body: DocumentPatch, s: Session = Depends(get_session), ctx: AppContext = CtxDep,
                   p: Principal = PrincipalDep) -> DocumentDetail:
    doc = get_owned_document(s, doc_id, p)
    if doc.status in st.ACTIVE - {st.QUEUED}:
        raise HTTPException(409, detail={"code": "busy", "message": "Dokument wird gerade bearbeitet"})
    if body.title is not None:
        doc.title = body.title.strip()
    if body.document_date is not None:
        doc.document_date = body.document_date or None
    if body.clear_manual_qr:
        doc.qr_source = None
        doc.qr_value = None
    elif body.qr_value is not None:
        v = body.qr_value.strip()
        doc.qr_value = v or None
        doc.qr_source = "manual" if v else "none"
        st.add_event(s, doc, "qr.manual", f"QR-Wert manuell gesetzt: {v or '(keiner)'}")
    if body.add_qr_to_filename is not None:
        doc.add_qr_to_filename = body.add_qr_to_filename
    if body.profile_id is not None:
        prof = _wrap(lambda: resolve_profile(s, body.profile_id, p.id))
        doc.profile_id = prof.id
        doc.profile_name = prof.name
        doc.profile_snapshot = ProfileSettings.model_validate(prof.settings).model_dump()
    if body.settings is not None:
        doc.profile_snapshot = body.settings.model_dump()
    st.publish_document(doc)
    return _detail(s, doc)


# ------------------------------------------------------------------ lifecycle
@router.post("/documents/{doc_id}/claim", response_model=DocumentDetail, responses=ERR)
def claim_document(doc_id: int, s: Session = Depends(get_session), p: Principal = PrincipalDep) -> DocumentDetail:
    """Take a walk-up inbox item. Atomic: if two users claim at once, exactly one wins (409 for the other)."""
    user = s.get(User, p.id)
    assert user
    doc = _wrap(lambda: claim(s, doc_id, user))
    return _detail(s, doc)


@router.post("/documents/{doc_id}/batches", response_model=DocumentDetail, responses=ERR)
def add_batch_ep(doc_id: int, s: Session = Depends(get_session), ctx: AppContext = CtxDep,
                 p: Principal = PrincipalDep) -> DocumentDetail:
    """Scan another stack into the same document (also: retry after an empty feeder/jam)."""
    doc = get_owned_document(s, doc_id, p)
    _wrap(lambda: add_batch(s, ctx, doc))
    s.commit()
    _notify(ctx)
    return _detail(s, doc)


@router.post("/documents/{doc_id}/finish", response_model=DocumentDetail, responses=ERR)
def finish_document(doc_id: int, s: Session = Depends(get_session), ctx: AppContext = CtxDep,
                    p: Principal = PrincipalDep) -> DocumentDetail:
    """Process (OCR etc.) and deliver. Can be repeated after edits (re-processing from raw pages)."""
    doc = get_owned_document(s, doc_id, p)
    _wrap(lambda: finish(s, doc))
    s.commit()
    _notify(ctx)
    return _detail(s, doc)


@router.post("/documents/{doc_id}/cancel", response_model=DocumentDetail, responses=ERR)
async def cancel_scan(doc_id: int, ctx: AppContext = CtxDep, p: Principal = PrincipalDep) -> DocumentDetail:
    """Stop a queued or running scan. Pages already received are kept (document → stored)."""
    from ..db import run_db

    state = await run_db(_cancel_queued, doc_id, p)
    if state == "running":
        runner = ctx.worker.scan_runner if ctx.worker else None
        if runner is not None:
            runner.cancel_requests.add(doc_id)
            job = runner.current_job
            if job and job[0] == doc_id and ctx.scanner:
                await ctx.scanner.client().cancel_job(job[1])  # unblocks a waiting NextDocument
    return await run_db(_detail_by_id, doc_id, p)


def _cancel_queued(doc_id: int, p: Principal) -> str:
    from ..db import session_scope
    from ..jobs.queue import cancel_for_document

    with session_scope() as s:
        doc = get_owned_document(s, doc_id, p)
        if doc.status == st.SCANNING:
            return "running"
        if doc.status != st.QUEUED:
            raise HTTPException(409, detail={"code": "invalid_state", "message": "Kein laufender Scan"})
        n = cancel_for_document(s, doc.id, ("scan",))
        if n == 0:
            return "running"  # the worker just picked it up
        for b in doc.batches:
            if b.status == "queued":
                b.status = "cancelled"
        pages = s.scalar(select(func.count(Page.id)).where(Page.document_id == doc.id, Page.deleted.is_(False))) or 0
        if pages:
            st.transition(s, doc, st.STORED, code="scan.cancelled", message="Wartender Stapel abgebrochen")
        else:
            st.transition(s, doc, st.DISCARDED, code="scan.cancelled", message="Scan abgebrochen")
        return "done"


def _detail_by_id(doc_id: int, p: Principal) -> DocumentDetail:
    from ..db import session_scope

    with session_scope() as s:
        return _detail(s, get_visible_document(s, doc_id, p))


@router.post("/documents/{doc_id}/discard", response_model=DocumentDetail, responses=ERR)
def discard_document(doc_id: int, s: Session = Depends(get_session), ctx: AppContext = CtxDep,
                     p: Principal = PrincipalDep) -> DocumentDetail:
    doc = get_owned_document(s, doc_id, p)
    _wrap(lambda: discard(s, ctx, doc))
    return _detail(s, doc)


@router.post("/documents/{doc_id}/resend", response_model=DocumentDetail, responses=ERR)
def resend(doc_id: int, s: Session = Depends(get_session), ctx: AppContext = CtxDep,
           p: Principal = PrincipalDep) -> DocumentDetail:
    """Deliver again to all destinations of the current profile (re-processes if outputs expired)."""
    doc = get_owned_document(s, doc_id, p)
    if doc.status not in (st.DONE, st.NEEDS_ATTENTION):
        raise HTTPException(409, detail={"code": "invalid_state", "message": "Erneut senden nicht möglich"})
    if doc.processed_expired or not doc.artifacts:
        _wrap(lambda: finish(s, doc))
    else:
        from ..processing.pipeline import Processor

        Processor(ctx)._start_deliveries(s, doc, ProfileSettings.model_validate(doc.profile_snapshot),
                                         doc.processing_run)
    s.commit()
    _notify(ctx)
    return _detail(s, doc)


@router.post("/documents/{doc_id}/deliveries/{delivery_id}/retry", response_model=DocumentDetail, responses=ERR)
def retry_delivery(doc_id: int, delivery_id: int, s: Session = Depends(get_session), ctx: AppContext = CtxDep,
                   p: Principal = PrincipalDep) -> DocumentDetail:
    """Retry one failed destination without rescanning."""
    doc = get_owned_document(s, doc_id, p)
    dl = s.get(Delivery, delivery_id)
    if dl is None or dl.document_id != doc.id:
        raise HTTPException(404, detail={"code": "not_found", "message": "Zustellung nicht gefunden"})
    if dl.status not in ("needs_attention", "failed"):
        raise HTTPException(409, detail={"code": "invalid_state", "message": "Zustellung ist nicht fehlgeschlagen"})
    if doc.processed_expired or not doc.artifacts:
        raise HTTPException(409, detail={"code": "processed_expired",
                                         "message": "Ausgabedateien abgelaufen – bitte 'Erneut verarbeiten'"})
    dl.status = "pending"
    dl.last_error = None
    n = s.scalar(select(func.count(Task.id)).where(Task.delivery_id == dl.id)) or 0
    enqueue(s, "deliver", idempotency_key=f"deliver:{dl.id}:{n + 1}", document_id=doc.id, delivery_id=dl.id,
            max_attempts=6)
    st.transition(s, doc, st.DELIVERING, code="deliver.retry", message=dl.label)
    s.commit()
    _notify(ctx)
    return _detail(s, doc)


# ------------------------------------------------------------------ editor operations
@router.post("/documents/{doc_id}/reorder", response_model=DocumentDetail, responses=ERR)
def reorder(doc_id: int, body: ReorderIn, s: Session = Depends(get_session), p: Principal = PrincipalDep) -> DocumentDetail:
    doc = get_owned_document(s, doc_id, p)
    _wrap(lambda: reorder_pages(s, doc, body.page_ids))
    s.flush()
    s.refresh(doc)
    return _detail(s, doc)


def _set_deleted(s: Session, doc: Document, ids: list[str], deleted: bool) -> None:
    ensure_editable(doc)
    for pid in ids:
        pg = s.get(Page, pid)
        if pg is None or pg.document_id != doc.id:
            raise DomainError("bad_pages", "Seite gehört nicht zum Dokument", 400)
        pg.deleted = deleted
    s.flush()
    doc.page_count = s.scalar(select(func.count(Page.id)).where(Page.document_id == doc.id, Page.deleted.is_(False))) or 0
    st.publish_document(doc)


@router.post("/documents/{doc_id}/pages/delete", response_model=DocumentDetail, responses=ERR)
def delete_pages(doc_id: int, body: PagesActionIn, s: Session = Depends(get_session),
                 p: Principal = PrincipalDep) -> DocumentDetail:
    """Soft delete (restorable; originals stay until retention)."""
    doc = get_owned_document(s, doc_id, p)
    _wrap(lambda: _set_deleted(s, doc, body.page_ids, True))
    return _detail(s, doc)


@router.post("/documents/{doc_id}/pages/restore", response_model=DocumentDetail, responses=ERR)
def restore_pages(doc_id: int, body: PagesActionIn, s: Session = Depends(get_session),
                  p: Principal = PrincipalDep) -> DocumentDetail:
    doc = get_owned_document(s, doc_id, p)
    _wrap(lambda: _set_deleted(s, doc, body.page_ids, False))
    return _detail(s, doc)


@router.post("/documents/{doc_id}/pages/insert", response_model=DocumentDetail, responses=ERR)
def insert_pages(doc_id: int, body: MovePagesIn, s: Session = Depends(get_session), ctx: AppContext = CtxDep,
                 p: Principal = PrincipalDep) -> DocumentDetail:
    """Insert pages from another of your documents (copy by default, or move)."""
    target = get_owned_document(s, doc_id, p)
    src = get_owned_document(s, body.source_document_id, p)
    _wrap(lambda: move_pages(s, ctx, src, target, body.page_ids, copy=body.copy_pages))
    s.flush()
    s.refresh(target)
    return _detail(s, target)


@router.post("/documents/{doc_id}/split", response_model=SplitResult, responses=ERR)
def split(doc_id: int, body: SplitIn, s: Session = Depends(get_session), p: Principal = PrincipalDep) -> SplitResult:
    """Split into several documents; a new document starts at each given page."""
    doc = get_owned_document(s, doc_id, p)
    _wrap(lambda: ensure_editable(doc))
    pages = [pg for pg in doc.pages if not pg.deleted]
    starts = set(body.before_page_ids)
    if not starts <= {pg.id for pg in pages}:
        raise HTTPException(400, detail={"code": "bad_pages", "message": "Unbekannte Seite"})
    groups: list[list[str]] = []
    for pg in pages:
        if pg.id in starts and (groups or pg is not pages[0]):
            groups.append([])
        if groups:
            groups[-1].append(pg.id)
    groups = [g for g in groups if g]
    children = split_into_children(s, doc.id, groups, process=False)
    st.publish_document(doc)
    return SplitResult(children=children)


@router.post("/documents/{doc_id}/merge", response_model=DocumentDetail, responses=ERR)
def merge(doc_id: int, body: MergeIn, s: Session = Depends(get_session), ctx: AppContext = CtxDep,
          p: Principal = PrincipalDep) -> DocumentDetail:
    target = get_owned_document(s, doc_id, p)
    sources = [get_owned_document(s, i, p) for i in body.source_document_ids if i != doc_id]
    _wrap(lambda: merge_documents(s, ctx, target, sources))
    s.flush()
    s.refresh(target)
    return _detail(s, target)


@router.post("/documents/{doc_id}/qr/detect", response_model=QrDetectOut, responses=ERR)
def qr_detect(doc_id: int, body: QrDetectIn, s: Session = Depends(get_session), ctx: AppContext = CtxDep,
              p: Principal = PrincipalDep) -> QrDetectOut:
    """Run QR detection on one page (default: first) with the profile zone or a zone being edited."""
    # Sync handler (threadpool): no DB transaction is ever opened on the event loop.
    doc = get_visible_document(s, doc_id, p)
    pages = [pg for pg in doc.pages if not pg.deleted]
    page = next((pg for pg in pages if pg.id == body.page_id), pages[0] if pages else None)
    if page is None or doc.raw_expired:
        raise HTTPException(409, detail={"code": "no_pages", "message": "Keine Seiten verfügbar"})
    profile = ProfileSettings.model_validate(doc.profile_snapshot)
    zone = body.zone or (profile.qr.effective_zone() if profile.qr.enabled else ZONE_PRESETS["full"])
    raw_path = ctx.storage.abs(page.raw_path)
    edits = PageEdits.model_validate(page.edits or {})
    analysis = dict(page.analysis or {})
    page_id = page.id
    s.commit()  # don't hold the DB lock during image work
    from ..processing.render import orient_and_straighten

    try:
        img = ops.load(str(raw_path))
    except (OSError, ValueError) as e:
        raise HTTPException(410, detail={"code": "raw_missing", "message": "Originalseite fehlt"}) from e
    oriented, _r, _a, upd = orient_and_straighten(img, edits, profile, analysis)
    hits = detect_qr(oriented, zone)
    if upd:
        pg = s.get(Page, page_id)
        if pg:
            pg.analysis = {**(pg.analysis or {}), **upd}
    sel = select_hit(hits, profile.qr) if hits else None
    return QrDetectOut(page_id=page_id, zone=zone, hits=[QrHitOut(**h.to_dict()) for h in hits],
                       selected=sel.value if sel else None)


# ------------------------------------------------------------------ pages
@router.patch("/pages/{page_id}", response_model=PageOut, responses=ERR)
def patch_page(page_id: str, body: PagePatch, s: Session = Depends(get_session), p: Principal = PrincipalDep) -> PageOut:
    page, doc = get_visible_page(s, page_id, p)
    if doc.owner_id != p.id:
        raise HTTPException(409, detail={"code": "not_claimed", "message": "Eingangsdokument zuerst übernehmen"})
    _wrap(lambda: ensure_editable(doc))
    if body.edits is not None:
        page.edits = body.edits.model_dump(exclude_none=True)
    if body.keep_blank is not None:
        page.keep_blank = body.keep_blank
    if body.deleted is not None:
        page.deleted = body.deleted
    s.flush()
    doc.page_count = s.scalar(select(func.count(Page.id)).where(Page.document_id == doc.id, Page.deleted.is_(False))) or 0
    from ..events import bus

    bus.publish("page.updated", {"document_id": doc.id, "page_id": page.id}, audience=doc.owner_id)
    return PageOut.model_validate(page)


SIZES = {"thumb": 320, "preview": 1400, "full": 4000}


def _render_cached(ctx: AppContext, page: Page, doc: Document, variant: str, size: str,
                   edits: PageEdits | None) -> tuple[Path, dict]:
    profile = ProfileSettings.model_validate(doc.profile_snapshot)
    e = edits if edits is not None else PageEdits.model_validate(page.edits or {})
    key_src = json.dumps({"v": variant, "s": size, "e": e.model_dump(), "p": profile.processing.model_dump(),
                          "c": profile.color_mode, "a": {k: v for k, v in (page.analysis or {}).items()
                                                        if k.startswith(("osd", "skew"))}}, sort_keys=True)
    key = hashlib.sha256((page.raw_sha256 + key_src).encode()).hexdigest()[:24]
    out = ctx.storage.render_cache_dir(page.id) / f"{key}.jpg"
    if out.exists():
        return out, {}
    raw = ops.load(str(ctx.storage.abs(page.raw_path)))
    small, _f = ops.scale_to_max(raw, SIZES[size])
    updates: dict = {}
    if variant == "raw":
        img = small
    else:
        res = render_page(small, e, profile, dict(page.analysis or {}))
        img, updates = res.image, res.analysis_updates
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.write_bytes(ops.encode_jpeg(img, 80 if size == "thumb" else 85))
    tmp.replace(out)
    return out, updates


@router.get("/pages/{page_id}/image", responses={200: {"content": {"image/jpeg": {}}}, **ERR})
def page_image(request: Request, page_id: str, variant: Literal["raw", "processed"] = "processed",
               size: Literal["thumb", "preview", "full"] = "preview", s: Session = Depends(get_session),
               ctx: AppContext = CtxDep, p: Principal = PrincipalDep) -> FileResponse:
    page, doc = get_visible_page(s, page_id, p)
    if doc.raw_expired:
        raise HTTPException(410, detail={"code": "raw_expired", "message": "Original abgelaufen"})
    s.expunge(page)
    s.expunge(doc)
    s.commit()  # release the lock before rendering
    try:
        path, upd = _render_cached(ctx, page, doc, variant, size, None)
    except (OSError, ValueError) as e:
        raise HTTPException(410, detail={"code": "raw_missing", "message": "Originalseite fehlt"}) from e
    if upd:
        pg = s.get(Page, page_id)
        if pg:
            pg.analysis = {**(pg.analysis or {}), **upd}
    # The cache file name is a hash of raw page + edits + profile processing → a perfect ETag.
    etag = f'"{path.stem}"'
    headers = {"Cache-Control": "private, no-cache", "ETag": etag}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)  # type: ignore[return-value]
    return FileResponse(path, media_type="image/jpeg", headers=headers)


@router.post("/pages/{page_id}/preview", responses={200: {"content": {"image/jpeg": {}}}, **ERR})
def page_preview(page_id: str, edits: PageEdits, size: Literal["thumb", "preview"] = "preview",
                 s: Session = Depends(get_session), ctx: AppContext = CtxDep,
                 p: Principal = PrincipalDep) -> FileResponse:
    """Render a page with unsaved edits (before/after preview in the editor)."""
    page, doc = get_visible_page(s, page_id, p)
    if doc.raw_expired:
        raise HTTPException(410, detail={"code": "raw_expired", "message": "Original abgelaufen"})
    s.expunge(page)
    s.expunge(doc)
    s.commit()
    try:
        path, _ = _render_cached(ctx, page, doc, "processed", size, edits)
    except (OSError, ValueError) as e:
        raise HTTPException(410, detail={"code": "raw_missing", "message": "Originalseite fehlt"}) from e
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


# ------------------------------------------------------------------ downloads
@router.get("/documents/{doc_id}/artifacts/{artifact_id}", responses={200: {"content": {"application/pdf": {}}}, **ERR})
def download_artifact(doc_id: int, artifact_id: int, inline: bool = False, s: Session = Depends(get_session),
                      ctx: AppContext = CtxDep, p: Principal = PrincipalDep) -> FileResponse:
    doc = get_owned_document(s, doc_id, p)
    a = s.get(ArtifactRow, artifact_id)
    if a is None or a.document_id != doc.id:
        raise HTTPException(404, detail={"code": "not_found", "message": "Datei nicht gefunden"})
    path = ctx.storage.abs(a.path)
    if not path.exists():
        raise HTTPException(410, detail={"code": "processed_expired", "message": "Datei abgelaufen – neu verarbeiten"})
    return FileResponse(path, media_type=a.content_type, filename=a.filename,
                        content_disposition_type="inline" if inline else "attachment")


@router.get("/documents/{doc_id}/download", responses={200: {"content": {"application/octet-stream": {}}}, **ERR})
def download_document(doc_id: int, s: Session = Depends(get_session), ctx: AppContext = CtxDep,
                      p: Principal = PrincipalDep) -> Response:
    """Primary output; several files (e.g. JPEG pages) are zipped."""
    doc = get_owned_document(s, doc_id, p)
    arts = [a for a in doc.artifacts if a.kind != "pdf_image" and a.kind != "txt"] or \
        [a for a in doc.artifacts if a.kind == "txt"]
    if not arts:
        raise HTTPException(410, detail={"code": "processed_expired", "message": "Keine Ausgabedateien vorhanden"})
    if len(arts) == 1:
        return download_artifact(doc_id, arts[0].id, False, s, ctx, p)
    tmp = tempfile.NamedTemporaryFile(prefix="sbx-zip-", suffix=".zip", dir=ctx.config.tmp_dir, delete=False)
    tmp.close()
    with zipfile.ZipFile(tmp.name, "w", compression=zipfile.ZIP_STORED) as z:
        for a in arts:
            z.write(ctx.storage.abs(a.path), arcname=a.filename)
    stem = (doc.filename or "scan").rsplit(".", 1)[0]
    return FileResponse(tmp.name, media_type="application/zip", filename=f"{stem}.zip",
                        background=BackgroundTask(lambda: Path(tmp.name).unlink(missing_ok=True)))


@router.get("/documents/{doc_id}/text", response_class=PlainTextResponse, responses=ERR)
def document_text(doc_id: int, s: Session = Depends(get_session), p: Principal = PrincipalDep) -> str:
    doc = get_visible_document(s, doc_id, p)
    if doc.owner_id != p.id:
        raise HTTPException(404, detail={"code": "not_found", "message": "Nicht gefunden"})
    return doc.ocr_text or ""
