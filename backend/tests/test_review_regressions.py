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
