"""System test against a running container (docker-compose.systest.yml), not in-process.

Every external service is real on the network: the app container, the eSCL simulator, SMTP, Paperless,
an OpenID provider and a webhook receiver (tests/mocks/servers.py). OCR, PDF/A and Ghostscript run for
real inside the image under test.

Phases (tools/systest.sh):
  1. ``-m "not after_restart"`` – everything, ending with test_y_restart_prep, which leaves a scan and a
     processing job running;
  2. the app container is killed (SIGKILL) and started again;
  3. ``-m after_restart`` – recovery checks, authenticated with an API token from phase 1.
"""

from __future__ import annotations

import io
import json
import os
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

BASE = os.environ.get("SCANBOX_URL", "http://scanbox:8080")
SIM = os.environ.get("SIM_URL", "http://escl-sim:8090")
MOCKS = os.environ.get("MOCKS_URL", "http://mocks:9000")
PAPERLESS = os.environ.get("PAPERLESS_URL", "http://mocks:8000")
STATE = Path(os.environ.get("SYSTEST_STATE", "/state/state.json"))
OUTPUT = Path("/output")
CONSUME = Path("/consume")
ADMIN = ("admin", "adminadmin")

# No OCR/OSD: for tests about flow, not about text recognition.
FAST = {"ocr": {"enabled": False, "suggest_title": False}, "processing": {"auto_rotate": False, "deskew": False}}


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "after_restart: runs after the app container was killed and restarted")


# ------------------------------------------------------------------ state shared between phases
def state_get() -> dict:
    return json.loads(STATE.read_text()) if STATE.exists() else {}


def state_put(**kw: Any) -> None:
    s = state_get()
    s.update(kw)
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(s))


# ------------------------------------------------------------------ clients
class Api(httpx.Client):
    """httpx client for Scanbox that raises with the response body on unexpected status codes."""

    def ok(self, method: str, url: str, expect: int | tuple[int, ...] = (200, 201, 204), **kw: Any) -> httpx.Response:
        r = self.request(method, url, **kw)
        codes = (expect,) if isinstance(expect, int) else expect
        assert r.status_code in codes, f"{method} {url} → {r.status_code}: {r.text[:800]}"
        return r

    def j(self, method: str, url: str, **kw: Any) -> Any:
        return self.ok(method, url, **kw).json()


def client(**kw: Any) -> Api:
    return Api(base_url=BASE, timeout=60, **kw)


def login(username: str, password: str) -> Api:
    c = client()
    r = c.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    c.headers["X-CSRF-Token"] = r.json()["csrf_token"]
    return c


def token_client(token: str) -> Api:
    return client(headers={"Authorization": f"Bearer {token}"})


def oidc_login(claims: dict) -> Api:
    """Full browser-like code flow: Scanbox → provider → callback, following redirects by hand."""
    httpx.post(f"{MOCKS}/oidc/next-user", json=claims).raise_for_status()
    c = client(follow_redirects=False)
    r = c.get("/api/auth/oidc/login", params={"next": "/history"})
    assert r.status_code in (302, 303, 307), r.text
    r2 = httpx.get(r.headers["location"], follow_redirects=False)  # the provider (separate cookie jar)
    assert r2.status_code == 302, r2.text
    cb = r2.headers["location"]
    assert cb.startswith(f"{BASE}/api/auth/oidc/callback"), cb
    r3 = c.get(cb.removeprefix(BASE))
    assert r3.status_code in (302, 303, 307), r3.text
    assert "error" not in r3.headers["location"], r3.headers["location"]
    me = c.j("GET", "/api/auth/me")
    c.headers["X-CSRF-Token"] = c.cookies.get("scanbox_csrf") or me.get("csrf_token", "")
    return c


@pytest.fixture(scope="session")
def admin() -> Iterator[Api]:
    c = login(*ADMIN)
    yield c
    c.close()


# ------------------------------------------------------------------ simulator + mocks
def sim(stack: str | list | None = "letter_de", faults: dict | None = None, config: dict | None = None) -> None:
    with httpx.Client(base_url=SIM, timeout=15) as s:
        s.post("/_sim/reset").raise_for_status()
        s.delete("/_sim/faults").raise_for_status()
        if config:
            s.post("/_sim/config", json=config).raise_for_status()
        body: dict = {"empty": True} if stack is None else {"pages": stack} if isinstance(stack, list) else {"stack": stack}
        s.post("/_sim/load", json=body).raise_for_status()
        if faults:
            s.post("/_sim/faults", json=faults).raise_for_status()


def sim_faults(faults: dict) -> None:
    httpx.post(f"{SIM}/_sim/faults", json=faults, timeout=15).raise_for_status()


def mocks(method: str, path: str, **kw: Any) -> Any:
    r = httpx.request(method, f"{MOCKS}{path}", timeout=15, **kw)
    r.raise_for_status()
    return r.json()


# ------------------------------------------------------------------ helpers
def profile_id(c: Api, name_prefix: str) -> int:
    for p in c.j("GET", "/api/profiles"):
        if p["name"].startswith(name_prefix):
            return p["id"]
    raise AssertionError(f"no profile {name_prefix!r}")


def scan(c: Api, profile: int, overrides: dict | None = None, *, fast: bool = True, **kw: Any) -> dict:
    ov = {**(FAST if fast else {}), **(overrides or {})}
    if fast and "processing" in (overrides or {}):
        ov["processing"] = {**FAST["processing"], **overrides["processing"]}  # type: ignore[index]
    return c.j("POST", "/api/scans", json={"profile_id": profile, "overrides": ov, **kw})


def wait(c: Api, doc_id: int, states: str | tuple[str, ...], timeout: float = 240,
         cond: Callable[[dict], bool] | None = None) -> dict:
    states = (states,) if isinstance(states, str) else states
    terminal = {"failed", "needs_attention", "done", "discarded", "stored"}
    end = time.time() + timeout
    d: dict = {}
    while time.time() < end:
        d = c.j("GET", f"/api/documents/{doc_id}")
        if d["status"] in states and (cond is None or cond(d)):
            return d
        if d["status"] in terminal - set(states) and d["status"] not in ("stored",):
            raise AssertionError(f"doc {doc_id} ended in {d['status']} (wanted {states}): "
                                 f"{[(e['code'], e['message']) for e in d['events'][:6]]}")
        time.sleep(0.4)
    raise AssertionError(f"doc {doc_id} still {d.get('status')} after {timeout}s (wanted {states}): "
                         f"{[(e['code'], e['message']) for e in d.get('events', [])[:6]]}")


def finish(c: Api, doc_id: int, timeout: float = 300) -> dict:
    c.ok("POST", f"/api/documents/{doc_id}/finish")
    return wait(c, doc_id, "done", timeout)


def download(c: Api, doc_id: int) -> httpx.Response:
    return c.ok("GET", f"/api/documents/{doc_id}/download")


def pdf_pages(data: bytes) -> int:
    import pikepdf

    return len(pikepdf.open(io.BytesIO(data)).pages)


def events(d: dict) -> list[str]:
    return [e["code"] for e in d["events"]]
