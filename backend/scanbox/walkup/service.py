"""Walk-up scan service (HP-specific, feature-flagged, polling only). See docs/WALKUP.md.

Isolated by design: any failure here is logged, shown in diagnostics and backed off;
nothing else in the app depends on it.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx
import structlog
from sqlalchemy import select

from ..db import session_scope, utcnow
from ..documents import DomainError, create_scan
from ..models import Profile
from .protocol import (
    HpWalkupClient,
    PollState,
    RegisteredDestination,
    WalkupDeviceBusy,
    WalkupError,
    WalkupEventType,
    WalkupScanRequest,
    WalkupSubscriptionLost,
    WalkupUnsupported,
    recommended_poll_timeout,
)

log = structlog.get_logger(__name__)

MAX_SHORTCUTS = 20


@dataclass
class WalkupStatus:
    enabled: bool = False
    state: str = "disabled"  # disabled | starting | registered | unsupported | error | backoff
    variant: str | None = None
    destination_uri: str | None = None
    shortcuts: list[str] = field(default_factory=list)
    last_event: str | None = None
    last_event_at: str | None = None
    last_error: str | None = None
    scans_started: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class WalkupService:
    def __init__(self, ctx) -> None:  # noqa: ANN001
        self.ctx = ctx
        self.status = WalkupStatus()
        self._task: asyncio.Task | None = None
        self._shortcut_profiles: dict[str, int] = {}

    # ---------------------------------------------------------------- lifecycle
    def enabled(self) -> bool:
        return bool(self.ctx.settings.get("walkup.enabled")) and bool(self.ctx.settings.get("scanner.url"))

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._supervise(), name="walkup")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None

    async def restart(self) -> None:
        await self.stop()
        self.start()

    def _set(self, **kw: Any) -> None:
        for k, v in kw.items():
            setattr(self.status, k, v)
        self.ctx.bus.publish("walkup.status", self.status.to_dict(), "admin")

    # ---------------------------------------------------------------- shortcuts
    def _shortcuts(self) -> list[str]:
        """One panel shortcut per built-in/shared profile, default profile first (max 20)."""
        default_id = self.ctx.settings.get("walkup.default_profile_id")
        with session_scope() as s:
            profiles = s.scalars(
                select(Profile).where(Profile.owner_id.is_(None)).order_by(Profile.sort_order, Profile.id)
            ).all()
            ordered = sorted(profiles, key=lambda p: (p.id != default_id, p.sort_order))
            self._shortcut_profiles = {}
            names = []
            for p in ordered[:MAX_SHORTCUTS]:
                label = p.name[:32]
                if label in self._shortcut_profiles:
                    continue
                self._shortcut_profiles[label] = p.id
                names.append(label)
        return names or ["Scanbox"]

    # ---------------------------------------------------------------- supervision
    async def _supervise(self) -> None:
        backoff = 5.0
        while True:
            if not self.enabled():
                self._set(enabled=False, state="disabled")
                await asyncio.sleep(30)
                continue
            self._set(enabled=True, state="starting", last_error=None)
            try:
                await self._session()
                backoff = 5.0
            except asyncio.CancelledError:
                raise
            except WalkupUnsupported as e:
                self._set(state="unsupported", last_error=str(e))
                log.warning("walkup.unsupported", error=str(e))
                await asyncio.sleep(3600)
            except Exception as e:  # noqa: BLE001 - must never crash the app
                self._set(state="backoff", last_error=f"{type(e).__name__}: {e}")
                log.warning("walkup.error", error=str(e), backoff=backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    async def _session(self) -> None:
        url = self.ctx.scanner.client().base_url
        factory = self.ctx.scanner_client_factory
        client = factory() if factory else httpx.AsyncClient(verify=False, timeout=httpx.Timeout(120.0, connect=5.0))  # noqa: S501
        async with client:
            hp = HpWalkupClient(client, url)
            support = await hp.probe()
            if not support.supported:
                raise WalkupUnsupported("scanner does not support walk-up destinations")
            name = (self.ctx.settings.get("walkup.destination_name") or "Scanbox")[:32]
            hostname = self.ctx.settings.get("walkup.hostname") or socket.gethostname()
            shortcuts = await asyncio.to_thread(self._shortcuts)
            stored = self.ctx.settings.get("walkup.state") or {}
            dest = await hp.register(name, hostname, shortcuts=shortcuts, known_uri=stored.get("uri"))
            await asyncio.to_thread(self.ctx.settings.set, "walkup.state",
                                    {"uri": dest.uri, "variant": str(dest.variant)})
            self._set(state="registered", variant=str(dest.variant), destination_uri=dest.uri,
                      shortcuts=list(dest.shortcuts))
            log.info("walkup.registered", uri=dest.uri, variant=str(dest.variant))
            await self._poll(hp, dest, recommended_poll_timeout(support), name, hostname, shortcuts)

    async def _poll(self, hp: HpWalkupClient, dest: RegisteredDestination, timeout: int, name: str,
                    hostname: str, shortcuts: list[str]) -> None:
        state = PollState(dest)
        busy = 0
        while self.enabled():
            try:
                ev = await hp.poll_event(state, timeout)
                busy = 0
            except WalkupSubscriptionLost:
                log.info("walkup.resubscribe")
                dest = await hp.register(name, hostname, shortcuts=shortcuts, known_uri=dest.uri)
                await asyncio.to_thread(self.ctx.settings.set, "walkup.state",
                                        {"uri": dest.uri, "variant": str(dest.variant)})
                state = PollState(dest)
                self._set(destination_uri=dest.uri)
                continue
            except WalkupDeviceBusy as e:
                busy += 1
                if busy >= 3:
                    with contextlib.suppress(WalkupError):
                        await hp.unregister(dest)
                    dest = await hp.register(name, hostname, shortcuts=shortcuts)
                    state = PollState(dest)
                    busy = 0
                await asyncio.sleep(max(1.0, e.retry_after_s or 5.0))
                continue
            if ev is None:
                await asyncio.sleep(1.0)
                continue
            self._set(last_event=str(ev.type), last_event_at=utcnow().isoformat() + "Z")
            log.info("walkup.event", type=str(ev.type), shortcut=ev.shortcut)
            if ev.type == WalkupEventType.SCAN_REQUESTED:
                req = await hp.requested_settings(ev)
                await self._start_scan(req)
            await asyncio.sleep(1.0)

    # ---------------------------------------------------------------- scan
    def _overrides(self, req: WalkupScanRequest) -> dict[str, Any]:
        o: dict[str, Any] = {}
        if req.duplex is not None:
            o["source"] = "adf_duplex" if req.duplex else "adf_simplex"
        cm = {"RGB24": "color", "Grayscale8": "gray", "BlackAndWhite1": "bw"}.get(req.color_mode or "")
        if cm:
            o["color_mode"] = cm
        caps = self.ctx.scanner.capabilities
        if req.x_resolution and caps and caps.adf_simplex and req.x_resolution in caps.adf_simplex.resolutions:
            o["resolution"] = req.x_resolution
        return o

    async def _start_scan(self, req: WalkupScanRequest) -> None:
        profile_id = self._shortcut_profiles.get(req.shortcut or "") or self.ctx.settings.get("walkup.default_profile_id")
        overrides = self._overrides(req)

        def _create() -> int:
            from ..documents import resolve_profile

            with session_scope() as s:
                profile = resolve_profile(s, profile_id, None)
                doc = create_scan(
                    s, self.ctx, user=None, profile=profile, overrides=overrides, origin="walkup",
                    auto_finish=False, scan_payload={"context_id": req.context_id} if req.context_id else None,
                )
                return doc.id

        try:
            doc_id = await asyncio.to_thread(_create)
            self.ctx.worker.notify("scanner")
            self._set(scans_started=self.status.scans_started + 1)
            log.info("walkup.scan_queued", document_id=doc_id, shortcut=req.shortcut)
        except DomainError as e:
            self._set(last_error=f"{e.code}: {e.message}")
            log.warning("walkup.scan_rejected", code=e.code, error=e.message)
