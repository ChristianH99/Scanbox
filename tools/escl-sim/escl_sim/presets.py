"""Named sample stacks. Each entry is one *sheet* (a PageSpec, optionally with ``back``)."""

from __future__ import annotations

from collections.abc import Callable

from .pages import PageSpec, QrSpec

P = PageSpec


def _letter_de() -> list[PageSpec]:
    return [P(kind="text", lang="de", seed=i) for i in (1, 2, 3)]


def _letter_en() -> list[PageSpec]:
    return [P(kind="text", lang="en", seed=i) for i in (1, 2, 3)]


def _invoice_qr() -> list[PageSpec]:
    return [
        P(kind="qr", qr="INV-2024-0042", qr_position="top_right", lang="de", seed=10),
        P(kind="text", lang="de", seed=11),
        P(kind="text", lang="de", seed=12),
    ]


def _multi_qr() -> list[PageSpec]:
    return [
        P(kind="multi_qr", qr="ZONE-123", qr_position="top_right",
          extra_qrs=(QrSpec("OTHER-999", "bottom_left"),), lang="de", seed=20),
        P(kind="text", lang="de", seed=21),
    ]


def _with_blanks() -> list[PageSpec]:
    return [
        P(kind="text", lang="de", seed=30),
        P(kind="blank"),
        P(kind="text", lang="de", seed=31),
        P(kind="near_blank", seed=32),
    ]


def _skewed() -> list[PageSpec]:
    return [
        P(kind="text", lang="de", seed=40, skew_deg=3.0),
        P(kind="text", lang="en", seed=41, skew_deg=-4.5),
        P(kind="text", lang="de", seed=42, skew_deg=5.0, rotation=180),
        P(kind="qr", qr="SKEW-1", lang="de", seed=43, skew_deg=-6.0),
    ]


def _sideways() -> list[PageSpec]:
    return [
        P(kind="text", lang="de", seed=45, rotation=90),
        P(kind="text", lang="en", seed=46, rotation=270),
    ]


def _duplex_mixed() -> list[PageSpec]:
    return [
        P(kind="text", lang="de", seed=50, back=P(kind="text", lang="de", seed=51)),
        P(kind="text", lang="de", seed=52, back=P(kind="blank")),
        P(kind="qr", qr="DUPLEX-7", lang="de", seed=53, back=P(kind="near_blank", seed=54)),
        P(kind="text", lang="en", seed=55),  # no back given → blank back
    ]


def _separator_batch() -> list[PageSpec]:
    return [
        P(kind="qr", qr="DOC-A", lang="de", seed=60),
        P(kind="text", lang="de", seed=61),
        P(kind="qr", qr="DOC-B", lang="de", seed=62),
        P(kind="text", lang="de", seed=63),
        P(kind="text", lang="de", seed=64),
    ]


def _separator_sheets() -> list[PageSpec]:
    return [
        P(kind="separator", qr="DOC-A"),
        P(kind="text", lang="de", seed=65),
        P(kind="separator", qr="DOC-B"),
        P(kind="text", lang="de", seed=66),
    ]


def _photo() -> list[PageSpec]:
    return [P(kind="photo", seed=70), P(kind="text", lang="de", seed=71)]


def _long_stack() -> list[PageSpec]:
    return [P(kind="text", lang="de" if i % 3 else "en", seed=100 + i) for i in range(30)]


def _mixed_paper() -> list[PageSpec]:
    return [
        P(kind="text", paper="a4", seed=80),
        P(kind="text", paper="letter", lang="en", seed=81),
        P(kind="text", paper="a5", seed=82),
    ]


PRESETS: dict[str, Callable[[], list[PageSpec]]] = {
    "letter_de": _letter_de,
    "letter_en": _letter_en,
    "invoice_qr": _invoice_qr,
    "multi_qr": _multi_qr,
    "with_blanks": _with_blanks,
    "skewed": _skewed,
    "sideways": _sideways,
    "duplex_mixed": _duplex_mixed,
    "separator_batch": _separator_batch,
    "separator_sheets": _separator_sheets,
    "photo": _photo,
    "long_stack": _long_stack,
    "mixed_paper": _mixed_paper,
}


def preset(name: str) -> list[PageSpec]:
    try:
        return PRESETS[name]()
    except KeyError:
        raise ValueError(f"unknown stack preset {name!r} (known: {sorted(PRESETS)})") from None
