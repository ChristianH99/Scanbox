"""Test doubles shared by destination tests (settings accessor, job builder, PDF builder)."""

from __future__ import annotations

import hashlib
import io
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from scanbox.destinations.base import Artifact, DeliveryEnv, DeliveryJob


@dataclass
class DictSettings:
    """Dict-backed SettingsAccessor."""

    admin_values: dict[str, Any] = field(default_factory=dict)
    user_values: dict[str, Any] = field(default_factory=dict)

    def admin(self, key: str, default: Any = None) -> Any:
        return self.admin_values.get(key, default)

    def user(self, key: str, default: Any = None) -> Any:
        return self.user_values.get(key, default)


def make_artifact(path: Path, kind: str = "pdf", filename: str | None = None) -> Artifact:
    data = path.read_bytes()
    ctype = {"pdf": "application/pdf", "txt": "text/plain"}.get(kind, "application/octet-stream")
    return Artifact(
        kind=kind,
        path=path,
        filename=filename or path.name,
        content_type=ctype,
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )


def make_pdf(path: Path, pages: int = 1, noise_bytes_per_page: int = 0, text: str = "Scanbox") -> Path:
    """Build a PDF; ``noise_bytes_per_page`` adds an incompressible image stream to each page."""
    import pikepdf

    pdf = pikepdf.Pdf.new()
    for i in range(pages):
        content = f"BT /F1 24 Tf 72 720 Td ({text} page {i + 1}) Tj ET".encode()
        resources = pikepdf.Dictionary(
            Font=pikepdf.Dictionary(
                F1=pikepdf.Dictionary(
                    Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1, BaseFont=pikepdf.Name.Helvetica
                )
            )
        )
        if noise_bytes_per_page:
            side = int((noise_bytes_per_page / 3) ** 0.5) + 1
            raw = os.urandom(side * side * 3)
            img = pikepdf.Stream(pdf, raw)
            img.Type = pikepdf.Name.XObject
            img.Subtype = pikepdf.Name.Image
            img.Width = side
            img.Height = side
            img.ColorSpace = pikepdf.Name.DeviceRGB
            img.BitsPerComponent = 8
            resources.XObject = pikepdf.Dictionary(Im0=img)
            content += b" q 200 0 0 200 72 400 cm /Im0 Do Q"
        page = pikepdf.Dictionary(
            Type=pikepdf.Name.Page,
            MediaBox=[0, 0, 612, 792],
            Resources=resources,
            Contents=pdf.make_stream(content),
        )
        pdf.pages.append(pikepdf.Page(page))
    buf = io.BytesIO()
    pdf.save(buf)
    path.write_bytes(buf.getvalue())
    return path


def make_job(
    pdf: Path,
    *,
    title: str = "Invoice ACME",
    qr: str | None = None,
    attempt: int = 1,
    extra_artifacts: tuple[Artifact, ...] = (),
    **overrides: Any,
) -> DeliveryJob:
    art = make_artifact(pdf, filename=overrides.pop("filename", pdf.name))
    kw: dict[str, Any] = dict(
        delivery_id=7,
        document_id=42,
        idempotency_key="deliver:7:1",
        attempt=attempt,
        title=title,
        filename=art.filename,
        qr_value=qr,
        created_at=datetime(2026, 3, 14, 9, 26, 53),
        page_count=1,
        profile_name="Default",
        user_id=1,
        username="alice",
        user_email="alice@example.com",
        user_display_name="Alice",
        artifacts=(art, *extra_artifacts),
    )
    kw.update(overrides)
    return DeliveryJob(**kw)


def make_env(tmp_path: Path, admin: dict | None = None, user: dict | None = None, **kw: Any) -> DeliveryEnv:
    return DeliveryEnv(settings=DictSettings(admin or {}, user or {}), tmp_dir=tmp_path, **kw)
