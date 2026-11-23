"""Regression tests for findings of the independent review."""

from __future__ import annotations

import threading

from sqlalchemy import event

from scanbox.db import get_engine, session_scope
from scanbox.models import User
from scanbox.security.auth import LoginRateLimiter, ensure_local_admin

from .conftest import FAST, builtin_profile_id


async def test_no_transaction_on_event_loop_thread(harness, tmp_path) -> None:  # noqa: ANN001
    """Rule: DB transactions (BEGIN IMMEDIATE) never run on the event-loop thread."""
    loop_thread = threading.get_ident()
    offenders: list[str] = []

    def on_begin(conn) -> None:  # noqa: ANN001
        if threading.get_ident() == loop_thread:
            import traceback

            offenders.append("".join(traceback.format_stack(limit=12)))

    out = tmp_path / "out"
    out.mkdir()
    c = await harness.login()
    await c.patch("/api/admin/settings", json={"values": {"output.roots": {"Out": str(out)}}})
    pid = await builtin_profile_id(c)
    engine = get_engine()
    event.listen(engine, "begin", on_begin)
    try:
        dests = [{"type": "download"}, {"type": "folder", "config": {"root": "Out"}}]
        r = await c.post("/api/scans", json={"profile_id": pid, "overrides": {
            **FAST, "source": "adf_simplex", "destinations": dests, "qr": {"enabled": True}}})
        doc = await harness.wait_status(c, r.json()["id"], "stored")
        page = doc["pages"][0]["id"]
        assert (await c.get(f"/api/pages/{page}/image", params={"size": "thumb"})).status_code == 200
        assert (await c.post(f"/api/pages/{page}/preview", json={"rotate": 90})).status_code == 200
        assert (await c.post(f"/api/documents/{doc['id']}/qr/detect", json={})).status_code == 200
        await c.post(f"/api/documents/{doc['id']}/finish")
        await harness.wait_status(c, doc["id"], "done")
        assert (await c.get("/api/admin/diagnostics")).status_code == 200
        assert (await c.post("/api/admin/clear-cache")).status_code == 200
    finally:
        event.remove(engine, "begin", on_begin)
    assert not offenders, offenders[0]


async def test_clear_cache_keeps_unclaimed_inbox(harness) -> None:  # noqa: ANN001
    from scanbox.documents import create_scan, resolve_profile

    with session_scope() as s:
        doc = create_scan(s, harness.ctx, user=None, profile=resolve_profile(s, None, None),
                          overrides={**FAST, "source": "adf_simplex"}, origin="walkup", auto_finish=False)
        doc_id = doc.id
    harness.ctx.worker.notify()
    c = await harness.login()
    await harness.wait_status(c, doc_id, "stored")
    assert (await c.post("/api/admin/clear-cache")).status_code == 200
    items = (await c.get("/api/documents", params={"scope": "inbox"})).json()["items"]
    assert [d["id"] for d in items] == [doc_id] and items[0]["raw_expired"] is False


async def test_local_admin_never_takes_over_sso_account(harness) -> None:  # noqa: ANN001
    with session_scope() as s:
        s.add(User(username="boss", sub="oidc-boss", is_local=False, is_admin=False))
    ensure_local_admin("boss", "a-password-123")
    with session_scope() as s:
        u = s.query(User).filter_by(username="boss").one()
        assert u.is_local is False and u.is_admin is False and u.password_hash is None


def test_rate_limit_across_ips() -> None:
    lim = LoginRateLimiter(per_user=5, per_ip=20, per_name=8)
    for i in range(8):
        lim.failure(f"10.0.0.{i}", "admin")  # attacker rotating (forged) IPs
    assert lim.retry_after("10.9.9.9", "admin") > 0
    assert lim.retry_after("10.9.9.9", "someone-else") == 0


async def test_invalid_state_is_409_not_500(harness) -> None:  # noqa: ANN001
    c = await harness.login()
    harness.sim_state.set_faults(slow_ms=2000, sticky=True)
    pid = await builtin_profile_id(c)
    r = await c.post("/api/scans", json={"profile_id": pid, "overrides": {**FAST, "source": "adf_simplex"}})
    doc_id = r.json()["id"]
    await harness.wait_status(c, doc_id, "scanning")
    r = await c.post(f"/api/documents/{doc_id}/discard")
    assert r.status_code == 409
    r = await c.post(f"/api/documents/{doc_id}/merge", json={"source_document_ids": [doc_id]})
    assert r.status_code == 409


async def test_duplicate_contact_update_is_409(harness) -> None:  # noqa: ANN001
    c = await harness.login()
    a = (await c.post("/api/me/contacts", json={"email": "a@x.de"})).json()
    await c.post("/api/me/contacts", json={"email": "b@x.de"})
    r = await c.put(f"/api/me/contacts/{a['id']}", json={"email": "b@x.de"})
    assert r.status_code == 409


def test_webhook_target_filter() -> None:
    from scanbox.destinations.service import webhook_target_allowed

    assert not webhook_target_allowed("http://127.0.0.1:8080/x")
    assert not webhook_target_allowed("http://169.254.169.254/latest")
    assert not webhook_target_allowed("http://[::1]/x")
    assert not webhook_target_allowed("file:///etc/passwd")
    assert webhook_target_allowed("http://192.168.1.20:8123/api/webhook/x")


async def test_output_roots_cannot_point_into_data_dir(harness) -> None:  # noqa: ANN001
    c = await harness.login()
    for bad in (str(harness.data_dir), str(harness.data_dir / "raw"), "/etc"):
        r = await c.patch("/api/admin/settings", json={"values": {"output.roots": {"X": bad}}})
        assert r.status_code == 422, bad


# ------------------------------------------------------------------ found by the system test (systest/)
def test_subfolder_template_supports_date_formats() -> None:
    """The UI advertises '{user}/{date:%Y}' – it rendered as a literal '{date_%Y}' folder."""
    from datetime import datetime

    from scanbox.destinations.builtin import check_subfolder, render_subfolder
    from scanbox.filenames import TemplateVars

    v = TemplateVars(created=datetime(2026, 3, 9, 8, 5), qr="A/B", title="T", profile="P", user="Anna", pages=2)
    assert render_subfolder("{user}/{date:%Y}", v) == "Anna/2026"
    assert render_subfolder("{year}/{month}/Belege", v) == "2026/03/Belege"
    assert render_subfolder("{qr}", v) == "A_B"  # a value can't add path levels
    assert render_subfolder("", v) == ""
    assert check_subfolder("{user}/{date:%Y}") == []
    assert check_subfolder("../{user}")


async def test_profile_save_rejects_unusable_destinations(harness, tmp_path) -> None:  # noqa: ANN001
    out = tmp_path / "nas"
    out.mkdir()
    c = await harness.login()
    await c.patch("/api/admin/settings", json={"values": {"output.roots": {"Scans": str(out)}}})
    base = (await c.get("/api/profiles")).json()[0]["settings"]
    for dest in ({"type": "folder", "config": {"root": "../etc"}},
                 {"type": "folder", "config": {"root": "Scans", "subfolder": "../x"}},
                 {"type": "email", "config": {"recipients": ["not-an-address"]}},
                 {"type": "paperless", "config": {"mode": "ftp"}}):
        r = await c.post("/api/profiles", json={"name": "Bad", "settings": {**base, "destinations": [dest]}})
        assert r.status_code == 422, (dest, r.text)
        assert r.json()["detail"]["code"] == "invalid_destination"
    ok = {**base, "destinations": [{"type": "folder", "config": {"root": "Scans", "subfolder": "{user}/{date:%Y}"}}]}
    r = await c.post("/api/profiles", json={"name": "Good", "settings": ok})
    assert r.status_code == 201, r.text


async def test_log_download_while_the_log_grows(harness) -> None:  # noqa: ANN001
    """The log file grows while it's sent (this very request logs) – must not break Content-Length."""
    c = await harness.login()
    log_dir = harness.ctx.config.log_dir
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "scanbox.log").write_bytes(b"line\n" * 1000)
    r = await c.get("/api/admin/logs")
    assert r.status_code == 200
    assert int(r.headers["content-length"]) == len(r.content)
    assert r.content.startswith(b"line\n")


async def test_validator_exception_is_422_not_500(harness) -> None:  # noqa: ANN001
    """Field validators raising ValueError put the exception object into the error context, which
    the JSON error response could not serialise."""
    c = await harness.login()
    base = (await c.get("/api/profiles")).json()[0]["settings"]
    for bad in ({**base, "qr": {**base["qr"], "regex": "(["}},
                {**base, "processing": {**base["processing"], "split": {"mode": "qr_pattern", "pattern": "(["}}}):
        r = await c.post("/api/profiles", json={"name": "Bad", "settings": bad})
        assert r.status_code == 422, r.text
        body = r.json()["detail"]
        assert body["code"] == "validation_error" and "regex" in body["message"]
        assert all(set(e) == {"type", "loc", "msg"} for e in body["errors"])


async def test_split_at_separators_keeps_each_separators_qr(harness) -> None:  # noqa: ANN001
    """Automatic split with separator removal: every resulting document carries the QR value of the
    separator that started it (the child used to have none) – also after re-processing."""
    c = await harness.login()
    harness.sim_state.load("separator_sheets")  # [SEP DOC-A, letter, SEP DOC-B, letter]
    pid = await builtin_profile_id(c)
    ov = {**FAST, "source": "adf_simplex", "filename_template": "{qr}",
          "qr": {"enabled": True, "zone_preset": "full", "pages": "each"},
          "processing": {**FAST["processing"], "split": {"mode": "qr_pattern", "pattern": "^DOC-",
                                                         "remove_separator": True}}}
    r = await c.post("/api/scans", json={"profile_id": pid, "overrides": ov})
    doc = await harness.wait_status(c, r.json()["id"], "stored")
    await c.post(f"/api/documents/{doc['id']}/finish")
    parent = await harness.wait_status(c, doc["id"], "done")
    items = (await c.get("/api/documents", params={"limit": 50})).json()["items"]
    children = [x for x in items if x["parent_id"] == doc["id"]]
    assert len(children) == 1
    child = await harness.wait_status(c, children[0]["id"], "done")
    assert (parent["qr_value"], parent["page_count"]) == ("DOC-A", 1)
    assert (child["qr_value"], child["page_count"], child["qr_source"]) == ("DOC-B", 1, "separator")
    assert parent["filename"] == "DOC-A.pdf" and child["filename"] == "DOC-B.pdf"
    # re-processing (separator pages are gone by now) keeps the values
    for d in (parent, child):
        await c.post(f"/api/documents/{d['id']}/finish")
        again = await harness.wait_status(c, d["id"], "done")
        assert again["qr_value"] == d["qr_value"]


async def test_details_save_without_date(harness) -> None:  # noqa: ANN001
    """The details form sends document_date '' when no date is set – that used to be a 422, so a title
    could only be saved together with a date."""
    c = await harness.login()
    pid = await builtin_profile_id(c)
    r = await c.post("/api/scans", json={"profile_id": pid, "overrides": {**FAST, "source": "adf_simplex"}})
    doc = await harness.wait_status(c, r.json()["id"], "stored")
    r = await c.patch(f"/api/documents/{doc['id']}", json={"title": "Nur Titel", "document_date": ""})
    assert r.status_code == 200, r.text
    assert (r.json()["title"], r.json()["document_date"]) == ("Nur Titel", None)
    r = await c.patch(f"/api/documents/{doc['id']}", json={"document_date": "2026-02-28"})
    assert r.json()["document_date"] == "2026-02-28"
    for bad in ("2026-02-30", "2026-13-01", "28.02.2026"):
        assert (await c.patch(f"/api/documents/{doc['id']}", json={"document_date": bad})).status_code == 422
