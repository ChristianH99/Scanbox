"""Deterministic sample page generation.

A :class:`PageSpec` describes one *side* of a sheet (optionally with a ``back``).
Rendering is pure: the same (spec, dpi) always yields the same pixels, and results
are cached.
"""

from __future__ import annotations

import dataclasses
import json
import math
import os
import random
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any

import numpy as np
import qrcode
from PIL import Image, ImageDraw, ImageFont

PAPER_MM: dict[str, tuple[float, float]] = {
    "a4": (210.0, 297.0),
    "letter": (215.9, 279.4),
    "a5": (148.0, 210.0),
}
KINDS = {"text", "qr", "multi_qr", "blank", "near_blank", "photo", "separator", "file"}
POSITIONS = {"top_right", "top_left", "top_center", "bottom_right", "bottom_left", "bottom_center", "center"}
ROTATIONS = {0, 90, 180, 270}

PAPER_RGB = (250, 250, 247)
INK_RGB = (28, 28, 34)

DEFAULT_SAMPLES_DIR = Path(os.environ.get("ESCL_SIM_SAMPLES_DIR") or Path(__file__).resolve().parent.parent / "samples")


@dataclass(frozen=True)
class QrSpec:
    value: str
    position: str = "top_right"
    size_mm: float = 25.0

    @classmethod
    def from_any(cls, data: Any) -> QrSpec:
        if isinstance(data, QrSpec):
            return data
        if isinstance(data, str):
            return cls(value=data)
        if not isinstance(data, dict) or "value" not in data:
            raise ValueError(f"invalid QR spec: {data!r}")
        q = cls(value=str(data["value"]), position=data.get("position", "top_right"),
                size_mm=float(data.get("size_mm", 25.0)))
        if q.position not in POSITIONS:
            raise ValueError(f"invalid QR position {q.position!r}")
        return q


@dataclass(frozen=True)
class PageSpec:
    """One page side. See docs/SIMULATOR.md for field semantics."""

    kind: str = "text"
    qr: str | None = None
    qr_position: str = "top_right"
    qr_size_mm: float = 25.0
    extra_qrs: tuple[QrSpec, ...] = ()
    rotation: int = 0  # clockwise rotation of the content as it appears in the scan
    skew_deg: float = 0.0  # clockwise skew of the sheet (crooked feeding)
    paper: str = "a4"
    text: str | None = None
    lang: str = "de"
    seed: int = 0
    path: str | None = None
    back: PageSpec | None = field(default=None, compare=True)

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"invalid kind {self.kind!r} (expected one of {sorted(KINDS)})")
        if self.rotation not in ROTATIONS:
            raise ValueError(f"invalid rotation {self.rotation!r}")
        if self.paper not in PAPER_MM:
            raise ValueError(f"invalid paper {self.paper!r}")
        if self.qr_position not in POSITIONS:
            raise ValueError(f"invalid qr_position {self.qr_position!r}")
        if self.lang not in ("de", "en"):
            raise ValueError(f"invalid lang {self.lang!r}")
        if self.kind == "file" and not self.path:
            raise ValueError("kind=file requires path")
        if abs(self.skew_deg) > 45:
            raise ValueError("skew_deg must be within ±45")

    @classmethod
    def from_any(cls, data: Any) -> PageSpec:
        if isinstance(data, PageSpec):
            return data
        if isinstance(data, str):
            return cls(kind=data)
        if not isinstance(data, dict):
            raise TypeError(f"invalid page spec: {data!r}")
        known = {f.name for f in dataclasses.fields(cls)}
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"unknown PageSpec fields: {sorted(unknown)}")
        kw = dict(data)
        if "extra_qrs" in kw:
            kw["extra_qrs"] = tuple(QrSpec.from_any(q) for q in kw["extra_qrs"] or ())
        if kw.get("back") is not None:
            kw["back"] = cls.from_any(kw["back"])
        if "rotation" in kw:
            kw["rotation"] = int(kw["rotation"]) % 360
        if "skew_deg" in kw:
            kw["skew_deg"] = float(kw["skew_deg"])
        if "seed" in kw:
            kw["seed"] = int(kw["seed"])
        return cls(**kw)

    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        d["extra_qrs"] = [dataclasses.asdict(q) for q in self.extra_qrs]
        d["back"] = None if self.back is None else self.back.to_dict()
        return d

    def side(self) -> PageSpec:
        """This spec without its back (a single side, used as cache key)."""
        return self if self.back is None else dataclasses.replace(self, back=None)

    def all_qrs(self) -> list[QrSpec]:
        qrs = []
        if self.qr:
            qrs.append(QrSpec(self.qr, self.qr_position, self.qr_size_mm))
        qrs.extend(self.extra_qrs)
        return qrs


BLANK = PageSpec(kind="blank")


# --------------------------------------------------------------------------- fonts


@lru_cache(maxsize=64)
def font(size_px: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    size_px = max(6, int(size_px))
    try:
        ref = resources.files("escl_sim").joinpath(f"data/fonts/{name}")
        with resources.as_file(ref) as p:
            return ImageFont.truetype(str(p), size_px)
    except OSError:  # pragma: no cover - font missing from a broken install
        return ImageFont.load_default(size=size_px)


def pt(points: float, dpi: int) -> int:
    return max(6, round(points / 72 * dpi))


def mm(value: float, dpi: int) -> int:
    return round(value / 25.4 * dpi)


# --------------------------------------------------------------------------- text corpora

_DE = {
    "cities": ["Berlin", "München", "Hamburg", "Köln", "Leipzig", "Freiburg"],
    "months": ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August",
               "September", "Oktober", "November", "Dezember"],
    "senders": ["Müller & Söhne GmbH · Hauptstraße 12 · 10115 Berlin",
                "Stadtwerke Nordheim · Postfach 4410 · 20095 Hamburg",
                "Kanzlei Weber Schäfer · Domplatz 3 · 50667 Köln"],
    "recipients": [["Frau", "Erika Mustermann", "Lindenweg 7", "80331 München"],
                   ["Herrn", "Max Beispiel", "Am Markt 21", "04109 Leipzig"]],
    "subjects": ["Rechnung Nr. {n}", "Ihre Anfrage vom {d}", "Vertragsänderung zum {d}",
                 "Mahnung zu Rechnung {n}", "Bestätigung Ihres Auftrags {n}"],
    "greeting": "Sehr geehrte Damen und Herren,",
    "closing": ["Mit freundlichen Grüßen", "Ihr Kundenservice-Team"],
    "sentences": [
        "vielen Dank für Ihr Schreiben und das entgegengebrachte Vertrauen.",
        "Hiermit bestätigen wir den Eingang Ihrer Unterlagen.",
        "Bitte überweisen Sie den offenen Betrag innerhalb von vierzehn Tagen auf das unten genannte Konto.",
        "Die Leistungen wurden im vergangenen Monat vollständig erbracht.",
        "Für Rückfragen steht Ihnen unser Kundenservice gerne zur Verfügung.",
        "Bitte geben Sie bei Zahlungen stets die Rechnungsnummer an.",
        "Die neuen Konditionen gelten ab dem ersten Tag des kommenden Quartals.",
        "Wir weisen darauf hin, dass sich unsere Bankverbindung geändert hat.",
        "Der Vertrag verlängert sich automatisch um ein weiteres Jahr, sofern er nicht fristgerecht gekündigt wird.",
        "Anbei erhalten Sie eine Kopie des unterschriebenen Protokolls.",
        "Die Lieferung erfolgt voraussichtlich in der nächsten Woche.",
        "Bitte prüfen Sie die beigefügte Aufstellung sorgfältig.",
        "Eventuelle Abweichungen teilen Sie uns bitte schriftlich mit.",
        "Wir freuen uns auf die weitere Zusammenarbeit.",
    ],
}
_EN = {
    "cities": ["London", "Manchester", "Boston", "Dublin", "Seattle", "Leeds"],
    "months": ["January", "February", "March", "April", "May", "June", "July", "August",
               "September", "October", "November", "December"],
    "senders": ["Acme Supplies Ltd · 12 High Street · London EC1A 1BB",
                "Northwind Energy · PO Box 4410 · Boston MA 02108",
                "Baker & Partners LLP · 3 Market Square · Leeds LS1 6DT"],
    "recipients": [["Ms", "Jane Example", "7 Linden Road", "Manchester M1 2AB"],
                   ["Mr", "John Sample", "21 Market Street", "Dublin D02 XY45"]],
    "subjects": ["Invoice No. {n}", "Your enquiry of {d}", "Amendment to contract as of {d}",
                 "Reminder for invoice {n}", "Confirmation of your order {n}"],
    "greeting": "Dear Sir or Madam,",
    "closing": ["Yours sincerely", "Customer Service Team"],
    "sentences": [
        "thank you for your letter and for the trust you have placed in us.",
        "We hereby confirm receipt of your documents.",
        "Please transfer the outstanding amount within fourteen days to the account stated below.",
        "The services were delivered in full during the previous month.",
        "Our customer service team will be happy to answer any questions.",
        "Please always quote the invoice number when making payments.",
        "The new terms apply from the first day of the next quarter.",
        "Please note that our bank details have changed.",
        "The contract renews automatically for another year unless cancelled in due time.",
        "Enclosed you will find a copy of the signed minutes.",
        "Delivery is expected to take place next week.",
        "Please check the attached statement carefully.",
        "Kindly inform us in writing of any discrepancies.",
        "We look forward to continuing our cooperation.",
    ],
}


def letter_content(spec: PageSpec) -> dict:
    """Deterministic letter content for a spec (sender, date, heading, paragraphs)."""
    c = _DE if spec.lang == "de" else _EN
    rng = random.Random(f"{spec.lang}:{spec.seed}")
    day, month, year = rng.randint(1, 28), rng.randint(1, 12), rng.choice([2023, 2024, 2025])
    city = rng.choice(c["cities"])
    if spec.lang == "de":
        date_line = f"{city}, {day}. {c['months'][month - 1]} {year}"
        short_date = f"{day:02d}.{month:02d}.{year}"
    else:
        date_line = f"{city}, {c['months'][month - 1]} {day}, {year}"
        short_date = f"{year}-{month:02d}-{day:02d}"
    number = f"{year}-{rng.randint(1000, 9999)}"
    heading = rng.choice(c["subjects"]).format(n=number, d=short_date)
    if spec.text:
        paragraphs = [p.strip() for p in spec.text.split("\n\n") if p.strip()]
    else:
        pool = list(c["sentences"])
        paragraphs = [c["greeting"]]
        for i in range(rng.randint(3, 5)):
            sents = rng.sample(pool, rng.randint(2, 4))
            if i == 0 and spec.lang == "de":
                sents[0] = "vielen Dank für Ihr Schreiben und das entgegengebrachte Vertrauen."
            paragraphs.append(" ".join(s[0].upper() + s[1:] for s in sents))
        paragraphs.append("\n".join(c["closing"]))
    return {
        "sender": rng.choice(c["senders"]),
        "recipient": rng.choice(c["recipients"]),
        "date_line": date_line,
        "heading": heading,
        "paragraphs": paragraphs,
    }


def _wrap(draw: ImageDraw.ImageDraw, text: str, fnt, max_w: int) -> list[str]:
    lines: list[str] = []
    for raw_line in text.split("\n"):
        words = raw_line.split()
        cur = ""
        for w in words:
            cand = f"{cur} {w}".strip()
            if draw.textlength(cand, font=fnt) <= max_w or not cur:
                cur = cand
            else:
                lines.append(cur)
                cur = w
        lines.append(cur)
    return lines


# --------------------------------------------------------------------------- QR


def qr_image(value: str, size_px: int) -> Image.Image:
    q = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=1, border=4)
    q.add_data(value)
    q.make(fit=True)
    img = q.make_image(fill_color="black", back_color="white").get_image().convert("L")
    modules = img.width
    scale = max(1, size_px // modules)
    img = img.resize((modules * scale, modules * scale), Image.NEAREST)
    return img.convert("RGB")


def qr_box(position: str, w: int, h: int, size: int, margin: int) -> tuple[int, int]:
    """Top-left corner for a QR of ``size`` px at ``position`` inside the page margins."""
    xs = {"left": margin, "right": w - margin - size, "center": (w - size) // 2}
    ys = {"top": margin, "bottom": h - margin - size, "center": (h - size) // 2}
    if position == "center":
        return xs["center"], ys["center"]
    vert, horiz = position.split("_")
    return xs[horiz], ys[vert]


# --------------------------------------------------------------------------- content


def _draw_letter(img: Image.Image, spec: PageSpec, dpi: int, top_reserved: int, bottom_reserved: int) -> None:
    w, h = img.size
    draw = ImageDraw.Draw(img)
    content = letter_content(spec)
    left, right = mm(22, dpi), w - mm(20, dpi)
    y = max(mm(18, dpi), top_reserved)
    bottom = h - max(mm(18, dpi), bottom_reserved)
    small, body, bold = font(pt(8, dpi)), font(pt(11, dpi)), font(pt(15, dpi), bold=True)
    line_h = round(pt(11, dpi) * 1.4)

    def text_line(t: str, fnt, x: int | None = None, step: int | None = None) -> None:
        nonlocal y
        if y + (step or line_h) > bottom:
            return
        draw.text((left if x is None else x, y), t, font=fnt, fill=INK_RGB)
        y += step or line_h

    text_line(content["sender"], small, step=round(pt(8, dpi) * 1.8))
    for part in content["recipient"]:
        text_line(part, body)
    y += line_h
    date_w = draw.textlength(content["date_line"], font=body)
    text_line(content["date_line"], body, x=int(right - date_w))
    y += line_h
    text_line(content["heading"], bold, step=round(pt(15, dpi) * 1.6))
    y += line_h // 2
    for para in content["paragraphs"]:
        for line in _wrap(draw, para, body, right - left):
            text_line(line, body)
        y += line_h // 2


def _qr_margin(dpi: int) -> int:
    return mm(12, dpi)


def _draw_qrs(img: Image.Image, qrs: list[QrSpec], dpi: int) -> tuple[int, int]:
    """Paste QR codes; return (top_reserved, bottom_reserved) pixel bands for text layout."""
    w, h = img.size
    margin = _qr_margin(dpi)
    top_res = bottom_res = 0
    for q in qrs:
        size = mm(q.size_mm, dpi)
        qimg = qr_image(q.value, size)
        x, y = qr_box(q.position, w, h, qimg.width, margin)
        img.paste(qimg, (x, y))
        if q.position.startswith("top"):
            top_res = max(top_res, y + qimg.height + mm(4, dpi))
        elif q.position.startswith("bottom"):
            bottom_res = max(bottom_res, h - y + mm(4, dpi))
    return top_res, bottom_res


def _draw_near_blank(img: Image.Image, spec: PageSpec, dpi: int) -> None:
    rng = random.Random(f"near_blank:{spec.seed}")
    draw = ImageDraw.Draw(img)
    w, h = img.size
    for _ in range(rng.randint(5, 9)):
        r = max(1, mm(rng.uniform(0.15, 0.45), dpi))
        x, y = rng.randint(r, w - r - 1), rng.randint(r, h - r - 1)
        g = rng.randint(60, 150)
        draw.ellipse((x - r, y - r, x + r, y + r), fill=(g, g, g))
    # a faint bleed-through line
    x0, y0 = rng.randint(w // 5, w // 2), rng.randint(h // 5, h // 2)
    draw.line((x0, y0, x0 + mm(30, dpi), y0), fill=(238, 238, 236), width=max(1, mm(0.3, dpi)))


def _draw_photo(img: Image.Image, spec: PageSpec, dpi: int) -> None:
    w, h = img.size
    rng = random.Random(f"photo:{spec.seed}")
    x0, y0, x1, y1 = mm(20, dpi), mm(30, dpi), w - mm(20, dpi), int(h * 0.62)
    pw, ph = x1 - x0, y1 - y0
    yy, xx = np.mgrid[0:ph, 0:pw].astype(np.float32)
    xx /= max(pw - 1, 1)
    yy /= max(ph - 1, 1)
    phase = rng.random() * math.pi
    r = 90 + 120 * xx
    g = 120 + 100 * np.sin(yy * math.pi + phase) ** 2
    b = 200 - 140 * yy
    arr = np.clip(np.stack([r, g, b], axis=-1), 0, 255).astype(np.uint8)
    photo = Image.fromarray(arr, "RGB")
    d = ImageDraw.Draw(photo)
    for _ in range(6):
        cx, cy, rad = rng.randint(0, pw), rng.randint(0, ph), rng.randint(pw // 16, pw // 6)
        col = (rng.randint(150, 255), rng.randint(30, 200), rng.randint(0, 120))
        d.ellipse((cx - rad, cy - rad, cx + rad, cy + rad), fill=col)
    img.paste(photo, (x0, y0))
    draw = ImageDraw.Draw(img)
    caption = "Urlaubsfoto – Sommer" if spec.lang == "de" else "Holiday photo – summer"
    draw.text((x0, y1 + mm(6, dpi)), f"{caption} #{spec.seed}", font=font(pt(14, dpi), True), fill=INK_RGB)


def _draw_separator(img: Image.Image, spec: PageSpec, dpi: int) -> None:
    w, h = img.size
    draw = ImageDraw.Draw(img)
    value = spec.qr or "SEPARATOR"
    size = mm(max(spec.qr_size_mm, 50), dpi)
    qimg = qr_image(value, size)
    img.paste(qimg, ((w - qimg.width) // 2, (h - qimg.height) // 2))
    title = "TRENNBLATT" if spec.lang == "de" else "SEPARATOR SHEET"
    for text, fnt, y in (
        (title, font(pt(32, dpi), True), h // 2 - qimg.height // 2 - mm(25, dpi)),
        (value, font(pt(16, dpi)), h // 2 + qimg.height // 2 + mm(8, dpi)),
    ):
        tw = draw.textlength(text, font=fnt)
        draw.text(((w - tw) / 2, y), text, font=fnt, fill=INK_RGB)
    for q in spec.extra_qrs:
        _draw_qrs(img, [q], dpi)


def _load_file(spec: PageSpec, w: int, h: int, samples_dir: Path) -> Image.Image:
    p = Path(spec.path)
    if not p.is_absolute():
        p = samples_dir / p
    with Image.open(p) as src:
        src = src.convert("RGB")
        if (src.width > src.height) != (w > h):
            src = src.rotate(90, expand=True)
        return src.resize((w, h), Image.LANCZOS)


def _render_content(spec: PageSpec, dpi: int, w: int, h: int, samples_dir: Path) -> Image.Image:
    if spec.kind == "file":
        return _load_file(spec, w, h, samples_dir)
    img = Image.new("RGB", (w, h), PAPER_RGB)
    kind = spec.kind
    if kind in ("text", "qr", "multi_qr"):
        top, bottom = _draw_qrs(img, spec.all_qrs(), dpi)
        _draw_letter(img, spec, dpi, top, bottom)
    elif kind == "near_blank":
        _draw_near_blank(img, spec, dpi)
    elif kind == "photo":
        _draw_photo(img, spec, dpi)
        _draw_qrs(img, spec.all_qrs(), dpi)
    elif kind == "separator":
        _draw_separator(img, spec, dpi)
    elif kind == "blank" and spec.all_qrs():
        _draw_qrs(img, spec.all_qrs(), dpi)
    return img


def paper_size_px(paper: str, dpi: int) -> tuple[int, int]:
    wmm, hmm = PAPER_MM[paper]
    return mm(wmm, dpi), mm(hmm, dpi)


@lru_cache(maxsize=12)
def render_paper(spec: PageSpec, dpi: int, samples_dir: Path = DEFAULT_SAMPLES_DIR) -> Image.Image:
    """The sheet (portrait, physical size at ``dpi``) with content rotated per ``spec.rotation``.

    Skew is *not* applied here (it belongs to feeding, see :func:`imaging.compose_scan`).
    The returned image is shared via the cache: do not modify it.
    """
    spec = spec.side()
    w, h = paper_size_px(spec.paper, dpi)
    if spec.rotation in (90, 270):
        content = _render_content(spec, dpi, h, w, samples_dir)  # landscape layout
    else:
        content = _render_content(spec, dpi, w, h, samples_dir)
    if spec.rotation:
        content = content.rotate(-spec.rotation, expand=True)
    if content.size != (w, h):
        content = content.resize((w, h))
    return content


ANALYSIS_DPI = 75


@lru_cache(maxsize=512)
def ink_fraction(spec: PageSpec, samples_dir: Path = DEFAULT_SAMPLES_DIR) -> float:
    """Fraction of 'ink' pixels on the sheet (used for scanner-side blank detection)."""
    arr = np.asarray(render_paper(spec.side(), ANALYSIS_DPI, samples_dir).convert("L"))
    return float((arr < 160).mean())


@lru_cache(maxsize=512)
def has_color(spec: PageSpec, samples_dir: Path = DEFAULT_SAMPLES_DIR) -> bool:
    """True when the sheet has noticeable colour content (AutoColorDetection)."""
    arr = np.asarray(render_paper(spec.side(), ANALYSIS_DPI, samples_dir)).astype(np.int16)
    chroma = arr.max(axis=-1) - arr.min(axis=-1)
    return bool((chroma > 40).mean() > 0.002)


def spec_key(spec: PageSpec) -> str:
    return json.dumps(spec.to_dict(), sort_keys=True)
