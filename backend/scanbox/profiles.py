"""Scan profile schema, built-in defaults and validation against scanner capabilities."""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .escl.xml import ScannerCapabilities

ColorMode = Literal["color", "gray", "bw", "auto"]
Source = Literal["adf_simplex", "adf_duplex"]
PageSize = Literal["auto", "a4", "a5", "letter", "legal", "custom"]

# Region sizes in 1/300 inch.
PAGE_SIZES_300: dict[str, tuple[int, int]] = {
    "a4": (2480, 3508),
    "a5": (1748, 2480),
    "letter": (2550, 3300),
    "legal": (2550, 4200),
}

ESCL_COLOR = {"color": "RGB24", "gray": "Grayscale8", "bw": "BlackAndWhite1", "auto": "scan:AutoColorDetection"}


class Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class Zone(Model):
    """Relative rectangle (0..1) on the oriented, deskewed, uncropped page."""

    x: float = Field(0.0, ge=0, le=1)
    y: float = Field(0.0, ge=0, le=1)
    w: float = Field(1.0, gt=0, le=1)
    h: float = Field(1.0, gt=0, le=1)

    @model_validator(mode="after")
    def _clip(self) -> Zone:
        self.w = min(self.w, 1.0 - self.x)
        self.h = min(self.h, 1.0 - self.y)
        if self.w <= 0.01 or self.h <= 0.01:
            raise ValueError("zone too small")
        return self


ZONE_PRESETS: dict[str, Zone] = {
    "top_right": Zone(x=0.6, y=0.0, w=0.4, h=0.3),
    "top_left": Zone(x=0.0, y=0.0, w=0.4, h=0.3),
    "full": Zone(x=0, y=0, w=1, h=1),
}


class ScannerOptions(Model):
    """Scanner-side (eSCL) options. ``None`` = don't send, let the scanner decide."""

    blank_page_removal: bool | None = None
    blank_page_sensitivity: int | None = Field(None, ge=0, le=255)
    multipick_detection: bool | None = True
    multipick_action: Literal["ContinueJob", "StopJob"] = "StopJob"
    auto_crop: bool | None = None
    auto_exposure: bool | None = None
    carrier_sheet: bool | None = None
    brightness: int | None = None
    contrast: int | None = None
    gamma: int | None = None
    highlight: int | None = None
    shadow: int | None = None
    sharpen: int | None = None
    noise_removal: int | None = None
    threshold: int | None = None
    compression_factor: int | None = None


class SplitOptions(Model):
    mode: Literal["off", "qr_change", "qr_pattern"] = "off"
    pattern: str | None = None  # regex for separator QR values (qr_pattern)
    remove_separator: bool = True  # only for qr_pattern: drop the separator sheet


class ProcessingOptions(Model):
    auto_rotate: bool = True
    deskew: bool = True
    auto_crop: bool = False
    blank_removal: Literal["off", "scanner", "software", "both"] = "software"
    blank_threshold: float = Field(0.004, ge=0, le=0.2)  # ink coverage fraction below → blank
    color: Literal["keep", "color", "gray", "bw"] = "keep"
    bw_threshold: int | None = Field(None, ge=0, le=255)  # None → Otsu
    denoise: bool = False
    sharpen: bool = False
    brightness: float = Field(0.0, ge=-1, le=1)
    contrast: float = Field(0.0, ge=-1, le=1)
    split: SplitOptions = SplitOptions()


class QrOptions(Model):
    enabled: bool = False
    zone_preset: Literal["top_right", "top_left", "full", "custom"] = "top_right"
    zone: Zone | None = None  # used when preset == custom
    pages: Literal["first", "all", "each"] = "first"
    selection: Literal["reading_order", "largest", "regex"] = "reading_order"
    regex: str | None = None
    on_missing: Literal["continue", "warn", "block"] = "warn"
    add_to_filename: bool = True

    @field_validator("regex")
    @classmethod
    def _valid_regex(cls, v: str | None) -> str | None:
        if v:
            if len(v) > 300:
                raise ValueError("regex too long")
            re.compile(v)
        return v or None

    def effective_zone(self) -> Zone:
        if self.zone_preset == "custom" and self.zone is not None:
            return self.zone
        return ZONE_PRESETS.get(self.zone_preset, ZONE_PRESETS["full"])


class OcrOptions(Model):
    enabled: bool = True
    languages: list[Literal["deu", "eng"]] = ["deu", "eng"]
    suggest_title: bool = True

    @field_validator("languages")
    @classmethod
    def _non_empty(cls, v: list[str]) -> list[str]:
        return list(dict.fromkeys(v)) or ["deu"]


class OutputOptions(Model):
    format: Literal["pdf", "pdfa", "pdf_image", "jpeg", "png", "tiff", "txt"] = "pdfa"
    compression: Literal["none", "low", "medium", "high"] = "medium"
    jpeg_quality: int = Field(85, ge=30, le=100)
    target_size_mb: float | None = Field(None, gt=0, le=500)


class DestinationConfig(Model):
    type: Literal["download", "folder", "email", "paperless"]
    enabled: bool = True
    label: str = ""
    config: dict[str, Any] = {}


class ProfileSettings(Model):
    source: Source = "adf_duplex"
    resolution: int = 300
    color_mode: ColorMode = "color"
    bw_strategy: Literal["software", "scanner"] = "software"
    page_size: PageSize = "a4"
    custom_width_mm: float | None = Field(None, gt=10, le=216)
    custom_height_mm: float | None = Field(None, gt=10, le=3098)
    intent: Literal["Document", "Photo", "TextAndGraphic", "Preview"] | None = "Document"
    content_type: Literal["Text", "Photo", "TextAndPhoto"] | None = None
    scanner: ScannerOptions = ScannerOptions()
    processing: ProcessingOptions = ProcessingOptions()
    qr: QrOptions = QrOptions()
    ocr: OcrOptions = OcrOptions()
    output: OutputOptions = OutputOptions()
    filename_template: str = Field("{datetime}_{title}", max_length=200)
    title_template: str = Field("{profile}", max_length=200)
    destinations: list[DestinationConfig] = [DestinationConfig(type="download")]
    review_before_processing: bool = True

    @model_validator(mode="after")
    def _custom_size(self) -> ProfileSettings:
        if self.page_size == "custom" and (not self.custom_width_mm or not self.custom_height_mm):
            raise ValueError("custom page size needs width and height")
        return self

    def scan_region_300(self, caps: ScannerCapabilities | None) -> tuple[int, int]:
        ic = caps.input_caps(self.source) if caps else None
        max_w = (ic.max_width if ic and ic.max_width else 2550) if caps else 2550
        max_h = (ic.max_height if ic and ic.max_height else 4200) if caps else 4200
        if self.page_size == "custom":
            w = round(self.custom_width_mm / 25.4 * 300)  # type: ignore[operator]
            h = round(self.custom_height_mm / 25.4 * 300)  # type: ignore[operator]
        elif self.page_size == "auto":
            # Full width; legal length. The scanner's AutoCrop (if enabled) and software
            # auto-crop trim to the real paper. See HARDWARE_CHECKLIST: verify paper-end detection.
            w, h = max_w, PAGE_SIZES_300["legal"][1]
        else:
            w, h = PAGE_SIZES_300[self.page_size]
        return min(w, max_w), min(h, max_h)


def validate_against_caps(p: ProfileSettings, caps: ScannerCapabilities | None) -> list[str]:
    """Problems with a profile given the live capabilities (empty = OK)."""
    if caps is None:
        return []
    problems: list[str] = []
    ic = caps.input_caps(p.source)
    if ic is None:
        problems.append(f"source {p.source} not supported by the scanner")
        return problems
    if ic.resolutions and p.resolution not in ic.resolutions:
        problems.append(f"resolution {p.resolution} not supported (available: {ic.resolutions})")
    wanted = escl_color_mode(p)
    if ic.color_modes and wanted not in ic.color_modes:
        problems.append(f"color mode {wanted} not supported")
    if p.content_type and ic.content_types and p.content_type not in ic.content_types:
        problems.append(f"content type {p.content_type} not supported")
    if p.intent and ic.intents and p.intent not in ic.intents:
        problems.append(f"intent {p.intent} not supported")
    if "image/jpeg" not in ic.document_formats and ic.document_formats:
        problems.append("scanner does not offer JPEG pages")
    s = p.scanner
    if s.blank_page_removal and not caps.blank_page_detection:
        problems.append("scanner has no blank page detection")
    if s.multipick_detection and caps.multipick_actions and s.multipick_action not in caps.multipick_actions:
        problems.append(f"multipick action {s.multipick_action} not supported")
    if s.auto_crop and not caps.auto_crop:
        problems.append("scanner has no auto-crop")
    for name, attr in (
        ("Brightness", "brightness"),
        ("Contrast", "contrast"),
        ("Gamma", "gamma"),
        ("Highlight", "highlight"),
        ("Shadow", "shadow"),
        ("Sharpen", "sharpen"),
        ("NoiseRemoval", "noise_removal"),
        ("Threshold", "threshold"),
        ("CompressionFactor", "compression_factor"),
        ("BlankPageSensitivity", "blank_page_sensitivity"),
    ):
        v = getattr(s, attr)
        if v is None:
            continue
        r = caps.ranges.get(name)
        if r is None:
            problems.append(f"{name} not supported by the scanner")
        elif not r.min <= v <= r.max:
            problems.append(f"{name} must be between {r.min} and {r.max}")
    return problems


def escl_color_mode(p: ProfileSettings) -> str:
    if p.color_mode == "bw" and p.bw_strategy == "software":
        return "Grayscale8"
    return ESCL_COLOR[p.color_mode]


def effective_processing_color(p: ProfileSettings) -> str:
    """Colour conversion the pipeline must apply by default."""
    if p.processing.color != "keep":
        return p.processing.color
    if p.color_mode == "bw":
        return "bw"
    return "keep"


BUILTIN_PROFILES: list[dict[str, Any]] = [
    {
        "builtin_key": "document",
        "name": "Dokument (PDF/A, OCR)",
        "description": "Duplex, 300 dpi, Farbe, Leerseiten entfernen, durchsuchbares PDF/A",
        "sort_order": 10,
        "settings": ProfileSettings().model_dump(),
    },
    {
        "builtin_key": "document_qr",
        "name": "Dokument mit QR-Nummer",
        "description": "Wie Dokument, QR-Code oben rechts wird in den Dateinamen übernommen",
        "sort_order": 20,
        "settings": ProfileSettings(
            qr=QrOptions(enabled=True, zone_preset="top_right", on_missing="warn", add_to_filename=True),
            filename_template="{qr}_{date}_{title}",
        ).model_dump(),
    },
    {
        "builtin_key": "bw_fast",
        "name": "Schwarzweiß schnell",
        "description": "Duplex, 200 dpi, Schwarzweiß, klein",
        "sort_order": 30,
        "settings": ProfileSettings(
            resolution=200, color_mode="bw", output=OutputOptions(format="pdfa", compression="high")
        ).model_dump(),
    },
    {
        "builtin_key": "photo",
        "name": "Foto (JPEG)",
        "description": "Simplex, 600 dpi, Farbe, ohne OCR",
        "sort_order": 40,
        "settings": ProfileSettings(
            source="adf_simplex",
            resolution=600,
            color_mode="color",
            intent="Photo",
            content_type="Photo",
            page_size="auto",
            processing=ProcessingOptions(auto_rotate=False, deskew=False, auto_crop=True, blank_removal="off"),
            ocr=OcrOptions(enabled=False, suggest_title=False),
            output=OutputOptions(format="jpeg", compression="none", jpeg_quality=92),
        ).model_dump(),
    },
]
