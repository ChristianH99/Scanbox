"""First-start setup with the one-time code from the log, and changing the local password in the app."""

from __future__ import annotations

from pathlib import Path

import pytest

from .conftest import ADMIN_PW, close_harness

NEW_PW = "brand-new-password-42"


async def _fresh(harness_factory, data_dir: Path | None = None):  # noqa: ANN001, ANN202
    """App started without SCANBOX_ADMIN_PASSWORD: setup is pending."""
    return await harness_factory(admin_password=None, data_dir=data_dir)


async def _setup(c, code: str, username: str = "boss", password: str = NEW_PW):  # noqa: ANN001, ANN202
    return await c.post("/api/auth/setup", json={"code": code, "username": username, "password": password})


async def test_setup_requires_the_right_code(harness_factory) -> None:  # noqa: ANN001
    h = await _fresh(harness_factory)
    code = h.ctx.setup_code
    assert code and len(code) == 14  # XXXX-XXXX-XXXX
    c = h.client()
    cfg = (await c.get("/api/auth/config")).json()
    assert cfg["setup_required"] is True
    assert code not in str(cfg)  # the code itself is never served

    r = await _setup(c, "AAAA-BBBB-CCCC")
    assert r.status_code == 403 and r.json()["detail"]["code"] == "invalid_setup_code"
    r = await _setup(c, "")
    assert r.status_code == 422
    # Nobody was created, nobody is signed in.
    assert (await c.get("/api/auth/me")).status_code == 401
    assert (await c.post("/api/auth/login", json={"username": "boss", "password": NEW_PW})).status_code == 401

    # Weak password is refused even with the right code.
    r = await _setup(c, code, password="short")
    assert r.status_code == 422

    # Lower case and without dashes is accepted (typed off the log).
    r = await _setup(c, code.replace("-", "").lower())
    assert r.status_code == 201, r.text
    me = r.json()
    assert me["user"]["username"] == "boss" and me["user"]["is_admin"] and me["user"]["is_local"]
    assert me["can_change_password"] is True
    # The setup response signs the new admin in.
    assert (await c.get("/api/auth/me")).json()["user"]["username"] == "boss"
    await h.login("boss", NEW_PW)


async def test_setup_closes_after_one_use(harness_factory, tmp_path: Path) -> None:  # noqa: ANN001
    data = tmp_path / "d"
    h = await _fresh(harness_factory, data)
    code = h.ctx.setup_code
    assert (await _setup(h.client(), code)).status_code == 201

    assert h.ctx.setup_code is None
    c = h.client()
    assert (await c.get("/api/auth/config")).json()["setup_required"] is False
    r = await _setup(c, code, username="intruder")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "setup_closed"

    # Still closed after a restart: no new code is generated once an admin exists.
    await close_harness(harness_factory)
    h2 = await _fresh(harness_factory, data)
    assert h2.ctx.setup_code is None
    c2 = h2.client()
    assert (await c2.get("/api/auth/config")).json()["setup_required"] is False
    assert (await _setup(c2, code, username="intruder")).status_code == 409
    await h2.login("boss", NEW_PW)


async def test_setup_code_rejected_after_restart(harness_factory, tmp_path: Path) -> None:  # noqa: ANN001
    data = tmp_path / "d"
    h = await _fresh(harness_factory, data)
    old = h.ctx.setup_code
    await close_harness(harness_factory)

    h2 = await _fresh(harness_factory, data)
    new = h2.ctx.setup_code
    assert new and new != old
    c = h2.client()
    r = await _setup(c, old)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "invalid_setup_code"
    assert (await _setup(c, new)).status_code == 201


async def test_setup_code_attempts_are_rate_limited(harness_factory) -> None:  # noqa: ANN001
    h = await _fresh(harness_factory)
    c = h.client()
    for _ in range(10):
        assert (await _setup(c, "WRONG-WRONG-WRNG")).status_code == 403
    r = await _setup(c, h.ctx.setup_code)
    assert r.status_code == 429


async def test_env_password_disables_setup_and_in_app_change(harness) -> None:  # noqa: ANN001
    assert harness.ctx.setup_code is None
    c = await harness.login()
    assert (await c.get("/api/auth/config")).json()["setup_required"] is False
    assert (await _setup(c, "AAAA-BBBB-CCCC")).status_code == 409
    me = (await c.get("/api/auth/me")).json()
    assert me["can_change_password"] is False
    r = await c.post("/api/auth/password", json={"current_password": ADMIN_PW, "new_password": NEW_PW})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "password_locked"
    await harness.login("admin", ADMIN_PW)


async def test_password_change_requires_current_and_ends_other_sessions(harness_factory) -> None:  # noqa: ANN001
    h = await _fresh(harness_factory)
    this = h.client()
    assert (await _setup(this, h.ctx.setup_code, password=ADMIN_PW)).status_code == 201
    this.headers["X-CSRF-Token"] = this.cookies["scanbox_csrf"]
    other = await h.login("boss", ADMIN_PW)
    assert (await other.get("/api/auth/me")).status_code == 200

    r = await this.post("/api/auth/password", json={"current_password": "not-the-password", "new_password": NEW_PW})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "invalid_current_password"
    r = await this.post("/api/auth/password", json={"current_password": ADMIN_PW, "new_password": "short"})
    assert r.status_code == 422
    # Nothing changed so far.
    assert (await other.get("/api/auth/me")).status_code == 200

    r = await this.post("/api/auth/password", json={"current_password": ADMIN_PW, "new_password": NEW_PW})
    assert r.status_code == 204, r.text
    # This session stays, the other one is signed out.
    assert (await this.get("/api/auth/me")).status_code == 200
    assert (await other.get("/api/auth/me")).status_code == 401
    # Old password no longer works, the new one does.
    old = h.client()
    assert (await old.post("/api/auth/login", json={"username": "boss", "password": ADMIN_PW})).status_code == 401
    await h.login("boss", NEW_PW)


async def test_password_change_needs_csrf_and_login(harness_factory) -> None:  # noqa: ANN001
    h = await _fresh(harness_factory)
    c = h.client()
    assert (await _setup(c, h.ctx.setup_code)).status_code == 201
    # Session cookie but no CSRF header.
    r = await c.post("/api/auth/password", json={"current_password": NEW_PW, "new_password": "another-password-1"})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "csrf_failed"
    anon = h.client()
    r = await anon.post("/api/auth/password", json={"current_password": NEW_PW, "new_password": "another-password-1"})
    assert r.status_code == 401


@pytest.mark.parametrize("username", ["", "with space", "a/b"])
async def test_setup_validates_username(harness_factory, username: str) -> None:  # noqa: ANN001
    h = await _fresh(harness_factory)
    assert (await _setup(h.client(), h.ctx.setup_code, username=username)).status_code == 422
    assert h.ctx.setup_code is not None
