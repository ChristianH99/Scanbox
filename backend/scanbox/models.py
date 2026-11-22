"""ORM models. See docs/ARCHITECTURE.md §3."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .db import utcnow


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON}


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    sub: Mapped[str | None] = mapped_column(String(255), unique=True)
    username: Mapped[str] = mapped_column(String(150), unique=True)
    display_name: Mapped[str] = mapped_column(String(200), default="")
    email: Mapped[str | None] = mapped_column(String(320))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    is_local: Mapped[bool] = mapped_column(Boolean, default=False)
    password_hash: Mapped[str | None] = mapped_column(String(255))
    locale: Mapped[str] = mapped_column(String(8), default="de")
    theme: Mapped[str] = mapped_column(String(10), default="system")
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime)


class SessionRow(Base):
    __tablename__ = "sessions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # sha256 hex of cookie value
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    csrf_token: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(300))
    auth_method: Mapped[str] = mapped_column(String(16), default="local")

    user: Mapped[User] = relationship()


class ApiToken(Base):
    __tablename__ = "api_tokens"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    prefix: Mapped[str] = mapped_column(String(16), unique=True)
    token_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)

    user: Mapped[User] = relationship()


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON)
    encrypted: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class UserSetting(Base):
    __tablename__ = "user_settings"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON)
    encrypted: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Contact(Base):
    __tablename__ = "contacts"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    email: Mapped[str] = mapped_column(String(320))
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (UniqueConstraint("user_id", "email"),)


class Profile(Base):
    __tablename__ = "profiles"
    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(String(300), default="")
    settings: Mapped[dict[str, Any]] = mapped_column(JSON)
    builtin_key: Mapped[str | None] = mapped_column(String(50), unique=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=100)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Document(Base):
    __tablename__ = "documents"
    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    origin: Mapped[str] = mapped_column(String(10), default="app")  # app | walkup | api
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    profile_id: Mapped[int | None] = mapped_column(ForeignKey("profiles.id", ondelete="SET NULL"))
    profile_name: Mapped[str] = mapped_column(String(100), default="")
    profile_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON)
    title: Mapped[str] = mapped_column(String(300), default="")
    title_suggestion: Mapped[str | None] = mapped_column(String(300))
    document_date: Mapped[str | None] = mapped_column(String(10))  # YYYY-MM-DD (from OCR or user)
    filename: Mapped[str | None] = mapped_column(String(255))
    qr_value: Mapped[str | None] = mapped_column(String(500))
    qr_source: Mapped[str | None] = mapped_column(String(10))  # auto | manual | none
    qr_page_id: Mapped[str | None] = mapped_column(String(36))
    add_qr_to_filename: Mapped[bool] = mapped_column(Boolean, default=False)
    page_count: Mapped[int] = mapped_column(Integer, default=0)
    ocr_text: Mapped[str | None] = mapped_column(Text)
    processing_run: Mapped[int] = mapped_column(Integer, default=0)
    counter: Mapped[int | None] = mapped_column(Integer)
    raw_expired: Mapped[bool] = mapped_column(Boolean, default=False)
    processed_expired: Mapped[bool] = mapped_column(Boolean, default=False)
    inbox_expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime)
    webhook_url: Mapped[str | None] = mapped_column(String(1000))
    auto_finish: Mapped[bool] = mapped_column(Boolean, default=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(200))
    error_code: Mapped[str | None] = mapped_column(String(50))
    error_message: Mapped[str | None] = mapped_column(Text)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)

    pages: Mapped[list[Page]] = relationship(
        back_populates="document", order_by="Page.position", cascade="all, delete-orphan"
    )
    batches: Mapped[list[Batch]] = relationship(
        back_populates="document", order_by="Batch.seq", cascade="all, delete-orphan"
    )
    deliveries: Mapped[list[Delivery]] = relationship(back_populates="document", cascade="all, delete-orphan")
    artifacts: Mapped[list[ArtifactRow]] = relationship(back_populates="document", cascade="all, delete-orphan")
    owner: Mapped[User | None] = relationship()

    __table_args__ = (
        Index("ix_documents_owner_created", "owner_id", "created_at"),
        UniqueConstraint("owner_id", "idempotency_key", name="uq_documents_idempotency"),
    )


class Batch(Base):
    __tablename__ = "batches"
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    seq: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(20), default="queued")  # queued|scanning|done|failed|interrupted|cancelled
    escl_job_uri: Mapped[str | None] = mapped_column(String(500))
    settings: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    pages_received: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str | None] = mapped_column(String(50))
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    document: Mapped[Document] = relationship(back_populates="batches")


class Page(Base):
    __tablename__ = "pages"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)  # uuid4
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    batch_id: Mapped[int | None] = mapped_column(ForeignKey("batches.id", ondelete="SET NULL"))
    position: Mapped[float] = mapped_column(Float)
    side: Mapped[str] = mapped_column(String(6), default="front")
    raw_path: Mapped[str] = mapped_column(String(500))  # relative to data dir
    raw_sha256: Mapped[str] = mapped_column(String(64))
    raw_size: Mapped[int] = mapped_column(Integer, default=0)
    content_type: Mapped[str] = mapped_column(String(50), default="image/jpeg")
    width: Mapped[int] = mapped_column(Integer, default=0)
    height: Mapped[int] = mapped_column(Integer, default=0)
    dpi: Mapped[int] = mapped_column(Integer, default=300)
    edits: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    analysis: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    is_blank: Mapped[bool] = mapped_column(Boolean, default=False)
    keep_blank: Mapped[bool] = mapped_column(Boolean, default=False)  # user override
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    document: Mapped[Document] = relationship(back_populates="pages")


class Task(Base):
    __tablename__ = "tasks"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(20))  # scan | process | deliver | webhook
    lane: Mapped[str] = mapped_column(String(20), index=True)  # scanner | process | deliver
    document_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    delivery_id: Mapped[int | None] = mapped_column(ForeignKey("deliveries.id", ondelete="CASCADE"))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(12), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=1)
    next_run_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_error: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Delivery(Base):
    __tablename__ = "deliveries"
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    destination_type: Mapped[str] = mapped_column(String(20))
    label: Mapped[str] = mapped_column(String(200), default="")
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending|running|done|failed|needs_attention|cancelled
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    run: Mapped[int] = mapped_column(Integer, default=1)
    last_error: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(String(50))
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime)

    document: Mapped[Document] = relationship(back_populates="deliveries")


class ArtifactRow(Base):
    __tablename__ = "artifacts"
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    run: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(20))  # pdf | pdf_image | txt | image | zip
    path: Mapped[str] = mapped_column(String(500))  # relative to data dir
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(100))
    size: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    document: Mapped[Document] = relationship(back_populates="artifacts")


class JobEvent(Base):
    __tablename__ = "job_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    level: Mapped[str] = mapped_column(String(10), default="info")  # info | warning | error
    code: Mapped[str] = mapped_column(String(50))
    message: Mapped[str] = mapped_column(Text, default="")
    data: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
