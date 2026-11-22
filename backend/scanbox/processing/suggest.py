"""Heuristic title and date suggestions from OCR text (always user-editable)."""

from __future__ import annotations

import re
from datetime import date

_MONTHS = {
    "januar": 1, "jan": 1, "january": 1, "februar": 2, "feb": 2, "february": 2, "märz": 3, "maerz": 3,
    "mär": 3, "march": 3, "mar": 3, "april": 4, "apr": 4, "mai": 5, "may": 5, "juni": 6, "june": 6,
    "jun": 6, "juli": 7, "july": 7, "jul": 7, "august": 8, "aug": 8, "september": 9, "sep": 9,
    "sept": 9, "oktober": 10, "october": 10, "okt": 10, "oct": 10, "november": 11, "nov": 11,
    "dezember": 12, "december": 12, "dez": 12, "dec": 12,
}  # fmt: skip

_KEYWORDS = [
    "Rechnung", "Gutschrift", "Mahnung", "Kündigung", "Vertrag", "Bescheid", "Steuerbescheid",
    "Kontoauszug", "Lieferschein", "Angebot", "Auftragsbestätigung", "Bestätigung", "Quittung",
    "Beitragsrechnung", "Abrechnung", "Nebenkostenabrechnung", "Lohnabrechnung", "Zeugnis",
    "Invoice", "Receipt", "Statement", "Contract", "Credit Note", "Reminder", "Order Confirmation",
]  # fmt: skip

_DATE_PATTERNS = [
    re.compile(r"\b(\d{1,2})\.\s?(\d{1,2})\.\s?(\d{4}|\d{2})\b"),
    re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"),
    re.compile(r"\b(\d{1,2})\.?\s+([A-Za-zÄÖÜäöü]{3,9})\.?\s+(\d{4})\b"),
    re.compile(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2}),\s+(\d{4})\b"),
]


def _valid(y: int, m: int, d: int) -> str | None:
    if y < 100:
        y += 2000 if y < 70 else 1900
    try:
        dt = date(y, m, d)
    except ValueError:
        return None
    if not (1990 <= dt.year <= date.today().year + 1):
        return None
    return dt.isoformat()


def suggest_date(text: str) -> str | None:
    head = text[:4000]
    for i, rx in enumerate(_DATE_PATTERNS):
        for m in rx.finditer(head):
            g = m.groups()
            res = None
            if i == 0:
                res = _valid(int(g[2]), int(g[1]), int(g[0]))
            elif i == 1:
                res = _valid(int(g[0]), int(g[1]), int(g[2]))
            elif i == 2:
                mon = _MONTHS.get(g[1].lower())
                res = _valid(int(g[2]), mon, int(g[0])) if mon else None
            else:
                mon = _MONTHS.get(g[0].lower())
                res = _valid(int(g[2]), mon, int(g[1])) if mon else None
            if res:
                return res
    return None


def suggest_title(text: str) -> str | None:
    if not text:
        return None
    head = text[:3000]
    for kw in _KEYWORDS:
        m = re.search(rf"\b{re.escape(kw)}\b[^\n]{{0,60}}", head, flags=re.IGNORECASE)
        if m:
            line = re.sub(r"\s+", " ", m.group(0)).strip(" :.-")
            return line[:80]
    for raw in head.splitlines()[:25]:
        line = re.sub(r"\s+", " ", raw).strip(" :.-|")
        words = line.split()
        if 2 <= len(words) <= 10 and 8 <= len(line) <= 80 and sum(c.isalpha() for c in line) > 0.6 * len(line):
            return line
    return None
