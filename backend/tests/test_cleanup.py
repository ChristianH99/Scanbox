"""Background cleanup and blank detection – synthetic cases plus two real N4000 scans.

Fixtures (tests/fixtures/real, reduced to 150 dpi):
* n4000_front_flyer.jpg – printed flyer, fold crease, grey tint, light artwork, backdrop strip on the
  left edge and 4 truncated (grey) JPEG rows at the bottom, as delivered by the scanner;
* n4000_back_blank.jpg – the empty back side of it: crease, show-through, backdrop strip, dark corner.
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest

from scanbox.processing import imageops as ops
from scanbox.processing.render import PageEdits, finish_page, measure_ink_fast
from scanbox.profiles import ProcessingOptions, ProfileSettings, effective_clean_background

from .conftest import FAST, builtin_profile_id

REAL = Path(__file__).parent / "fixtures" / "real"
CLEAN = ops.Enhance(clean_background=True)


def _real(name: str) -> np.ndarray:
    img = cv2.imread(str(REAL / name))
    assert img is not None
    return img


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


def test_real_n4000_pages() -> None:
    profile = ProfileSettings()
    assert effective_clean_background(profile)
    front, back = _real("n4000_front_flyer.jpg"), _real("n4000_back_blank.jpg")
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
    back = _real("n4000_back_blank.jpg")
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
