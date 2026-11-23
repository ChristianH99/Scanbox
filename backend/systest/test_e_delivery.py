"""Destinations over the network: folder (NAS share), email (SMTP), Paperless (API + consume folder),
webhooks; failures, retries and resend without rescanning."""

from __future__ import annotations

import time

from .conftest import CONSUME, OUTPUT, Api, mocks, profile_id, scan, sim, token_client, wait


def _profile(admin: Api, name: str, destinations: list[dict], **settings: object) -> int:
    base = next(p for p in admin.j("GET", "/api/profiles") if p["name"].startswith("Dokument (PDF/A"))["settings"]
    s = {**base, "source": "adf_simplex", "ocr": {**base["ocr"], "enabled": False, "suggest_title": False},
         "processing": {**base["processing"], "auto_rotate": False, "deskew": False},
         "review_before_processing": False, "destinations": destinations, **settings}
    for p in admin.j("GET", "/api/profiles"):
        if p["name"] == name:
            admin.ok("DELETE", f"/api/profiles/{p['id']}")
    return admin.j("POST", "/api/profiles", json={"name": name, "settings": s})["id"]


def test_folder_destination_with_subfolder_template(admin: Api) -> None:
    pid = _profile(admin, "ST Ordner", [{"type": "folder", "config": {"root": "Scans", "subfolder": "{user}/{date:%Y}",
                                                                      "include_text": False}}],
                   filename_template="{title}_{counter}", title_template="Ordnertest")
    sim("letter_de")
    d = scan(admin, pid, auto_finish=True)
    d = wait(admin, d["id"], "done")
    dl = d["deliveries"][0]
    assert dl["status"] == "done", dl
    year = time.strftime("%Y")
    user = admin.j("GET", "/api/auth/me")["user"]
    udir = OUTPUT / (user["display_name"] or user["username"]) / year
    files = list(udir.glob("Ordnertest_*.pdf"))
    assert files, list(OUTPUT.rglob("*"))[:20]
    assert files[0].stat().st_size > 10_000
    # a second document with the same name never overwrites
    sim("letter_de")
    d2 = wait(admin, scan(admin, pid, auto_finish=True)["id"], "done")
    assert d2["deliveries"][0]["status"] == "done"
    assert len(list(udir.glob("Ordnertest_*.pdf"))) == len(files) + 1


def test_folder_destination_rejects_unknown_root(admin: Api) -> None:
    base = next(p for p in admin.j("GET", "/api/profiles") if p["name"].startswith("Dokument (PDF/A"))["settings"]
    bad = {**base, "destinations": [{"type": "folder", "config": {"root": "../etc"}}]}
    r = admin.post("/api/profiles", json={"name": "ST bad root", "settings": bad})
    assert r.status_code == 422, r.text


def test_email_destination_default_contacts_and_templates(admin: Api) -> None:
    mocks("DELETE", "/smtp/messages")
    pid = _profile(admin, "ST Mail", [{"type": "email", "config": {"recipients": ["extra@example.test"],
                                                                   "use_default_recipients": True}}],
                   title_template="Mailtest")
    sim("letter_de")
    d = wait(admin, scan(admin, pid, auto_finish=True)["id"], "done")
    assert d["deliveries"][0]["status"] == "done", d["deliveries"]
    msgs = mocks("GET", "/smtp/messages")
    assert len(msgs) == 1, msgs
    m = msgs[0]
    assert sorted(m["to"]) == ["buha@example.test", "extra@example.test"], m["to"]
    assert m["subject"] == "Scan: Mailtest", m["subject"]
    assert "Seiten" in m["body"]
    assert m["attachments"][0]["filename"].endswith(".pdf") and m["attachments"][0]["size"] > 10_000


def test_email_smtp_failure_then_retry(admin: Api) -> None:
    mocks("DELETE", "/smtp/messages")
    mocks("POST", "/smtp/reply", json={"data": "554 rejected by policy"})
    pid = profile_id(admin, "ST Mail")
    sim("letter_de")
    d = scan(admin, pid, auto_finish=True)
    d = wait(admin, d["id"], ("needs_attention", "failed"), timeout=180)
    dl = d["deliveries"][0]
    assert dl["status"] in ("needs_attention", "failed") and dl["last_error"], dl
    mocks("POST", "/smtp/reply", json={"data": None})
    admin.ok("POST", f"/api/documents/{d['id']}/deliveries/{dl['id']}/retry")
    d = wait(admin, d["id"], "done")
    assert len(mocks("GET", "/smtp/messages")) == 1


def test_paperless_api_with_metadata(admin: Api) -> None:
    pid = _profile(admin, "ST Paperless", [{"type": "paperless", "config": {
        "mode": "api", "title_template": "{title}", "tags": ["Scan"], "correspondent": "ACME Corp",
        "document_type": "Invoice", "qr_mapping": "none"}}], title_template="Paperlesstest")
    before = len(mocks("GET", "/paperless/state")["uploads"])
    sim("letter_de")
    d = wait(admin, scan(admin, pid, auto_finish=True)["id"], "done")
    assert d["deliveries"][0]["status"] == "done", d["deliveries"]
    st = mocks("GET", "/paperless/state")
    assert len(st["uploads"]) == before + 1
    up = st["uploads"][-1]
    assert str(up.get("title")) == "Paperlesstest", up
    assert up.get("tags"), up


def test_paperless_offline_then_retry_without_rescan(admin: Api) -> None:
    mocks("POST", "/paperless/toggle", json={"fail_status": 503})
    pid = profile_id(admin, "ST Paperless")
    sim("letter_de")
    d = scan(admin, pid, auto_finish=True)
    d = wait(admin, d["id"], ("needs_attention", "delivering"), timeout=120,
             cond=lambda x: x["deliveries"] and x["deliveries"][0]["attempts"] >= 1)
    mocks("POST", "/paperless/toggle", json={"fail_status": None})
    dl = d["deliveries"][0]
    if d["status"] == "needs_attention":
        admin.ok("POST", f"/api/documents/{d['id']}/deliveries/{dl['id']}/retry")
    d = wait(admin, d["id"], "done", timeout=240)
    assert d["deliveries"][0]["status"] == "done"


def test_paperless_consume_folder(admin: Api) -> None:
    pid = _profile(admin, "ST Consume", [{"type": "paperless", "config": {"mode": "consume"}}],
                   title_template="Consumetest", filename_template="{title}")
    sim("letter_de")
    d = wait(admin, scan(admin, pid, auto_finish=True)["id"], "done")
    assert d["deliveries"][0]["status"] == "done", d["deliveries"]
    files = list(CONSUME.rglob("Consumetest*.pdf"))
    assert files, list(CONSUME.rglob("*"))
    assert not list(CONSUME.rglob("*.sbx*")), "temp files must not be left in the consume folder"


def test_multiple_destinations_and_webhook_via_api_token(admin: Api) -> None:
    mocks("DELETE", "/smtp/messages")
    pid = _profile(admin, "ST Multi", [
        {"type": "download", "config": {}},
        {"type": "folder", "config": {"root": "Scans", "subfolder": "multi"}},
        {"type": "email", "config": {"recipients": ["multi@example.test"], "use_default_recipients": False}},
    ], title_template="Multi")
    t = admin.j("POST", "/api/me/tokens", json={"name": "systest-webhook"})
    api = token_client(t["token"])
    sim("letter_de")
    r = api.ok("POST", "/api/scans", json={"profile_id": pid, "auto_finish": True,
                                           "webhook_url": "http://mocks:9000/hook/multi"},
               headers={"Idempotency-Key": "systest-multi-1"})
    r2 = api.ok("POST", "/api/scans", json={"profile_id": pid, "auto_finish": True},
                headers={"Idempotency-Key": "systest-multi-1"})
    assert r.json()["id"] == r2.json()["id"]  # idempotent
    d = wait(api, r.json()["id"], "done")
    assert sorted(x["destination_type"] for x in d["deliveries"]) == ["download", "email", "folder"]
    assert all(x["status"] == "done" for x in d["deliveries"])
    assert list((OUTPUT / "multi").glob("*Multi*.pdf")), list(OUTPUT.rglob("*"))[:20]
    assert len(mocks("GET", "/smtp/messages")) == 1
    end = time.time() + 30
    hooks: list = []
    while time.time() < end:
        hooks = [h for h in mocks("GET", "/hook") if h["name"] == "multi"]
        if hooks:
            break
        time.sleep(0.5)
    assert hooks, "webhook not called"
    body = hooks[-1]["body"]
    assert body.get("document_id") == d["id"] or body.get("id") == d["id"] or str(d["id"]) in str(body), body
    assert body.get("status") in ("done", None) or "done" in str(body)
