"""Restart/crash recovery: interrupted scans, orphan pages, interrupted deliveries, idempotency."""

from __future__ import annotations

import uuid

from sqlalchemy import select

from scanbox.db import session_scope
from scanbox.jobs.queue import enqueue
from scanbox.models import Batch, Delivery, Document, Page, Task

from .conftest import FAST, builtin_profile_id


async def _stored_doc(h, c, overrides=None) -> dict:  # noqa: ANN001
    pid = await builtin_profile_id(c)
    r = await c.post("/api/scans", json={"profile_id": pid, "overrides": {**FAST, "source": "adf_simplex",
                                                                           **(overrides or {})}})
    return await h.wait_status(c, r.json()["id"], "stored")


async def test_restart_mid_scan_keeps_pages(harness_factory, tmp_path) -> None:  # noqa: ANN001
    data = tmp_path / "d"
    h = await harness_factory(data_dir=data)
    c = await h.login()
    doc = await _stored_doc(h, c)
    # Simulate a crash in the middle of a second batch: task running, batch scanning, a page
    # file on disk that never made it into the DB.
    with session_scope() as s:
        d = s.get(Document, doc["id"])
        d.status = "scanning"
        b = Batch(document_id=d.id, seq=2, status="scanning")
        s.add(b)
        s.flush()
        t = enqueue(s, "scan", idempotency_key=f"scan:{b.id}", document_id=d.id, payload={"batch_id": b.id})
        t.status = "running"
    orphan = data / "raw" / str(doc["id"]) / f"{uuid.uuid4()}.jpg"
    orphan.write_bytes((data / "raw" / str(doc["id"]) / next(
        p.name for p in (data / "raw" / str(doc["id"])).iterdir())).read_bytes())
    # "restart"
    cm, _ = h_factory_pop(harness_factory)
    await cm.__aexit__(None, None, None)
    h2 = await harness_factory(data_dir=data)
    c2 = await h2.login()
    d = (await c2.get(f"/api/documents/{doc['id']}")).json()
    assert d["status"] == "needs_attention"
    assert d["error_code"] == "interrupted"
    assert d["page_count"] == 4  # 3 + adopted orphan
    assert any(e["code"] == "recovery.page_adopted" for e in d["events"])
    with session_scope() as s:
        assert s.scalar(select(Batch.status).where(Batch.seq == 2)) == "interrupted"
    # user can continue: finish with what we have
    assert (await c2.post(f"/api/documents/{doc['id']}/finish")).status_code == 200
    await h2.wait_status(c2, doc["id"], "done")


def h_factory_pop(factory):  # noqa: ANN001, ANN201
    return factory.created.pop()


async def test_restart_mid_processing_requeues(harness_factory, tmp_path) -> None:  # noqa: ANN001
    data = tmp_path / "d"
    h = await harness_factory(data_dir=data)
    c = await h.login()
    doc = await _stored_doc(h, c)
    await h.ctx.worker.stop()  # freeze workers, then fake an in-flight processing task
    with session_scope() as s:
        d = s.get(Document, doc["id"])
        d.status = "processing"
        d.processing_run = 1
        t = enqueue(s, "process", idempotency_key=f"process:{d.id}:1", document_id=d.id, payload={"run": 1})
        t.status = "running"
    cm, _ = harness_factory.created.pop()
    await cm.__aexit__(None, None, None)
    h2 = await harness_factory(data_dir=data)
    c2 = await h2.login()
    d = await h2.wait_status(c2, doc["id"], "done")
    assert d["filename"]


async def test_interrupted_email_delivery_needs_attention(harness_factory, tmp_path) -> None:  # noqa: ANN001
    """An email that may already have been sent is never re-sent automatically."""
    data = tmp_path / "d"
    h = await harness_factory(data_dir=data)
    c = await h.login()
    doc = await _stored_doc(h, c)
    await c.post(f"/api/documents/{doc['id']}/finish")
    await h.wait_status(c, doc["id"], "done")
    await h.ctx.worker.stop()
    with session_scope() as s:
        d = s.get(Document, doc["id"])
        d.status = "delivering"
        dl = Delivery(document_id=d.id, destination_type="email", label="Mail", status="running",
                      config={"recipients": ["x@example.test"]}, run=d.processing_run)
        s.add(dl)
        s.flush()
        t = enqueue(s, "deliver", idempotency_key=f"deliver:{dl.id}:1", document_id=d.id, delivery_id=dl.id,
                    max_attempts=6)
        t.status = "running"
        dl_id = dl.id
    cm, _ = harness_factory.created.pop()
    await cm.__aexit__(None, None, None)
    h2 = await harness_factory(data_dir=data, )
    c2 = await h2.login()
    d = await h2.wait_status(c2, doc["id"], "needs_attention")
    dl = next(x for x in d["deliveries"] if x["id"] == dl_id)
    assert dl["status"] == "needs_attention" and dl["error_code"] == "interrupted"


async def test_interrupted_folder_delivery_not_duplicated(harness_factory, tmp_path) -> None:  # noqa: ANN001
    data = tmp_path / "d"
    out = tmp_path / "out"
    out.mkdir()
    h = await harness_factory(data_dir=data, output_roots=f"Out={out}")
    c = await h.login()
    doc = await _stored_doc(h, c, {"destinations": [{"type": "folder", "config": {"root": "Out"}}],
                                   "filename_template": "x"})
    await c.post(f"/api/documents/{doc['id']}/finish")
    await h.wait_status(c, doc["id"], "done")
    assert [p.name for p in out.iterdir()] == ["x.pdf"]
    await h.ctx.worker.stop()
    # crash after the file was written but before the delivery was marked done
    with session_scope() as s:
        dl = s.scalar(select(Delivery).where(Delivery.document_id == doc["id"]))
        dl.status = "running"
        s.get(Document, doc["id"]).status = "delivering"
        t = s.scalar(select(Task).where(Task.delivery_id == dl.id))
        t.status = "running"
    cm, _ = harness_factory.created.pop()
    await cm.__aexit__(None, None, None)
    h2 = await harness_factory(data_dir=data, output_roots=f"Out={out}")
    c2 = await h2.login()
    await h2.wait_status(c2, doc["id"], "done")
    assert [p.name for p in out.iterdir()] == ["x.pdf"]  # verified by checksum, not written twice


async def test_task_idempotency_key(harness) -> None:  # noqa: ANN001
    with session_scope() as s:
        a = enqueue(s, "webhook", idempotency_key="same-key")
        b = enqueue(s, "webhook", idempotency_key="same-key")
        assert a.id == b.id
        assert s.scalar(select(Task.id).where(Task.idempotency_key == "same-key")) == a.id


async def test_pages_persisted_before_db(harness) -> None:  # noqa: ANN001
    c = await harness.login()
    doc = await _stored_doc(harness, c)
    with session_scope() as s:
        pages = s.scalars(select(Page).where(Page.document_id == doc["id"])).all()
        for p in pages:
            f = harness.ctx.storage.abs(p.raw_path)
            assert f.exists() and f.stat().st_size == p.raw_size
    # no temp files left behind
    assert not list((harness.data_dir / "raw").rglob(".sbx-*"))
