"""Phase 1 end: leave work in flight. tools/systest.sh kills the app container right after this.

* a slow 30-page scan that is still transferring pages,
* a 10-page OCR job that is processing, delivering to a folder and by email.
"""

from __future__ import annotations

import time

from .conftest import Api, mocks, profile_id, scan, sim, state_get, state_put, token_client, wait


def test_leave_work_in_flight() -> None:
    api: Api = token_client(state_get()["admin_token"])
    # processing job (OCR, folder + email)
    base = next(p for p in api.j("GET", "/api/profiles") if p["name"].startswith("Dokument (PDF/A"))["settings"]
    s = {**base, "source": "adf_simplex", "review_before_processing": True,
         "destinations": [{"type": "folder", "config": {"root": "Scans", "subfolder": "restart"}},
                          {"type": "email", "config": {"recipients": ["restart@example.test"],
                                                       "use_default_recipients": False}}]}
    pid = api.j("POST", "/api/profiles", json={"name": "ST Restart", "settings": s})["id"]
    mocks("DELETE", "/smtp/messages")
    sim([{"kind": "text", "lang": "de", "seed": 500 + i} for i in range(10)])
    proc = scan(api, pid, fast=False, auto_finish=False)  # API scans finish automatically by default
    wait(api, proc["id"], "stored")
    api.ok("POST", f"/api/documents/{proc['id']}/finish")
    wait(api, proc["id"], "processing", timeout=30)
    # slow scan
    sim("long_stack", faults={"slow_ms": 2500, "slow_style": "block"})
    sc = scan(api, profile_id(api, "Dokument (PDF/A"), {"source": "adf_simplex"}, auto_finish=False)
    wait(api, sc["id"], "scanning", cond=lambda x: x["page_count"] >= 2, timeout=60)
    state_put(scan_doc=sc["id"], proc_doc=proc["id"], killed_at=time.time())
