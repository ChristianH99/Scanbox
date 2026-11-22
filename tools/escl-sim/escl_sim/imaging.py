"""Turning a sheet into what the scanner delivers: placement, skew, colour mode, encoding."""

from __future__ import annotations

import io
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageEnhance

from . import pages
from .caps import U_PER_MM
from .pages import PageSpec

# Where the leading edge of the sheet appears in the scan (feed timing), in mm.
FEED_OFFSET_MM = 2.0


@dataclass(frozen=True)
class OutputParams:
    dpi: int
    region: tuple[int, int, int, int]  # width, height, x_offset, y_offset in 300ths
    bed_width: int  # device MaxWidth in 300ths (paper is centred: Justification Center)
    background: tuple[int, int, int]
    color_mode: str  # RGB24 | Grayscale8 | BlackAndWhite1 | scan:AutoColorDetection
    document_format: str  # image/jpeg | application/pdf
    quality: int
    threshold: int
    brightness: int | None
    contrast: int | None
    auto_crop: bool
    samples_dir: Path


@dataclass(frozen=True)
class Encoded:
    data: bytes
    content_type: str
    width: int
    height: int
    mode: str  # PIL mode of the encoded image
    color_mode: str  # effective eSCL colour mode

    @property
    def bytes_per_line(self) -> int:
        if self.mode == "1":
            return (self.width + 7) // 8
        return self.width * (3 if self.mode == "RGB" else 1)


def region_px(value_300: int, dpi: int) -> int:
    return max(1, round(value_300 * dpi / 300))


@lru_cache(maxsize=8)
def compose_scan(spec: PageSpec, p: OutputParams) -> tuple[Image.Image, tuple[int, int, int, int]]:
    """Place the (skewed) sheet on the scanner background inside the scan region.

    Returns the RGB scan image and the bounding box of the paper within it.
    Coordinates: the ADF bed is ``bed_width`` wide; the sheet is centred horizontally
    (Justification Center) and its leading edge is ``FEED_OFFSET_MM`` below the top.
    The requested region is also centred, shifted by its XOffset/YOffset.
    """
    dpi = p.dpi
    rw, rh, rx, ry = p.region
    out_w, out_h = region_px(rw, dpi), region_px(rh, dpi)
    canvas = Image.new("RGB", (out_w, out_h), p.background)
    paper = pages.render_paper(spec.side(), dpi, p.samples_dir)
    pw300 = round(pages.PAPER_MM[spec.paper][0] * U_PER_MM)
    paper_x300 = (p.bed_width - pw300) / 2
    region_x300 = (p.bed_width - rw) / 2 + rx
    px = round((paper_x300 - region_x300) * dpi / 300)
    py = round((FEED_OFFSET_MM * U_PER_MM - ry) * dpi / 300)
    if spec.skew_deg:
        rgba = paper.convert("RGBA")
        rotated = rgba.rotate(-spec.skew_deg, resample=Image.BICUBIC, expand=True)
        cx, cy = px + paper.width / 2, py + paper.height / 2
        ox, oy = round(cx - rotated.width / 2), round(cy - rotated.height / 2)
        canvas.paste(rotated, (ox, oy), rotated)
        bbox = (ox, oy, ox + rotated.width, oy + rotated.height)
    else:
        canvas.paste(paper, (px, py))
        bbox = (px, py, px + paper.width, py + paper.height)
    clipped = (max(0, bbox[0]), max(0, bbox[1]), min(out_w, bbox[2]), min(out_h, bbox[3]))
    return canvas, clipped


def effective_color_mode(spec: PageSpec, color_mode: str, samples_dir: Path) -> str:
    if color_mode == "scan:AutoColorDetection":
        return "RGB24" if pages.has_color(spec.side(), samples_dir) else "Grayscale8"
    return color_mode


@lru_cache(maxsize=256)
def encode_page(spec: PageSpec, p: OutputParams) -> Encoded:
    img, bbox = compose_scan(spec.side(), p)
    if p.auto_crop and bbox[2] > bbox[0] and bbox[3] > bbox[1]:
        img = img.crop(bbox)
    if p.brightness is not None and p.brightness != 996:
        img = ImageEnhance.Brightness(img).enhance(max(0.0, p.brightness / 996))
    if p.contrast is not None and p.contrast != 996:
        img = ImageEnhance.Contrast(img).enhance(max(0.0, p.contrast / 996))

    mode = effective_color_mode(spec, p.color_mode, p.samples_dir)
    if mode == "RGB24":
        out = img
    elif mode == "Grayscale8":
        out = img.convert("L")
    elif mode == "BlackAndWhite1":
        thr = p.threshold
        out = img.convert("L").point(lambda v: 255 if v >= thr else 0)
        if p.document_format == "application/pdf":
            out = out.convert("1", dither=Image.Dither.NONE)
    else:  # pragma: no cover - validated earlier
        raise ValueError(mode)

    buf = io.BytesIO()
    if p.document_format == "application/pdf":
        out.save(buf, "PDF", resolution=float(p.dpi))
        ctype = "application/pdf"
    else:
        out.save(buf, "JPEG", quality=p.quality, dpi=(p.dpi, p.dpi), optimize=False)
        ctype = "image/jpeg"
    return Encoded(buf.getvalue(), ctype, out.width, out.height, out.mode, mode)


def jpeg_quality(compression_factor: int | None) -> int:
    """Map eSCL CompressionFactor (0..11, normal 4; higher = smaller files) to JPEG quality."""
    cf = 4 if compression_factor is None else compression_factor
    return max(30, min(97, 95 - cf * 5))
