"""Scanner service: client lifecycle, capabilities cache, status monitor, exclusive access."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

import structlog

from ..db import utcnow
from ..escl.client import EsclClient, EsclError, normalise_base_url
from ..escl.xml import ScannerCapabilities, ScannerStatus, parse_capabilities
from ..events import ALL

log = structlog.get_logger(__name__)

ADF_MESSAGES = {
    "ScannerAdfLoaded": ("ready", "paper_loaded"),
    "ScannerAdfEmpty": ("ready", "feeder_empty"),
    "ScannerAdfJam": ("error", "paper_jam"),
    "ScannerAdfDoorOpen": ("error", "cover_open"),
    "ScannerAdfHatchOpen": ("error", "cover_open"),
    "ScannerAdfMultipickDetected": ("error", "multipick"),
    "ScannerAdfMispick": ("error", "paper_jam"),
    "ScannerAdfProcessing": ("busy", "processing"),
}


@dataclass
class StatusSnapshot:
    configured: bool = False
    online: bool = False
    state: str = "unknown"  # ready | busy | error | offline | unconfigured
    escl_state: str | None = None
    adf_state: str | None = None
    code: str | None = None  # paper_loaded | feeder_empty | paper_jam | cover_open | multipick | …
    message: str | None = None
    paper_loaded: bool | None = None
    active_document_id: int | None = None
    checked_at: datetime | None = None
    model: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["checked_at"] = self.checked_at.isoformat() + "Z" if self.checked_at else None
        return d

    def key(self) -> tuple:
        return (self.online, self.state, self.escl_state, self.adf_state, self.code, self.active_document_id)


@dataclass
class ScannerService:
    ctx: Any  # AppContext (avoid import cycle)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    status: StatusSnapshot = field(default_factory=StatusSnapshot)
    _client: EsclClient | None = None
    _client_url: str | None = None
    _caps: ScannerCapabilities | None = None
    _monitor_task: asyncio.Task | None = None
    _wake: asyncio.Event = field(default_factory=asyncio.Event)
    active_document_id: int | None = None

    # ------------------------------------------------------------- configuration
    @property
    def url(self) -> str | None:
        u = self.ctx.settings.get("scanner.url")
        return u or None

    def client(self) -> EsclClient:
        url = self.url
        if not url:
            raise EsclError("no scanner address configured", code="scanner_not_configured")
        if self._client is None or self._client_url != url:
            if self._client is not None:
                asyncio.ensure_future(self._client.aclose())
            self._client = EsclClient(url, client_factory=self.ctx.scanner_client_factory)
            self._client_url = url
        return self._client

    def make_client(self, url: str) -> EsclClient:
        return EsclClient(url, client_factory=self.ctx.scanner_client_factory)

    async def close(self) -> None:
        await self.stop_monitor()
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------- capabilities
    @property
    def capabilities(self) -> ScannerCapabilities | None:
        if self._caps is None:
            cached = self.ctx.settings.get("scanner.capabilities")
            if cached:
                try:
                    self._caps = ScannerCapabilities.from_dict(cached)
                except Exception:  # noqa: BLE001 - stale cache format
                    xml = self.ctx.settings.get("scanner.capabilities_xml")
                    self._caps = parse_capabilities(xml) if xml else None
        return self._caps

    async def refresh_capabilities(self) -> ScannerCapabilities:
        caps, xml = await self.client().get_capabilities()
        self._caps = caps
        self.ctx.settings.set("scanner.capabilities", caps.to_dict())
        self.ctx.settings.set("scanner.capabilities_xml", xml.decode("utf-8", errors="replace"))
        self.ctx.settings.set("scanner.capabilities_fetched_at", utcnow().isoformat())
        self.ctx.bus.publish("scanner.capabilities", {"model": caps.make_and_model}, ALL)
        return caps

    async def test_connection(self, address: str) -> dict[str, Any]:
        """Used by the admin 'Test connection' button. Does not change settings."""
        url = normalise_base_url(address)
        client = self.make_client(url)
        try:
            caps, _xml = await client.get_capabilities()
            status = await client.get_status()
        finally:
            await client.aclose()
        return {
            "url": url,
            "model": caps.make_and_model,
            "serial_number": caps.serial_number,
            "uuid": caps.uuid,
            "escl_version": caps.version,
            "admin_uri": caps.admin_uri,
            "sources": caps.sources,
            "resolutions": caps.adf_simplex.resolutions if caps.adf_simplex else [],
            "status": {"state": status.state, "adf_state": status.adf_state},
        }

    # ------------------------------------------------------------- status
    def _snapshot_from(self, st: ScannerStatus) -> StatusSnapshot:
        snap = StatusSnapshot(configured=True, online=True, escl_state=st.state, adf_state=st.adf_state)
        snap.model = self._caps.make_and_model if self._caps else None
        mapped = ADF_MESSAGES.get(st.adf_state or "")
        if mapped:
            snap.state, snap.code = mapped
        else:
            snap.state, snap.code = "ready", None
        if st.state in ("Processing", "Testing") and snap.state == "ready":
            snap.state = "busy"
            snap.code = snap.code if snap.code not in (None, "paper_loaded", "feeder_empty") else "processing"
        if st.state in ("Stopped", "Down") and snap.state == "ready":
            snap.state = "error"
            snap.code = "scanner_stopped"
        snap.paper_loaded = {"ScannerAdfLoaded": True, "ScannerAdfEmpty": False}.get(st.adf_state or "")
        if self.active_document_id is not None:
            snap.state = "busy"
        snap.active_document_id = self.active_document_id
        return snap

    async def poll_status(self) -> StatusSnapshot:
        if not self.url:
            snap = StatusSnapshot(configured=False, state="unconfigured", code="scanner_not_configured")
        else:
            try:
                st = await self.client().get_status()
                if self._caps is None and self.capabilities is None:
                    try:
                        await self.refresh_capabilities()
                    except EsclError:
                        pass
                was_offline = not self.status.online
                snap = self._snapshot_from(st)
                if was_offline and self.status.checked_at is not None:
                    # Came back online → refresh capabilities (firmware update etc.)
                    try:
                        await self.refresh_capabilities()
                    except EsclError as e:
                        log.info("scanner.caps_refresh_failed", error=str(e))
            except EsclError as e:
                snap = StatusSnapshot(
                    configured=True, online=False, state="offline", code=e.code, message=e.message
                )
                snap.active_document_id = self.active_document_id
        snap.checked_at = utcnow()
        self._set_status(snap)
        return snap

    def _set_status(self, snap: StatusSnapshot) -> None:
        changed = snap.key() != self.status.key()
        self.status = snap
        if changed:
            self.ctx.bus.publish("scanner.status", snap.to_dict(), ALL)

    def set_active(self, document_id: int | None) -> None:
        self.active_document_id = document_id
        s = StatusSnapshot(**{**asdict(self.status)})
        s.active_document_id = document_id
        if document_id is not None:
            s.state = "busy"
        self._set_status(s)
        self.wake()

    def wake(self) -> None:
        self._wake.set()

    # ------------------------------------------------------------- monitor
    def start_monitor(self) -> None:
        if self._monitor_task is None:
            self._monitor_task = asyncio.create_task(self._monitor(), name="scanner-monitor")

    async def stop_monitor(self) -> None:
        if self._monitor_task is not None:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._monitor_task = None

    def _interval(self) -> float:
        if self.active_document_id is not None or self.ctx.bus.subscriber_count > 0:
            return 3.0
        return 60.0

    async def _monitor(self) -> None:
        while True:
            try:
                if not self.lock.locked() or self.active_document_id is not None:
                    await self.poll_status()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - monitor must never die
                log.warning("scanner.monitor_error", error=str(e))
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._interval())
            except TimeoutError:
                pass
