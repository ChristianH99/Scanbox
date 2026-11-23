"""Platform: health, static app/PWA, security headers, local login/logout, CSRF, API tokens."""

from __future__ import annotations

from .conftest import ADMIN, Api, client, login, state_put, token_client


def test_health_and_ready() -> None:
    c = client()
    assert c.get("/healthz").status_code == 200
    r = c.get("/readyz").json()
    assert r["status"] == "ok", r
    assert r["checks"]["scanner"] == "ready" and r["checks"]["db"] == "ok"


def test_spa_pwa_and_headers() -> None:
    c = client()
    r = c.get("/")
    assert r.status_code == 200 and "<div id=\"app\"" in r.text or 'id="app"' in r.text
    for h in ("content-security-policy", "x-content-type-options", "referrer-policy", "x-frame-options"):
        assert h in {k.lower() for k in r.headers}, f"missing {h}"
    # client-side routes are served by the SPA
    assert c.get("/history").status_code == 200
    assert c.get("/documents/123").status_code == 200
    man = c.get("/manifest.webmanifest")
    assert man.status_code == 200 and man.json()["name"]
    assert c.get("/sw.js").status_code == 200
    assert c.get("/favicon.svg").status_code == 200
    # unknown API paths are JSON 404, not the SPA
    r = c.get("/api/does-not-exist")
    assert r.status_code == 404 and r.headers["content-type"].startswith("application/json")
    assert client().get("/openapi.json").status_code in (200, 404)  # optional


def test_auth_config_without_sso() -> None:
    cfg = client().get("/api/auth/config").json()
    assert cfg["oidc_enabled"] is False


def test_login_logout_csrf() -> None:
    c = client()
    assert c.get("/api/documents").status_code == 401
    bad = c.post("/api/auth/login", json={"username": "admin", "password": "wrong-password"})
    assert bad.status_code == 401
    r = c.post("/api/auth/login", json={"username": ADMIN[0], "password": ADMIN[1]})
    assert r.status_code == 200
    csrf = r.json()["csrf_token"]
    cookie = r.headers.get("set-cookie", "")
    assert "httponly" in cookie.lower() and "samesite" in cookie.lower()
    me = c.get("/api/auth/me").json()
    assert me["user"]["username"] == "admin" and me["user"]["is_admin"] is True
    # state-changing request without CSRF header is refused
    assert c.patch("/api/auth/me", json={"display_name": "x"}).status_code == 403
    assert c.patch("/api/auth/me", json={"display_name": "Admin"}, headers={"X-CSRF-Token": csrf}).status_code == 200
    assert c.post("/api/auth/logout", headers={"X-CSRF-Token": csrf}).status_code in (200, 204)
    assert c.get("/api/auth/me").status_code == 401


def test_preferences(admin: Api) -> None:
    u = admin.j("PATCH", "/api/auth/me", json={"locale": "en", "theme": "dark"})
    assert u["locale"] == "en" and u["theme"] == "dark"
    u = admin.j("PATCH", "/api/auth/me", json={"locale": "de", "theme": "system"})
    assert u["locale"] == "de"
    assert admin.patch("/api/auth/me", json={"locale": "fr"}).status_code == 422


def test_api_tokens(admin: Api) -> None:
    t = admin.j("POST", "/api/me/tokens", json={"name": "systest-temp"})
    tc = token_client(t["token"])
    assert tc.get("/api/documents").status_code == 200
    # bearer requests need no CSRF header
    assert tc.post("/api/me/contacts", json={"name": "T", "email": "t@example.test"}).status_code == 201
    assert any(x["id"] == t["id"] for x in admin.j("GET", "/api/me/tokens"))
    admin.ok("DELETE", f"/api/me/tokens/{t['id']}")
    assert tc.get("/api/documents").status_code == 401
    assert token_client("sbx_invalid").get("/api/documents").status_code == 401
    # long-lived token for the after-restart phase
    keep = admin.j("POST", "/api/me/tokens", json={"name": "systest-restart"})
    state_put(admin_token=keep["token"])


def test_contacts_and_user_settings(admin: Api) -> None:
    for x in admin.j("GET", "/api/me/contacts"):
        admin.ok("DELETE", f"/api/me/contacts/{x['id']}")
    a = admin.j("POST", "/api/me/contacts", json={"name": "Buchhaltung", "email": "buha@example.test", "is_default": True})
    admin.j("POST", "/api/me/contacts", json={"name": "Other", "email": "other@example.test"})
    assert admin.post("/api/me/contacts", json={"name": "Dup", "email": "buha@example.test"}).status_code == 409
    assert admin.post("/api/me/contacts", json={"email": "not-an-address"}).status_code == 422
    admin.ok("PUT", f"/api/me/contacts/{a['id']}", json={"name": "Buchhaltung", "email": "buha@example.test",
                                                         "is_default": True})
    s = admin.j("PATCH", "/api/me/settings", json={"email_subject_template": "Scan: {title}",
                                                   "email_body_template": "Anbei {filename} ({pages} Seiten)"})
    assert s["email_subject_template"] == "Scan: {title}"
    assert s["output_roots"] == ["Scans"]


def test_csrf_double_submit_mismatch() -> None:
    c = login(*ADMIN)
    c.headers["X-CSRF-Token"] = "forged"
    assert c.patch("/api/auth/me", json={"display_name": "x"}).status_code == 403
