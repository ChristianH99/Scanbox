"""Login rate limiting. Runs last in phase 1: it blocks new password logins from this client's IP."""

from __future__ import annotations

from .conftest import client


def test_login_rate_limit() -> None:
    c = client()
    codes = [c.post("/api/auth/login", json={"username": "nobody-x", "password": f"wrong-{i}"}).status_code
             for i in range(15)]
    assert 429 in codes, codes
    r = c.post("/api/auth/login", json={"username": "nobody-x", "password": "wrong"})
    assert r.status_code == 429 and "retry-after" in {k.lower() for k in r.headers}
