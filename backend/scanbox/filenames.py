"""Filename templates, strict sanitising and conflict handling (docs/ARCHITECTURE.md §9)."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

MAX_NAME_BYTES = 150
TOKENS = ("date", "time", "datetime", "qr", "title", "counter", "profile", "user", "pages")
_TOKEN_RE = re.compile(r"\{([a-z]+)(?::([^{}]{1,40}))?\}")
_ILLEGAL = re.compile(r'[\x00-\x1f\x7f/\\:*?"<>|]')
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}  # fmt: skip
_ALLOWED_STRFTIME = re.compile(r"^[%a-zA-Z0-9 ._\-]+$")


@dataclass
class TemplateVars:
    created: datetime
    qr: str | None = None
    title: str = ""
    counter: int = 0
    profile: str = ""
    user: str = ""
    pages: int = 0


def _fmt_date(dt: datetime, spec: str | None, default: str) -> str:
    if spec and _ALLOWED_STRFTIME.match(spec) and "%" in spec:
        try:
            return dt.strftime(spec)
        except ValueError:
            pass
    return dt.strftime(default)


def render_filename_template(template: str, v: TemplateVars) -> str:
    """Render tokens; unknown tokens are dropped. Result is NOT yet sanitised."""

    def repl(m: re.Match[str]) -> str:
        name, spec = m.group(1), m.group(2)
        if name == "date":
            return _fmt_date(v.created, spec, "%Y-%m-%d")
        if name == "time":
            return _fmt_date(v.created, spec, "%H-%M-%S")
        if name == "datetime":
            return _fmt_date(v.created, spec, "%Y-%m-%d_%H-%M-%S")
        if name == "qr":
            return v.qr or ""
        if name == "title":
            return v.title or ""
        if name == "counter":
            width = int(spec) if spec and spec.isdigit() and 1 <= int(spec) <= 8 else 4
            return str(v.counter).zfill(width)
        if name == "profile":
            return v.profile
        if name == "user":
            return v.user
        if name == "pages":
            return str(v.pages)
        return ""

    out = _TOKEN_RE.sub(repl, template)
    # Tidy separators left behind by empty tokens: "__", leading/trailing "_- ".
    out = re.sub(r"([_\- ])[_\- ]+", r"\1", out)
    return out.strip("_- .")


def sanitize_component(name: str, max_bytes: int = MAX_NAME_BYTES) -> str:
    """Make a single path component safe on Linux, SMB and Windows."""
    name = unicodedata.normalize("NFC", name or "")
    name = _ILLEGAL.sub("_", name)
    name = re.sub(r"\s+", " ", name).strip()
    name = name.lstrip(".-").rstrip(". ")
    if name in ("", ".", ".."):
        return ""
    stem = name.split(".")[0].upper()
    if stem in _WINDOWS_RESERVED:
        name = "_" + name
    return truncate_utf8(name, max_bytes).rstrip(". ")


def truncate_utf8(s: str, max_bytes: int) -> str:
    b = s.encode("utf-8")
    if len(b) <= max_bytes:
        return s
    return b[:max_bytes].decode("utf-8", errors="ignore")


def build_filename(template: str, v: TemplateVars, ext: str, *, add_qr: bool = False) -> str:
    """Render + sanitise + extension. ``add_qr`` prefixes ``{qr}_`` if the template lacks it."""
    if add_qr and v.qr and "{qr" not in template:
        template = "{qr}_" + template
    base = sanitize_component(render_filename_template(template, v)) or "scan"
    ext = re.sub(r"[^a-z0-9]", "", ext.lower())[:5]
    return f"{base}.{ext}" if ext else base


def sanitize_subpath(subpath: str) -> Path:
    """Sanitise a relative sub-folder (per segment). Rejects traversal."""
    if not subpath:
        return Path()
    if subpath.startswith(("/", "\\")) or re.match(r"^[a-zA-Z]:", subpath):
        raise ValueError("sub-folder must be relative")
    segs = []
    for raw in re.split(r"[/\\]+", subpath):
        if raw in ("", "."):
            continue
        if raw == "..":
            raise ValueError("sub-folder must not contain '..'")
        seg = sanitize_component(raw, 100)
        if seg:
            segs.append(seg)
    if len(segs) > 8:
        raise ValueError("sub-folder too deep")
    return Path(*segs) if segs else Path()


def unique_path(directory: Path, filename: str) -> Path:
    """Return a non-existing path, adding ' (2)', ' (3)', … before the extension."""
    candidate = directory / filename
    if not candidate.exists():
        return candidate
    stem, dot, ext = filename.rpartition(".")
    if not dot:
        stem, ext = filename, ""
    for i in range(2, 10000):
        name = f"{stem} ({i}).{ext}" if ext else f"{stem} ({i})"
        candidate = directory / name
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"too many files named {filename}")
