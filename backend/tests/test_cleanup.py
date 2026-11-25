"""Background cleanup and blank detection.

``_scanned_front`` / ``_scanned_back`` reproduce what a real HP ScanJet N4000 delivered for a folded
A4 flyer at 150 dpi (measured on the original scans, which are not in the repository):
* front – text, dark artwork, light grey background artwork, fold crease, a grey backdrop strip on the
  left edge, 2 truncated (grey) JPEG rows at the bottom and dark corners;
* back – the empty back side: crease, faint mirrored show-through, backdrop strip, dark corners.
"""

from __future__ import annotations

import cv2
import numpy as np
import pytest

from scanbox.processing import imageops as ops
from scanbox.processing.render import PageEdits, finish_page, measure_ink_fast
from scanbox.profiles import ProcessingOptions, ProfileSettings, effective_clean_background

from .conftest import FAST, builtin_profile_id

CLEAN = ops.Enhance(clean_background=True)


def _scanner_artefacts(pg: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """Fold crease, backdrop strip, dark corners, sensor noise and a JPEG round trip, as from the N4000."""
    h, w = pg.shape[:2]
    for x in range(40, w - 20, 6):  # fold crease: broken, slightly bluish line across the middle
        y = h // 2 + int(4 * np.sin(x / 90))
        pg[y:y + 3, x:x + 4] = (238, 226, 226) if rng.random() < 0.7 else (250, 246, 246)
    pg[:, :12] = np.linspace(134, 121, 12).astype(np.uint8)[None, :, None]  # backdrop strip
    for y0, y1, x0, x1, v in ((0, 30, 0, 30, 170), (h - 30, h, 0, 30, 175), (0, 4, w - 60, w, 90)):
        pg[y0:y1, x0:x1] = v  # dark corners
    noise = rng.normal(0, 1.5, pg.shape)
    pg = np.clip(pg + noise, 0, 255).astype(np.uint8)
    ok, enc = cv2.imencode(".jpg", pg, [cv2.IMWRITE_JPEG_QUALITY, 85])
    assert ok
    return cv2.imdecode(enc, cv2.IMREAD_COLOR)


def _scanned_front() -> np.ndarray:
    rng = np.random.default_rng(1)
    pg = np.full((1754, 1240, 3), 255, np.uint8)
    cv2.circle(pg, (1040, 140), 95, (20, 20, 20), 22)  # stamp-like logo
    cv2.rectangle(pg, (150, 230), (290, 500), (25, 25, 25), -1)  # dark artwork
    cv2.rectangle(pg, (170, 260), (270, 330), (250, 250, 250), -1)
    for i in range(8):  # light grey background artwork behind the text
        cv2.line(pg, (650 + i * 60, 1100), (700 + i * 60, 1550), (205, 205, 205), 4)
    lines = [(120, 150, 1.4), (500, 400, 3.0), (180, 700, 1.6), (120, 800, 1.6), (240, 900, 1.2),
             (130, 1090, 1.3), (130, 1150, 1.3), (130, 1210, 1.3), (110, 1370, 1.3), (110, 1430, 1.3),
             (180, 1490, 1.3), (150, 1590, 0.9)]
    for x, y, size in lines:
        cv2.putText(pg, "Einladung zum Fest am Sonntag", (x, y), cv2.FONT_HERSHEY_DUPLEX, size, (15, 15, 15),
                    max(2, int(size * 2.5)))
    pg = _scanner_artefacts(pg, rng)
    pg[-2:, :] = 128  # truncated JPEG rows
    return pg


def _scanned_back() -> np.ndarray:
    rng = np.random.default_rng(2)
    pg = np.full((1748, 1240, 3), 255, np.uint8)
    for i, y in enumerate((340, 690, 800, 1080, 1180, 1420, 1480)):  # show-through: mirrored, very faint
        cv2.putText(pg, "tsef muz gnudalniE"[: 10 + i], (300, y), cv2.FONT_HERSHEY_DUPLEX, 1.5,
                    (243, 241, 243), 3)
    return _scanner_artefacts(pg, rng)


def _page(h: int = 1754, w: int = 1240) -> np.ndarray:
    """Synthetic A4 page at 150 dpi: shading gradient, black box, highlighter, smooth 'photo', text."""
    pg = np.full((h, w, 3), 245, np.uint8)
    pg = np.clip(pg.astype(np.int16) + np.linspace(0, -40, w).astype(np.int16)[None, :, None], 0, 255).astype(np.uint8)
    pg[150:450, 150:550] = 20
    pg[600:650, 150:750] = (60, 230, 240)  # yellow highlighter (BGR)
    yy, xx = np.mgrid[0:400, 0:500]
    pg[750:1150, 650:1150] = np.stack([xx * 0.2 + 40, yy * 0.2 + 60, xx * 0.1 + yy * 0.1 + 50], -1).astype(np.uint8)
    for i in range(10):
        cv2.putText(pg, "Lorem ipsum dolor sit amet", (150, 1300 + i * 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7,
                    (30, 30, 30), 2)
    return pg


# ---------------------------------------------------------------- background normalisation
def test_clean_background_whitens_paper_and_keeps_content() -> None:
    pg = _page()
    out = ops.enhance(pg, CLEAN)
    # shading gradient gone: paper on both sides is white
    assert out[1700, 50:100].mean() > 252 and out[1700, 1150:1200].mean() > 252
    # dark box, highlighter colour, photo and text survive
    assert out[200:400, 200:500].mean() < 40
    b, g, r = out[610:640, 200:700].reshape(-1, 3).mean(0)
    assert b < 100 and g > 200 and r > 200
    assert np.abs(out[800:1100, 700:1100].astype(int) - pg[800:1100, 700:1100]).mean() < 25
    assert out[1290:1600, 150:600].min() < 60


def test_clean_background_skips_pages_without_paper() -> None:
    rng = np.random.default_rng(3)
    photo = rng.integers(30, 220, (800, 600, 3), dtype=np.uint8)
    assert np.array_equal(ops.normalize_background(photo), photo)


def test_clean_borders_removes_edge_artefacts_but_not_content() -> None:
    pg = np.full((1754, 1240), 255, np.uint8)
    pg[:, :15] = 130  # backdrop strip
    pg[-3:, :] = 128  # truncated JPEG rows
    pg[0:20, 1180:] = 60  # dark corner
    pg[800:860, 0:400] = 0  # black bar reaching far into the page: content
    out = ops.clean_borders(pg)
    assert out[:780, :15].min() == 255 and out[880:-5, :15].min() == 255
    assert out[-3:, 500:].min() == 255
    assert out[0:20, 1180:].min() == 255
    assert out[810:850, 100:390].max() == 0


# ---------------------------------------------------------------- blank detection
def test_ink_ignores_creases_strips_and_show_through() -> None:
    pg = np.full((1754, 1240), 250, np.uint8)
    pg[:, :18] = 135  # backdrop strip
    cv2.line(pg, (100, 870), (1150, 880), 215, 3)  # fold crease
    for i in range(12):  # show-through: low-contrast mirrored text
        cv2.putText(pg, "tnetnoc edis kcab", (200, 300 + i * 90), cv2.FONT_HERSHEY_SIMPLEX, 1.0, 228, 2)
    assert ops.ink_coverage(pg) < 0.001
    cv2.putText(pg, "Real text on the page, several words long", (150, 1500), cv2.FONT_HERSHEY_SIMPLEX, 1.0, 30, 2)
    cv2.putText(pg, "and a second line of it", (150, 1550), cv2.FONT_HERSHEY_SIMPLEX, 1.0, 30, 2)
    assert ops.ink_coverage(pg) > 0.004


def test_scanned_flyer_front_and_blank_back() -> None:
    profile = ProfileSettings()
    assert effective_clean_background(profile)
    front, back = _scanned_front(), _scanned_back()
    assert measure_ink_fast(back, profile) < profile.processing.blank_threshold / 10
    assert measure_ink_fast(front, profile) > 0.05
    clean_back = finish_page(back, PageEdits(), profile)
    assert (ops.to_gray(clean_back) < 250).mean() < 0.002  # practically pure white
    clean_front = finish_page(front, PageEdits(), profile)
    g = ops.to_gray(clean_front)
    assert g[-3:, :].min() > 240  # truncated grey rows removed
    assert g[100:-100, :8].min() > 240  # backdrop strip removed
    assert g.min() < 40  # text kept
    bw = ops.enhance(back, ops.Enhance(color="bw", clean_background=True))
    assert (bw == 0).mean() < 0.001


def test_clean_background_modes_and_page_override() -> None:
    photo = ProfileSettings(intent="Photo")
    assert not effective_clean_background(photo)
    assert not effective_clean_background(ProfileSettings(processing=ProcessingOptions(clean_background="off")))
    assert effective_clean_background(ProfileSettings(intent="Photo",
                                                      processing=ProcessingOptions(clean_background="on")))
    back = _scanned_back()
    kept = finish_page(back, PageEdits(clean_background=False), ProfileSettings())
    assert (ops.to_gray(kept) < 250).mean() > 0.01  # per-page override disables cleanup


# ---------------------------------------------------------------- integration
async def test_blank_pages_flagged_before_processing(harness) -> None:  # noqa: ANN001
    """Blank pages are marked while the document waits for review (not only after processing)."""
    c = await harness.login()
    harness.sim_state.load("with_blanks")
    pid = await builtin_profile_id(c)
    r = await c.post("/api/scans", json={"profile_id": pid, "overrides": {**FAST, "source": "adf_simplex"}})
    assert r.status_code == 201, r.text
    doc = await harness.wait_status(c, r.json()["id"], "stored")
    flags: list[bool] = []
    for _ in range(100):
        doc = (await c.get(f"/api/documents/{doc['id']}")).json()
        if all("ink" in p["analysis"] for p in doc["pages"]):
            flags = [p["is_blank"] for p in doc["pages"]]
            break
        await __import__("asyncio").sleep(0.1)
    assert flags == [False, True, False, True]
    # keeping one of them overrides the removal
    blank_id = doc["pages"][1]["id"]
    r = await c.patch(f"/api/pages/{blank_id}", json={"keep_blank": True})
    assert r.status_code == 200
    r = await c.post(f"/api/documents/{doc['id']}/finish")
    assert r.status_code == 200, r.text
    doc = await harness.wait_status(c, doc["id"], "done")
    assert any(e["code"] == "process.blank_removed" for e in doc["events"])
    r = await c.get(f"/api/documents/{doc['id']}/download")
    import pikepdf
    assert len(pikepdf.open(__import__("io").BytesIO(r.content)).pages) == 3


@pytest.mark.parametrize("blank_removal", ["off", "scanner"])
async def test_no_early_blank_flag_without_software_removal(harness, blank_removal: str) -> None:  # noqa: ANN001
    c = await harness.login()
    harness.sim_state.load("with_blanks")
    pid = await builtin_profile_id(c)
    overrides = {**FAST, "source": "adf_simplex",
                 "processing": {**FAST["processing"], "blank_removal": blank_removal}}
    r = await c.post("/api/scans", json={"profile_id": pid, "overrides": overrides})
    assert r.status_code == 201, r.text
    doc = await harness.wait_status(c, r.json()["id"], "stored")
    await __import__("asyncio").sleep(0.5)
    doc = (await c.get(f"/api/documents/{doc['id']}")).json()
    assert not any(p["is_blank"] for p in doc["pages"])
