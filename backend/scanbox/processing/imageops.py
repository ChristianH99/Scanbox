"""Pure image operations (OpenCV/numpy). Images are numpy arrays: HxW (gray) or HxWx3 (BGR)."""

from __future__ import annotations

import math
from dataclasses import dataclass

import cv2
import numpy as np

cv2.setNumThreads(2)


def decode(data: bytes) -> np.ndarray:
    arr = np.frombuffer(data, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_UNCHANGED)
    if img is None:
        raise ValueError("cannot decode image")
    if img.ndim == 3 and img.shape[2] == 4:
        img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
    return img


def load(path: str) -> np.ndarray:
    with open(path, "rb") as f:
        return decode(f.read())


def to_gray(img: np.ndarray) -> np.ndarray:
    return img if img.ndim == 2 else cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)


def is_effectively_gray(img: np.ndarray, tol: float = 6.0) -> bool:
    if img.ndim == 2:
        return True
    small = cv2.resize(img, (200, int(200 * img.shape[0] / max(1, img.shape[1]))), interpolation=cv2.INTER_AREA)
    b, g, r = cv2.split(small.astype(np.int16))
    diff = np.maximum(np.abs(b - g), np.maximum(np.abs(g - r), np.abs(b - r)))
    return float(np.percentile(diff, 99)) < tol


def scale_to_max(img: np.ndarray, max_side: int) -> tuple[np.ndarray, float]:
    h, w = img.shape[:2]
    s = max(h, w)
    if s <= max_side:
        return img, 1.0
    f = max_side / s
    return cv2.resize(img, (max(1, int(w * f)), max(1, int(h * f))), interpolation=cv2.INTER_AREA), f


def rotate90(img: np.ndarray, degrees_cw: int) -> np.ndarray:
    d = degrees_cw % 360
    if d == 0:
        return img
    if d == 90:
        return cv2.rotate(img, cv2.ROTATE_90_CLOCKWISE)
    if d == 180:
        return cv2.rotate(img, cv2.ROTATE_180)
    if d == 270:
        return cv2.rotate(img, cv2.ROTATE_90_COUNTERCLOCKWISE)
    raise ValueError("rotation must be a multiple of 90")


def background_value(img: np.ndarray) -> int | tuple[int, int, int]:
    """Estimate the page background (brightest common tone) for filling rotated corners."""
    if img.ndim == 2:
        return int(np.percentile(img, 95))
    return tuple(int(np.percentile(img[:, :, c], 95)) for c in range(3))  # type: ignore[return-value]


def rotate_fine(img: np.ndarray, angle_deg: float) -> np.ndarray:
    """Rotate counter-clockwise by ``angle_deg`` keeping the full image (expanded canvas)."""
    if abs(angle_deg) < 0.05:
        return img
    h, w = img.shape[:2]
    m = cv2.getRotationMatrix2D((w / 2, h / 2), angle_deg, 1.0)
    cos, sin = abs(m[0, 0]), abs(m[0, 1])
    nw, nh = int(h * sin + w * cos), int(h * cos + w * sin)
    m[0, 2] += nw / 2 - w / 2
    m[1, 2] += nh / 2 - h / 2
    fill = background_value(img)
    return cv2.warpAffine(img, m, (nw, nh), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=fill)


def _binarize_text(gray: np.ndarray) -> np.ndarray:
    blur = cv2.GaussianBlur(gray, (3, 3), 0)
    _, bw = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    return bw


def detect_skew(img: np.ndarray, max_angle: float = 15.0) -> float:
    """Return the CCW angle (degrees) that straightens text lines. Projection-profile method.

    Coarse search (0.5°) then fine (0.1°) on a downscaled binarised image. Returns 0.0 when
    there is too little content to decide.
    """
    gray = to_gray(img)
    small, _ = scale_to_max(gray, 1000)
    bw = _binarize_text(small)
    # Remove page borders/background edges that would dominate the profile.
    h, w = bw.shape
    my, mx = int(h * 0.04), int(w * 0.04)
    bw[:my, :] = 0
    bw[-my:, :] = 0
    bw[:, :mx] = 0
    bw[:, -mx:] = 0
    if np.count_nonzero(bw) < 0.002 * bw.size:
        return 0.0
    # Dilate horizontally to merge characters into line blobs.
    bw = cv2.dilate(bw, cv2.getStructuringElement(cv2.MORPH_RECT, (9, 1)))
    center = (w / 2, h / 2)

    def score(angle: float) -> float:
        m = cv2.getRotationMatrix2D(center, angle, 1.0)
        rot = cv2.warpAffine(bw, m, (w, h), flags=cv2.INTER_NEAREST)
        proj = rot.sum(axis=1, dtype=np.float64)
        return float(np.var(proj))

    best, best_s = 0.0, score(0.0)
    a = -max_angle
    while a <= max_angle + 1e-9:
        s = score(a)
        if s > best_s:
            best, best_s = a, s
        a += 0.5
    fine_best = best
    for i in range(-5, 6):
        a = best + i * 0.1
        s = score(a)
        if s > best_s:
            fine_best, best_s = a, s
    return round(fine_best, 2)


def find_paper_rect(img: np.ndarray) -> tuple[int, int, int, int] | None:
    """Find the paper against a darker scanner background. Returns (x, y, w, h) or None."""
    gray = to_gray(img)
    small, f = scale_to_max(gray, 800)
    blur = cv2.GaussianBlur(small, (5, 5), 0)
    _, th = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    th = cv2.morphologyEx(th, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (15, 15)))
    contours, _ = cv2.findContours(th, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    c = max(contours, key=cv2.contourArea)
    x, y, w, h = cv2.boundingRect(c)
    area_frac = (w * h) / float(small.shape[0] * small.shape[1])
    if area_frac < 0.25 or area_frac > 0.985:
        return None  # nothing sensible / already tight
    # Paper must be clearly brighter than the background.
    inside = float(np.mean(small[y : y + h, x : x + w]))
    mask = np.ones_like(small, dtype=bool)
    mask[y : y + h, x : x + w] = False
    outside = float(np.mean(small[mask])) if mask.any() else inside
    if inside - outside < 25:
        return None
    inv = 1.0 / f
    return int(x * inv), int(y * inv), int(math.ceil(w * inv)), int(math.ceil(h * inv))


def crop_rel(img: np.ndarray, x: float, y: float, w: float, h: float) -> np.ndarray:
    H, W = img.shape[:2]
    x0, y0 = max(0, int(round(x * W))), max(0, int(round(y * H)))
    x1, y1 = min(W, int(round((x + w) * W))), min(H, int(round((y + h) * H)))
    if x1 - x0 < 8 or y1 - y0 < 8:
        return img
    return img[y0:y1, x0:x1]


def crop_abs(img: np.ndarray, rect: tuple[int, int, int, int], margin: int = 0) -> np.ndarray:
    x, y, w, h = rect
    H, W = img.shape[:2]
    x0, y0 = max(0, x + margin), max(0, y + margin)
    x1, y1 = min(W, x + w - margin), min(H, y + h - margin)
    if x1 - x0 < 8 or y1 - y0 < 8:
        return img
    return img[y0:y1, x0:x1]


# ---------------------------------------------------------------------------- background
# Long side of the working copy used for background estimation (~75 dpi for A4).
_BG_WORK_SIDE = 900


def _paper_mask(small: np.ndarray, tol: int = 35) -> tuple[np.ndarray, np.ndarray]:
    """Pixels that look like bare paper: close to the page's paper colour in *every* channel.

    Shading, tint, show-through and faint creases are close to the paper colour and count as paper;
    ink, coloured areas and highlighter marks do not (so they are never "whitened away")."""
    gray = to_gray(small)
    bright = gray >= np.percentile(gray, 90)
    if small.ndim == 2:
        paper = np.array([np.median(small[bright])], np.float32)
        dev = np.abs(small.astype(np.float32) - paper[0])
    else:
        paper = np.median(small[bright], axis=0).astype(np.float32)
        dev = np.abs(small.astype(np.float32) - paper[None, None, :]).max(axis=2)
    mask = (dev <= tol).astype(np.uint8)
    # Isolated paper-coloured pixels inside photos/halftones are not paper.
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)))
    return mask.astype(bool), paper


def estimate_background(img: np.ndarray) -> np.ndarray | None:
    """Estimate the illumination/paper surface (same shape as ``img``, float32), or None when the
    page has too little bare paper for a reliable estimate (photos, dark paper)."""
    h, w = img.shape[:2]
    f = max(1, round(max(h, w) / _BG_WORK_SIDE))
    small = cv2.resize(img, (max(1, w // f), max(1, h // f)), interpolation=cv2.INTER_AREA) if f > 1 else img
    mask, _paper = _paper_mask(small)
    if mask.mean() < 0.35:
        return None
    m8 = mask.astype(np.uint8)
    masked = small * (m8 if small.ndim == 2 else m8[:, :, None])
    k = cv2.getStructuringElement(cv2.MORPH_RECT, (15, 15))
    # Max over nearby paper pixels: thin dark features (text, creases) never win.
    bg = cv2.dilate(masked, k)
    invalid = cv2.dilate(m8, k) == 0
    if invalid.any():
        # Large non-paper areas (photos, coloured boxes): interpolate the paper surface around them.
        bg = cv2.inpaint(bg, invalid.astype(np.uint8) * 255, 5, cv2.INPAINT_TELEA)
    bg = cv2.GaussianBlur(bg, (0, 0), 3)
    bg = cv2.resize(bg, (w, h), interpolation=cv2.INTER_LINEAR) if f > 1 else bg
    return np.maximum(bg.astype(np.float32), 1.0)


def white_point(strength: float) -> float:
    """Normalised brightness from which a pixel becomes pure white (0 → gentle, 1 → aggressive)."""
    return 0.97 - 0.12 * min(1.0, max(0.0, float(strength)))


_KNEE = 0.6  # tones below this (relative to paper) are left untouched


def _highlight_curve(n: np.ndarray, wp: float) -> np.ndarray:
    """Map [knee, wp] linearly onto [knee, 1] and everything above wp to white; darker tones keep
    their value, so text, photos and colours don't get washed out."""
    hi = _KNEE + (n - _KNEE) * ((1.0 - _KNEE) / (wp - _KNEE))
    return np.where(n > _KNEE, hi, n)


def normalize_background(img: np.ndarray, strength: float = 0.5) -> np.ndarray:
    """Make the paper pure white: divide out the paper surface (removes shading, tint, creases and
    show-through gradients), then lift near-white to white. Ink, colour and photos are kept."""
    bg = estimate_background(img)
    if bg is None:
        return img
    n = _highlight_curve(img.astype(np.float32) / bg, white_point(strength))
    return (np.clip(n, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)


def _edge_runs(dark: np.ndarray, band: int) -> np.ndarray:
    """For a band whose outer edge is column 0: mask of dark runs that start at the edge and end
    inside the band (per row). Runs that cross the whole band belong to page content."""
    region = dark[:, :band]
    first_light = np.where(region.all(axis=1), band, np.argmin(region, axis=1))
    cols = np.arange(band)[None, :]
    mask = np.zeros_like(dark, dtype=bool)
    mask[:, :band] = (cols < first_light[:, None]) & (first_light[:, None] < band)
    return mask


def clean_borders(img: np.ndarray, band: float = 0.03) -> np.ndarray:
    """Whiten dark artefacts in a thin band along each image edge (scanner backdrop strips, dark
    corners, truncated JPEG rows). Dark runs that start at the edge but reach across the band – i.e.
    content extending into the page – are left alone."""
    gray = to_gray(img)
    small, f = scale_to_max(gray, _BG_WORK_SIDE)
    h, w = small.shape
    bx, by = max(3, int(w * band)), max(3, int(h * band))
    dark = small < 200
    kill = _edge_runs(dark, bx)
    kill |= _edge_runs(dark[:, ::-1], bx)[:, ::-1]
    kill |= _edge_runs(dark.T, by).T
    kill |= _edge_runs(dark[::-1, :].T, by).T[::-1, :]
    if not kill.any():
        return img
    k8 = cv2.dilate(kill.astype(np.uint8) * 255, cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)))
    if f != 1.0:
        k8 = cv2.resize(k8, (img.shape[1], img.shape[0]), interpolation=cv2.INTER_NEAREST)
    out = img.copy()
    out[k8 > 0] = 255
    return out


def ink_coverage(img: np.ndarray) -> float:
    """Fraction of real 'ink' pixels; used for blank detection.

    Measured against the local paper tone with a contrast threshold, so shading, creases and
    show-through from the back side do not count. Specks, edge artefacts and long hairlines
    (fold lines) are ignored."""
    gray = to_gray(img)
    small, _ = scale_to_max(gray, 1000)
    bg = estimate_background(small)
    norm = small.astype(np.float32) / bg if bg is not None else small.astype(np.float32) / 255.0
    h, w = small.shape
    my, mx = int(h * 0.03), int(w * 0.03)
    core = norm[my : h - my, mx : w - mx]
    if core.size == 0:
        return 0.0
    ink = (cv2.GaussianBlur(core, (3, 3), 0) < 0.70).astype(np.uint8)
    n, _labels, stats, _ = cv2.connectedComponentsWithStats(ink, connectivity=8)
    ch, cw = core.shape
    keep = 0
    for i in range(1, n):
        x, y, bw, bh, area = (int(v) for v in stats[i])
        if area < 6:
            continue  # dust
        if x == 0 or y == 0 or x + bw == cw or y + bh == ch:
            continue  # touches the margin: backdrop / edge artefact
        long_side, short_side = max(bw, bh), min(bw, bh)
        if long_side >= 40 and (short_side <= 4 or area / long_side <= 3):
            continue  # hairline (fold line, scanner streak)
        keep += area
    return keep / float(core.size)


@dataclass
class Enhance:
    color: str = "keep"  # keep | color | gray | bw
    brightness: float = 0.0  # -1..1
    contrast: float = 0.0  # -1..1
    bw_threshold: int | None = None  # None → automatic
    denoise: bool = False
    sharpen: bool = False
    clean_background: bool = False
    clean_strength: float = 0.5  # 0..1


def bw_auto_threshold(gray: np.ndarray) -> int:
    """Otsu on a background-normalised page, clamped to a sane range (an almost empty page would
    otherwise get a threshold inside the paper noise)."""
    t, _ = cv2.threshold(cv2.GaussianBlur(gray, (3, 3), 0), 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return int(min(215, max(110, t)))


def enhance(img: np.ndarray, e: Enhance) -> np.ndarray:
    out = img
    if e.clean_background:
        out = clean_borders(normalize_background(out, e.clean_strength))
    if e.color in ("gray", "bw"):
        out = to_gray(out)
    elif e.color == "color" and out.ndim == 2:
        out = cv2.cvtColor(out, cv2.COLOR_GRAY2BGR)
    if e.brightness or e.contrast:
        alpha = 1.0 + float(e.contrast)
        beta = float(e.brightness) * 100.0
        # contrast around mid-grey
        out = cv2.convertScaleAbs(out, alpha=alpha, beta=beta + 128 * (1 - alpha))
    if e.denoise:
        out = cv2.medianBlur(out, 3)
    if e.sharpen:
        blur = cv2.GaussianBlur(out, (0, 0), 2.0)
        out = cv2.addWeighted(out, 1.6, blur, -0.6, 0)
    if e.color == "bw":
        g = to_gray(out)
        if e.bw_threshold is None:
            # Threshold relative to the local paper tone: creases and shadows don't turn black.
            if not e.clean_background:
                g = normalize_background(g, 0.0)
            _, out = cv2.threshold(g, bw_auto_threshold(g), 255, cv2.THRESH_BINARY)
        else:
            _, out = cv2.threshold(g, int(e.bw_threshold), 255, cv2.THRESH_BINARY)
    return out


def encode_jpeg(img: np.ndarray, quality: int = 85) -> bytes:
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, int(quality), cv2.IMWRITE_JPEG_OPTIMIZE, 1])
    if not ok:
        raise ValueError("JPEG encoding failed")
    return buf.tobytes()


def encode_png(img: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", img, [cv2.IMWRITE_PNG_COMPRESSION, 6])
    if not ok:
        raise ValueError("PNG encoding failed")
    return buf.tobytes()
