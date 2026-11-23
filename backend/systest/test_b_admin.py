"""Admin: scanner connection, settings for SMTP / Paperless / SSO (configured in the app, not via env),
connection tests, diagnostics, logs, users, storage usage."""

from __future__ import annotations

from .conftest import MOCKS, PAPERLESS, Api, client


def test_scanner_status_caps_and_test(admin: Api) -> None:
    st = admin.j("GET", "/api/scanner/status")
    assert st["state"] in ("ready", "idle", "ready_empty") or st.get("reachable", True), st
    caps = admin.j("GET", "/api/scanner/capabilities")
    assert caps["make_and_model"] and caps["sources"], caps
    t = admin.j("POST", "/api/scanner/test", json={"address": "escl-sim:8090"})
    assert t["ok"] is True and t["url"] == "http://escl-sim:8090", t
    bad = admin.j("POST", "/api/scanner/test", json={"address": "http://192.0.2.1:1"})
    assert bad["ok"] is False and bad["error"]
    r = admin.j("POST", "/api/scanner/refresh")
    assert r["make_and_model"] == caps["make_and_model"]


def test_scanner_url_is_env_locked(admin: Api) -> None:
    s = admin.j("GET", "/api/admin/settings")["settings"]
    assert s["scanner.url"]["locked"] is True
    r = admin.patch("/api/admin/settings", json={"values": {"scanner.url": "http://elsewhere"}})
    assert r.status_code in (400, 409, 422), r.text


def test_configure_smtp_and_send_test_mail(admin: Api) -> None:
    out = admin.j("PATCH", "/api/admin/settings", json={"values": {
        "smtp.host": "mocks", "smtp.port": 2525, "smtp.security": "none", "smtp.username": "scanbox",
        "smtp.password": "mailpass", "smtp.from_address": "scanbox@example.test", "smtp.from_name": "Scanbox Test",
        "smtp.max_attachment_mb": 25}})["settings"]
    assert out["smtp.password"]["is_set"] is True and out["smtp.password"]["value"] in (None, "", "********")
    # the secret is never returned
    assert "mailpass" not in admin.get("/api/admin/settings").text
    assert admin.j("POST", "/api/admin/test/smtp")["ok"] is True
    from .conftest import mocks

    mocks("DELETE", "/smtp/messages")
    assert admin.j("POST", "/api/admin/test/mail", json={"to": "admin@example.test"})["ok"] is True
    msgs = mocks("GET", "/smtp/messages")
    assert len(msgs) == 1 and msgs[0]["to"] == ["admin@example.test"], msgs
    # wrong password is reported, not a 500
    admin.ok("PATCH", "/api/admin/settings", json={"values": {"smtp.password": "wrong"}})
    assert admin.j("POST", "/api/admin/test/smtp")["ok"] is False
    admin.ok("PATCH", "/api/admin/settings", json={"values": {"smtp.password": "mailpass"}})


def test_configure_paperless(admin: Api) -> None:
    admin.ok("PATCH", "/api/admin/settings", json={"values": {"paperless.url": PAPERLESS}})
    admin.ok("PATCH", "/api/me/settings", json={"paperless_token": "paperless-token"})
    r = admin.j("POST", "/api/admin/test/paperless")
    assert r["ok"] is True, r
    s = admin.j("GET", "/api/me/settings")
    assert s["paperless_token_set"] is True and s["paperless_url"] == PAPERLESS


def test_configure_sso_in_app(admin: Api) -> None:
    admin.ok("PATCH", "/api/admin/settings", json={"values": {
        "oidc.issuer": f"{MOCKS}/oidc", "oidc.client_id": "scanbox", "oidc.client_secret": "oidc-secret",
        "oidc.admin_claim": "groups", "oidc.admin_value": "scanbox-admins", "oidc.button_label": "Test SSO"}})
    cfg = client().get("/api/auth/config").json()
    assert cfg["oidc_enabled"] is True and cfg["oidc_label"] == "Test SSO"


def test_diagnostics_logs_usage_users(admin: Api) -> None:
    d = admin.j("GET", "/api/admin/diagnostics")
    text = str(d)
    for tool in ("tesseract", "ocrmypdf", "gs", "qpdf"):
        assert tool in text, f"{tool} missing from diagnostics"
    logs = admin.ok("GET", "/api/admin/logs")
    assert len(logs.content) > 100
    u = admin.j("GET", "/api/admin/usage")
    assert u["free_bytes"] > 0 and u["total_bytes"] > 0
    users = admin.j("GET", "/api/admin/users")
    assert any(x["username"] == "admin" for x in users)


def test_admin_only_endpoints_need_admin() -> None:
    assert client().get("/api/admin/settings").status_code == 401
