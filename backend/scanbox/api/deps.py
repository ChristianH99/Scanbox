"""Shared API dependencies. Document visibility rules live here – and only here."""

from __future__ import annotations

from fastapi import Depends, HTTPException
from sqlalchemy import Select, and_, or_, select
from sqlalchemy.orm import Session

from ..context import AppContext, get_context
from ..db import get_session
from ..documents import DomainError
from ..jobs import states as st
from ..models import Document, Page
from ..security.auth import Principal, current_principal


def ctx_dep() -> AppContext:
    return get_context()


def visible_condition(user_id: int):  # noqa: ANN201
    """Own documents, or unclaimed walk-up inbox items (visible to every signed-in user)."""
    return or_(
        Document.owner_id == user_id,
        and_(
            Document.owner_id.is_(None),
            Document.origin == "walkup",
            Document.status.not_in((st.DISCARDED, st.EXPIRED)),
        ),
    )


def visible_documents(user_id: int) -> Select:
    return select(Document).where(visible_condition(user_id))


def get_visible_document(s: Session, doc_id: int, p: Principal) -> Document:
    doc = s.scalar(visible_documents(p.id).where(Document.id == doc_id))
    if doc is None:
        # Same answer for "doesn't exist" and "not yours" – no information leak.
        raise HTTPException(404, detail={"code": "not_found", "message": "Dokument nicht gefunden"})
    return doc


def get_owned_document(s: Session, doc_id: int, p: Principal) -> Document:
    """For mutations: the document must belong to the caller (inbox items must be claimed first)."""
    doc = get_visible_document(s, doc_id, p)
    if doc.owner_id != p.id:
        raise HTTPException(409, detail={"code": "not_claimed", "message": "Eingangsdokument zuerst übernehmen"})
    return doc


def get_visible_page(s: Session, page_id: str, p: Principal) -> tuple[Page, Document]:
    page = s.get(Page, page_id)
    if page is None:
        raise HTTPException(404, detail={"code": "not_found", "message": "Seite nicht gefunden"})
    doc = get_visible_document(s, page.document_id, p)
    return page, doc


def domain_http(e: DomainError) -> HTTPException:
    return HTTPException(e.status, detail={"code": e.code, "message": e.message})


SessionDep = Depends(get_session)
PrincipalDep = Depends(current_principal)
CtxDep = Depends(ctx_dep)
