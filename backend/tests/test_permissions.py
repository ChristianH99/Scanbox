"""Visibility rules, inbox claim race, CSRF, sessions, API tokens, rate limiting."""

from __future__ import annotations

import asyncio

from scanbox.db import session_scope
from scanbox.documents import create_scan, resolve_profile

from .conftest import FAST, builtin_profile_id


def _walkup_doc(h) -> int:  # noqa: ANN001
    with session_scope() as s:
        prof = resolve_profile(s, None, None)
        doc = create_scan(s, h.ctx, user=None, profile=prof, overrides={**FAST, "source": "adf_simplex"},
                          origin="walkup", auto_finish=False)
        return doc.id


async def test_inbox_visible_to_all_until_claimed_then_private(harness) -> None:  # noqa: ANN001
    alice = await harness.user("alice")
    bob = await harness.user("bob")
    doc_id = _walkup_doc(harness)
    harness.ctx.worker.notify()
    await harness.wait_status(alice, doc_id, "stored")
    for c in (alice, bob):
        r = await c.get("/api/documents", params={"scope": "inbox"})
        assert [d["id"] for d in r.json()["items"]] == [doc_id]
    # unclaimed items cannot be edited/finished
    r = await alice.post(f"/api/documents/{doc_id}/finish")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "not_claimed"
    r = await alice.post(f"/api/documents/{doc_id}/claim")
    assert r.status_code == 200 and r.json()["owner_id"] is not None
    # gone for bob, in alice's history
    assert (await bob.get("/api/documents", params={"scope": "inbox"})).json()["items"] == []
    assert (await bob.get(f"/api/documents/{doc_id}")).status_code == 404
    assert (await bob.post(f"/api/documents/{doc_id}/claim")).status_code == 409
    hist = (await alice.get("/api/documents", params={"scope": "history"})).json()["items"]
    assert [d["id"] for d in hist] == [doc_id]


async def test_inbox_claim_race_exactly_one_winner(harness) -> None:  # noqa: ANN001
    users = [await harness.user(f"u{i}") for i in range(6)]
    doc_id = _walkup_doc(harness)
    harness.ctx.worker.notify()
    await harness.wait_status(users[0], doc_id, "stored")
    results = await asyncio.gather(*(c.post(f"/api/documents/{doc_id}/claim") for c in users))
    codes = sorted(r.status_code for r in results)
    assert codes == [200, 409, 409, 409, 409, 409]
    loser = next(r for r in results if r.status_code == 409)
    assert loser.json()["detail"]["code"] == "already_claimed"


async def test_history_isolation(harness) -> None:  # noqa: ANN001
    alice = await harness.user("alice")
    bob = await harness.user("bob")
    admin = await harness.login()
    pid = await builtin_profile_id(alice)
    r = await alice.post("/api/scans", json={"profile_id": pid, "overrides": {**FAST, "source": "adf_simplex"},
                                             "title": "Geheim"})
    doc = r.json()
    await harness.wait_status(alice, doc["id"], "stored")
    page_id = (await alice.get(f"/api/documents/{doc['id']}")).json()["pages"][0]["id"]
    for other in (bob, admin):  # admins see aggregates, not other users' documents
        assert (await other.get(f"/api/documents/{doc['id']}")).status_code == 404
        assert (await other.get("/api/documents", params={"q": "Geheim"})).json()["items"] == []
        assert (await other.get(f"/api/pages/{page_id}/image")).status_code == 404
        assert (await other.patch(f"/api/pages/{page_id}", json={"deleted": True})).status_code == 404
        assert (await other.post(f"/api/documents/{doc['id']}/discard")).status_code == 404
        assert (await other.get(f"/api/documents/{doc['id']}/download")).status_code == 404
        r = await other.post(f"/api/documents/{doc['id']}/batches")
        assert r.status_code == 404


async def test_cannot_insert_pages_from_foreign_document(harness) -> None:  # noqa: ANN001
    alice = await harness.user("alice")
    bob = await harness.user("bob")
    pid = await builtin_profile_id(alice)
    a = (await alice.post("/api/scans", json={"profile_id": pid, "overrides": {**FAST, "source": "adf_simplex"}})).json()
    await harness.wait_status(alice, a["id"], "stored")
    harness.sim_state.load("letter_de")
    b = (await bob.post("/api/scans", json={"profile_id": pid, "overrides": {**FAST, "source": "adf_simplex"}})).json()
    await harness.wait_status(bob, b["id"], "stored")
    a_pages = [p["id"] for p in (await alice.get(f"/api/documents/{a['id']}")).json()["pages"]]
    r = await bob.post(f"/api/documents/{b['id']}/pages/insert",
                       json={"source_document_id": a["id"], "page_ids": a_pages[:1]})
    assert r.status_code == 404
    r = await bob.post(f"/api/documents/{b['id']}/merge", json={"source_document_ids": [a["id"]]})
    assert r.status_code == 404


async def test_private_profiles(harness) -> None:  # noqa: ANN001
    alice = await harness.user("alice")
    bob = await harness.user("bob")
    base = (await alice.get("/api/profiles")).json()[0]
    r = await alice.post("/api/profiles", json={"name": "Mein Profil", "settings": base["settings"]})
    assert r.status_code == 201
    pid = r.json()["id"]
    assert (await bob.get(f"/api/profiles/{pid}")).status_code == 404
    assert all(p["id"] != pid for p in (await bob.get("/api/profiles")).json())
    r = await bob.post("/api/scans", json={"profile_id": pid})
    assert r.status_code == 404
    # built-ins: users can't modify, can't create shared
    r = await bob.put(f"/api/profiles/{base['id']}", json={"name": "x", "settings": base["settings"]})
    assert r.status_code == 403
    assert (await bob.post("/api/profiles?shared=true", json={"name": "x", "settings": base["settings"]})).status_code == 403


async def test_csrf_required_for_cookie_auth(harness) -> None:  # noqa: ANN001
    c = await harness.login()
    del c.headers["X-CSRF-Token"]
    r = await c.post("/api/me/contacts", json={"email": "a@b.de"})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "csrf_failed"
    c.headers["X-CSRF-Token"] = "wrong"
    assert (await c.post("/api/me/contacts", json={"email": "a@b.de"})).status_code == 403
    c.headers["X-CSRF-Token"] = c.cookies["scanbox_csrf"]
    assert (await c.post("/api/me/contacts", json={"email": "a@b.de"})).status_code == 201


async def test_unauthenticated_and_admin_only(harness) -> None:  # noqa: ANN001
    anon = harness.client()
    for path in ("/api/documents", "/api/profiles", "/api/scanner/status", "/api/admin/settings", "/api/events"):
        assert (await anon.get(path)).status_code == 401, path
    bob = await harness.user("bob")
    for path in ("/api/admin/settings", "/api/admin/users", "/api/admin/diagnostics", "/api/admin/usage"):
        assert (await bob.get(path)).status_code == 403, path
    assert (await bob.post("/api/scanner/test", json={"address": "10.0.0.1"})).status_code == 403


async def test_login_rate_limit(harness) -> None:  # noqa: ANN001
    c = harness.client()
    for _ in range(5):
        r = await c.post("/api/auth/login", json={"username": "admin", "password": "nope"})
        assert r.status_code == 401
    r = await c.post("/api/auth/login", json={"username": "admin", "password": "nope"})
    assert r.status_code == 429 and "Retry-After" in r.headers
    # even the right password is refused while limited
    from .conftest import ADMIN_PW

    r = await c.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PW})
    assert r.status_code == 429


async def test_session_cookie_flags_and_logout(harness_factory) -> None:  # noqa: ANN001
    h = await harness_factory(cookie_secure=True)
    from .conftest import ADMIN_PW

    c = h.client()
    r = await c.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PW})
    cookies = r.headers.get_list("set-cookie")
    session = next(x for x in cookies if x.startswith("scanbox_session="))
    assert "HttpOnly" in session and "Secure" in session and "SameSite=lax" in session
    csrf = next(x for x in cookies if x.startswith("scanbox_csrf="))
    assert "HttpOnly" not in csrf


async def test_logout_invalidates_session(harness) -> None:  # noqa: ANN001
    c = await harness.login()
    raw = c.cookies["scanbox_session"]
    assert (await c.post("/api/auth/logout")).status_code == 204
    other = harness.client()
    other.cookies.set("scanbox_session", raw)
    assert (await other.get("/api/auth/me")).status_code == 401


async def test_api_token_lifecycle(harness) -> None:  # noqa: ANN001
    c = await harness.login()
    created = (await c.post("/api/me/tokens", json={"name": "ha"})).json()
    api = harness.client()
    api.headers["Authorization"] = f"Bearer {created['token']}"
    assert (await api.get("/api/auth/me")).status_code == 200
    # bearer requests need no CSRF header
    assert (await api.post("/api/me/contacts", json={"email": "x@y.de"})).status_code == 201
    # tokens can't mint tokens
    assert (await api.post("/api/me/tokens", json={"name": "x"})).status_code == 403
    assert (await c.delete(f"/api/me/tokens/{created['id']}")).status_code == 204
    assert (await api.get("/api/auth/me")).status_code == 401
    bad = harness.client()
    bad.headers["Authorization"] = "Bearer sbx_nope_nope"
    assert (await bad.get("/api/auth/me")).status_code == 401


async def test_secrets_never_returned(harness) -> None:  # noqa: ANN001
    c = await harness.login()
    r = await c.patch("/api/admin/settings", json={"values": {"smtp.password": "hunter2-secret", "smtp.host": "smtp.x"}})
    assert r.status_code == 200
    body = r.text
    assert "hunter2" not in body
    assert r.json()["settings"]["smtp.password"]["is_set"] is True
    # stored encrypted
    from scanbox.models import Setting

    with session_scope() as s:
        row = s.get(Setting, "smtp.password")
        assert row.encrypted and "hunter2" not in str(row.value)
    # empty value keeps the secret
    await c.patch("/api/admin/settings", json={"values": {"smtp.password": ""}})
    assert harness.ctx.settings.get("smtp.password") == "hunter2-secret"
    r = await c.patch("/api/me/settings", json={"paperless_token": "tok-123"})
    assert r.json()["paperless_token_set"] is True and "tok-123" not in r.text


async def test_env_locked_settings(harness_factory) -> None:  # noqa: ANN001
    h = await harness_factory(raw_retention_days=7)
    c = await h.login()
    s = (await c.get("/api/admin/settings")).json()["settings"]
    assert s["retention.raw_days"]["locked"] is True and s["retention.raw_days"]["value"] == 7
    r = await c.patch("/api/admin/settings", json={"values": {"retention.raw_days": 3}})
    assert r.status_code == 422


async def test_disk_guard(harness_factory) -> None:  # noqa: ANN001
    h = await harness_factory(min_free_mb=None)
    c = await h.login()
    r = await c.patch("/api/admin/settings", json={"values": {"disk.min_free_mb": 10**9}})
    assert r.status_code == 200, r.text
    pid = await builtin_profile_id(c)
    r = await c.post("/api/scans", json={"profile_id": pid})
    assert r.status_code == 507 and r.json()["detail"]["code"] == "insufficient_disk"


async def test_unsupported_settings_rejected(harness) -> None:  # noqa: ANN001
    c = await harness.login()
    pid = await builtin_profile_id(c)
    r = await c.post("/api/scans", json={"profile_id": pid, "overrides": {"resolution": 1200}})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "unsupported_settings"


async def test_queue_bound(harness_factory) -> None:  # noqa: ANN001
    h = await harness_factory(max_queued_scans=2)
    h.sim_state.set_faults(slow_ms=3000, sticky=True)
    c = await h.login()
    pid = await builtin_profile_id(c)
    codes = []
    for _ in range(4):
        codes.append((await c.post("/api/scans", json={"profile_id": pid, "overrides": FAST})).status_code)
    assert codes[:2] == [201, 201] and 429 in codes[2:]


async def test_security_headers(harness) -> None:  # noqa: ANN001
    c = harness.client()
    r = await c.get("/healthz")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    r = await c.get("/readyz")
    assert r.status_code == 200 and r.json()["checks"]["db"] == "ok"
