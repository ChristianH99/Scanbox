"""Shared fixtures: full app (real lifespan, workers, migrations) wired to the in-process eSCL simulator."""

from __future__ import annotations

import asyncio
import os
import shutil
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

from scanbox.config import AppConfig
from scanbox.context import AppContext
from scanbox.security.auth import login_limiter

ADMIN_PW = "admin-password-123"
HAVE_TESSERACT = shutil.which("tesseract") is not None and shutil.which("ocrmypdf") is not None

if os.environ.get("SCANBOX_REQUIRE_OCR") == "1" and not HAVE_TESSERACT:
    raise RuntimeError("SCANBOX_REQUIRE_OCR=1 but tesseract/ocrmypdf are missing – OCR tests would be skipped")

requires_ocr = pytest.mark.skipif(not HAVE_TESSERACT, reason="tesseract/ocrmypdf not installed")


@dataclass
class Harness:
    app: Any
    ctx: AppContext
    sim_state: Any
    sim_app: Any
    data_dir: Path
    clients: list[httpx.AsyncClient] = field(default_factory=list)
    extra_factories: dict[str, Any] = field(default_factory=dict)

    def client(self) -> httpx.AsyncClient:
        c = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://scanbox.test",
                              timeout=30)
        self.clients.append(c)
        return c

    async def login(self, username: str = "admin", password: str = ADMIN_PW) -> httpx.AsyncClient:
        c = self.client()
        r = await c.post("/api/auth/login", json={"username": username, "password": password})
        assert r.status_code == 200, r.text
        c.headers["X-CSRF-Token"] = c.cookies["scanbox_csrf"]
        return c

    async def user(self, username: str, *, admin: bool = False) -> httpx.AsyncClient:
        """Create a local user (as if provisioned) and return a logged-in client."""
        from scanbox.db import session_scope
        from scanbox.models import User
        from scanbox.security.auth import hash_password

        pw = f"pw-{username}-12345"
        with session_scope() as s:
            s.add(User(username=username, display_name=username.title(), is_local=True, is_admin=admin,
                       password_hash=hash_password(pw), email=f"{username}@example.test"))
        return await self.login(username, pw)

    async def wait_status(self, c: httpx.AsyncClient, doc_id: int, statuses: set[str] | str,
                          timeout: float = 60) -> dict:
        if isinstance(statuses, str):
            statuses = {statuses}
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        last = None
        while loop.time() < end:
            r = await c.get(f"/api/documents/{doc_id}")
            assert r.status_code == 200, r.text
            last = r.json()
            if last["status"] in statuses:
                return last
            await asyncio.sleep(0.1)
        raise AssertionError(f"document {doc_id} stuck in {last and last['status']}: "
                             f"{[e['code'] + ' ' + e['message'] for e in (last or {}).get('events', [])][:8]}")


def make_config(data_dir: Path, **kw: Any) -> AppConfig:
    base = {
        "data_dir": data_dir,
        "cookie_secure": False,
        "secret_key": "test-secret-key-0123456789abcdef",
        "admin_password": ADMIN_PW,
        "scanner_url": "http://sim",
        "min_free_mb": 0,
        "log_json": False,
        "log_level": "WARNING",
        "base_url": "http://scanbox.test",
    }
    base.update(kw)
    return AppConfig(**base)


@pytest.fixture
async def harness_factory(tmp_path: Path) -> AsyncIterator[Callable[..., Any]]:
    from escl_sim import SimState, create_app as create_sim

    from scanbox.main import create_app

    created: list[tuple[Any, Harness]] = []

    async def make(stack: str | None = "letter_de", data_dir: Path | None = None,
                   customize: Callable[[AppContext], None] | None = None, sim_state: Any = None,
                   **config_kw: Any) -> Harness:
        login_limiter.reset()
        state = sim_state or SimState(stack=stack)
        sim_app = create_sim(state)
        dd = data_dir or tmp_path / "data"
        holder: dict[str, AppContext] = {}

        def _customize(ctx: AppContext) -> None:
            ctx.scanner_client_factory = lambda: httpx.AsyncClient(
                transport=httpx.ASGITransport(app=sim_app), base_url="http://sim", timeout=30)
            holder["ctx"] = ctx
            if customize:
                customize(ctx)

        app = create_app(make_config(dd, **config_kw), customize=_customize)
        cm = app.router.lifespan_context(app)
        await cm.__aenter__()
        h = Harness(app=app, ctx=holder["ctx"], sim_state=state, sim_app=sim_app, data_dir=dd)
        created.append((cm, h))
        return h

    make.created = created  # type: ignore[attr-defined]
    yield make
    await close_harness(make)


@pytest.fixture
async def harness(harness_factory) -> Harness:  # noqa: ANN001
    return await harness_factory()


async def close_harness(factory) -> None:  # noqa: ANN001
    while factory.created:
        cm, h = factory.created.pop()
        for c in h.clients:
            await c.aclose()
        await cm.__aexit__(None, None, None)


async def builtin_profile_id(c: httpx.AsyncClient, key_name_prefix: str = "Dokument (PDF/A") -> int:
    r = await c.get("/api/profiles")
    for p in r.json():
        if p["name"].startswith(key_name_prefix):
            return p["id"]
    raise AssertionError("builtin profile not found")


FAST = {
    # keep tests fast and independent of tesseract: no OCR, no OSD
    "ocr": {"enabled": False, "suggest_title": False},
    "processing": {"auto_rotate": False, "deskew": False},
    "output": {"format": "pdf_image"},
    "resolution": 150,
}
