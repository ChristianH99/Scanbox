"""Fake HP device implementing the walk-up endpoints as documented in docs/WALKUP.md.

Usage::

    fake = create_walkup_fake(variant="escl")
    transport = httpx.ASGITransport(app=fake)          # the fake is itself an ASGI app
    ...
    fake.press_scan("Scanbox", shortcut="Document", XResolution=300, Duplex=True)

Variants:

* ``escl``           – /eSCL/WalkupSubscriptions with a (guessed) listing format.
* ``escl_nolist``    – same, but GET on the collection answers 405 and a second
                       POST with the same HostIdentifier returns 200 + the existing
                       Location (firmware behaviour reported by node-hp-scan-to).
* ``ledm``           – LEDM WalkupScanToComp + EventTable (+ comp event).
* ``ledm_walkupscan``– LEDM 1st generation WalkupScan + EventTable.
* ``none``           – eSCL scanning only, no walk-up.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape

from lxml import etree
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

REPO = Path(__file__).resolve().parents[2]
CAPS_FIXTURE = REPO / "docs" / "fixtures" / "scanner-capabilities.xml"

SCAN = "http://schemas.hp.com/imaging/escl/2011/05/03"
PWG = "http://www.pwg.org/schemas/2010/12/sm"
WUS = "http://www.hp.com/schemas/imaging/con/ledm/walkupscan/2010/09/28"
WUSREST = "http://www.hp.com/schemas/imaging/con/rest/walkupscan/2009/09/21"
DD = "http://www.hp.com/schemas/imaging/con/dictionaries/1.0/"
DD3 = "http://www.hp.com/schemas/imaging/con/dictionaries/2009/04/06"
EV = "http://www.hp.com/schemas/imaging/con/ledm/events/2007/09/16"
SCANTYPE = "http://www.hp.com/schemas/imaging/con/ledm/scantype/2008/03/17"

VARIANTS = ("escl", "escl_nolist", "ledm", "ledm_walkupscan", "none")


def _xml(body: str, status: int = 200, headers: dict[str, str] | None = None) -> Response:
    return Response(body, status_code=status, media_type="text/xml", headers=headers)


@dataclass
class _Sub:
    id: str
    host: str
    shortcuts: list[str]
    queue: asyncio.Queue[str] = field(default_factory=asyncio.Queue)


@dataclass
class _Dest:
    id: str
    name: str
    hostname: str
    shortcut: str | None = None
    plex: str | None = None


class FakeHpDevice:
    def __init__(
        self, variant: str = "escl", *, max_wait_s: float = 5.0, base_host: str = "http://192.0.2.10"
    ):
        if variant not in VARIANTS:
            raise ValueError(f"unknown variant {variant!r}")
        self.variant = variant
        self.max_wait_s = max_wait_s
        self.base_host = base_host
        self.requests: list[tuple[str, str]] = []
        self.fail_next_events: list[int] = []  # statuses returned by the next event polls
        # eSCL
        self.subs: dict[str, _Sub] = {}
        self.max_shortcuts = 20
        # LEDM
        self.dests: dict[str, _Dest] = {}
        self._events: list[tuple[str, str, list[tuple[str, str]]]] = [
            ("DeviceCapabilitiesChanged", "1-1", []),
            ("PowerUpEvent", "1-2", []),
        ]
        self._stamp = 2
        self._etag_n = 1
        self._changed = asyncio.Condition()
        self.comp_event = "HostSelected"
        self.app = Starlette(routes=self._routes())

    async def __call__(self, scope, receive, send):  # ASGI
        await self.app(scope, receive, send)

    # ------------------------------------------------------------------ routing

    def _routes(self) -> list[Route]:
        r = [
            Route("/eSCL/ScannerCapabilities", self.caps),
            Route("/eSCL/ScannerStatus", self.status),
            Route("/eSCL/WalkupSubscriptions", self.subs_collection, methods=["GET", "POST"]),
            Route("/eSCL/WalkupSubscriptions/{sid}", self.sub_item, methods=["GET", "DELETE"]),
            Route("/eSCL/WalkupSubscriptions/{sid}/Event", self.sub_event),
            Route("/DevMgmt/DiscoveryTree.xml", self.discovery_tree),
            Route("/EventMgmt/EventTable", self.event_table),
            Route("/WalkupScanToComp/WalkupScanToCompCaps", self.comp_caps),
            Route("/WalkupScanToComp/WalkupScanToCompEvent", self.comp_event_get),
            Route(
                "/WalkupScanToComp/WalkupScanToCompDestinations",
                self.dests_collection,
                methods=["GET", "POST"],
            ),
            Route(
                "/WalkupScanToComp/WalkupScanToCompDestinations/{did}",
                self.dest_item,
                methods=["GET", "DELETE"],
            ),
            Route("/WalkupScan/WalkupScanDestinations", self.dests_collection, methods=["GET", "POST"]),
            Route("/WalkupScan/WalkupScanDestinations/{did}", self.dest_item, methods=["GET", "DELETE"]),
        ]
        return r

    def _log(self, request: Request) -> None:
        self.requests.append((request.method, request.url.path))

    @property
    def _escl(self) -> bool:
        return self.variant in ("escl", "escl_nolist")

    @property
    def _ledm(self) -> bool:
        return self.variant in ("ledm", "ledm_walkupscan")

    @property
    def _dest_base(self) -> str:
        if self.variant == "ledm":
            return "/WalkupScanToComp/WalkupScanToCompDestinations"
        return "/WalkupScan/WalkupScanDestinations"

    # ------------------------------------------------------------------ eSCL

    async def caps(self, request: Request) -> Response:
        self._log(request)
        if self._ledm:
            return Response(status_code=404)
        xml = CAPS_FIXTURE.read_text(encoding="utf-8")
        if self.variant == "none":
            start = xml.index("<scan:WalkupScanSupport>")
            end = xml.index("</scan:WalkupScanSupport>") + len("</scan:WalkupScanSupport>")
            xml = xml[:start] + xml[end:]
        return _xml(xml)

    async def status(self, request: Request) -> Response:
        self._log(request)
        return _xml(
            f'<?xml version="1.0"?><scan:ScannerStatus xmlns:scan="{SCAN}" xmlns:pwg="{PWG}">'
            "<pwg:Version>2.9</pwg:Version><pwg:State>Idle</pwg:State>"
            "<scan:AdfState>ScannerAdfLoaded</scan:AdfState></scan:ScannerStatus>"
        )

    async def subs_collection(self, request: Request) -> Response:
        self._log(request)
        if not self._escl:
            return Response(status_code=404)
        if request.method == "GET":
            if self.variant == "escl_nolist":
                return Response(status_code=405)
            items = "".join(
                f"<scan:WalkupScanSubscription><scan:ResourceURI>/eSCL/WalkupSubscriptions/{s.id}</scan:ResourceURI>"
                f"<scan:HostIdentifier>{escape(s.host)}</scan:HostIdentifier><scan:Connection>Network</scan:Connection>"
                "<scan:Shortcuts>"
                + "".join(f"<scan:Shortcut>{escape(x)}</scan:Shortcut>" for x in s.shortcuts)
                + "</scan:Shortcuts></scan:WalkupScanSubscription>"
                for s in self.subs.values()
            )
            return _xml(
                f'<?xml version="1.0"?><scan:WalkupScanSubscriptions xmlns:scan="{SCAN}">{items}</scan:WalkupScanSubscriptions>'
            )
        root = etree.fromstring(await request.body())
        if etree.QName(root).localname != "WalkupScanSubscription" or etree.QName(root).namespace != SCAN:
            return Response(status_code=400)
        host = root.findtext(f"{{{SCAN}}}HostIdentifier") or ""
        shortcuts = [e.text or "" for e in root.iter(f"{{{SCAN}}}Shortcut")]
        if not host or not shortcuts or len(shortcuts) > self.max_shortcuts:
            return Response(status_code=400)
        if self.variant == "escl_nolist":
            for s in self.subs.values():
                if s.host == host:
                    s.shortcuts = shortcuts
                    return Response(
                        status_code=200,
                        headers={"Location": f"{self.base_host}/eSCL/WalkupSubscriptions/{s.id}"},
                    )
        if len(self.subs) >= 10:
            return Response(status_code=503)
        sid = str(uuid.uuid4())
        self.subs[sid] = _Sub(sid, host, shortcuts)
        return Response(
            status_code=201, headers={"Location": f"{self.base_host}/eSCL/WalkupSubscriptions/{sid}"}
        )

    async def sub_item(self, request: Request) -> Response:
        self._log(request)
        sid = request.path_params["sid"]
        if sid not in self.subs:
            return Response(status_code=403)
        if request.method == "DELETE":
            del self.subs[sid]
            return Response(status_code=200)
        return Response(status_code=405)

    async def sub_event(self, request: Request) -> Response:
        self._log(request)
        if self.fail_next_events:
            status = self.fail_next_events.pop(0)
            return Response(status_code=status, headers={"Retry-After": "1"} if status == 503 else None)
        sub = self.subs.get(request.path_params["sid"])
        if sub is None:
            return Response(status_code=403)  # stale id after reboot (8120e behaviour)
        timeout = min(float(request.query_params.get("timeout", "100")), self.max_wait_s)
        try:
            body = await asyncio.wait_for(sub.queue.get(), timeout)
        except TimeoutError:
            return Response(status_code=204)
        return _xml(body)

    def _escl_event(
        self, sub: _Sub, event: str, shortcut: str | None = None, settings: dict | None = None
    ) -> str:
        parts = [
            f'<?xml version="1.0"?><scan:WalkupScanEvent xmlns:scan="{SCAN}" xmlns:pwg="{PWG}">',
            f"<scan:ResourceURI>/eSCL/WalkupSubscriptions/{sub.id}</scan:ResourceURI>",
            f"<scan:HostIdentifier>{escape(sub.host)}</scan:HostIdentifier>",
            "<scan:Connection>Network</scan:Connection>",
            f"<scan:Event>{event}</scan:Event>",
        ]
        if shortcut is not None:
            parts.append(f"<scan:Shortcut>{escape(shortcut)}</scan:Shortcut>")
        if settings is not None:
            parts.append("<scan:ScanSettings><pwg:Version>2.4</pwg:Version>")
            for k, v in settings.items():
                prefix = "pwg" if k in ("InputSource", "DocumentFormat", "ContentType") else "scan"
                if isinstance(v, bool):
                    v = "true" if v else "false"
                parts.append(f"<{prefix}:{k}>{escape(str(v))}</{prefix}:{k}>")
            parts.append("</scan:ScanSettings>")
        parts.append("</scan:WalkupScanEvent>")
        return "".join(parts)

    # ------------------------------------------------------------------ LEDM

    async def discovery_tree(self, request: Request) -> Response:
        self._log(request)
        if not self._ledm:
            return Response(status_code=404)
        rt = "ledm:hpLedmWalkupScanToCompManifest" if self.variant == "ledm" else "hpCnxWalkupScanManifest"
        uri = (
            "/WalkupScanToComp/WalkupScanToCompManifest.xml"
            if self.variant == "ledm"
            else "/WalkupScan/WalkupScanManifest.xml"
        )
        return _xml(
            f'<?xml version="1.0"?><ledm:DiscoveryTree xmlns:ledm="http://www.hp.com/schemas/imaging/con/ledm/2007/09/21" xmlns:dd="{DD}">'
            "<ledm:SupportedIfc><ledm:ManifestURI>/EventMgmt/EventMgmtManifest.xml</ledm:ManifestURI>"
            "<dd:ResourceType>ledm:hpLedmEventMgmtManifest</dd:ResourceType></ledm:SupportedIfc>"
            f"<ledm:SupportedIfc><ledm:ManifestURI>{uri}</ledm:ManifestURI><dd:ResourceType>{rt}</dd:ResourceType></ledm:SupportedIfc>"
            "</ledm:DiscoveryTree>"
        )

    async def comp_caps(self, request: Request) -> Response:
        self._log(request)
        if self.variant != "ledm":
            return Response(status_code=404)
        return _xml(
            f'<?xml version="1.0"?><wus:WalkupScanToCompCaps xmlns:wus="{WUS}" xmlns:dd="{DD}">'
            "<wus:MaxNetworkDestinations>15</wus:MaxNetworkDestinations>"
            "<wus:SupportsMultiItemScanFromPlaten>true</wus:SupportsMultiItemScanFromPlaten>"
            "<wus:UserActionTimeout><dd:ValueFloat>60</dd:ValueFloat><dd:Unit>seconds</dd:Unit></wus:UserActionTimeout>"
            "</wus:WalkupScanToCompCaps>"
        )

    async def comp_event_get(self, request: Request) -> Response:
        self._log(request)
        if self.variant != "ledm":
            return Response(status_code=404)
        return _xml(
            f'<?xml version="1.0"?><wus:WalkupScanToCompEvent xmlns:wus="{WUS}">'
            f"<wus:WalkupScanToCompEventType>{self.comp_event}</wus:WalkupScanToCompEventType></wus:WalkupScanToCompEvent>"
        )

    def _ns(self) -> tuple[str, str]:
        return (WUS, "WalkupScanToComp") if self.variant == "ledm" else (WUSREST, "WalkupScan")

    def _dest_xml(self, d: _Dest, with_settings: bool) -> str:
        _, prefix = self._ns()
        settings = ""
        if with_settings and d.shortcut:
            settings = (
                f"<wus:{prefix}Settings><scantype:ScanSettings><dd:ScanPlexMode>{d.plex or 'Simplex'}</dd:ScanPlexMode>"
                f"</scantype:ScanSettings><wus:Shortcut>{d.shortcut}</wus:Shortcut></wus:{prefix}Settings>"
            )
        return (
            f"<wus:{prefix}Destination><dd:ResourceURI>{self._dest_base}/{d.id}</dd:ResourceURI>"
            f"<dd:Name>{escape(d.name)}</dd:Name><dd3:Hostname>{escape(d.hostname)}</dd3:Hostname>"
            f"<wus:LinkType>Network</wus:LinkType>{settings}</wus:{prefix}Destination>"
        )

    def _ns_decl(self) -> str:
        ns, _ = self._ns()
        return f'xmlns:wus="{ns}" xmlns:dd="{DD}" xmlns:dd3="{DD3}" xmlns:scantype="{SCANTYPE}"'

    async def dests_collection(self, request: Request) -> Response:
        self._log(request)
        if not self._ledm or not request.url.path.startswith(self._dest_base):
            return Response(status_code=404)
        _, prefix = self._ns()
        if request.method == "GET":
            items = "".join(self._dest_xml(d, False) for d in self.dests.values())
            return _xml(
                f'<?xml version="1.0"?><wus:{prefix}Destinations {self._ns_decl()}>{items}</wus:{prefix}Destinations>'
            )
        root = etree.fromstring(await request.body())
        ns, _ = self._ns()
        if etree.QName(root).localname != f"{prefix}Destination" or etree.QName(root).namespace != ns:
            return Response(status_code=400)
        name = root.findtext(f"{{{DD}}}Name") or ""
        hostname = root.findtext(f"{{{DD3}}}Hostname") or ""
        did = str(uuid.uuid4())
        self.dests[did] = _Dest(did, name, hostname)
        return Response(status_code=201, headers={"Location": f"{self.base_host}{self._dest_base}/{did}"})

    async def dest_item(self, request: Request) -> Response:
        self._log(request)
        d = self.dests.get(request.path_params["did"])
        if d is None or not request.url.path.startswith(self._dest_base):
            return Response(status_code=404)
        if request.method == "DELETE":
            del self.dests[d.id]
            return Response(status_code=204)
        _, prefix = self._ns()
        body = self._dest_xml(d, True).replace(
            f"<wus:{prefix}Destination>", f"<wus:{prefix}Destination {self._ns_decl()}>", 1
        )
        if self.variant == "ledm_walkupscan":
            body = f"<wus:WalkupScanDestinations {self._ns_decl()}>{self._dest_xml(d, True)}</wus:WalkupScanDestinations>"
        return _xml('<?xml version="1.0"?>' + body)

    def _etag(self) -> str:
        return f'"ev-{self._etag_n}"'

    async def event_table(self, request: Request) -> Response:
        self._log(request)
        if not self._ledm:
            return Response(status_code=404)
        inm = request.headers.get("if-none-match")
        timeout = min(float(request.query_params.get("timeout", "0")) / 10, self.max_wait_s)
        if inm and inm == self._etag():
            async with self._changed:
                try:
                    await asyncio.wait_for(self._changed.wait_for(lambda: inm != self._etag()), timeout)
                except TimeoutError:
                    return Response(status_code=304, headers={"ETag": self._etag()})
        events = []
        for cat, stamp, payloads in self._events:
            p = "".join(
                f"<ev:Payload><dd:ResourceURI>{u}</dd:ResourceURI><dd:ResourceType>{t}</dd:ResourceType></ev:Payload>"
                for u, t in payloads
            )
            events.append(
                f"<ev:Event><dd:UnqualifiedEventCategory>{cat}</dd:UnqualifiedEventCategory><dd:AgingStamp>{stamp}</dd:AgingStamp>{p}</ev:Event>"
            )
        return _xml(
            f'<?xml version="1.0"?><ev:EventTable xmlns:ev="{EV}" xmlns:dd="{DD}">{"".join(events)}</ev:EventTable>',
            headers={"ETag": self._etag()},
        )

    async def _add_ledm_event(self, d: _Dest) -> None:
        self._stamp += 1
        if self.variant == "ledm":
            payloads = [
                (f"{self._dest_base}/{d.id}", "wus:WalkupScanToCompDestination"),
                ("/WalkupScanToComp/WalkupScanToCompEvent", "wus:WalkupScanToCompEvent"),
            ]
        else:
            payloads = [(f"{self.base_host}:8080{self._dest_base}/{d.id}", "hpCnxWalkupScanDestinations")]
        self._events.append(("ScanEvent", f"10-{self._stamp}", payloads))
        self._events = self._events[-20:]
        async with self._changed:
            self._etag_n += 1
            self._changed.notify_all()

    # ------------------------------------------------------------------ test helpers

    def _sub_by_name(self, name: str) -> _Sub:
        for s in self.subs.values():
            if s.host == name:
                return s
        raise KeyError(f"no subscription for {name!r}")

    def _dest_by_name(self, name: str) -> _Dest:
        for d in self.dests.values():
            if d.name == name:
                return d
        raise KeyError(f"no destination named {name!r}")

    async def select_host(self, name: str) -> None:
        """User picks destination ``name`` on the panel."""
        if self._escl:
            s = self._sub_by_name(name)
            s.queue.put_nowait(self._escl_event(s, "HostSelected"))
        elif self._ledm:
            d = self._dest_by_name(name)
            d.shortcut = None
            self.comp_event = "HostSelected"
            await self._add_ledm_event(d)

    async def start_scan(self, name: str, shortcut: str = "SavePDF", **settings) -> None:
        """User picks ``shortcut`` and presses Start with ``settings`` (eSCL element names)."""
        if self._escl:
            s = self._sub_by_name(name)
            s.queue.put_nowait(self._escl_event(s, "ScanRequested", shortcut, settings))
        elif self._ledm:
            d = self._dest_by_name(name)
            d.shortcut = shortcut
            d.plex = "Duplex" if settings.get("Duplex") else "Simplex"
            self.comp_event = "ScanRequested"
            if self.variant == "ledm_walkupscan":
                await self._add_ledm_event(d)

    async def press_scan(self, name: str, shortcut: str = "SavePDF", **settings) -> None:
        """Simulate "user selected destination ``name`` and pressed scan with ``settings``"."""
        await self.select_host(name)
        await self.start_scan(name, shortcut, **settings)

    async def request_new_page(self, name: str) -> None:
        if self._escl:
            s = self._sub_by_name(name)
            s.queue.put_nowait(self._escl_event(s, "ScanRequested", "SavePDF", {}))
        else:
            self.comp_event = "ScanNewPageRequested"

    async def complete_session(self, name: str) -> None:
        if self._escl:
            s = self._sub_by_name(name)
            s.queue.put_nowait(self._escl_event(s, "ScanPagesComplete"))
        else:
            self.comp_event = "ScanPagesComplete"

    def reboot(self) -> None:
        """Device reboot: all registrations are forgotten."""
        self.subs.clear()
        self.dests.clear()
        self._etag_n += 100
        self._events = [("PowerUpEvent", "1-1", [])]


def create_walkup_fake(variant: str = "escl", **kwargs) -> FakeHpDevice:
    """ASGI app factory. The returned object is an ASGI app with test helpers."""
    return FakeHpDevice(variant, **kwargs)
