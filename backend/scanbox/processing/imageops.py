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


def ink_coverage(img: np.ndarray) -> float:
    """Fraction of 'ink' pixels after removing specks and borders. Used for blank detection."""
    gray = to_gray(img)
    small, _ = scale_to_max(gray, 1000)
    h, w = small.shape
    my, mx = int(h * 0.05), int(w * 0.05)
    core = small[my : h - my, mx : w - mx]
    if core.size == 0:
        return 0.0
    th = cv2.adaptiveThreshold(
        cv2.GaussianBlur(core, (3, 3), 0), 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 25, 15
    )
    th = cv2.morphologyEx(th, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2, 2)))
    # Remove tiny connected components (dust).
    n, _labels, stats, _ = cv2.connectedComponentsWithStats(th, connectivity=8)
    keep = sum(int(stats[i, cv2.CC_STAT_AREA]) for i in range(1, n) if stats[i, cv2.CC_STAT_AREA] >= 6)
    return keep / float(core.size)


@dataclass
class Enhance:
    color: str = "keep"  # keep | color | gray | bw
    brightness: float = 0.0  # -1..1
    contrast: float = 0.0  # -1..1
    bw_threshold: int | None = None  # None → Otsu
    denoise: bool = False
    sharpen: bool = False


def enhance(img: np.ndarray, e: Enhance) -> np.ndarray:
    out = img
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
            g = cv2.GaussianBlur(g, (3, 3), 0)
            _, out = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
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
