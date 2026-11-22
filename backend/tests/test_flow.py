"""End-to-end flows against the eSCL simulator: scan → store → process → deliver."""

from __future__ import annotations

import pikepdf
import pytest

from .conftest import FAST, builtin_profile_id


async def start(c, profile_id, overrides=None, **kw):  # noqa: ANN001, ANN201
    body = {"profile_id": profile_id, "overrides": {**FAST, **(overrides or {})}, **kw}
    r = await c.post("/api/scans", json=body)
    assert r.status_code == 201, r.text
    return r.json()


async def test_scan_review_finish_download(harness) -> None:  # noqa: ANN001
    c = await harness.login()
    pid = await builtin_profile_id(c)
    doc = await start(c, pid, {"source": "adf_simplex"})
    doc = await harness.wait_status(c, doc["id"], "stored")
    assert doc["page_count"] == 3
    assert len(doc["pages"]) == 3
    assert [b["status"] for b in doc["batches"]] == ["done"]
    # raw thumbnails are served
    r = await c.get(f"/api/pages/{doc['pages'][0]['id']}/image", params={"variant": "raw", "size": "thumb"})
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"

    r = await c.post(f"/api/documents/{doc['id']}/finish")
    assert r.status_code == 200, r.text
    doc = await harness.wait_status(c, doc["id"], "done")
    assert doc["filename"].endswith(".pdf")
    assert [d["status"] for d in doc["deliveries"]] == ["done"]
    r = await c.get(f"/api/documents/{doc['id']}/download")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    pdf = pikepdf.open(__import__("io").BytesIO(r.content))
    assert len(pdf.pages) == 3


async def test_multi_batch_and_duplex(harness) -> None:  # noqa: ANN001
    c = await harness.login()
    pid = await builtin_profile_id(c)
    doc = await start(c, pid, {"source": "adf_duplex", "processing": {"blank_removal": "off"}})
    doc = await harness.wait_status(c, doc["id"], "stored")
    assert doc["page_count"] == 6  # 3 sheets, front+back
    assert [p["side"] for p in doc["pages"]][:2] == ["front", "back"]
    harness.sim_state.load("letter_de")
    r = await c.post(f"/api/documents/{doc['id']}/batches")
    assert r.status_code == 200, r.text
    doc = await harness.wait_status(c, doc["id"], "stored")
    assert doc["page_count"] == 12
    assert len(doc["batches"]) == 2


async def test_auto_finish_api_token_flow(harness) -> None:  # noqa: ANN001
    c = await harness.login()
    r = await c.post("/api/me/tokens", json={"name": "home-assistant"})
    assert r.status_code == 201
    token = r.json()["token"]
    api = harness.client()
    api.headers["Authorization"] = f"Bearer {token}"
    pid = await builtin_profile_id(c)
    r = await api.post("/api/scans", json={"profile_id": pid, "overrides": FAST}, headers={"Idempotency-Key": "k1"})
    assert r.status_code == 201, r.text
    doc_id = r.json()["id"]
    # same idempotency key → same document, no second scan
    r2 = await api.post("/api/scans", json={"profile_id": pid, "overrides": FAST}, headers={"Idempotency-Key": "k1"})
    assert r2.json()["id"] == doc_id
    doc = await harness.wait_status(api, doc_id, "done")
    assert doc["origin"] == "api"


async def test_empty_feeder_fails_clearly(harness) -> None:  # noqa: ANN001
    c = await harness.login()
    harness.sim_state.load_empty()
    pid = await builtin_profile_id(c)
    doc = await start(c, pid, {"source": "adf_simplex"})
    doc = await harness.wait_status(c, doc["id"], "failed")
    assert doc["error_code"] == "feeder_empty"
    # retry after loading paper: add a batch on the failed document
    harness.sim_state.load("letter_de")
    r = await c.post(f"/api/documents/{doc['id']}/batches")
    assert r.status_code == 200, r.text
    doc = await harness.wait_status(c, doc["id"], "stored")
    assert doc["page_count"] == 3


async def test_jam_keeps_received_pages(harness) -> None:  # noqa: ANN001
    c = await harness.login()
    harness.sim_state.load("long_stack")
    harness.sim_state.faults.jam_after_pages = 2
    pid = await builtin_profile_id(c)
    doc = await start(c, pid, {"source": "adf_simplex"})
    doc = await harness.wait_status(c, doc["id"], "needs_attention")
    assert doc["error_code"] == "paper_jam"
    assert doc["page_count"] == 2
    assert doc["batches"][0]["status"] == "failed"
    # after clearing the jam the user scans the rest into the same document
    harness.sim_state.clear_jam()
    r = await c.post(f"/api/documents/{doc['id']}/batches")
    assert r.status_code == 200, r.text
    doc = await harness.wait_status(c, doc["id"], "stored")
    assert doc["page_count"] > 2


async def test_dropped_connection_is_not_silent(harness) -> None:  # noqa: ANN001
    c = await harness.login()
    harness.sim_state.faults.drop_connection_after_pages = 1
    pid = await builtin_profile_id(c)
    doc = await start(c, pid, {"source": "adf_simplex"})
    doc = await harness.wait_status(c, doc["id"], {"needs_attention", "failed"})
    assert doc["error_code"] == "connection_lost"
    assert doc["page_count"] == 1


async def test_scanner_offline(harness) -> None:  # noqa: ANN001
    c = await harness.login()
    harness.sim_state.faults.offline = True
    harness.sim_state.faults.sticky = True
    pid = await builtin_profile_id(c)
    doc = await start(c, pid)
    doc = await harness.wait_status(c, doc["id"], "failed", timeout=90)
    assert doc["error_code"] == "scanner_offline"


async def test_qr_value_in_filename(harness) -> None:  # noqa: ANN001
    c = await harness.login()
    harness.sim_state.load("invoice_qr")
    pid = await builtin_profile_id(c, "Dokument mit QR")
    doc = await start(c, pid, {"source": "adf_simplex", "resolution": 200})
    doc = await harness.wait_status(c, doc["id"], "stored")
    r = await c.post(f"/api/documents/{doc['id']}/qr/detect", json={})
    assert r.json()["selected"] == "INV-2024-0042"
    await c.post(f"/api/documents/{doc['id']}/finish")
    doc = await harness.wait_status(c, doc["id"], "done")
    assert doc["qr_value"] == "INV-2024-0042"
    assert doc["filename"].startswith("INV-2024-0042_")


async def test_qr_zone_ignores_other_codes(harness) -> None:  # noqa: ANN001
    c = await harness.login()
    harness.sim_state.load("multi_qr")
    pid = await builtin_profile_id(c, "Dokument mit QR")
    doc = await start(c, pid, {"source": "adf_simplex", "resolution": 200})
    doc = await harness.wait_status(c, doc["id"], "stored")
    await c.post(f"/api/documents/{doc['id']}/finish")
    doc = await harness.wait_status(c, doc["id"], "done")
    assert doc["qr_value"] == "ZONE-123"


async def test_qr_block_then_manual_value(harness) -> None:  # noqa: ANN001
    c = await harness.login()
    pid = await builtin_profile_id(c, "Dokument mit QR")
    doc = await start(c, pid, {"source": "adf_simplex", "qr": {"on_missing": "block"}})
    doc = await harness.wait_status(c, doc["id"], "stored")
    await c.post(f"/api/documents/{doc['id']}/finish")
    doc = await harness.wait_status(c, doc["id"], "needs_attention")
    assert doc["error_code"] == "qr_missing"
    r = await c.patch(f"/api/documents/{doc['id']}", json={"qr_value": "MANUAL-7"})
    assert r.status_code == 200
    await c.post(f"/api/documents/{doc['id']}/finish")
    doc = await harness.wait_status(c, doc["id"], "done")
    assert doc["qr_value"] == "MANUAL-7"
    assert doc["filename"].startswith("MANUAL-7_")


async def test_folder_destination(harness, tmp_path) -> None:  # noqa: ANN001
    out = tmp_path / "nas-out"
    out.mkdir()
    c = await harness.login()
    r = await c.patch("/api/admin/settings", json={"values": {"output.roots": {"Scans": str(out)}}})
    assert r.status_code == 200, r.text
    pid = await builtin_profile_id(c)
    dests = [{"type": "folder", "config": {"root": "Scans", "subfolder": "{user}/{year}"}}]
    doc = await start(c, pid, {"source": "adf_simplex", "destinations": dests, "filename_template": "scan"})
    await harness.wait_status(c, doc["id"], "stored")
    await c.post(f"/api/documents/{doc['id']}/finish")
    doc = await harness.wait_status(c, doc["id"], "done")
    files = list(out.rglob("*.pdf"))
    assert len(files) == 1 and files[0].name == "scan.pdf"
    # resend → no overwrite, suffix instead
    r = await c.post(f"/api/documents/{doc['id']}/resend")
    assert r.status_code == 200, r.text
    await harness.wait_status(c, doc["id"], "done")
    names = sorted(p.name for p in out.rglob("*.pdf"))
    assert names == ["scan (2).pdf", "scan.pdf"]


async def test_folder_destination_rejects_traversal(harness, tmp_path) -> None:  # noqa: ANN001
    out = tmp_path / "nas"
    out.mkdir()
    c = await harness.login()
    await c.patch("/api/admin/settings", json={"values": {"output.roots": {"Scans": str(out)}}})
    pid = await builtin_profile_id(c)
    dests = [{"type": "folder", "config": {"root": "Scans", "subfolder": "../../etc"}}]
    doc = await start(c, pid, {"source": "adf_simplex", "destinations": dests})
    await harness.wait_status(c, doc["id"], "stored")
    await c.post(f"/api/documents/{doc['id']}/finish")
    doc = await harness.wait_status(c, doc["id"], "needs_attention")
    assert doc["deliveries"][0]["error_code"] == "folder_invalid_subfolder"
    assert not list(out.rglob("*.pdf"))
    assert not (tmp_path / "etc").exists()


async def test_editor_reorder_delete_rotate(harness) -> None:  # noqa: ANN001
    c = await harness.login()
    pid = await builtin_profile_id(c)
    doc = await start(c, pid, {"source": "adf_simplex"})
    doc = await harness.wait_status(c, doc["id"], "stored")
    ids = [p["id"] for p in doc["pages"]]
    r = await c.post(f"/api/documents/{doc['id']}/reorder", json={"page_ids": ids[::-1]})
    assert [p["id"] for p in r.json()["pages"]] == ids[::-1]
    r = await c.post(f"/api/documents/{doc['id']}/pages/delete", json={"page_ids": [ids[0]]})
    assert r.json()["page_count"] == 2
    r = await c.patch(f"/api/pages/{ids[1]}", json={"edits": {"rotate": 90}})
    assert r.status_code == 200 and r.json()["edits"]["rotate"] == 90
    r = await c.post(f"/api/pages/{ids[1]}/preview", json={"rotate": 180, "color": "gray"})
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    await c.post(f"/api/documents/{doc['id']}/finish")
    doc = await harness.wait_status(c, doc["id"], "done")
    assert doc["page_count"] == 2


async def test_split_and_merge(harness) -> None:  # noqa: ANN001
    c = await harness.login()
    pid = await builtin_profile_id(c)
    doc = await start(c, pid, {"source": "adf_simplex"})
    doc = await harness.wait_status(c, doc["id"], "stored")
    ids = [p["id"] for p in doc["pages"]]
    r = await c.post(f"/api/documents/{doc['id']}/split", json={"before_page_ids": [ids[2]]})
    assert r.status_code == 200, r.text
    child = r.json()["children"][0]
    d1 = (await c.get(f"/api/documents/{doc['id']}")).json()
    d2 = (await c.get(f"/api/documents/{child}")).json()
    assert d1["page_count"] == 2 and d2["page_count"] == 1
    r = await c.post(f"/api/documents/{doc['id']}/merge", json={"source_document_ids": [child]})
    assert r.json()["page_count"] == 3
    assert (await c.get(f"/api/documents/{child}")).json()["status"] == "discarded"


async def test_search_history(harness) -> None:  # noqa: ANN001
    c = await harness.login()
    pid = await builtin_profile_id(c)
    doc = await start(c, pid, {"source": "adf_simplex"}, title="Stromrechnung Mai")
    await harness.wait_status(c, doc["id"], "stored")
    r = await c.get("/api/documents", params={"q": "strom"})
    assert [d["id"] for d in r.json()["items"]] == [doc["id"]]
    r = await c.get("/api/documents", params={"q": "wasser"})
    assert r.json()["items"] == []


@pytest.mark.parametrize("fmt", ["jpeg", "tiff", "png"])
async def test_image_outputs(harness, fmt) -> None:  # noqa: ANN001
    c = await harness.login()
    pid = await builtin_profile_id(c)
    doc = await start(c, pid, {"source": "adf_simplex", "output": {"format": fmt}})
    await harness.wait_status(c, doc["id"], "stored")
    await c.post(f"/api/documents/{doc['id']}/finish")
    doc = await harness.wait_status(c, doc["id"], "done")
    r = await c.get(f"/api/documents/{doc['id']}/download")
    assert r.status_code == 200
    expected = {"jpeg": "application/zip", "png": "application/zip", "tiff": "image/tiff"}[fmt]
    assert r.headers["content-type"] == expected


async def test_cancel_running_scan_keeps_pages(harness) -> None:  # noqa: ANN001
    import asyncio

    c = await harness.login()
    harness.sim_state.load("long_stack")
    harness.sim_state.set_faults(slow_ms=300, sticky=True)
    pid = await builtin_profile_id(c)
    doc = await start(c, pid, {"source": "adf_simplex"})
    for _ in range(200):
        d = (await c.get(f"/api/documents/{doc['id']}")).json()
        if d["page_count"] >= 2:
            break
        await asyncio.sleep(0.05)
    r = await c.post(f"/api/documents/{doc['id']}/cancel")
    assert r.status_code == 200, r.text
    d = await harness.wait_status(c, doc["id"], "stored")
    assert 2 <= d["page_count"] < 30
    assert d["batches"][0]["status"] == "cancelled"
    assert any(e["code"] == "scan.cancelled" for e in d["events"])


async def test_cancel_queued_scan(harness) -> None:  # noqa: ANN001
    c = await harness.login()
    harness.sim_state.set_faults(slow_ms=3000, sticky=True)
    pid = await builtin_profile_id(c)
    first = await start(c, pid, {"source": "adf_simplex"})
    second = await start(c, pid, {"source": "adf_simplex"})
    assert second["status"] == "queued"
    r = await c.post(f"/api/documents/{second['id']}/cancel")
    assert r.status_code == 200 and r.json()["status"] == "discarded"
    await c.post(f"/api/documents/{first['id']}/cancel")


async def test_list_thumbnail_etag_and_login_csrf(harness) -> None:  # noqa: ANN001
    c = harness.client()
    from .conftest import ADMIN_PW

    r = await c.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PW})
    assert r.json()["csrf_token"] == c.cookies["scanbox_csrf"]
    c.headers["X-CSRF-Token"] = c.cookies["scanbox_csrf"]
    pid = await builtin_profile_id(c)
    doc = await start(c, pid, {"source": "adf_simplex"})
    doc = await harness.wait_status(c, doc["id"], "stored")
    items = (await c.get("/api/documents")).json()["items"]
    assert items[0]["first_page_id"] == doc["pages"][0]["id"]
    url = f"/api/pages/{doc['pages'][0]['id']}/image"
    r1 = await c.get(url, params={"size": "thumb"})
    etag = r1.headers["etag"]
    r2 = await c.get(url, params={"size": "thumb"}, headers={"If-None-Match": etag})
    assert r2.status_code == 304
    await c.patch(f"/api/pages/{doc['pages'][0]['id']}", json={"edits": {"rotate": 90}})
    r3 = await c.get(url, params={"size": "thumb"}, headers={"If-None-Match": etag})
    assert r3.status_code == 200 and r3.headers["etag"] != etag
