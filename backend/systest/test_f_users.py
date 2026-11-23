"""Users: SSO login (code + PKCE against a networked provider), admin role from claims, per-user
isolation, private/shared profiles, disabling users, retention and cache."""

from __future__ import annotations

import pytest

from .conftest import Api, oidc_login, profile_id, scan, sim, wait

ALICE = {"sub": "alice-sub", "preferred_username": "alice", "email": "alice@example.test", "name": "Alice",
         "groups": ["users"]}
BOB = {"sub": "bob-sub", "preferred_username": "bob", "email": "bob@example.test", "name": "Bob",
       "groups": ["users", "scanbox-admins"]}


@pytest.fixture(scope="module")
def alice() -> Api:
    return oidc_login(ALICE)


def test_sso_user_roles(alice: Api) -> None:
    me = alice.j("GET", "/api/auth/me")["user"]
    assert me["username"] == "alice" and me["is_admin"] is False
    assert alice.get("/api/admin/settings").status_code == 403
    bob = oidc_login(BOB)
    assert bob.j("GET", "/api/auth/me")["user"]["is_admin"] is True


def test_local_login_cannot_take_over_sso_name(admin: Api) -> None:
    from .conftest import client

    r = client().post("/api/auth/login", json={"username": "alice", "password": "anything-long"})
    assert r.status_code == 401


def test_history_isolation(admin: Api, alice: Api) -> None:
    sim("letter_de")
    pid = profile_id(alice, "Dokument (PDF/A")
    d = scan(alice, pid, {"source": "adf_simplex"}, title="Alice privat")
    d = wait(alice, d["id"], "stored")
    # admins don't see other users' documents either
    assert admin.get(f"/api/documents/{d['id']}").status_code == 404
    assert d["id"] not in [x["id"] for x in admin.j("GET", "/api/documents", params={"limit": 200})["items"]]
    assert admin.get(f"/api/pages/{d['pages'][0]['id']}/image").status_code == 404
    assert admin.post(f"/api/documents/{d['id']}/discard").status_code == 404
    # nor can they pull her pages into their own document
    sim("letter_de")
    mine = wait(admin, scan(admin, pid, {"source": "adf_simplex"})["id"], "stored")
    r = admin.post(f"/api/documents/{mine['id']}/pages/insert",
                   json={"source_document_id": d["id"], "page_ids": [d["pages"][0]["id"]]})
    assert r.status_code in (403, 404)
    alice.ok("POST", f"/api/documents/{d['id']}/finish")
    wait(alice, d["id"], "done")


def test_private_and_shared_profiles(admin: Api, alice: Api) -> None:
    base = next(p for p in alice.j("GET", "/api/profiles") if p["builtin"])["settings"]
    mine = alice.j("POST", "/api/profiles", json={"name": "Alice Profil", "settings": base})
    assert mine["shared"] is False
    assert mine["id"] not in [p["id"] for p in admin.j("GET", "/api/profiles")]
    # builtins are read-only for users
    b = next(p for p in alice.j("GET", "/api/profiles") if p["builtin"])
    assert alice.put(f"/api/profiles/{b['id']}", json={"name": "x", "settings": b["settings"]}).status_code == 403
    assert alice.delete(f"/api/profiles/{b['id']}").status_code == 403
    # invalid settings → 422 with a message, not a 500
    # unsupported by the scanner: saved, but flagged (capabilities can change); scanning refuses it
    odd = alice.j("POST", "/api/profiles", json={"name": "Odd", "settings": {**base, "resolution": 12345}})
    assert odd["problems"], odd
    r = alice.post("/api/scans", json={"profile_id": odd["id"]})
    assert r.status_code in (409, 422), r.text
    alice.ok("DELETE", f"/api/profiles/{odd['id']}")
    bad = {**base, "qr": {**base["qr"], "regex": "(["}}
    assert alice.post("/api/profiles", json={"name": "Bad", "settings": bad}).status_code == 422
    alice.ok("DELETE", f"/api/profiles/{mine['id']}")


def test_admin_disables_user(admin: Api) -> None:
    carol = oidc_login({"sub": "carol-sub", "preferred_username": "carol", "email": "carol@example.test"})
    uid = carol.j("GET", "/api/auth/me")["user"]["id"]
    admin.ok("PATCH", f"/api/admin/users/{uid}", json={"disabled": True})
    assert carol.get("/api/documents").status_code == 401
    admin.ok("PATCH", f"/api/admin/users/{uid}", json={"disabled": False})


def test_clear_cache_and_usage(admin: Api) -> None:
    before = admin.j("GET", "/api/admin/usage")
    r = admin.j("POST", "/api/admin/clear-cache", json={"confirm": True})
    after = admin.j("GET", "/api/admin/usage")
    assert after["cache_bytes"] <= before["cache_bytes"]
    assert isinstance(r, dict)
    # documents waiting for review keep their originals
    stored = admin.j("GET", "/api/documents", params={"status": "stored", "limit": 1})["items"]
    if stored:
        d = admin.j("GET", f"/api/documents/{stored[0]['id']}")
        admin.ok("GET", f"/api/pages/{d['pages'][0]['id']}/image", params={"variant": "raw", "size": "thumb"})
