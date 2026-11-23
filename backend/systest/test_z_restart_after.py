"""Phase 3: after SIGKILL + restart of the app container."""

from __future__ import annotations

import pytest

from .conftest import OUTPUT, Api, client, mocks, sim, state_get, token_client, wait

pytestmark = pytest.mark.after_restart


@pytest.fixture(scope="module")
def api() -> Api:
    return token_client(state_get()["admin_token"])


def test_app_ready_after_kill() -> None:
    r = client().get("/readyz").json()
    assert r["status"] == "ok", r


def test_interrupted_scan_keeps_pages_and_can_continue(api: Api) -> None:
    doc = state_get()["scan_doc"]
    d = wait(api, doc, ("needs_attention", "stored"), timeout=60)
    assert d["page_count"] >= 2, d["page_count"]
    for p in d["pages"]:  # every recorded page is readable
        api.ok("GET", f"/api/pages/{p['id']}/image", params={"variant": "raw", "size": "thumb"})
    assert any(e["code"].startswith("scan.") for e in d["events"])
    n = d["page_count"]
    sim("letter_de")
    api.ok("POST", f"/api/documents/{doc}/batches")
    d = wait(api, doc, "stored", cond=lambda x: x["batches"][-1]["status"] == "done", timeout=120)
    assert d["page_count"] == n + 3
    api.ok("POST", f"/api/documents/{doc}/finish")
    wait(api, doc, "done", timeout=300)


def test_interrupted_processing_resumes_without_duplicates(api: Api) -> None:
    doc = state_get()["proc_doc"]
    d = wait(api, doc, ("done", "needs_attention"), timeout=400)
    folder = [x for x in d["deliveries"] if x["destination_type"] == "folder"][0]
    assert folder["status"] == "done", d["deliveries"]
    files = list((OUTPUT / "restart").glob("*.pdf"))
    assert len(files) == 1, files  # exactly once
    mails = [m for m in mocks("GET", "/smtp/messages") if "restart@example.test" in m["to"]]
    assert len(mails) <= 1, mails  # never twice
    email = [x for x in d["deliveries"] if x["destination_type"] == "email"][0]
    if email["status"] == "done":
        assert len(mails) == 1
    else:  # killed mid-send: flagged for the user instead of re-sent blindly
        assert email["status"] == "needs_attention", email
