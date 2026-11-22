"""API request/response schemas (the OpenAPI contract the frontend is generated from)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..profiles import ProfileSettings, Zone
from ..processing.render import PageEdits


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ErrorDetail(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    detail: ErrorDetail


# ------------------------------------------------------------------ auth / users
class UserOut(ORM):
    id: int
    username: str
    display_name: str
    email: str | None
    is_admin: bool
    is_local: bool
    locale: str
    theme: str
    disabled: bool = False
    last_login_at: datetime | None = None


class MeOut(BaseModel):
    user: UserOut
    csrf_token: str | None = None


class AuthConfigOut(BaseModel):
    oidc_enabled: bool
    oidc_label: str
    local_login: bool = True


class LoginIn(BaseModel):
    username: str = Field(..., min_length=1, max_length=150)
    password: str = Field(..., min_length=1, max_length=500)


class UserPrefsIn(BaseModel):
    locale: Literal["de", "en"] | None = None
    theme: Literal["system", "light", "dark"] | None = None
    display_name: str | None = Field(None, max_length=200)


class UserAdminIn(BaseModel):
    is_admin: bool | None = None
    disabled: bool | None = None


# ------------------------------------------------------------------ scanner
class ScannerStatusOut(BaseModel):
    configured: bool
    online: bool
    state: str
    escl_state: str | None = None
    adf_state: str | None = None
    code: str | None = None
    message: str | None = None
    paper_loaded: bool | None = None
    active_document_id: int | None = None
    checked_at: str | None = None
    model: str | None = None


class RangeOut(BaseModel):
    min: int
    max: int
    normal: int
    step: int = 1


class InputCapsOut(BaseModel):
    color_modes: list[str]
    content_types: list[str]
    document_formats: list[str]
    resolutions: list[int]
    intents: list[str]
    max_width: int | None = None
    max_height: int | None = None


class CapabilitiesOut(BaseModel):
    available: bool
    fetched_at: str | None = None
    make_and_model: str | None = None
    serial_number: str | None = None
    sources: list[str] = []
    adf_simplex: InputCapsOut | None = None
    adf_duplex: InputCapsOut | None = None
    feeder_capacity: int | None = None
    auto_crop: bool = False
    blank_page_detection: bool = False
    multipick_actions: list[str] = []
    carrier_sheet: bool = False
    walkup_supported: bool = False
    ranges: dict[str, RangeOut] = {}
    # App-level choices derived from the caps (what the UI should offer)
    color_modes: list[Literal["color", "gray", "bw", "auto"]] = []


class TestConnectionIn(BaseModel):
    address: str = Field(..., min_length=1, max_length=300)


class TestConnectionOut(BaseModel):
    ok: bool
    url: str | None = None
    model: str | None = None
    serial_number: str | None = None
    escl_version: str | None = None
    admin_uri: str | None = None
    sources: list[str] = []
    resolutions: list[int] = []
    status: dict[str, Any] | None = None
    error: str | None = None
    error_code: str | None = None


# ------------------------------------------------------------------ profiles
class ProfileOut(ORM):
    id: int
    name: str
    description: str
    builtin: bool
    owner_id: int | None
    settings: ProfileSettings
    problems: list[str] = []


class ProfileIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: str = Field("", max_length=300)
    settings: ProfileSettings


# ------------------------------------------------------------------ documents
DocStatus = Literal[
    "queued", "scanning", "stored", "processing", "delivering", "done", "failed", "needs_attention", "discarded",
    "expired",
]


class PageOut(ORM):
    id: str
    document_id: int
    batch_id: int | None
    position: float
    side: str
    width: int
    height: int
    dpi: int
    edits: PageEdits
    analysis: dict[str, Any]
    is_blank: bool
    keep_blank: bool
    deleted: bool


class BatchOut(ORM):
    id: int
    seq: int
    status: str
    pages_received: int
    error_code: str | None
    error_message: str | None
    started_at: datetime | None
    finished_at: datetime | None


class DeliveryOut(ORM):
    id: int
    destination_type: str
    label: str
    status: str
    attempts: int
    run: int
    last_error: str | None
    error_code: str | None
    result: dict[str, Any] | None
    created_at: datetime
    delivered_at: datetime | None


class ArtifactOut(ORM):
    id: int
    kind: str
    filename: str
    content_type: str
    size: int


class EventOut(ORM):
    id: int
    level: str
    code: str
    message: str
    created_at: datetime


class DocumentSummary(ORM):
    id: int
    owner_id: int | None
    origin: str
    status: DocStatus
    profile_id: int | None
    profile_name: str
    title: str
    filename: str | None
    qr_value: str | None
    page_count: int
    error_code: str | None
    error_message: str | None
    raw_expired: bool
    processed_expired: bool
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None
    inbox_expires_at: datetime | None
    destinations: list[str] = []


class DocumentDetail(DocumentSummary):
    profile_snapshot: ProfileSettings
    title_suggestion: str | None
    document_date: str | None
    qr_source: str | None
    qr_page_id: str | None
    add_qr_to_filename: bool
    auto_finish: bool
    pages: list[PageOut]
    batches: list[BatchOut]
    deliveries: list[DeliveryOut]
    artifacts: list[ArtifactOut]
    events: list[EventOut]
    queue_position: int | None = None
    has_ocr_text: bool = False


class DocumentList(BaseModel):
    items: list[DocumentSummary]
    total: int
    limit: int
    offset: int


class ScanIn(BaseModel):
    profile_id: int | None = None
    overrides: dict[str, Any] | None = Field(None, description="Partial ProfileSettings merged over the profile")
    title: str | None = Field(None, max_length=300)
    add_qr_to_filename: bool | None = None
    auto_finish: bool | None = Field(None, description="Process+deliver without review (default from profile)")
    webhook_url: str | None = Field(None, max_length=1000, pattern=r"^https?://")


class DocumentPatch(BaseModel):
    title: str | None = Field(None, max_length=300)
    document_date: str | None = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    qr_value: str | None = Field(None, max_length=500, description="Manual QR value; empty string = 'no QR'")
    clear_manual_qr: bool = False
    add_qr_to_filename: bool | None = None
    profile_id: int | None = Field(None, description="Switch profile (re-snapshots settings)")
    settings: ProfileSettings | None = Field(None, description="Replace this document's settings snapshot")


class ReorderIn(BaseModel):
    page_ids: list[str] = Field(..., max_length=2000)


class PagesActionIn(BaseModel):
    page_ids: list[str] = Field(..., min_length=1, max_length=2000)


class MovePagesIn(BaseModel):
    source_document_id: int
    page_ids: list[str] = Field(..., min_length=1, max_length=2000)
    copy_pages: bool = Field(True, alias="copy")
    model_config = ConfigDict(populate_by_name=True)


class SplitIn(BaseModel):
    before_page_ids: list[str] = Field(..., min_length=1, description="Start a new document at each of these pages")


class MergeIn(BaseModel):
    source_document_ids: list[int] = Field(..., min_length=1, max_length=50)


class PagePatch(BaseModel):
    edits: PageEdits | None = None
    keep_blank: bool | None = None
    deleted: bool | None = None


class QrDetectIn(BaseModel):
    zone: Zone | None = None
    page_id: str | None = None


class QrDetectOut(BaseModel):
    page_id: str
    zone: Zone
    hits: list[dict[str, Any]]
    selected: str | None


class SplitResult(BaseModel):
    children: list[int]


# ------------------------------------------------------------------ settings
class ContactIn(BaseModel):
    name: str = Field("", max_length=200)
    email: str = Field(..., max_length=320, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    is_default: bool = False


class ContactOut(ORM):
    id: int
    name: str
    email: str
    is_default: bool


class UserSettingsIn(BaseModel):
    paperless_token: str | None = Field(None, max_length=200, description="Empty string removes the token")
    email_subject_template: str | None = Field(None, max_length=300)
    email_body_template: str | None = Field(None, max_length=5000)
    default_profile_id: int | None = None


class UserSettingsOut(BaseModel):
    paperless_token_set: bool
    email_subject_template: str | None
    email_body_template: str | None
    default_profile_id: int | None
    paperless_url: str | None
    smtp_configured: bool
    output_roots: list[str]


class ApiTokenIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    expires_days: int | None = Field(None, ge=1, le=3650)


class ApiTokenOut(ORM):
    id: int
    name: str
    prefix: str
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime | None
    revoked: bool


class ApiTokenCreated(ApiTokenOut):
    token: str = Field(..., description="Shown once")


class AdminSettingValue(BaseModel):
    value: Any = None
    is_set: bool
    secret: bool
    locked: bool
    type: str


class AdminSettingsOut(BaseModel):
    settings: dict[str, AdminSettingValue]


class AdminSettingsIn(BaseModel):
    values: dict[str, Any] = Field(..., description="key → value; null clears; secrets: null keeps unchanged unless clear=true")
    clear_secrets: list[str] = []


class UsageOut(BaseModel):
    raw_bytes: int
    processed_bytes: int
    cache_bytes: int
    db_bytes: int
    logs_bytes: int
    free_bytes: int
    total_bytes: int
    min_free_mb: int


class ClearCacheOut(BaseModel):
    raw_expired: int
    processed_expired: int
    inbox_expired: int
    render_cache_bytes: int


class TestResultOut(BaseModel):
    ok: bool
    message: str


class TestMailIn(BaseModel):
    to: str = Field(..., pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class DiagnosticsOut(BaseModel):
    version: str
    scanner: ScannerStatusOut
    tools: dict[str, bool]
    queue: dict[str, dict[str, int]]
    workers_alive: bool
    usage: UsageOut
    recent_errors: list[dict[str, Any]]
    walkup: dict[str, Any] | None = None
