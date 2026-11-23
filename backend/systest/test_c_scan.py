"""Scanning: real OCR → PDF/A, duplex + blank removal, multi-stack, cancel, scanner faults, QR, outputs."""

from __future__ import annotations

import io
import time
import zipfile

import httpx
import pikepdf
import pytest

from .conftest import (
    BASE,
    SIM,
    Api,
    download,
    events,
    finish,
    pdf_pages,
    profile_id,
    scan,
    sim,
    sim_faults,
    wait,
)


def test_ocr_pdfa_title_and_search(admin: Api) -> None:
    sim("letter_de")
    pid = profile_id(admin, "Dokument (PDF/A")
    t0 = time.time()
    d = scan(admin, pid, {"source": "adf_simplex"}, fast=False)
    d = wait(admin, d["id"], "stored")
    assert d["page_count"] == 3
    d = finish(admin, d["id"])
    took = time.time() - t0
    assert took < 180, f"OCR of 3 pages took {took:.0f}s"
    r = download(admin, d["id"])
    assert r.headers["content-type"] == "application/pdf"
    pdf = pikepdf.open(io.BytesIO(r.content))
    assert len(pdf.pages) == 3
    meta = pdf.open_metadata()
    assert meta.get("pdfaid:part") == "2", dict(meta)
    # OCR text layer: the scanned pages are pure images, so any font is OCRmyPDF's invisible text
    # (GlyphLessFont or a NotoSans subset depending on the version; compressed object streams → walk objects)
    fonts = {str(o.get("/BaseFont")) for o in pdf.objects
             if isinstance(o, pikepdf.Dictionary) and o.get("/Type") == pikepdf.Name.Font}
    assert fonts, "no text layer"
    text = admin.ok("GET", f"/api/documents/{d['id']}/text").text
    assert len(text) > 500 and "Sehr geehrte" in text, text[:300]
    assert d["title_suggestion"] or d["title"], d
    assert d["filename"].endswith(".pdf")
    # full-text search finds it, other words don't
    word = "geehrte"
    hits = admin.j("GET", "/api/documents", params={"q": word})["items"]
    assert d["id"] in [h["id"] for h in hits]
    assert admin.j("GET", "/api/documents", params={"q": "xylophonquartett"})["items"] == []


def test_duplex_blank_pages_flagged_early_and_removed(admin: Api) -> None:
    sim("with_blanks")
    pid = profile_id(admin, "Dokument (PDF/A")
    d = scan(admin, pid, {"source": "adf_duplex"})
    d = wait(admin, d["id"], "stored", cond=lambda x: all("ink" in p["analysis"] for p in x["pages"]))
    assert d["page_count"] == 8
    blank = [p["is_blank"] for p in d["pages"]]
    assert blank == [False, True, True, True, False, True, True, True], blank
    assert [p["side"] for p in d["pages"]][:2] == ["front", "back"]
    d = finish(admin, d["id"])
    assert "process.blank_removed" in events(d)
    assert pdf_pages(download(admin, d["id"]).content) == 2


def test_multi_stack_then_finish(admin: Api) -> None:
    sim("letter_de")
    pid = profile_id(admin, "Dokument (PDF/A")
    d = scan(admin, pid, {"source": "adf_simplex"})
    wait(admin, d["id"], "stored")
    sim("letter_en")
    admin.ok("POST", f"/api/documents/{d['id']}/batches")
    d = wait(admin, d["id"], "stored", cond=lambda x: len(x["batches"]) == 2 and x["batches"][1]["status"] == "done")
    assert d["page_count"] == 6
    d = finish(admin, d["id"])
    assert pdf_pages(download(admin, d["id"]).content) == 6


@pytest.mark.parametrize(("fault", "code", "status"), [
    ({"jam_after_pages": 2}, "paper_jam", "needs_attention"),
    ({"multipick_after_pages": 1}, "multipick", "needs_attention"),
    ({"drop_connection_after_pages": 2}, "connection_lost", "needs_attention"),
    ({"hatch_open": True}, "cover_open", "failed"),
    ({"offline": True, "sticky": True}, "scanner_offline", "failed"),
])
def test_scanner_faults_keep_pages_and_explain(admin: Api, fault: dict, code: str, status: str) -> None:
    sim("long_stack", faults=fault)
    pid = profile_id(admin, "Dokument (PDF/A")
    d = scan(admin, pid, {"source": "adf_simplex"})
    end = time.time() + 120
    while time.time() < end:
        d = admin.j("GET", f"/api/documents/{d['id']}")
        if d["status"] in ("needs_attention", "failed", "stored"):
            break
        time.sleep(0.4)
    sim("letter_de")  # clear faults for the next tests
    assert d["status"] == status, (d["status"], events(d))
    assert d["error_code"] == code, (d["error_code"], events(d))
    if status == "needs_attention":
        assert d["page_count"] >= 1  # pages received before the fault are kept
        # recovery: continue with the remaining sheets, then finish
        admin.ok("POST", f"/api/documents/{d['id']}/batches")
        d = wait(admin, d["id"], "stored", cond=lambda x: x["batches"][-1]["status"] == "done")
        admin.ok("POST", f"/api/documents/{d['id']}/finish")
        wait(admin, d["id"], "done")
    else:
        admin.ok("POST", f"/api/documents/{d['id']}/discard", expect=(200, 204, 409))


def test_empty_feeder_then_load_and_rescan(admin: Api) -> None:
    sim(None)
    pid = profile_id(admin, "Dokument (PDF/A")
    d = scan(admin, pid, {"source": "adf_simplex"})
    end = time.time() + 60
    while admin.j("GET", f"/api/documents/{d['id']}")["status"] not in ("failed", "needs_attention"):
        assert time.time() < end
        time.sleep(0.3)
    d = admin.j("GET", f"/api/documents/{d['id']}")
    assert d["error_code"] == "feeder_empty"
    sim("letter_de")
    d2 = scan(admin, pid, {"source": "adf_simplex"})
    wait(admin, d2["id"], "stored")


def test_scanner_busy_then_free(admin: Api) -> None:
    sim("letter_de", faults={"busy": True, "sticky": True})
    pid = profile_id(admin, "Dokument (PDF/A")
    d = scan(admin, pid, {"source": "adf_simplex"})
    time.sleep(4)
    assert admin.j("GET", f"/api/documents/{d['id']}")["status"] in ("queued", "scanning")
    sim_faults({"busy": False})
    httpx.delete(f"{SIM}/_sim/faults").raise_for_status()
    wait(admin, d["id"], "stored", timeout=120)


def test_stop_running_scan_keeps_pages(admin: Api) -> None:
    sim("long_stack", faults={"slow_ms": 700, "slow_style": "block"})
    pid = profile_id(admin, "Dokument (PDF/A")
    d = scan(admin, pid, {"source": "adf_simplex"})
    wait(admin, d["id"], ("scanning",), cond=lambda x: x["page_count"] >= 2, timeout=60)
    admin.ok("POST", f"/api/documents/{d['id']}/cancel")
    d = wait(admin, d["id"], "stored", timeout=60)
    assert 2 <= d["page_count"] < 30
    sim("letter_de")


def test_queue_of_scans_one_at_a_time(admin: Api) -> None:
    sim("long_stack", faults={"slow_ms": 150, "slow_style": "block"})
    pid = profile_id(admin, "Dokument (PDF/A")
    a = scan(admin, pid, {"source": "adf_simplex"})
    b = scan(admin, pid, {"source": "adf_simplex"})
    wait(admin, a["id"], "scanning", cond=lambda x: x["page_count"] >= 1, timeout=60)
    db = admin.j("GET", f"/api/documents/{b['id']}")
    assert db["status"] == "queued" and db["queue_position"] >= 1
    admin.ok("POST", f"/api/documents/{b['id']}/cancel")  # cancel the queued one
    assert admin.j("GET", f"/api/documents/{b['id']}")["status"] in ("failed", "discarded")
    admin.ok("POST", f"/api/documents/{a['id']}/cancel")
    wait(admin, a["id"], "stored", timeout=60)
    sim("letter_de")


def test_qr_value_in_filename_and_zone(admin: Api) -> None:
    sim("invoice_qr")
    pid = profile_id(admin, "Dokument mit QR")
    d = scan(admin, pid, {"source": "adf_simplex"}, auto_finish=True)
    d = wait(admin, d["id"], "done", timeout=240)
    assert d["qr_value"], events(d)
    assert d["qr_value"] in d["filename"], (d["qr_value"], d["filename"])


def test_split_on_separator_sheets(admin: Api) -> None:
    sim("separator_sheets")  # separator sheets with QR DOC-A / DOC-B, each followed by a letter
    pid = profile_id(admin, "Dokument (PDF/A")
    d = scan(admin, pid, {"source": "adf_simplex", "qr": {"enabled": True, "zone_preset": "full", "pages": "each"},
                          "processing": {"split": {"mode": "qr_pattern", "pattern": "^DOC-",
                                                   "remove_separator": True}}})
    wait(admin, d["id"], "stored")
    admin.ok("POST", f"/api/documents/{d['id']}/finish")
    end = time.time() + 240
    children: list = []
    while time.time() < end:
        docs = admin.j("GET", "/api/documents", params={"limit": 100})["items"]
        children = [x for x in docs if x.get("parent_id") == d["id"]]
        parent = admin.j("GET", f"/api/documents/{d['id']}")
        if children and all(x["status"] == "done" for x in children) and parent["status"] in ("done", "discarded"):
            break
        time.sleep(1)
    # DOC-A stays in the original document, DOC-B becomes a child; separator sheets are removed
    assert len(children) == 1, [(x["id"], x.get("parent_id"), x["status"]) for x in docs[:8]]
    for doc_id in (d["id"], children[0]["id"]):
        assert pdf_pages(download(admin, doc_id).content) == 1
    assert {admin.j("GET", f"/api/documents/{i}")["qr_value"] for i in (d["id"], children[0]["id"])} == {"DOC-A", "DOC-B"}


@pytest.mark.parametrize(("fmt", "ctype"), [
    ("pdf", "application/pdf"), ("pdf_image", "application/pdf"), ("jpeg", None), ("png", None),
    ("tiff", "image/tiff"), ("txt", "text/plain"),
])
def test_output_formats(admin: Api, fmt: str, ctype: str | None) -> None:
    sim("letter_de")
    pid = profile_id(admin, "Dokument (PDF/A")
    ocr = fmt == "txt"
    d = scan(admin, pid, {"source": "adf_simplex", "output": {"format": fmt, "compression": "low"},
                          **({} if not ocr else {"ocr": {"enabled": True, "suggest_title": False}})},
             fast=True, auto_finish=True)
    d = wait(admin, d["id"], "done", timeout=300)
    r = download(admin, d["id"])
    if ctype:
        assert r.headers["content-type"].startswith(ctype), r.headers["content-type"]
    if fmt in ("jpeg", "png"):  # one image per page → zip (or single image)
        if r.headers["content-type"] == "application/zip":
            names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
            assert len(names) == 3, names
        else:
            assert r.headers["content-type"].startswith("image/")
    if fmt == "txt":
        assert len(r.text) > 300


def test_target_size_and_compression(admin: Api) -> None:
    sim("letter_de")
    pid = profile_id(admin, "Dokument (PDF/A")
    d = scan(admin, pid, {"source": "adf_simplex", "resolution": 300, "color_mode": "color",
                          "output": {"format": "pdf", "compression": "none", "target_size_mb": 0.25}},
             auto_finish=True)
    d = wait(admin, d["id"], "done", timeout=300)
    size = len(download(admin, d["id"]).content)
    assert size <= 0.25 * 1024 * 1024 or "process.target_size_missed" in events(d), size


def test_live_events_stream(admin: Api) -> None:
    """SSE delivers page.added while a scan runs."""
    sim("letter_de", faults={"slow_ms": 400, "slow_style": "block"})
    pid = profile_id(admin, "Dokument (PDF/A")
    seen: list[str] = []
    with httpx.Client(base_url=BASE, cookies=admin.cookies, timeout=30) as c, \
            c.stream("GET", "/api/events") as stream:
        assert stream.headers["content-type"].startswith("text/event-stream")
        d = scan(admin, pid, {"source": "adf_simplex"})
        end = time.time() + 30
        for line in stream.iter_lines():
            if line.startswith("event:"):
                seen.append(line.split(":", 1)[1].strip())
            if "page.added" in seen or time.time() > end:
                break
    assert "page.added" in seen, seen
    wait(admin, d["id"], "stored")
    sim("letter_de")
