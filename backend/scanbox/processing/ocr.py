"""External tools: Tesseract OSD, OCRmyPDF, Ghostscript – run niced with timeouts."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
import structlog

from . import imageops as ops

log = structlog.get_logger(__name__)

NICE = int(os.environ.get("SCANBOX_NICE_LEVEL", "10"))


def tool_available(name: str) -> bool:
    return shutil.which(name) is not None


def _nice_prefix() -> list[str]:
    if os.name == "posix" and shutil.which("nice"):
        return ["nice", "-n", str(NICE)]
    return []


def detect_orientation(img: np.ndarray, timeout: float = 30.0) -> tuple[int, float]:
    """Tesseract OSD → (clockwise rotation to apply, confidence). (0, 0.0) if unavailable/unsure."""
    if not tool_available("tesseract"):
        return 0, 0.0
    gray = ops.to_gray(img)
    small, _ = ops.scale_to_max(gray, 2000)
    with tempfile.TemporaryDirectory(prefix="sbx-osd-") as d:
        p = Path(d) / "page.png"
        cv2.imwrite(str(p), small)
        try:
            res = subprocess.run(
                [*_nice_prefix(), "tesseract", str(p), "stdout", "--psm", "0", "-l", "osd"],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            log.warning("osd.failed", error=str(e))
            return 0, 0.0
    out = res.stdout + res.stderr
    m = re.search(r"Rotate:\s*(\d+)", out)
    c = re.search(r"Orientation confidence:\s*([\d.]+)", out)
    if not m:
        return 0, 0.0
    rot = int(m.group(1)) % 360
    conf = float(c.group(1)) if c else 0.0
    # Low confidence (blank pages, photos) → don't rotate.
    if conf < 1.5:
        return 0, conf
    return rot, conf


@dataclass
class OcrResult:
    pdf: Path
    text: str


class OcrError(Exception):
    pass


async def run_ocrmypdf(
    src_pdf: Path,
    out_pdf: Path,
    *,
    languages: list[str],
    pdfa: bool,
    do_ocr: bool,
    optimize: int,
    jobs: int,
    jpeg_quality: int | None = None,
    clean: bool = False,
    timeout: float = 1800.0,
) -> OcrResult:
    """Run OCRmyPDF as a niced subprocess. ``do_ocr=False`` keeps OCRmyPDF for PDF/A + optimise only."""
    sidecar = out_pdf.with_suffix(".txt")
    args = [
        *_nice_prefix(),
        "ocrmypdf",
        "--jobs",
        str(max(1, jobs)),
        "--output-type",
        "pdfa-2" if pdfa else "pdf",
        "--optimize",
        str(optimize),
        "-l",
        "+".join(languages or ["deu"]),
        "--sidecar",
        str(sidecar),
        "--quiet",
    ]
    if jpeg_quality and optimize >= 1:
        args += ["--jpeg-quality", str(jpeg_quality)]
    if clean and tool_available("unpaper"):
        args += ["--clean-final"]
    if not do_ocr:
        args += ["--tesseract-timeout", "0"]
    # No --rotate-pages: pages are already oriented by our pipeline (OSD + user edits).
    args += [str(src_pdf), str(out_pdf)]
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        _stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (TimeoutError, asyncio.CancelledError):
        proc.kill()
        await proc.wait()
        raise
    # 0 ok; 10 = PDF/A conversion produced a valid PDF but not PDF/A ("output file is okay but…")
    if proc.returncode not in (0, 10) or not out_pdf.exists():
        raise OcrError(f"ocrmypdf exit {proc.returncode}: {stderr.decode(errors='replace')[-2000:]}")
    if proc.returncode == 10:
        log.warning("ocr.pdfa_validation_failed", file=str(out_pdf))
    text = sidecar.read_text(encoding="utf-8", errors="replace") if sidecar.exists() else ""
    if not do_ocr:
        text = ""
    return OcrResult(out_pdf, text.replace("\f", "\n").strip())


async def ghostscript_compress(src: Path, out: Path, level: str = "ebook", timeout: float = 900.0) -> Path:
    """Re-render with Ghostscript (/printer, /ebook, /screen). Keeps the text layer."""
    if level not in ("printer", "ebook", "screen"):
        raise ValueError(level)
    gs = shutil.which("gs") or shutil.which("gswin64c")
    if not gs:
        raise OcrError("ghostscript not available")
    args = [
        *_nice_prefix(),
        gs,
        "-sDEVICE=pdfwrite",
        "-dCompatibilityLevel=1.7",
        f"-dPDFSETTINGS=/{level}",
        "-dNOPAUSE",
        "-dBATCH",
        "-dSAFER",
        "-dQUIET",
        f"-sOutputFile={out}",
        str(src),
    ]
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        _o, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (TimeoutError, asyncio.CancelledError):
        proc.kill()
        await proc.wait()
        raise
    if proc.returncode != 0 or not out.exists():
        raise OcrError(f"ghostscript exit {proc.returncode}: {err.decode(errors='replace')[-1000:]}")
    return out


def tools_status() -> dict[str, bool]:
    return {name: tool_available(name) for name in ("tesseract", "ocrmypdf", "gs", "qpdf", "unpaper", "pngquant")}
