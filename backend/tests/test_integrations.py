"""Full-pipeline integrations: email (real SMTP server), Paperless (mock API), webhooks,
retention / clear cache, walk-up scan into the shared inbox."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import httpx
from fastapi import FastAPI, Request

from scanbox.db import session_scope, utcnow
from scanbox.models import Document

from .conftest import FAST, builtin_profile_id
from .mocks.paperless import create_paperless_mock, paperless_client_factory
from .mocks.smtp import run_smtp_server
from .walkup_fake import create_walkup_fake


async def _scan_and_finish(h, c, overrides: dict, final: str | set[str] = "done") -> dict:  # noqa: ANN001
    pid = await builtin_profile_id(c)
    r = await c.post("/api/scans", json={"profile_id": pid,
                                         "overrides": {**FAST, "source": "adf_simplex", **overrides}})
    assert r.status_code == 201, r.text
    doc = await h.wait_status(c, r.json()["id"], "stored")
    assert (await c.post(f"/api/documents/{doc['id']}/finish")).status_code == 200
    return await h.wait_status(c, doc["id"], final)


async def test_email_delivery(harness) -> None:  # noqa: ANN001
    with run_smtp_server(users={"scanbox@example.test": "app-pw"}) as smtp:
        c = await harness.login()
        r = await c.patch("/api/admin/settings", json={"values": {
            "smtp.host": smtp.host, "smtp.port": smtp.port, "smtp.security": "none",
            "smtp.username": "scanbox@example.test", "smtp.password": "app-pw",
            "smtp.from_address": "scanbox@example.test"}})
        assert r.status_code == 200, r.text
        assert (await c.post("/api/admin/test/smtp")).json()["ok"] is True
        await c.post("/api/me/contacts", json={"name": "Steuerberater", "email": "tax@example.test",
                                               "is_default": True})
        dests = [{"type": "email", "config": {"recipients": ["me@example.test"],
                                              "subject_template": "Scan {title} ({pages} S.)"}}]
        doc = await _scan_and_finish(harness, c, {"destinations": dests}, )
        assert [d["status"] for d in doc["deliveries"]] == ["done"]
        assert len(smtp.messages) == 1
        m = smtp.messages[0]
        assert set(m.rcpt_tos) == {"me@example.test", "tax@example.test"}
        msg = m.message
        assert msg["Subject"].startswith("Scan ")
        atts = [p for p in msg.iter_attachments()]
        assert atts and atts[0].get_filename().endswith(".pdf")


async def test_email_smtp_down_retries_then_attention(harness_factory) -> None:  # noqa: ANN001
    h = await harness_factory()
    c = await h.login()
    await c.patch("/api/admin/settings", json={"values": {
        "smtp.host": "127.0.0.1", "smtp.port": 1, "smtp.security": "none", "smtp.from_address": "a@b.de",
        "smtp.timeout_s": 2}})
    dests = [{"type": "email", "config": {"recipients": ["x@example.test"]}}]
    doc = await _scan_and_finish(h, c, {"destinations": dests}, final="delivering")
    # first attempt failed and was rescheduled with backoff – visible, not silent
    for _ in range(100):
        d = (await c.get(f"/api/documents/{doc['id']}")).json()
        if d["deliveries"][0]["last_error"]:
            break
        await asyncio.sleep(0.1)
    assert d["deliveries"][0]["status"] == "pending"
    assert d["deliveries"][0]["last_error"]
    assert any("neuer Versuch" in e["message"] for e in d["events"])


async def test_paperless_api_delivery_with_asn(harness_factory) -> None:  # noqa: ANN001
    mock = create_paperless_mock()

    def customize(ctx) -> None:  # noqa: ANN001
        ctx.http_client_factory = paperless_client_factory(mock)

    h = await harness_factory(customize=customize, stack="invoice_qr")
    c = await h.login()
    await c.patch("/api/admin/settings", json={"values": {"paperless.url": "http://paperless.test"}})
    await c.patch("/api/me/settings", json={"paperless_token": "test-token"})
    dests = [{"type": "paperless", "config": {"mode": "api", "tags": ["Scan"], "qr_mapping": "asn",
                                              "wait_for_task": True}}]
    doc = await _scan_and_finish(h, c, {"destinations": dests, "resolution": 200,
                                        "qr": {"enabled": True, "regex": r"\d+$"}})
    assert doc["qr_value"] == "INV-2024-0042"
    assert doc["deliveries"][0]["status"] == "done", doc["deliveries"]
    up = mock.state.mock.uploads[-1]
    # non-numeric QR → no ASN (fallback none); tag resolved to id
    assert "archive_serial_number" not in up or not up["archive_serial_number"]
    assert up.get("tags")


async def test_paperless_offline_then_retry_without_rescan(harness_factory) -> None:  # noqa: ANN001
    mock = create_paperless_mock()
    mock.state.mock.offline = True

    def customize(ctx) -> None:  # noqa: ANN001
        ctx.http_client_factory = paperless_client_factory(mock)

    h = await harness_factory(customize=customize)
    c = await h.login()
    await c.patch("/api/admin/settings", json={"values": {"paperless.url": "http://paperless.test"}})
    await c.patch("/api/me/settings", json={"paperless_token": "test-token"})
    dests = [{"type": "paperless", "config": {"mode": "api", "wait_for_task": False}}]
    doc = await _scan_and_finish(h, c, {"destinations": dests}, final="delivering")
    # make the next attempt due immediately and bring Paperless back
    mock.state.mock.offline = False
    from scanbox.models import Task

    for _ in range(50):
        with session_scope() as s:
            t = s.query(Task).filter(Task.kind == "deliver", Task.document_id == doc["id"]).one()
            if t.status == "pending" and t.attempts >= 1:
                t.next_run_at = utcnow()
                break
        await asyncio.sleep(0.1)
    h.ctx.worker.notify()
    doc = await h.wait_status(c, doc["id"], "done")
    assert len(mock.state.mock.uploads) == 1


async def test_webhook_after_api_scan(harness_factory) -> None:  # noqa: ANN001
    received: list[dict] = []
    hook = FastAPI()

    @hook.post("/hook")
    async def _hook(req: Request) -> dict:
        received.append(await req.json())
        return {}

    def customize(ctx) -> None:  # noqa: ANN001
        ctx.http_client_factory = lambda **kw: httpx.AsyncClient(transport=httpx.ASGITransport(app=hook), **kw)

    h = await harness_factory(customize=customize)
    c = await h.login()
    token = (await c.post("/api/me/tokens", json={"name": "ha"})).json()["token"]
    api = h.client()
    api.headers["Authorization"] = f"Bearer {token}"
    pid = await builtin_profile_id(c)
    r = await api.post("/api/scans", json={"profile_id": pid, "overrides": FAST,
                                           "webhook_url": "http://ha.local/hook"})
    await h.wait_status(api, r.json()["id"], "done")
    for _ in range(50):
        if received:
            break
        await asyncio.sleep(0.1)
    assert received and received[0]["status"] == "done" and received[0]["files"]


async def test_retention_and_clear_cache(harness) -> None:  # noqa: ANN001
    c = await harness.login()
    doc = await _scan_and_finish(harness, c, {})
    raw_dir = harness.data_dir / "raw" / str(doc["id"])
    assert any(raw_dir.iterdir())
    # age the document beyond retention
    with session_scope() as s:
        d = s.get(Document, doc["id"])
        d.created_at = utcnow() - timedelta(days=60)
        d.finished_at = utcnow() - timedelta(days=60)
    from scanbox.retention import run_retention

    stats = await asyncio.to_thread(run_retention, harness.ctx)
    assert stats["raw_expired"] == 1 and stats["processed_expired"] == 1
    d = (await c.get(f"/api/documents/{doc['id']}")).json()
    assert d["raw_expired"] and d["processed_expired"] and d["status"] == "done"
    assert d["filename"]  # metadata stays in history
    assert not raw_dir.exists() or not any(raw_dir.iterdir())
    usage = (await c.get("/api/admin/usage")).json()
    assert usage["raw_bytes"] == 0


async def test_clear_cache_keeps_unfinished_work(harness) -> None:  # noqa: ANN001
    c = await harness.login()
    pid = await builtin_profile_id(c)
    r = await c.post("/api/scans", json={"profile_id": pid, "overrides": {**FAST, "source": "adf_simplex"}})
    doc = await harness.wait_status(c, r.json()["id"], "stored")
    r = await c.post("/api/admin/clear-cache")
    assert r.status_code == 200
    d = (await c.get(f"/api/documents/{doc['id']}")).json()
    assert d["raw_expired"] is False
    assert (await c.post(f"/api/documents/{doc['id']}/finish")).status_code == 200
    await harness.wait_status(c, doc["id"], "done")


async def test_inbox_expiry(harness) -> None:  # noqa: ANN001
    from scanbox.documents import create_scan, resolve_profile
    from scanbox.retention import run_retention

    with session_scope() as s:
        doc = create_scan(s, harness.ctx, user=None, profile=resolve_profile(s, None, None),
                          overrides={**FAST, "source": "adf_simplex"}, origin="walkup", auto_finish=False)
        doc_id = doc.id
    harness.ctx.worker.notify()
    c = await harness.login()
    await harness.wait_status(c, doc_id, "stored")
    with session_scope() as s:
        s.get(Document, doc_id).inbox_expires_at = utcnow() - timedelta(minutes=1)
    await asyncio.to_thread(run_retention, harness.ctx)
    assert (await c.get("/api/documents", params={"scope": "inbox"})).json()["items"] == []


async def test_walkup_scan_lands_in_inbox(harness_factory) -> None:  # noqa: ANN001
    fake = create_walkup_fake("escl", max_wait_s=0.5)
    holder: dict = {}

    async def router(scope, receive, send):  # noqa: ANN001
        path = scope.get("path", "") if scope["type"] == "http" else ""
        if "Walkup" in path or path.startswith(("/EventMgmt", "/DevMgmt")):
            await fake(scope, receive, send)
        else:
            await holder["sim"](scope, receive, send)

    from escl_sim import SimState
    from escl_sim import create_app as create_sim

    state = SimState(stack="letter_de")
    holder["sim"] = create_sim(state)

    def customize(ctx) -> None:  # noqa: ANN001
        ctx.scanner_client_factory = lambda: httpx.AsyncClient(
            transport=httpx.ASGITransport(app=router), base_url="http://sim", timeout=30)

    h = await harness_factory(customize=customize, sim_state=state, walkup_enabled=True)
    c = await h.login()
    for _ in range(100):
        if h.ctx.walkup.status.state == "registered":
            break
        await asyncio.sleep(0.05)
    assert h.ctx.walkup.status.state == "registered", h.ctx.walkup.status
    await fake.press_scan("Scanbox", shortcut="unknown-shortcut", Duplex=False)
    for _ in range(200):
        items = (await c.get("/api/documents", params={"scope": "inbox"})).json()["items"]
        if items:
            break
        await asyncio.sleep(0.05)
    assert items and items[0]["origin"] == "walkup"
    doc = await h.wait_status(c, items[0]["id"], "stored")
    assert doc["page_count"] == 3 and doc["owner_id"] is None
