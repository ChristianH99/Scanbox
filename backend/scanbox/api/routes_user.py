"""Per-user settings: email templates, Paperless token, contacts, API tokens."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..context import AppContext
from ..db import get_session
from ..models import ApiToken, Contact, User
from ..security.auth import Principal, create_api_token
from .deps import CtxDep, PrincipalDep
from .schemas import (
    ApiTokenCreated,
    ApiTokenIn,
    ApiTokenOut,
    ContactIn,
    ContactOut,
    UserSettingsIn,
    UserSettingsOut,
)

router = APIRouter(prefix="/api/me", tags=["me"])


def _settings_out(s: Session, ctx: AppContext, uid: int) -> UserSettingsOut:
    st = ctx.settings
    return UserSettingsOut(
        paperless_token_set=bool(st.user_get(uid, "paperless.token", session=s)),
        email_subject_template=st.user_get(uid, "email.subject_template", session=s),
        email_body_template=st.user_get(uid, "email.body_template", session=s),
        default_profile_id=st.user_get(uid, "ui.default_profile_id", session=s),
        paperless_url=st.get("paperless.url"),
        smtp_configured=bool(st.get("smtp.host")),
        output_roots=sorted((st.get("output.roots") or {}).keys()),
    )


@router.get("/settings", response_model=UserSettingsOut)
def get_settings(s: Session = Depends(get_session), ctx: AppContext = CtxDep, p: Principal = PrincipalDep) -> UserSettingsOut:
    return _settings_out(s, ctx, p.id)


@router.patch("/settings", response_model=UserSettingsOut)
def patch_settings(body: UserSettingsIn, s: Session = Depends(get_session), ctx: AppContext = CtxDep,
                   p: Principal = PrincipalDep) -> UserSettingsOut:
    st = ctx.settings
    if body.paperless_token is not None:
        st.user_set(p.id, "paperless.token", body.paperless_token.strip() or None, session=s)
    if body.email_subject_template is not None:
        st.user_set(p.id, "email.subject_template", body.email_subject_template or None, session=s)
    if body.email_body_template is not None:
        st.user_set(p.id, "email.body_template", body.email_body_template or None, session=s)
    if body.default_profile_id is not None:
        st.user_set(p.id, "ui.default_profile_id", body.default_profile_id or None, session=s)
    s.flush()
    return _settings_out(s, ctx, p.id)


# ------------------------------------------------------------------ contacts
def _sync_default_recipients(s: Session, ctx: AppContext, uid: int) -> None:
    defaults = [c.email for c in s.scalars(select(Contact).where(Contact.user_id == uid, Contact.is_default))]
    ctx.settings.user_set(uid, "email.default_recipients", defaults, session=s)


@router.get("/contacts", response_model=list[ContactOut])
def list_contacts(s: Session = Depends(get_session), p: Principal = PrincipalDep) -> list[Contact]:
    return list(s.scalars(select(Contact).where(Contact.user_id == p.id).order_by(Contact.name, Contact.email)))


@router.post("/contacts", response_model=ContactOut, status_code=201)
def add_contact(body: ContactIn, s: Session = Depends(get_session), ctx: AppContext = CtxDep,
                p: Principal = PrincipalDep) -> Contact:
    email = body.email.strip().lower()
    if s.scalar(select(Contact).where(Contact.user_id == p.id, Contact.email == email)):
        raise HTTPException(409, detail={"code": "exists", "message": "Kontakt existiert bereits"})
    c = Contact(user_id=p.id, name=body.name.strip(), email=email, is_default=body.is_default)
    s.add(c)
    s.flush()
    _sync_default_recipients(s, ctx, p.id)
    return c


@router.put("/contacts/{contact_id}", response_model=ContactOut)
def update_contact(contact_id: int, body: ContactIn, s: Session = Depends(get_session), ctx: AppContext = CtxDep,
                   p: Principal = PrincipalDep) -> Contact:
    c = s.get(Contact, contact_id)
    if c is None or c.user_id != p.id:
        raise HTTPException(404, detail={"code": "not_found", "message": "Kontakt nicht gefunden"})
    email = body.email.strip().lower()
    if s.scalar(select(Contact.id).where(Contact.user_id == p.id, Contact.email == email, Contact.id != c.id)):
        raise HTTPException(409, detail={"code": "exists", "message": "Kontakt existiert bereits"})
    c.name, c.email, c.is_default = body.name.strip(), email, body.is_default
    s.flush()
    _sync_default_recipients(s, ctx, p.id)
    return c


@router.delete("/contacts/{contact_id}", status_code=204)
def delete_contact(contact_id: int, s: Session = Depends(get_session), ctx: AppContext = CtxDep,
                   p: Principal = PrincipalDep) -> None:
    c = s.get(Contact, contact_id)
    if c is None or c.user_id != p.id:
        raise HTTPException(404, detail={"code": "not_found", "message": "Kontakt nicht gefunden"})
    s.delete(c)
    s.flush()
    _sync_default_recipients(s, ctx, p.id)


# ------------------------------------------------------------------ API tokens
@router.get("/tokens", response_model=list[ApiTokenOut])
def list_tokens(s: Session = Depends(get_session), p: Principal = PrincipalDep) -> list[ApiToken]:
    return list(s.scalars(select(ApiToken).where(ApiToken.user_id == p.id).order_by(ApiToken.id.desc())))


@router.post("/tokens", response_model=ApiTokenCreated, status_code=201)
def new_token(body: ApiTokenIn, s: Session = Depends(get_session), p: Principal = PrincipalDep) -> ApiTokenCreated:
    if p.via == "token":
        raise HTTPException(403, detail={"code": "forbidden", "message": "Tokens können nur im Browser erstellt werden"})
    user = s.get(User, p.id)
    assert user
    row, token = create_api_token(s, user, body.name, body.expires_days)
    return ApiTokenCreated(**ApiTokenOut.model_validate(row).model_dump(), token=token)


@router.delete("/tokens/{token_id}", status_code=204)
def revoke_token(token_id: int, s: Session = Depends(get_session), p: Principal = PrincipalDep) -> None:
    row = s.get(ApiToken, token_id)
    if row is None or row.user_id != p.id:
        raise HTTPException(404, detail={"code": "not_found", "message": "Token nicht gefunden"})
    row.revoked = True
