"""Document domain operations (create scan, batches, finish, claim, split/merge, discard)."""

from __future__ import annotations

import contextlib
import shutil
import uuid
from datetime import timedelta
from typing import Any

import structlog
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from .db import utcnow
from .jobs import states as st
from .jobs.queue import cancel_for_document, enqueue, pending_count
from .models import ArtifactRow, Batch, Document, Page, Profile, User
from .profiles import ProfileSettings, validate_against_caps
from .scanner.runner import start_processing

log = structlog.get_logger(__name__)


class DomainError(Exception):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def resolve_profile(s: Session, profile_id: int | None, user_id: int | None) -> Profile:
    if profile_id is None:
        p = s.scalar(select(Profile).where(Profile.builtin_key == "document"))
    else:
        p = s.get(Profile, profile_id)
    if p is None or (p.owner_id is not None and p.owner_id != user_id):
        raise DomainError("profile_not_found", "Profil nicht gefunden", 404)
    return p


def create_scan(
    s: Session,
    ctx,  # noqa: ANN001
    *,
    user: User | None,
    profile: Profile,
    overrides: dict[str, Any] | None = None,
    origin: str = "app",
    auto_finish: bool | None = None,
    webhook_url: str | None = None,
    idempotency_key: str | None = None,
    add_qr_to_filename: bool | None = None,
    title: str | None = None,
    scan_payload: dict[str, Any] | None = None,
) -> Document:
    owner_id = user.id if user else None
    if idempotency_key and owner_id is not None:
        existing = s.scalar(
            select(Document).where(Document.owner_id == owner_id, Document.idempotency_key == idempotency_key)
        )
        if existing is not None:
            return existing
    settings = dict(profile.settings)
    if overrides:
        settings = _deep_merge(settings, overrides)
    try:
        ps = ProfileSettings.model_validate(settings)
    except Exception as e:
        raise DomainError("invalid_profile", f"Ungültige Profileinstellungen: {e}", 422) from e
    problems = validate_against_caps(ps, ctx.scanner.capabilities if ctx.scanner else None)
    if problems:
        raise DomainError("unsupported_settings", "; ".join(problems), 422)
    if pending_count(s, "scanner") >= ctx.config.max_queued_scans:
        raise DomainError("queue_full", "Zu viele wartende Scans", 429)
    free = ctx.storage.free_mb()
    min_free = int(ctx.settings.get("disk.min_free_mb") or 0)
    if free < min_free:
        raise DomainError(
            "insufficient_disk", f"Zu wenig Speicherplatz: {free} MB frei, {min_free} MB benötigt", 507
        )
    doc = Document(
        owner_id=owner_id,
        origin=origin,
        status=st.QUEUED,
        profile_id=profile.id,
        profile_name=profile.name,
        profile_snapshot=ps.model_dump(),
        title=(title or "").strip()[:300],
        add_qr_to_filename=ps.qr.add_to_filename if add_qr_to_filename is None else add_qr_to_filename,
        auto_finish=(not ps.review_before_processing) if auto_finish is None else auto_finish,
        webhook_url=webhook_url,
        idempotency_key=idempotency_key,
    )
    if origin == "walkup":
        days = int(ctx.settings.get("retention.raw_days") or 30)
        doc.inbox_expires_at = utcnow() + timedelta(days=days)
    s.add(doc)
    s.flush()
    st.add_event(s, doc, "created", f"Profil {profile.name}")
    _new_batch(s, doc, scan_payload)
    st.publish_document(doc, "document.created")
    return doc


def _new_batch(s: Session, doc: Document, extra: dict[str, Any] | None = None) -> Batch:
    seq = (s.scalar(select(func.max(Batch.seq)).where(Batch.document_id == doc.id)) or 0) + 1
    b = Batch(document_id=doc.id, seq=seq, status="queued")
    s.add(b)
    s.flush()
    enqueue(s, "scan", idempotency_key=f"scan:{b.id}", document_id=doc.id, payload={"batch_id": b.id, **(extra or {})})
    return b


def add_batch(s: Session, ctx, doc: Document) -> Batch:  # noqa: ANN001
    if doc.status not in (st.STORED, st.NEEDS_ATTENTION, st.FAILED):
        raise DomainError("invalid_state", f"Stapel hinzufügen nicht möglich im Status {doc.status}", 409)
    if doc.raw_expired:
        raise DomainError("raw_expired", "Originalseiten abgelaufen", 409)
    if pending_count(s, "scanner") >= ctx.config.max_queued_scans:
        raise DomainError("queue_full", "Zu viele wartende Scans", 429)
    st.transition(s, doc, st.QUEUED, code="scan.batch_queued")
    return _new_batch(s, doc)


def finish(s: Session, doc: Document) -> None:
    if doc.status not in (st.STORED, st.NEEDS_ATTENTION, st.DONE, st.FAILED):
        raise DomainError("invalid_state", f"Abschließen nicht möglich im Status {doc.status}", 409)
    live = s.scalar(select(func.count(Page.id)).where(Page.document_id == doc.id, Page.deleted.is_(False))) or 0
    if live == 0:
        raise DomainError("no_pages", "Dokument hat keine Seiten", 409)
    if doc.raw_expired:
        raise DomainError("raw_expired", "Originalseiten abgelaufen – erneut verarbeiten nicht möglich", 409)
    cancel_for_document(s, doc.id, ("process",))
    start_processing(s, doc)


def claim(s: Session, doc_id: int, user: User) -> Document:
    """Atomically move an inbox document to the user. Exactly one concurrent claimant wins."""
    res = s.execute(
        update(Document)
        .where(
            Document.id == doc_id,
            Document.owner_id.is_(None),
            Document.origin == "walkup",
            Document.status.not_in((st.DISCARDED, st.EXPIRED)),
        )
        .values(owner_id=user.id, claimed_at=utcnow(), inbox_expires_at=None, updated_at=utcnow())
    )
    if res.rowcount != 1:
        exists = s.scalar(select(Document.id).where(Document.id == doc_id, Document.origin == "walkup"))
        if exists is None:
            raise DomainError("not_found", "Nicht gefunden", 404)
        raise DomainError("already_claimed", "Wurde bereits von jemand anderem übernommen", 409)
    doc = s.get(Document, doc_id)
    assert doc is not None
    s.refresh(doc)
    st.add_event(s, doc, "inbox.claimed", f"Übernommen von {user.display_name or user.username}")
    from .events import bus

    bus.publish("inbox.changed", {"id": doc_id, "claimed": True})
    st.publish_document(doc)
    return doc


def discard(s: Session, ctx, doc: Document) -> None:  # noqa: ANN001
    if st.DISCARDED not in st.TRANSITIONS.get(doc.status, set()):
        raise DomainError("invalid_state", f"Verwerfen nicht möglich im Status {doc.status}", 409)
    cancel_for_document(s, doc.id)
    st.transition(s, doc, st.DISCARDED, code="discarded")
    delete_document_files(s, ctx, doc)


def delete_document_files(s: Session, ctx, doc: Document, *, raw: bool = True, processed: bool = True) -> None:  # noqa: ANN001
    if raw:
        pages = s.scalars(select(Page).where(Page.document_id == doc.id)).all()
        for p in pages:
            _delete_raw_if_unreferenced(s, ctx, p)
        doc.raw_expired = True
        d = ctx.config.raw_dir / str(doc.id)
        with contextlib.suppress(OSError):
            if d.exists() and not any(d.iterdir()):
                d.rmdir()
    if processed:
        for a in list(doc.artifacts):
            s.delete(a)
        shutil.rmtree(ctx.config.processed_dir / str(doc.id), ignore_errors=True)
        doc.processed_expired = True


def _delete_raw_if_unreferenced(s: Session, ctx, page: Page) -> None:  # noqa: ANN001
    others = s.scalar(
        select(func.count(Page.id)).where(
            Page.raw_path == page.raw_path,
            Page.id != page.id,
            Page.document_id.in_(
                select(Document.id).where(Document.raw_expired.is_(False), Document.id != page.document_id)
            ),
        )
    )
    if others:
        return
    with contextlib.suppress(OSError):
        ctx.storage.abs(page.raw_path).unlink(missing_ok=True)


def split_into_children(s: Session, parent_id: int, groups: list[list[str]], *, process: bool = True) -> list[int]:
    """Move page groups into new child documents (same owner/profile). Returns child ids."""
    parent = s.get(Document, parent_id)
    assert parent is not None
    children: list[int] = []
    for group in groups:
        child = Document(
            owner_id=parent.owner_id,
            origin=parent.origin,
            status=st.STORED,
            profile_id=parent.profile_id,
            profile_name=parent.profile_name,
            profile_snapshot=parent.profile_snapshot,
            add_qr_to_filename=parent.add_qr_to_filename,
            parent_id=parent.id,
            created_at=parent.created_at,
            webhook_url=parent.webhook_url,
            inbox_expires_at=parent.inbox_expires_at,
        )
        s.add(child)
        s.flush()
        for pos, pid in enumerate(group, start=1):
            p = s.get(Page, pid)
            if p is not None and p.document_id == parent.id:
                p.document_id = child.id
                p.position = float(pos)
        child.page_count = len(group)
        st.add_event(s, child, "created.split", f"Aus Dokument #{parent.id} abgetrennt")
        if process and parent.owner_id is not None:
            start_processing(s, child)
        st.publish_document(child, "document.created")
        children.append(child.id)
    s.flush()
    parent.page_count = s.scalar(
        select(func.count(Page.id)).where(Page.document_id == parent.id, Page.deleted.is_(False))
    ) or 0
    return children


def ensure_editable(doc: Document) -> None:
    if doc.status not in st.EDITABLE:
        raise DomainError("invalid_state", f"Bearbeiten nicht möglich im Status {doc.status}", 409)
    if doc.owner_id is None:
        raise DomainError("not_claimed", "Eingangsdokument zuerst übernehmen", 409)
    if doc.raw_expired:
        raise DomainError("raw_expired", "Originalseiten abgelaufen", 409)


def reorder_pages(s: Session, doc: Document, page_ids: list[str]) -> None:
    ensure_editable(doc)
    pages = {p.id: p for p in s.scalars(select(Page).where(Page.document_id == doc.id)).all()}
    if set(page_ids) - set(pages):
        raise DomainError("bad_pages", "Unbekannte Seiten in der Reihenfolge", 400)
    rest = [p for pid, p in sorted(pages.items(), key=lambda kv: kv[1].position) if pid not in page_ids]
    for i, pid in enumerate(page_ids + [p.id for p in rest], start=1):
        pages[pid].position = float(i)
    _mark_stale(s, doc)


def move_pages(s: Session, ctx, src: Document, target: Document, page_ids: list[str], copy: bool) -> None:  # noqa: ANN001
    """Move (or copy) pages from ``src`` to the end of ``target``. Both must belong to the same user."""
    ensure_editable(target)
    if src.owner_id != target.owner_id:
        raise DomainError("forbidden", "Nicht erlaubt", 403)
    if src.raw_expired:
        raise DomainError("raw_expired", "Originalseiten des Quelldokuments sind abgelaufen", 409)
    maxpos = s.scalar(select(func.max(Page.position)).where(Page.document_id == target.id)) or 0
    for i, pid in enumerate(page_ids, start=1):
        p = s.get(Page, pid)
        if p is None or p.document_id != src.id:
            raise DomainError("bad_pages", "Seite gehört nicht zum Quelldokument", 400)
        if copy:
            new_id = str(uuid.uuid4())
            src_path = ctx.storage.abs(p.raw_path)
            ext = src_path.suffix.lstrip(".") or "jpg"
            dst = ctx.storage.raw_page_path(target.id, new_id, ext)
            from .storage import atomic_copy

            atomic_copy(src_path, dst)
            s.add(Page(
                id=new_id, document_id=target.id, batch_id=None, position=float(maxpos) + i, side=p.side,
                raw_path=ctx.storage.rel(dst), raw_sha256=p.raw_sha256, raw_size=p.raw_size,
                content_type=p.content_type, width=p.width, height=p.height, dpi=p.dpi, edits=dict(p.edits or {}),
                analysis=dict(p.analysis or {}), is_blank=p.is_blank, keep_blank=p.keep_blank,
            ))
        else:
            ensure_editable(src)
            p.document_id = target.id
            p.position = float(maxpos) + i
    s.flush()
    for d in (src, target):
        d.page_count = s.scalar(select(func.count(Page.id)).where(Page.document_id == d.id, Page.deleted.is_(False))) or 0
    _mark_stale(s, target)
    if not copy:
        _mark_stale(s, src)


def merge_documents(s: Session, ctx, target: Document, sources: list[Document]) -> None:  # noqa: ANN001
    ensure_editable(target)
    for src in sources:
        if src.id != target.id:
            ensure_editable(src)
    for src in sources:
        if src.id == target.id:
            continue
        ids = [p.id for p in s.scalars(select(Page).where(Page.document_id == src.id).order_by(Page.position))]
        move_pages(s, ctx, src, target, ids, copy=False)
        cancel_for_document(s, src.id)
        st.add_event(s, src, "merged", f"In Dokument #{target.id} zusammengeführt")
        st.transition(s, src, st.DISCARDED, code="merged.discarded")


def _mark_stale(s: Session, doc: Document) -> None:
    """Edits after processing: outputs are stale until the user re-finishes."""
    if doc.status == st.DONE:
        st.add_event(s, doc, "edited", "Seiten geändert – erneut abschließen, um neu zu erzeugen")
    doc.updated_at = utcnow()
    st.publish_document(doc)


def artifacts_for(s: Session, doc: Document) -> list[ArtifactRow]:
    return list(
        s.scalars(
            select(ArtifactRow).where(ArtifactRow.document_id == doc.id).order_by(ArtifactRow.kind, ArtifactRow.filename)
        )
    )


def _deep_merge(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    out = dict(a)
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out
