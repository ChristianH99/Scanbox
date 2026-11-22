"""QR code detection within a search zone (docs/ARCHITECTURE.md §8).

The zone is relative (0..1) to the page *after* orientation correction and deskew,
*before* cropping. Hits are reported with page-relative boxes so the UI can overlay them.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

import cv2
import numpy as np
import zxingcpp

from ..profiles import QrOptions, Zone
from .imageops import to_gray

_FORMATS = zxingcpp.BarcodeFormat.QRCode | zxingcpp.BarcodeFormat.MicroQRCode


@dataclass
class QrHit:
    value: str
    x: float  # page-relative bounding box
    y: float
    w: float
    h: float
    page_id: str | None = None
    page_index: int | None = None

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2

    @property
    def area(self) -> float:
        return self.w * self.h

    def to_dict(self) -> dict:
        return asdict(self)


def _read(img: np.ndarray) -> list[zxingcpp.Result]:
    try:
        return list(
            zxingcpp.read_barcodes(img, formats=_FORMATS, try_rotate=True, try_downscale=True, try_invert=False)
        )
    except TypeError:  # older zxing-cpp without some kwargs
        return list(zxingcpp.read_barcodes(img, formats=_FORMATS))


def _variants(gray: np.ndarray):
    """Yield (image, scale) variants for robustness: plain, Otsu, CLAHE, 2x upscale."""
    yield gray, 1.0
    blur = cv2.GaussianBlur(gray, (3, 3), 0)
    _, otsu = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    yield otsu, 1.0
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8)).apply(gray)
    yield clahe, 1.0
    if max(gray.shape) < 2500:
        up = cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
        yield up, 2.0
    # Light morphological closing helps with broken modules from noisy scans.
    closed = cv2.morphologyEx(otsu, cv2.MORPH_CLOSE, np.ones((2, 2), np.uint8))
    yield closed, 1.0


def detect_qr(img: np.ndarray, zone: Zone) -> list[QrHit]:
    """Detect QR codes whose centre lies inside ``zone``. Returns page-relative hits."""
    gray = to_gray(img)
    H, W = gray.shape
    # Search a slightly enlarged window so codes on the zone edge are fully visible,
    # then keep those whose centre is inside the zone.
    pad = 0.03
    x0 = max(0, int((zone.x - pad) * W))
    y0 = max(0, int((zone.y - pad) * H))
    x1 = min(W, int((zone.x + zone.w + pad) * W))
    y1 = min(H, int((zone.y + zone.h + pad) * H))
    window = gray[y0:y1, x0:x1]
    if window.size == 0:
        return []
    hits: dict[tuple[str, int, int], QrHit] = {}
    for variant, scale in _variants(window):
        for r in _read(variant):
            if not r.valid or not r.text:
                continue
            pos = r.position
            xs = [pos.top_left.x, pos.top_right.x, pos.bottom_left.x, pos.bottom_right.x]
            ys = [pos.top_left.y, pos.top_right.y, pos.bottom_left.y, pos.bottom_right.y]
            bx0, bx1 = min(xs) / scale + x0, max(xs) / scale + x0
            by0, by1 = min(ys) / scale + y0, max(ys) / scale + y0
            hit = QrHit(r.text, bx0 / W, by0 / H, (bx1 - bx0) / W, (by1 - by0) / H)
            if not (zone.x <= hit.cx <= zone.x + zone.w and zone.y <= hit.cy <= zone.y + zone.h):
                continue
            key = (hit.value, int(hit.cx * 50), int(hit.cy * 50))
            hits.setdefault(key, hit)
        if hits:
            break  # first variant that finds anything wins (stable, fast)
    return list(hits.values())


def reading_order(hits: list[QrHit], row_tol: float = 0.02) -> list[QrHit]:
    """Top-to-bottom, then left-to-right; codes within ``row_tol`` vertically share a row."""
    ordered = sorted(hits, key=lambda h: h.cy)
    rows: list[list[QrHit]] = []
    for h in ordered:
        if rows and abs(rows[-1][0].cy - h.cy) <= row_tol:
            rows[-1].append(h)
        else:
            rows.append([h])
    out: list[QrHit] = []
    for row in rows:
        out.extend(sorted(row, key=lambda h: h.cx))
    return out


def select_hit(hits: list[QrHit], opts: QrOptions) -> QrHit | None:
    """Apply the regex filter and the deterministic selection rule."""
    if not hits:
        return None
    rx = re.compile(opts.regex) if opts.regex else None
    candidates = reading_order(hits)
    if rx is not None:
        candidates = [h for h in candidates if rx.search(h.value)]
        if not candidates:
            return None
    if opts.selection == "largest":
        return max(candidates, key=lambda h: (round(h.area, 6), -candidates.index(h)))
    # reading_order and regex both pick the first remaining candidate in reading order
    return candidates[0]
