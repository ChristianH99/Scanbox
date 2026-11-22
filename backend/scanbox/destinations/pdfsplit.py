"""Split a PDF into page-range parts whose file size stays below a bound (pikepdf)."""

from __future__ import annotations

import io
from pathlib import Path

import pikepdf


class PageTooLargeError(Exception):
    """A single page alone exceeds the size bound; splitting cannot help."""

    def __init__(self, page_index: int, size: int, max_bytes: int) -> None:
        super().__init__(f"page {page_index + 1} alone is {size} bytes (limit {max_bytes})")
        self.page_index = page_index
        self.size = size


def _save_range(pdf: pikepdf.Pdf, start: int, end: int) -> bytes:
    out = pikepdf.Pdf.new()
    out.pages.extend(pdf.pages[start:end])
    buf = io.BytesIO()
    out.save(buf, compress_streams=True, object_stream_mode=pikepdf.ObjectStreamMode.generate)
    return buf.getvalue()


def plan_split(src: Path, max_bytes: int) -> list[tuple[int, int, bytes]]:
    """Return ``[(start, end, pdf_bytes), ...]`` (``end`` exclusive) covering all pages in order.

    Greedy: estimate per-page sizes from single-page saves, group pages, then verify each
    group by actually saving it; a group that is still too large is halved. Raises
    :class:`PageTooLargeError` if a single page cannot fit.
    """
    with pikepdf.open(src) as pdf:
        n = len(pdf.pages)
        if n == 0:
            raise ValueError("PDF has no pages")
        single = [_save_range(pdf, i, i + 1) for i in range(n)]
        # Fixed overhead of a one-page file (header, trailer, xref) – roughly what is
        # shared rather than additive when pages are combined.
        overhead = min(len(b) for b in single) // 4

        groups: list[tuple[int, int]] = []
        start, acc = 0, 0
        for i, b in enumerate(single):
            if len(b) > max_bytes:
                raise PageTooLargeError(i, len(b), max_bytes)
            size = len(b) - overhead
            if i > start and acc + size + overhead > max_bytes:
                groups.append((start, i))
                start, acc = i, 0
            acc += size
        groups.append((start, n))

        parts: list[tuple[int, int, bytes]] = []
        stack = list(reversed(groups))
        while stack:
            s, e = stack.pop()
            data = single[s] if e - s == 1 else _save_range(pdf, s, e)
            if len(data) <= max_bytes:
                parts.append((s, e, data))
                continue
            if e - s == 1:  # pragma: no cover - guarded above
                raise PageTooLargeError(s, len(data), max_bytes)
            mid = (s + e) // 2
            stack.append((mid, e))
            stack.append((s, mid))
        return parts


def split_pdf(src: Path, max_bytes: int, out_dir: Path, stem: str) -> list[Path]:
    """Write size-bounded parts as ``<stem>_part<i>of<n>.pdf`` into ``out_dir``."""
    parts = plan_split(src, max_bytes)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for i, (_s, _e, data) in enumerate(parts, start=1):
        p = out_dir / f"{stem}_part{i}of{len(parts)}.pdf"
        p.write_bytes(data)
        paths.append(p)
    return paths
