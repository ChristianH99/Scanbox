"""Editor: page edits + previews (non-destructive), reorder, delete/restore, split, merge, insert,
document details, profile switch, QR detection, reprocess."""

from __future__ import annotations

import io

import pikepdf
from PIL import Image

from .conftest import Api, download, finish, pdf_pages, profile_id, scan, sim, wait


def _stored(admin: Api, stack: str = "letter_de", **ov: object) -> dict:
    sim(stack)
    pid = profile_id(admin, "Dokument (PDF/A")
    d = scan(admin, pid, {"source": "adf_simplex", **ov})
    return wait(admin, d["id"], "stored")


def _img(admin: Api, page_id: str, **params: str) -> Image.Image:
    r = admin.ok("GET", f"/api/pages/{page_id}/image", params=params)
    assert r.headers["content-type"] == "image/jpeg"
    return Image.open(io.BytesIO(r.content))


def test_page_edits_preview_and_result(admin: Api) -> None:
    d = _stored(admin)
    p0, p1, p2 = (p["id"] for p in d["pages"])
    raw = _img(admin, p0, variant="raw", size="preview")
    # unsaved preview: rotate 90° → landscape
    r = admin.ok("POST", f"/api/pages/{p0}/preview", json={"rotate": 90})
    prev = Image.open(io.BytesIO(r.content))
    assert prev.width > prev.height and raw.height > raw.width
    # ETag / 304
    r1 = admin.ok("GET", f"/api/pages/{p1}/image", params={"size": "thumb"})
    r2 = admin.get(f"/api/pages/{p1}/image", params={"size": "thumb"}, headers={"If-None-Match": r1.headers["etag"]})
    assert r2.status_code == 304
    # save edits: rotate p0, crop p1 to the top half, B/W + no cleanup on p2
    admin.ok("PATCH", f"/api/pages/{p0}", json={"edits": {"rotate": 90}})
    admin.ok("PATCH", f"/api/pages/{p1}", json={"edits": {"crop": {"x": 0, "y": 0, "w": 1, "h": 0.5}, "angle": 2.0}})
    admin.ok("PATCH", f"/api/pages/{p2}", json={"edits": {"color": "bw", "clean_background": False,
                                                          "brightness": 0.1, "contrast": 0.2, "sharpen": True}})
    r3 = admin.ok("GET", f"/api/pages/{p1}/image", params={"size": "thumb"})
    assert r3.headers["etag"] != r1.headers["etag"]  # edit invalidates the render
    crop = _img(admin, p1, size="preview")
    assert crop.width > crop.height  # top half of a portrait page is landscape
    # invalid edits are rejected
    assert admin.patch(f"/api/pages/{p0}", json={"edits": {"rotate": 45}}).status_code == 422
    d = finish(admin, d["id"])
    pdf = pikepdf.open(io.BytesIO(download(admin, d["id"]).content))
    w0, h0 = (float(x) for x in pdf.pages[0].mediabox[2:])
    w1, h1 = (float(x) for x in pdf.pages[1].mediabox[2:])
    assert w0 > h0, "rotated page must be landscape"
    assert w1 > h1, "cropped page must be landscape"
    # originals untouched: raw is still portrait
    assert _img(admin, p0, variant="raw", size="thumb").height > _img(admin, p0, variant="raw", size="thumb").width


def test_reorder_delete_restore(admin: Api) -> None:
    d = _stored(admin)
    ids = [p["id"] for p in d["pages"]]
    admin.ok("POST", f"/api/documents/{d['id']}/reorder", json={"page_ids": list(reversed(ids))})
    admin.ok("POST", f"/api/documents/{d['id']}/pages/delete", json={"page_ids": [ids[1]]})
    d2 = admin.j("GET", f"/api/documents/{d['id']}")
    assert d2["page_count"] == 2
    admin.ok("POST", f"/api/documents/{d['id']}/pages/restore", json={"page_ids": [ids[1]]})
    admin.ok("POST", f"/api/documents/{d['id']}/pages/delete", json={"page_ids": [ids[0]]})
    d2 = admin.j("GET", f"/api/documents/{d['id']}")
    live = [p["id"] for p in d2["pages"] if not p["deleted"]]
    assert live == [ids[2], ids[1]], live
    d2 = finish(admin, d["id"])
    assert pdf_pages(download(admin, d["id"]).content) == 2


def test_split_merge_insert(admin: Api) -> None:
    a = _stored(admin)
    ids = [p["id"] for p in a["pages"]]
    r = admin.j("POST", f"/api/documents/{a['id']}/split", json={"before_page_ids": [ids[2]]})
    a = admin.j("GET", f"/api/documents/{a['id']}")
    assert a["page_count"] == 2
    assert len(r["children"]) == 1
    b_id = r["children"][0]
    assert admin.j("GET", f"/api/documents/{b_id}")["page_count"] == 1
    # insert (copy) one page of b into a
    b = admin.j("GET", f"/api/documents/{b_id}")
    admin.ok("POST", f"/api/documents/{a['id']}/pages/insert",
             json={"source_document_id": b_id, "page_ids": [b["pages"][0]["id"]], "copy": True})
    assert admin.j("GET", f"/api/documents/{a['id']}")["page_count"] == 3
    # merge b into a → b discarded
    admin.ok("POST", f"/api/documents/{a['id']}/merge", json={"source_document_ids": [b_id]})
    assert admin.j("GET", f"/api/documents/{a['id']}")["page_count"] == 4
    assert admin.j("GET", f"/api/documents/{b_id}")["status"] == "discarded"
    finish(admin, a["id"])
    assert pdf_pages(download(admin, a["id"]).content) == 4


def test_details_profile_switch_and_reprocess(admin: Api) -> None:
    d = _stored(admin)
    bw = profile_id(admin, "Schwarzweiß")
    d = admin.j("PATCH", f"/api/documents/{d['id']}", json={"title": "Systest Rechnung", "document_date": "2026-01-15",
                                                            "profile_id": bw})
    assert d["profile_id"] == bw and d["profile_snapshot"]["color_mode"] in ("bw", "gray")
    assert admin.patch(f"/api/documents/{d['id']}", json={"document_date": "15.01.2026"}).status_code == 422
    d = finish(admin, d["id"])
    assert "Systest Rechnung" in d["filename"] or "Systest_Rechnung" in d["filename"], d["filename"]
    first = d["filename"]
    # "Erneut senden": same files delivered again
    n = len(d["deliveries"])
    admin.ok("POST", f"/api/documents/{d['id']}/resend")
    d = wait(admin, d["id"], "done", cond=lambda x: len(x["deliveries"]) > n or x["deliveries"][-1]["attempts"] > 1)
    assert d["filename"] == first
    # "Erneut verarbeiten" with a new title → re-rendered from the originals, new file name
    admin.ok("PATCH", f"/api/documents/{d['id']}", json={"title": "Neuer Titel"})
    admin.ok("POST", f"/api/documents/{d['id']}/finish")
    d = wait(admin, d["id"], "done", cond=lambda x: x["filename"] != first)
    assert "Neuer" in d["filename"]


def test_qr_detect_and_manual_value(admin: Api) -> None:
    d = _stored(admin, "invoice_qr")
    r = admin.j("POST", f"/api/documents/{d['id']}/qr/detect", json={"zone": {"x": 0, "y": 0, "w": 1, "h": 1}})
    assert r["hits"], r
    d = admin.j("PATCH", f"/api/documents/{d['id']}", json={"qr_value": "MANUELL-42", "add_qr_to_filename": True})
    assert d["qr_value"] == "MANUELL-42" and d["qr_source"] == "manual"
    d = finish(admin, d["id"])
    assert "MANUELL-42" in d["filename"]


def test_keep_blank_override(admin: Api) -> None:
    sim("with_blanks")
    pid = profile_id(admin, "Dokument (PDF/A")
    from .conftest import scan as _scan

    d = _scan(admin, pid, {"source": "adf_simplex"})
    d = wait(admin, d["id"], "stored", cond=lambda x: all("ink" in p["analysis"] for p in x["pages"]))
    blank = [p for p in d["pages"] if p["is_blank"]]
    assert len(blank) == 2
    admin.ok("PATCH", f"/api/pages/{blank[0]['id']}", json={"keep_blank": True})
    d = finish(admin, d["id"])
    assert pdf_pages(download(admin, d["id"]).content) == 3


def test_discard_and_history_filters(admin: Api) -> None:
    d = _stored(admin)
    admin.ok("POST", f"/api/documents/{d['id']}/discard")
    assert admin.j("GET", f"/api/documents/{d['id']}")["status"] == "discarded"
    # discarded documents can't be edited or finished any more
    r = admin.post(f"/api/documents/{d['id']}/pages/delete", json={"page_ids": [d["pages"][0]["id"]]})
    assert r.status_code == 409, r.text
    assert admin.post(f"/api/documents/{d['id']}/finish").status_code == 409
    # … and are not listed in the history
    assert d["id"] not in [x["id"] for x in admin.j("GET", "/api/documents", params={"limit": 200})["items"]]
    lst = admin.j("GET", "/api/documents", params={"limit": 5})
    assert lst["items"] and "total" in lst
