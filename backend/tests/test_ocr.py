"""OCR pipeline (needs tesseract + ocrmypdf + ghostscript; always runs in the Docker test image/CI)."""

from __future__ import annotations

import io

import pikepdf

from .conftest import builtin_profile_id, requires_ocr

pytestmark = requires_ocr


async def _process(h, c, stack: str, overrides: dict) -> dict:  # noqa: ANN001
    h.sim_state.load(stack)
    pid = await builtin_profile_id(c)
    r = await c.post("/api/scans", json={"profile_id": pid, "overrides": {"source": "adf_simplex", **overrides}})
    assert r.status_code == 201, r.text
    doc = await h.wait_status(c, r.json()["id"], "stored")
    await c.post(f"/api/documents/{doc['id']}/finish")
    return await h.wait_status(c, doc["id"], "done", timeout=300)


async def test_searchable_pdfa_and_fulltext(harness) -> None:  # noqa: ANN001
    c = await harness.login()
    doc = await _process(harness, c, "letter_de", {"resolution": 300, "output": {"format": "pdfa"}})
    r = await c.get(f"/api/documents/{doc['id']}/download")
    pdf = pikepdf.open(io.BytesIO(r.content))
    meta = pdf.open_metadata()
    assert meta.pdfa_status.startswith("2"), meta.pdfa_status
    text = (await c.get(f"/api/documents/{doc['id']}/text")).text
    assert len(text) > 200
    word = max(text.split(), key=len).strip(".,:;")
    hits = (await c.get("/api/documents", params={"q": word})).json()["items"]
    assert [d["id"] for d in hits] == [doc["id"]]
    assert doc["title"]  # suggested from OCR or template


async def test_auto_orientation_and_deskew(harness) -> None:  # noqa: ANN001
    c = await harness.login()
    doc = await _process(harness, c, "skewed", {"resolution": 200, "output": {"format": "pdf"},
                                               "processing": {"auto_rotate": True, "deskew": True}})
    rotations = [p["analysis"].get("applied_rotate") for p in doc["pages"]]
    angles = [abs(p["analysis"].get("applied_angle") or 0) for p in doc["pages"]]
    assert 180 in rotations, rotations  # the upside-down page was turned
    assert any(a >= 2 for a in angles), angles  # skewed pages were straightened


async def test_ocr_text_output(harness) -> None:  # noqa: ANN001
    c = await harness.login()
    doc = await _process(harness, c, "letter_de", {"resolution": 300, "output": {"format": "txt"}})
    r = await c.get(f"/api/documents/{doc['id']}/download")
    assert r.headers["content-type"].startswith("text/plain")
    assert len(r.text) > 200


async def test_target_size_compression(harness) -> None:  # noqa: ANN001
    c = await harness.login()
    doc = await _process(harness, c, "photo", {"resolution": 300, "color_mode": "color",
                                              "ocr": {"enabled": False},
                                              "output": {"format": "pdf", "compression": "none",
                                                         "target_size_mb": 0.2}})
    art = next(a for a in doc["artifacts"] if a["kind"] == "pdf")
    assert art["size"] <= 0.2 * 1024 * 1024 or any(e["code"] == "process.target_size_missed" for e in doc["events"])
