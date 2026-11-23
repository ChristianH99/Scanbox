"""Non-destructive page rendering: raw page + edit list + profile → processed image.

Order (docs/ARCHITECTURE.md §7): orientation → fine rotation/deskew → [QR detection] →
crop → colour/enhancement → blank detection. Used for previews and the real pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from ..profiles import (
    ZONE_PRESETS,
    ProfileSettings,
    Zone,
    effective_clean_background,
    effective_processing_color,
)
from . import imageops as ops
from .ocr import detect_orientation
from .qr import QrHit, detect_qr


class PageEdits(BaseModel):
    """User edits for one page. ``None`` means 'automatic / profile default'."""

    model_config = ConfigDict(extra="ignore")

    rotate: Literal[0, 90, 180, 270] | None = None  # clockwise, absolute orientation override
    angle: float | None = Field(None, ge=-45, le=45)  # CCW fine rotation override (None → auto deskew)
    crop: Zone | None = None  # relative to oriented+deskewed page
    auto_crop: bool | None = None
    color: Literal["keep", "color", "gray", "bw"] | None = None
    brightness: float | None = Field(None, ge=-1, le=1)
    contrast: float | None = Field(None, ge=-1, le=1)
    bw_threshold: int | None = Field(None, ge=0, le=255)
    denoise: bool | None = None
    sharpen: bool | None = None
    clean_background: bool | None = None


# Bump when the rendering algorithm changes, so cached previews are re-rendered.
RENDER_VERSION = 2


@dataclass
class RenderResult:
    image: np.ndarray
    rotate: int
    angle: float
    qr_hits: list[QrHit] = field(default_factory=list)
    ink: float | None = None
    analysis_updates: dict[str, Any] = field(default_factory=dict)


def orient_and_straighten(
    img: np.ndarray,
    edits: PageEdits,
    profile: ProfileSettings,
    analysis: dict[str, Any],
    *,
    allow_osd: bool = True,
) -> tuple[np.ndarray, int, float, dict[str, Any]]:
    """Steps 2–3. Returns (image, rotate, angle, analysis_updates). Uses/updates cached analysis."""
    updates: dict[str, Any] = {}
    # 2. orientation
    if edits.rotate is not None:
        rotate = int(edits.rotate)
    elif profile.processing.auto_rotate:
        if "osd_rotate" in analysis:
            rotate = int(analysis["osd_rotate"])
        elif allow_osd:
            rot, conf = detect_orientation(img)
            rotate = rot
            updates["osd_rotate"] = rot
            updates["osd_confidence"] = conf
        else:
            rotate = 0
    else:
        rotate = 0
    out = ops.rotate90(img, rotate)
    # 3. fine rotation / deskew (analysis key depends on orientation)
    if edits.angle is not None:
        angle = float(edits.angle)
    elif profile.processing.deskew:
        key = f"skew_{rotate}"
        if key in analysis:
            angle = float(analysis[key])
        else:
            angle = ops.detect_skew(out)
            updates[key] = angle
    else:
        angle = 0.0
    out = ops.rotate_fine(out, angle)
    return out, rotate, angle, updates


def finish_page(
    img: np.ndarray, edits: PageEdits, profile: ProfileSettings
) -> np.ndarray:
    """Steps 5–6: crop + colour/enhancement."""
    out = img
    if edits.crop is not None:
        c = edits.crop
        out = ops.crop_rel(out, c.x, c.y, c.w, c.h)
    elif edits.auto_crop if edits.auto_crop is not None else profile.processing.auto_crop:
        rect = ops.find_paper_rect(out)
        if rect is not None:
            out = ops.crop_abs(out, rect, margin=2)
    p = profile.processing
    e = ops.Enhance(
        color=edits.color or effective_processing_color(profile),
        brightness=edits.brightness if edits.brightness is not None else p.brightness,
        contrast=edits.contrast if edits.contrast is not None else p.contrast,
        bw_threshold=edits.bw_threshold if edits.bw_threshold is not None else p.bw_threshold,
        denoise=edits.denoise if edits.denoise is not None else p.denoise,
        sharpen=edits.sharpen if edits.sharpen is not None else p.sharpen,
        clean_background=(
            edits.clean_background if edits.clean_background is not None else effective_clean_background(profile)
        ),
        clean_strength=p.clean_strength,
    )
    return ops.enhance(out, e)


def render_page(
    raw: np.ndarray,
    edits: PageEdits,
    profile: ProfileSettings,
    analysis: dict[str, Any],
    *,
    detect_qr_codes: bool = False,
    measure_blank: bool = False,
    allow_osd: bool = True,
) -> RenderResult:
    oriented, rotate, angle, updates = orient_and_straighten(raw, edits, profile, analysis, allow_osd=allow_osd)
    hits: list[QrHit] = []
    if detect_qr_codes:
        zone = profile.qr.effective_zone() if profile.qr.enabled else ZONE_PRESETS["full"]
        hits = detect_qr(oriented, zone)
    out = finish_page(oriented, edits, profile)
    ink = ops.ink_coverage(out) if measure_blank else None
    return RenderResult(out, rotate, angle, hits, ink, updates)


def measure_ink_fast(raw: np.ndarray, profile: ProfileSettings) -> float:
    """Blank check right after a page arrives: crop + colour/cleanup on a reduced copy, no
    orientation/deskew (they don't change the ink fraction). Same measure as the pipeline."""
    small, _f = ops.scale_to_max(raw, 1400)
    return ops.ink_coverage(finish_page(small, PageEdits(), profile))
