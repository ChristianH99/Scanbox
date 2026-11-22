"""Async eSCL client with timeouts, retries and typed errors."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import httpx
import structlog

from .xml import ScanSettings, ScannerCapabilities, ScannerStatus, build_scan_settings, parse_capabilities, parse_status

log = structlog.get_logger(__name__)


class EsclError(Exception):
    code = "scanner_error"

    def __init__(self, message: str, *, status_code: int | None = None, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        if code:
            self.code = code


class ScannerOffline(EsclError):
    code = "scanner_offline"


class ScannerBusy(EsclError):
    code = "scanner_busy"


class ScannerTimeout(EsclError):
    code = "scanner_timeout"


class ScanRejected(EsclError):
    """409 / 400 on job creation: invalid settings or device condition (empty feeder, …)."""

    code = "scan_rejected"


class JobGone(EsclError):
    code = "scanner_job_lost"


@dataclass
class PageData:
    content: bytes
    content_type: str


@dataclass
class RetryPolicy:
    attempts: int = 3
    base_delay: float = 0.5
    max_delay: float = 5.0

    def delay(self, attempt: int) -> float:
        d = min(self.max_delay, self.base_delay * (2**attempt))
        return d * (0.75 + random.random() * 0.5)


DEFAULT_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
PAGE_TIMEOUT = httpx.Timeout(120.0, connect=5.0)


def normalise_base_url(address: str) -> str:
    """Accept 'host', 'host:port', 'http://host[:port][/eSCL]' → 'http://host[:port]'."""
    address = address.strip()
    if not address:
        raise ValueError("empty scanner address")
    if "://" not in address:
        address = "http://" + address
    p = urlparse(address)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ValueError(f"invalid scanner address: {address!r}")
    if any(c in p.hostname for c in "/\\@ "):
        raise ValueError("invalid host")
    path = p.path.rstrip("/")
    if path.lower().endswith("/escl"):
        path = path[: -len("/escl")]
    netloc = p.hostname if p.port is None else f"{p.hostname}:{p.port}"
    if ":" in p.hostname:  # IPv6
        netloc = f"[{p.hostname}]" + (f":{p.port}" if p.port else "")
    return f"{p.scheme}://{netloc}{path}"


class EsclClient:
    def __init__(
        self,
        base_url: str,
        *,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
        retry: RetryPolicy | None = None,
        root: str = "/eSCL",
    ) -> None:
        self.base_url = normalise_base_url(base_url)
        self.root = root
        self._client_factory = client_factory
        self._client: httpx.AsyncClient | None = None
        self.retry = retry or RetryPolicy()

    # -- lifecycle
    async def __aenter__(self) -> EsclClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            if self._client_factory:
                self._client = self._client_factory()
            else:
                self._client = httpx.AsyncClient(
                    timeout=DEFAULT_TIMEOUT,
                    verify=False,  # noqa: S501 - LAN scanner with self-signed cert
                    headers={"User-Agent": "Scanbox/1.0"},
                    follow_redirects=False,
                )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return self.resolve_job_uri(path)
        if not path.startswith("/"):
            path = f"{self.root}/{path}"
        return self.base_url + path

    def resolve_job_uri(self, location: str) -> str:
        """Scanners return absolute Location URLs with their own hostname (e.g. mDNS name that
        doesn't resolve inside Docker). Always talk to the configured address and keep only the path."""
        p = urlparse(urljoin(self.base_url + "/", location))
        return self.base_url + p.path

    # -- transport with retry
    async def _request(
        self,
        method: str,
        path: str,
        *,
        retry_on_503: bool = True,
        timeout: httpx.Timeout | None = None,
        **kw,
    ) -> httpx.Response:
        url = self.url(path)
        last_exc: Exception | None = None
        for attempt in range(self.retry.attempts):
            try:
                resp = await self.client.request(method, url, timeout=timeout or DEFAULT_TIMEOUT, **kw)
            except httpx.TimeoutException as e:
                last_exc = ScannerTimeout(f"{method} {path}: timeout")
                log.warning("escl.timeout", method=method, path=path, attempt=attempt, error=str(e))
            except (httpx.ConnectError, httpx.RemoteProtocolError, httpx.ReadError, httpx.WriteError) as e:
                last_exc = ScannerOffline(f"{method} {path}: {type(e).__name__}: {e}")
                log.warning("escl.transport_error", method=method, path=path, attempt=attempt, error=str(e))
            except httpx.HTTPError as e:
                last_exc = ScannerOffline(f"{method} {path}: {e}")
            else:
                if resp.status_code == 503 and retry_on_503 and attempt + 1 < self.retry.attempts:
                    await asyncio.sleep(self._retry_after(resp, attempt))
                    continue
                return resp
            if attempt + 1 < self.retry.attempts:
                await asyncio.sleep(self.retry.delay(attempt))
        assert last_exc is not None
        raise last_exc

    def _retry_after(self, resp: httpx.Response, attempt: int) -> float:
        ra = resp.headers.get("Retry-After")
        if ra:
            try:
                return max(0.05, min(float(ra), 10.0))
            except ValueError:
                pass
        return self.retry.delay(attempt)

    # -- API
    async def get_capabilities(self) -> tuple[ScannerCapabilities, bytes]:
        resp = await self._request("GET", "ScannerCapabilities")
        if resp.status_code != 200:
            raise EsclError(f"ScannerCapabilities returned HTTP {resp.status_code}", status_code=resp.status_code)
        try:
            return parse_capabilities(resp.content), resp.content
        except Exception as e:
            raise EsclError(f"invalid capabilities XML: {e}") from e

    async def get_status(self) -> ScannerStatus:
        resp = await self._request("GET", "ScannerStatus")
        if resp.status_code != 200:
            raise EsclError(f"ScannerStatus returned HTTP {resp.status_code}", status_code=resp.status_code)
        try:
            return parse_status(resp.content)
        except Exception as e:
            raise EsclError(f"invalid status XML: {e}") from e

    async def create_job(self, settings: ScanSettings) -> str:
        body = build_scan_settings(settings)
        # Job creation is not blindly retried on 503 (busy is a real state the caller handles).
        resp = await self._request(
            "POST", "ScanJobs", content=body, headers={"Content-Type": "text/xml"}, retry_on_503=False
        )
        if resp.status_code in (200, 201):
            loc = resp.headers.get("Location")
            if not loc:
                raise EsclError("scanner accepted job but sent no Location header")
            return self.resolve_job_uri(loc)
        if resp.status_code == 503:
            raise ScannerBusy("scanner is busy", status_code=503)
        if resp.status_code in (400, 409, 415):
            raise ScanRejected(
                f"scanner rejected the job (HTTP {resp.status_code})", status_code=resp.status_code
            )
        raise EsclError(f"ScanJobs returned HTTP {resp.status_code}", status_code=resp.status_code)

    async def next_document(self, job_uri: str, *, max_wait_s: float = 180.0) -> PageData | None:
        """Fetch the next page. Returns None when the job has no more pages (404).

        503 means the page is not ready yet → wait (Retry-After) up to ``max_wait_s``.
        Transport errors are retried a few times (``_request``) – the eSCL job keeps the page
        until it was transferred successfully.
        """
        url = job_uri.rstrip("/") + "/NextDocument"
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max_wait_s
        attempt = 0
        while True:
            resp = await self._request("GET", url, retry_on_503=False, timeout=PAGE_TIMEOUT)
            if resp.status_code == 200:
                ctype = resp.headers.get("Content-Type", "application/octet-stream").split(";")[0].strip()
                if not resp.content:
                    raise EsclError("scanner returned an empty page")
                return PageData(resp.content, ctype)
            if resp.status_code == 404:
                return None
            if resp.status_code == 503:
                if loop.time() >= deadline:
                    raise ScannerTimeout("timed out waiting for the next page")
                await asyncio.sleep(self._retry_after(resp, min(attempt, 3)))
                attempt += 1
                continue
            if resp.status_code == 410:
                raise JobGone("scan job no longer exists on the scanner", status_code=410)
            raise EsclError(f"NextDocument returned HTTP {resp.status_code}", status_code=resp.status_code)

    async def cancel_job(self, job_uri: str) -> None:
        try:
            await self._request("DELETE", job_uri, retry_on_503=False)
        except EsclError as e:
            log.info("escl.cancel_failed", job=job_uri, error=str(e))
