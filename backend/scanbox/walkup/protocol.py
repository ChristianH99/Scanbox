"""HP walk-up scan ("Scan to Computer") protocol layer.

Pure protocol code: XML builders/parsers plus an async httpx client. No app
imports, no database, no global state. See ``docs/WALKUP.md`` for the research
behind every endpoint and payload used here.

Three device variants are supported, selected by :meth:`HpWalkupClient.probe`:

* ``ESCL`` – HP eSCL extension (newer firmware, advertised by
  ``<scan:WalkupScanSupport>`` in ``ScannerCapabilities``):
  ``POST /eSCL/WalkupSubscriptions`` registers the host plus a list of
  shortcuts; ``GET /eSCL/WalkupSubscriptions/{id}/Event?timeout=N`` long-polls
  for ``HostSelected`` / ``ScanRequested`` / ``ScanPagesComplete``. The
  ``ScanRequested`` event carries the shortcut and the settings chosen on the
  panel. This is the expected variant for the ScanJet Pro N4000 snw1.
* ``LEDM_SCAN_TO_COMP`` – HP LEDM 2nd generation
  (``/WalkupScanToComp/WalkupScanToCompDestinations`` + ``/EventMgmt/EventTable``
  long-poll + ``/WalkupScanToComp/WalkupScanToCompEvent``).
* ``LEDM_WALKUP_SCAN`` – HP LEDM 1st generation
  (``/WalkupScan/WalkupScanDestinations`` + ``/EventMgmt/EventTable``).

Every variant is polling-only: the device never connects back to us.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit

import httpx
from lxml import etree

__all__ = [
    "NS",
    "HpWalkupClient",
    "PollState",
    "RegisteredDestination",
    "WalkupDeviceBusy",
    "WalkupError",
    "WalkupEvent",
    "WalkupEventType",
    "WalkupProtocolError",
    "WalkupScanRequest",
    "WalkupSubscriptionLost",
    "WalkupSupport",
    "WalkupTimeout",
    "WalkupUnreachable",
    "WalkupUnsupported",
    "WalkupVariant",
    "build_escl_subscription",
    "build_ledm_destination",
    "parse_escl_event",
    "parse_escl_subscriptions",
    "parse_event_table",
    "parse_ledm_destinations",
    "parse_walkup_support",
    "recommended_poll_timeout",
]

# --------------------------------------------------------------------------- namespaces

NS: dict[str, str] = {
    "scan": "http://schemas.hp.com/imaging/escl/2011/05/03",
    "pwg": "http://www.pwg.org/schemas/2010/12/sm",
    # HP destination extension advertised in the N4000 ScannerCapabilities.
    "dest": "http://schemas.hp.com/imaging/destination/2011/06/06",
    # Namespace declared (unused) on the 8122e WalkupScanEvent.
    "httpdest": "http://schemas.hp.com/imaging/httpdestination/2011/10/13",
    "xsi": "http://www.w3.org/2001/XMLSchema-instance",
    "ledm": "http://www.hp.com/schemas/imaging/con/ledm/2007/09/21",
    "dd": "http://www.hp.com/schemas/imaging/con/dictionaries/1.0/",
    "dd3": "http://www.hp.com/schemas/imaging/con/dictionaries/2009/04/06",
    "ev": "http://www.hp.com/schemas/imaging/con/ledm/events/2007/09/16",
    # LEDM WalkupScanToComp (2nd gen).
    "wus": "http://www.hp.com/schemas/imaging/con/ledm/walkupscan/2010/09/28",
    # LEDM WalkupScan (1st gen) REST namespace.
    "wusrest": "http://www.hp.com/schemas/imaging/con/rest/walkupscan/2009/09/21",
    "scantype": "http://www.hp.com/schemas/imaging/con/ledm/scantype/2008/03/17",
}

ESCL_SUBSCRIPTIONS = "/eSCL/WalkupSubscriptions"
ESCL_CAPS_PATHS = ("/eSCL/ScannerCapabilities", "/eSCL/ScanCapabilities")
DISCOVERY_TREE = "/DevMgmt/DiscoveryTree.xml"
EVENT_TABLE = "/EventMgmt/EventTable"
LEDM_COMP_CAPS = "/WalkupScanToComp/WalkupScanToCompCaps"
LEDM_COMP_DESTS = "/WalkupScanToComp/WalkupScanToCompDestinations"
LEDM_COMP_EVENT = "/WalkupScanToComp/WalkupScanToCompEvent"
LEDM_WS_DESTS = "/WalkupScan/WalkupScanDestinations"

_PARSER = etree.XMLParser(
    resolve_entities=False, no_network=True, remove_comments=True, remove_pis=True, huge_tree=False
)

DEFAULT_TIMEOUT_S = 10.0
CONNECT_TIMEOUT_S = 5.0
LONG_POLL_GRACE_S = 15.0
ESCL_MAX_POLL_S = 100  # what HP's own Windows client sends (8122e capture)
LEDM_MAX_POLL_DS = 1200  # deciseconds, node-hp-scan-to default/maximum


# --------------------------------------------------------------------------- exceptions


class WalkupError(Exception):
    """Base class for all walk-up errors."""


class WalkupUnsupported(WalkupError):
    """The device (or this operation on it) does not support walk-up scanning."""


class WalkupProtocolError(WalkupError):
    """Unexpected HTTP status or malformed XML."""

    def __init__(self, message: str, *, status: int | None = None, body: str | None = None):
        super().__init__(message if status is None else f"{message} (HTTP {status})")
        self.status = status
        self.body = body


class WalkupSubscriptionLost(WalkupProtocolError):
    """The registration is gone (device reboot, expiry, deleted). Register again."""


class WalkupDeviceBusy(WalkupProtocolError):
    """HTTP 503. Retry after ``retry_after_s`` (if the device sent one)."""

    def __init__(self, message: str, *, status: int | None = 503, retry_after_s: float | None = None):
        super().__init__(message, status=status)
        self.retry_after_s = retry_after_s


class WalkupUnreachable(WalkupError):
    """Connection refused / reset / DNS / TLS failure."""


class WalkupTimeout(WalkupError):
    """The HTTP call exceeded its timeout (including long-poll grace)."""


# --------------------------------------------------------------------------- data model


class WalkupVariant(StrEnum):
    ESCL = "escl"
    LEDM_SCAN_TO_COMP = "ledm_scan_to_comp"
    LEDM_WALKUP_SCAN = "ledm_walkup_scan"


class WalkupEventType(StrEnum):
    HOST_SELECTED = "HostSelected"
    SCAN_REQUESTED = "ScanRequested"
    SCAN_NEW_PAGE_REQUESTED = "ScanNewPageRequested"
    SCAN_PAGES_COMPLETE = "ScanPagesComplete"
    OTHER = "Other"

    @classmethod
    def parse(cls, value: str | None) -> WalkupEventType:
        value = (value or "").strip()
        for member in cls:
            if member.value == value:
                return member
        return cls.OTHER


@dataclass(frozen=True, slots=True)
class WalkupSupport:
    """Result of :meth:`HpWalkupClient.probe`."""

    variants: tuple[WalkupVariant, ...] = ()
    #: eSCL ``<scan:WalkupScanSupport>`` present in ScannerCapabilities.
    escl_caps_advertised: bool = False
    max_destinations: int | None = None
    max_shortcuts: int | None = None
    #: eSCL: seconds a subscription survives without the client polling (interpretation, see WALKUP.md).
    subscription_timeout_s: int | None = None
    #: eSCL: seconds the panel waits for the user / host during a session (interpretation).
    inactivity_timeout_s: int | None = None
    #: LEDM WalkupScanToCompCaps ``UserActionTimeout``.
    user_action_timeout_s: int | None = None
    pin_support: bool | None = None
    #: Path -> HTTP status (``None`` = transport error) for every endpoint probed.
    endpoints: dict[str, int | None] = field(default_factory=dict)

    @property
    def supported(self) -> bool:
        return bool(self.variants)

    @property
    def preferred(self) -> WalkupVariant | None:
        return self.variants[0] if self.variants else None


@dataclass(frozen=True, slots=True)
class RegisteredDestination:
    """A destination (LEDM) or subscription (eSCL) registered on the device."""

    variant: WalkupVariant
    #: Path on the device, e.g. ``/eSCL/WalkupSubscriptions/<uuid>``.
    uri: str
    #: Text shown on the panel (eSCL HostIdentifier / LEDM Name).
    name: str
    hostname: str = ""
    shortcuts: tuple[str, ...] = ()

    @property
    def id(self) -> str:
        return self.uri.rstrip("/").rsplit("/", 1)[-1]


@dataclass(slots=True)
class PollState:
    """Mutable cursor for :meth:`HpWalkupClient.poll_event`. One per registration."""

    destination: RegisteredDestination
    #: LEDM EventTable ETag (``If-None-Match``).
    etag: str = ""
    #: LEDM: the initial event table (historic events) has been consumed.
    primed: bool = False
    #: LEDM AgingStamps already handled (bounded).
    seen_stamps: deque[str] = field(default_factory=lambda: deque(maxlen=256))
    #: Monotonic time of the last successful poll answer (any status the device sends on purpose).
    last_ok_monotonic: float | None = None


@dataclass(frozen=True, slots=True)
class WalkupEvent:
    variant: WalkupVariant
    type: WalkupEventType
    destination_uri: str
    #: Raw event name as sent by the device (useful when ``type`` is OTHER).
    raw_type: str = ""
    #: eSCL: shortcut label from the event.
    shortcut: str | None = None
    #: eSCL: leaf elements of ``<scan:ScanSettings>`` (localname -> text).
    settings: dict[str, str] = field(default_factory=dict)
    #: LEDM ScanToComp: URI of the WalkupScanToCompEvent resource.
    comp_event_uri: str | None = None
    #: LEDM: AgingStamp of the EventTable entry.
    aging_stamp: str | None = None
    host_identifier: str | None = None
    raw_xml: str = ""


@dataclass(frozen=True, slots=True)
class WalkupScanRequest:
    """What the user chose on the panel, normalised towards eSCL ScanSettings terms.

    ``None`` means "not reported by the device" – the caller applies its own
    profile default.
    """

    variant: WalkupVariant
    destination_uri: str
    shortcut: str | None = None
    #: pwg:InputSource value: ``Feeder`` | ``Platen`` | ``Camera``.
    input_source: str | None = None
    duplex: bool | None = None
    #: eSCL ColorMode: ``RGB24`` | ``Grayscale8`` | ``BlackAndWhite1`` | ...
    color_mode: str | None = None
    x_resolution: int | None = None
    y_resolution: int | None = None
    #: MIME type hinted by the panel/shortcut (``application/pdf``, ``image/jpeg``).
    document_format: str | None = None
    intent: str | None = None
    brightness: int | None = None
    contrast: int | None = None
    content_orientation: str | None = None
    #: eSCL: send as ``<scan:ContextID>`` in POST /eSCL/ScanJobs (HP's client does).
    context_id: str | None = None
    #: Every raw setting the device sent (localname -> text).
    extra: dict[str, str] = field(default_factory=dict)


# --------------------------------------------------------------------------- XML helpers


def _parse(xml: bytes | str) -> etree._Element:
    if isinstance(xml, str):
        xml = xml.encode("utf-8")
    try:
        return etree.fromstring(xml, parser=_PARSER)
    except etree.XMLSyntaxError as exc:
        raise WalkupProtocolError(f"malformed XML: {exc}") from exc


def _local(el: Any) -> str:
    tag = el.tag
    if not isinstance(tag, str):
        return ""
    return etree.QName(tag).localname


def _iter_local(root: etree._Element, name: str):
    for el in root.iter():
        if _local(el) == name:
            yield el


def _child_local(el: etree._Element, name: str) -> etree._Element | None:
    for child in el:
        if _local(child) == name:
            return child
    return None


def _child_text(el: etree._Element, name: str) -> str | None:
    child = _child_local(el, name)
    if child is None or child.text is None:
        return None
    return child.text.strip()


def _first_text(root: etree._Element, name: str) -> str | None:
    for el in _iter_local(root, name):
        if el.text is not None and el.text.strip():
            return el.text.strip()
    return None


def _to_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(float(value.strip()))
    except ValueError:
        return None


def _to_bool(value: str | None) -> bool | None:
    if value is None:
        return None
    v = value.strip().lower()
    if v in ("true", "1", "yes", "duplex"):
        return True
    if v in ("false", "0", "no", "simplex"):
        return False
    return None


def _path_of(uri: str) -> str:
    """Strip scheme/host from absolute URIs the device returns (Location, ResourceURI)."""
    parts = urlsplit(uri.strip())
    path = parts.path if parts.scheme else uri.strip()
    if parts.scheme and parts.query:
        path += "?" + parts.query
    return path or "/"


def _xml_bytes(root: etree._Element) -> bytes:
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True)


# --------------------------------------------------------------------------- builders


def build_escl_subscription(host_identifier: str, shortcuts: list[str] | tuple[str, ...]) -> bytes:
    """``POST /eSCL/WalkupSubscriptions`` body, as sent by HP's Windows client (8122e capture)."""
    scan = NS["scan"]
    root = etree.Element(f"{{{scan}}}WalkupScanSubscription", nsmap={None: scan, "xsi": NS["xsi"]})
    root.set(f"{{{NS['xsi']}}}schemaLocation", f"{scan} eSCL.xsd")
    etree.SubElement(root, f"{{{scan}}}HostIdentifier").text = host_identifier
    etree.SubElement(root, f"{{{scan}}}Connection").text = "Network"
    sc = etree.SubElement(root, f"{{{scan}}}Shortcuts")
    for s in shortcuts:
        etree.SubElement(sc, f"{{{scan}}}Shortcut").text = s
    return _xml_bytes(root)


def build_ledm_destination(name: str, hostname: str, *, to_comp: bool = True) -> bytes:
    """LEDM destination registration body (node-hp-scan-to ``Destination.toXML``)."""
    if to_comp:
        ns, root_name, xsd = NS["wus"], "WalkupScanToCompDestination", "WalkupScanToComp.xsd"
    else:
        ns, root_name, xsd = NS["wusrest"], "WalkupScanDestination", "WalkupScanDestinations.xsd"
    root = etree.Element(f"{{{ns}}}{root_name}", nsmap={None: ns, "xsi": NS["xsi"]})
    root.set(f"{{{NS['xsi']}}}schemaLocation", f"{ns} {xsd}")
    etree.SubElement(root, f"{{{NS['dd3']}}}Hostname", nsmap={None: NS["dd3"]}).text = hostname
    etree.SubElement(root, f"{{{NS['dd']}}}Name", nsmap={None: NS["dd"]}).text = name
    etree.SubElement(root, f"{{{ns}}}LinkType").text = "Network"
    return _xml_bytes(root)


# --------------------------------------------------------------------------- parsers


def parse_walkup_support(caps_xml: bytes | str) -> dict[str, Any]:
    """Extract ``<scan:WalkupScanSupport>`` from eSCL ScannerCapabilities (empty dict if absent)."""
    root = _parse(caps_xml)
    node = next(_iter_local(root, "WalkupScanSupport"), None)
    if node is None:
        return {}
    return {
        "max_destinations": _to_int(_child_text(node, "MaxNetworkDestinations")),
        "inactivity_timeout_s": _to_int(_child_text(node, "InactivityTimeout")),
        "subscription_timeout_s": _to_int(_child_text(node, "SubscriptionTimeout")),
        "max_shortcuts": _to_int(_child_text(node, "MaxScanShortcuts")),
        "pin_support": _to_bool(_child_text(node, "PINSupport")),
    }


def parse_escl_event(xml: bytes | str, destination_uri: str = "") -> WalkupEvent:
    """Parse a ``<scan:WalkupScanEvent>``."""
    root = _parse(xml)
    if _local(root) != "WalkupScanEvent":
        raise WalkupProtocolError(f"expected WalkupScanEvent, got {_local(root)!r}")
    raw_type = _child_text(root, "Event") or ""
    settings: dict[str, str] = {}
    ss = _child_local(root, "ScanSettings")
    if ss is not None:
        for el in ss.iter():
            if el is ss or len(el):
                continue
            if el.text is not None and el.text.strip():
                settings[_local(el)] = el.text.strip()
    resource = _child_text(root, "ResourceURI")
    return WalkupEvent(
        variant=WalkupVariant.ESCL,
        type=WalkupEventType.parse(raw_type),
        raw_type=raw_type,
        destination_uri=_path_of(resource) if resource else destination_uri,
        shortcut=_child_text(root, "Shortcut"),
        settings=settings,
        host_identifier=_child_text(root, "HostIdentifier"),
        raw_xml=xml.decode("utf-8", "replace") if isinstance(xml, bytes) else xml,
    )


_URI_CHILD_NAMES = ("ResourceURI", "SubscriptionURI", "SubscriptionUri", "URI", "Uri", "Location")
_ID_CHILD_NAMES = ("SubscriptionId", "SubscriptionID", "Id", "ID", "Uuid", "UUID")


def parse_escl_subscriptions(xml: bytes | str) -> list[RegisteredDestination]:
    """Parse ``GET /eSCL/WalkupSubscriptions``.

    The listing format is **not documented anywhere we found** (guessed). Be
    tolerant: any element that has a ``HostIdentifier`` child counts as one
    subscription; its URI comes from a ResourceURI-like child or an id-like child.
    """
    root = _parse(xml)
    result: list[RegisteredDestination] = []
    for el in root.iter():
        if _child_local(el, "HostIdentifier") is None or _local(el) == "WalkupScanEvent":
            continue
        uri = None
        for n in _URI_CHILD_NAMES:
            uri = _child_text(el, n) or el.get(n)
            if uri:
                break
        if not uri:
            for n in _ID_CHILD_NAMES:
                ident = _child_text(el, n) or el.get(n)
                if ident:
                    uri = f"{ESCL_SUBSCRIPTIONS}/{ident}"
                    break
        if not uri:
            continue
        shortcuts = tuple(
            (s.text or "").strip() for s in _iter_local(el, "Shortcut") if (s.text or "").strip()
        )
        result.append(
            RegisteredDestination(
                variant=WalkupVariant.ESCL,
                uri=_path_of(uri),
                name=_child_text(el, "HostIdentifier") or "",
                shortcuts=shortcuts,
            )
        )
    return result


def parse_ledm_destinations(xml: bytes | str, variant: WalkupVariant) -> list[RegisteredDestination]:
    root = _parse(xml)
    elname = (
        "WalkupScanToCompDestination"
        if variant == WalkupVariant.LEDM_SCAN_TO_COMP
        else "WalkupScanDestination"
    )
    out = []
    for el in _iter_local(root, elname):
        uri = _child_text(el, "ResourceURI")
        if not uri:
            continue
        out.append(
            RegisteredDestination(
                variant=variant,
                uri=_path_of(uri),
                name=_child_text(el, "Name") or "",
                hostname=_child_text(el, "Hostname") or "",
            )
        )
    return out


@dataclass(frozen=True, slots=True)
class _LedmEvent:
    category: str
    aging_stamp: str
    payloads: tuple[tuple[str, str], ...]  # (uri, resource type)


def parse_event_table(xml: bytes | str) -> list[_LedmEvent]:
    """Parse ``/EventMgmt/EventTable``."""
    root = _parse(xml)
    out = []
    for ev in _iter_local(root, "Event"):
        payloads = []
        for p in ev:
            if _local(p) != "Payload":
                continue
            payloads.append((_child_text(p, "ResourceURI") or "", _child_text(p, "ResourceType") or ""))
        out.append(
            _LedmEvent(
                category=_child_text(ev, "UnqualifiedEventCategory") or "",
                aging_stamp=_child_text(ev, "AgingStamp") or "",
                payloads=tuple(payloads),
            )
        )
    return out


def _parse_ledm_destination_settings(xml: bytes | str) -> tuple[str | None, str | None]:
    """(shortcut, ScanPlexMode) of a LEDM destination resource (when a scan was requested)."""
    root = _parse(xml)
    return _first_text(root, "Shortcut"), _first_text(root, "ScanPlexMode")


_SHORTCUT_FORMATS = {
    "savepdf": "application/pdf",
    "emailpdf": "application/pdf",
    "savedocument1": "application/pdf",
    "savejpeg": "image/jpeg",
    "emailjpeg": "image/jpeg",
    "savephoto1": "image/jpeg",
}


def recommended_poll_timeout(support: WalkupSupport | None) -> int:
    """Long-poll timeout (seconds) that stays well inside the subscription timeout.

    HP's own client uses 100 s against a 3600 s SubscriptionTimeout. For the
    N4000 (SubscriptionTimeout 150) this yields 90 s.
    """
    sub = support.subscription_timeout_s if support else None
    if not sub:
        return 60
    return max(5, min(ESCL_MAX_POLL_S, int(sub * 0.6)))


# --------------------------------------------------------------------------- client


class HpWalkupClient:
    """Async walk-up client for one device.

    ``client`` is owned by the caller (connection pool, TLS settings).
    ``base_url`` is e.g. ``http://192.168.1.50`` or ``https://scanner:443``.
    """

    def __init__(self, client: httpx.AsyncClient, base_url: str, *, timeout_s: float = DEFAULT_TIMEOUT_S):
        self._client = client
        self._base = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self.support: WalkupSupport | None = None

    # -- HTTP -----------------------------------------------------------------

    def _url(self, path: str) -> str:
        return self._base + _path_of(path)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        read_timeout_s: float | None = None,
    ) -> httpx.Response:
        timeout = httpx.Timeout(
            read_timeout_s or self._timeout_s,
            connect=CONNECT_TIMEOUT_S,
        )
        hdrs = {"Cache-Control": "no-cache"}
        if content is not None:
            hdrs["Content-Type"] = "text/xml"
        hdrs.update(headers or {})
        try:
            return await self._client.request(
                method, self._url(path), content=content, headers=hdrs, params=params, timeout=timeout
            )
        except httpx.TimeoutException as exc:
            raise WalkupTimeout(f"{method} {path}: timeout") from exc
        except httpx.TransportError as exc:
            raise WalkupUnreachable(f"{method} {path}: {exc!r}") from exc

    async def _status(self, path: str) -> tuple[int | None, httpx.Response | None]:
        try:
            r = await self._request("GET", path)
        except (WalkupUnreachable, WalkupTimeout):
            return None, None
        return r.status_code, r

    @staticmethod
    def _retry_after(r: httpx.Response) -> float | None:
        raw = r.headers.get("retry-after")
        if not raw:
            return None
        try:
            return max(0.0, min(float(raw), 60.0))
        except ValueError:
            return None

    def _raise_for(self, r: httpx.Response, what: str, *, lost_on: tuple[int, ...] = ()) -> None:
        if r.status_code == 503:
            raise WalkupDeviceBusy(f"{what}: device busy", retry_after_s=self._retry_after(r))
        if r.status_code in lost_on:
            raise WalkupSubscriptionLost(
                f"{what}: registration gone", status=r.status_code, body=r.text[:500]
            )
        raise WalkupProtocolError(f"{what}: unexpected status", status=r.status_code, body=r.text[:500])

    # -- probe ----------------------------------------------------------------

    async def probe(self) -> WalkupSupport:
        """Detect which walk-up variant(s) the device implements. Read-only (GET only)."""
        endpoints: dict[str, int | None] = {}
        variants: list[WalkupVariant] = []
        info: dict[str, Any] = {}

        # 1. eSCL capabilities (WalkupScanSupport) + subscription collection.
        for path in ESCL_CAPS_PATHS:
            status, r = await self._status(path)
            endpoints[path] = status
            if status == 200 and r is not None:
                try:
                    info = parse_walkup_support(r.content)
                except WalkupProtocolError:
                    info = {}
                break
        status, _ = await self._status(ESCL_SUBSCRIPTIONS)
        endpoints[ESCL_SUBSCRIPTIONS] = status
        # node-hp-scan-to: 200 or 405 on GET means the collection exists.
        if status in (200, 405):
            variants.append(WalkupVariant.ESCL)

        # 2. LEDM discovery tree.
        user_action_timeout = None
        ledm_max_dest = None
        status, r = await self._status(DISCOVERY_TREE)
        endpoints[DISCOVERY_TREE] = status
        tree_types: set[str] = set()
        if status == 200 and r is not None:
            try:
                root = _parse(r.content)
                for ifc in _iter_local(root, "SupportedIfc"):
                    t = _child_text(ifc, "ResourceType")
                    if t:
                        tree_types.add(t.split(":", 1)[-1])
            except WalkupProtocolError:
                pass
        status, r = await self._status(LEDM_COMP_CAPS)
        endpoints[LEDM_COMP_CAPS] = status
        if (status == 200 and r is not None) or "hpLedmWalkupScanToCompManifest" in tree_types:
            variants.append(WalkupVariant.LEDM_SCAN_TO_COMP)
            if status == 200 and r is not None:
                try:
                    root = _parse(r.content)
                    ledm_max_dest = _to_int(_first_text(root, "MaxNetworkDestinations"))
                    uat = next(_iter_local(root, "UserActionTimeout"), None)
                    if uat is not None:
                        user_action_timeout = _to_int(_first_text(uat, "ValueFloat"))
                except WalkupProtocolError:
                    pass
        if "hpCnxWalkupScanManifest" in tree_types:
            variants.append(WalkupVariant.LEDM_WALKUP_SCAN)
        elif WalkupVariant.LEDM_SCAN_TO_COMP not in variants and tree_types:
            status, _ = await self._status(LEDM_WS_DESTS)
            endpoints[LEDM_WS_DESTS] = status
            if status == 200:
                variants.append(WalkupVariant.LEDM_WALKUP_SCAN)
        if any(v != WalkupVariant.ESCL for v in variants):
            status, _ = await self._status(EVENT_TABLE)
            endpoints[EVENT_TABLE] = status

        self.support = WalkupSupport(
            variants=tuple(variants),
            escl_caps_advertised=bool(info),
            max_destinations=info.get("max_destinations") or ledm_max_dest,
            max_shortcuts=info.get("max_shortcuts"),
            subscription_timeout_s=info.get("subscription_timeout_s"),
            inactivity_timeout_s=info.get("inactivity_timeout_s"),
            user_action_timeout_s=user_action_timeout,
            pin_support=info.get("pin_support"),
            endpoints=endpoints,
        )
        return self.support

    async def _variant(self, variant: WalkupVariant | None) -> WalkupVariant:
        if variant is not None:
            return variant
        if self.support is None:
            await self.probe()
        assert self.support is not None
        if self.support.preferred is None:
            raise WalkupUnsupported("device exposes no walk-up scan interface")
        return self.support.preferred

    # -- destinations ---------------------------------------------------------

    async def list_destinations(self, variant: WalkupVariant | None = None) -> list[RegisteredDestination]:
        """All destinations/subscriptions on the device (not only ours).

        eSCL: raises :class:`WalkupUnsupported` when the device does not allow
        listing (405) – the caller must then rely on its stored URI.
        """
        v = await self._variant(variant)
        if v == WalkupVariant.ESCL:
            r = await self._request("GET", ESCL_SUBSCRIPTIONS)
            if r.status_code in (404, 405, 501):
                raise WalkupUnsupported(f"listing eSCL subscriptions not supported (HTTP {r.status_code})")
            if r.status_code == 204 or (r.status_code == 200 and not r.content.strip()):
                return []
            if r.status_code != 200:
                self._raise_for(r, "list subscriptions")
            return parse_escl_subscriptions(r.content)
        path = LEDM_COMP_DESTS if v == WalkupVariant.LEDM_SCAN_TO_COMP else LEDM_WS_DESTS
        r = await self._request("GET", path)
        if r.status_code != 200:
            self._raise_for(r, "list destinations")
        return parse_ledm_destinations(r.content, v)

    async def register(
        self,
        name: str,
        hostname: str,
        *,
        shortcuts: list[str] | tuple[str, ...] | None = None,
        known_uri: str | None = None,
        variant: WalkupVariant | None = None,
    ) -> RegisteredDestination:
        """Register this host on the device panel. Idempotent.

        * Reuses an existing registration with the same ``name`` (and, for eSCL,
          the same shortcut list) when the device can list registrations.
        * eSCL shortcuts cannot be edited: a mismatching registration is deleted
          and re-created.
        * When listing is impossible, ``known_uri`` (the URI persisted from the
          previous run) is deleted best-effort before posting, so restarts do not
          leave duplicates behind.
        """
        v = await self._variant(variant)
        if v == WalkupVariant.ESCL:
            return await self._register_escl(name, tuple(shortcuts or ("Scan",)), known_uri)
        return await self._register_ledm(v, name, hostname)

    async def _register_escl(
        self, name: str, shortcuts: tuple[str, ...], known_uri: str | None
    ) -> RegisteredDestination:
        max_sc = self.support.max_shortcuts if self.support else None
        if not shortcuts:
            raise ValueError("at least one shortcut is required")
        if max_sc and len(shortcuts) > max_sc:
            raise ValueError(f"device supports at most {max_sc} shortcuts, got {len(shortcuts)}")
        try:
            existing = [d for d in await self.list_destinations(WalkupVariant.ESCL) if d.name == name]
        except WalkupUnsupported:
            existing = []
            if known_uri:
                await self._delete_quietly(known_uri)
        for d in existing:
            if not d.shortcuts or d.shortcuts == shortcuts:
                return replace(d, shortcuts=shortcuts)
        for d in existing:
            await self._delete_quietly(d.uri)

        r = await self._request("POST", ESCL_SUBSCRIPTIONS, content=build_escl_subscription(name, shortcuts))
        location = r.headers.get("location")
        # Some firmware answers 200 when the host already has a subscription (node-hp-scan-to).
        if r.status_code not in (200, 201) or not location:
            self._raise_for(r, "register eSCL subscription")
        return RegisteredDestination(
            variant=WalkupVariant.ESCL, uri=_path_of(location or ""), name=name, shortcuts=shortcuts
        )

    async def _register_ledm(self, v: WalkupVariant, name: str, hostname: str) -> RegisteredDestination:
        for d in await self.list_destinations(v):
            if d.name == name:
                return d
        path = LEDM_COMP_DESTS if v == WalkupVariant.LEDM_SCAN_TO_COMP else LEDM_WS_DESTS
        body = build_ledm_destination(name, hostname, to_comp=v == WalkupVariant.LEDM_SCAN_TO_COMP)
        r = await self._request("POST", path, content=body)
        location = r.headers.get("location")
        if r.status_code not in (200, 201) or not location:
            self._raise_for(r, "register LEDM destination")
        return RegisteredDestination(variant=v, uri=_path_of(location or ""), name=name, hostname=hostname)

    async def _delete_quietly(self, uri: str) -> None:
        try:
            r = await self._request("DELETE", uri)
            _ = r.status_code
        except WalkupError:
            pass

    async def unregister(self, dest: RegisteredDestination) -> None:
        """Delete the registration. Already-gone (403/404/410) counts as success."""
        r = await self._request("DELETE", dest.uri)
        if r.status_code in (200, 202, 204, 403, 404, 410):
            return
        self._raise_for(r, f"unregister {dest.uri}")

    # -- events ---------------------------------------------------------------

    async def poll_event(self, state: PollState, timeout_s: float) -> WalkupEvent | None:
        """One long-poll cycle. ``None`` = nothing happened within ``timeout_s``.

        Raises :class:`WalkupSubscriptionLost` when the registration must be
        re-created, :class:`WalkupDeviceBusy` on 503.
        """
        dest = state.destination
        if dest.variant == WalkupVariant.ESCL:
            return await self._poll_escl(state, timeout_s)
        return await self._poll_ledm(state, timeout_s)

    async def _poll_escl(self, state: PollState, timeout_s: float) -> WalkupEvent | None:
        dest = state.destination
        t = max(1, min(ESCL_MAX_POLL_S, int(timeout_s)))
        r = await self._request(
            "GET", f"{dest.uri}/Event", params={"timeout": t}, read_timeout_s=t + LONG_POLL_GRACE_S
        )
        if r.status_code == 204:
            state.last_ok_monotonic = time.monotonic()
            return None
        if r.status_code != 200:
            # 403 after reboot (8120e), 404/410 on other firmware (node-hp-scan-to).
            self._raise_for(r, "eSCL walkup event", lost_on=(403, 404, 410))
        state.last_ok_monotonic = time.monotonic()
        if not r.content.strip():
            return None
        return parse_escl_event(r.content, dest.uri)

    async def _poll_ledm(self, state: PollState, timeout_s: float) -> WalkupEvent | None:
        deadline = time.monotonic() + timeout_s
        if not state.primed:
            # The first read returns the whole (historic) table: remember, don't act on it.
            r = await self._request("GET", EVENT_TABLE)
            if r.status_code != 200:
                self._raise_for(r, "event table")
            for ev in parse_event_table(r.content):
                state.seen_stamps.append(ev.aging_stamp)
            state.etag = r.headers.get("etag", "")
            state.primed = True
        remaining = max(1.0, deadline - time.monotonic())
        ds = max(10, min(LEDM_MAX_POLL_DS, int(remaining * 10)))
        headers = {"If-None-Match": state.etag} if state.etag else {}
        r = await self._request(
            "GET",
            EVENT_TABLE,
            params={"timeout": ds},
            headers=headers,
            read_timeout_s=ds / 10 + LONG_POLL_GRACE_S,
        )
        if r.status_code == 304:
            state.last_ok_monotonic = time.monotonic()
            return None
        if r.status_code != 200:
            self._raise_for(r, "event table")
        state.last_ok_monotonic = time.monotonic()
        state.etag = r.headers.get("etag", state.etag)
        found: _LedmEvent | None = None
        for ev in parse_event_table(r.content):
            if ev.aging_stamp in state.seen_stamps:
                continue
            state.seen_stamps.append(ev.aging_stamp)
            if found is None and ev.category == "ScanEvent" and self._ledm_matches(ev, state.destination):
                found = ev
        if found is None:
            return None
        comp_uri = next((_path_of(u) for u, t in found.payloads if "CompEvent" in t), None)
        if comp_uri is None:
            # 1st-gen WalkupScan: the ScanEvent itself means "scan requested".
            return WalkupEvent(
                variant=state.destination.variant,
                type=WalkupEventType.SCAN_REQUESTED,
                raw_type="ScanEvent",
                destination_uri=state.destination.uri,
                aging_stamp=found.aging_stamp,
            )
        etype, raw = await self._read_comp_event(comp_uri)
        return WalkupEvent(
            variant=state.destination.variant,
            type=etype,
            raw_type=raw,
            destination_uri=state.destination.uri,
            comp_event_uri=comp_uri,
            aging_stamp=found.aging_stamp,
        )

    @staticmethod
    def _ledm_matches(ev: _LedmEvent, dest: RegisteredDestination) -> bool:
        for uri, rtype in ev.payloads:
            if "Destination" in rtype and _path_of(uri).rstrip("/") == dest.uri.rstrip("/"):
                return True
        return False

    async def _read_comp_event(self, comp_uri: str) -> tuple[WalkupEventType, str]:
        r = await self._request("GET", comp_uri)
        if r.status_code != 200:
            self._raise_for(r, "WalkupScanToCompEvent")
        raw = _first_text(_parse(r.content), "WalkupScanToCompEventType") or ""
        return WalkupEventType.parse(raw), raw

    async def wait_for_scan_request(
        self,
        state: PollState,
        event: WalkupEvent,
        timeout_s: float,
        *,
        interval_s: float = 1.0,
    ) -> WalkupEvent | None:
        """After ``HostSelected``: wait until the user presses Start on the panel.

        Returns the ``ScanRequested`` event, or ``None`` on
        ``ScanPagesComplete`` / cancel / timeout. Returns ``event`` unchanged if
        it already is a scan request (eSCL, LEDM 1st gen).
        """
        if event.type == WalkupEventType.SCAN_REQUESTED and event.variant != WalkupVariant.LEDM_SCAN_TO_COMP:
            return event
        return await self._wait(
            state,
            event,
            timeout_s,
            interval_s=interval_s,
            accept=(WalkupEventType.SCAN_REQUESTED, WalkupEventType.SCAN_NEW_PAGE_REQUESTED),
            keep_waiting=(WalkupEventType.HOST_SELECTED,),
        )

    async def wait_for_next_page(
        self,
        state: PollState,
        event: WalkupEvent,
        timeout_s: float,
        *,
        interval_s: float = 1.0,
    ) -> WalkupEvent | None:
        """After a job finished: did the user ask for another page/batch in the same session?

        LEDM ScanToComp: the comp event stays ``ScanRequested`` while the panel
        asks; ``ScanNewPageRequested`` -> scan again, ``ScanPagesComplete`` -> done.
        eSCL: a further ``ScanRequested`` on the subscription means "scan again".
        Returns the triggering event or ``None`` (session over / timeout).
        Optional: a plain :meth:`poll_event` loop also sees eSCL follow-up requests.
        """
        if event.variant == WalkupVariant.LEDM_WALKUP_SCAN:
            return None
        return await self._wait(
            state,
            event,
            timeout_s,
            interval_s=interval_s,
            accept=(WalkupEventType.SCAN_NEW_PAGE_REQUESTED,)
            if event.variant == WalkupVariant.LEDM_SCAN_TO_COMP
            else (WalkupEventType.SCAN_REQUESTED,),
            keep_waiting=(WalkupEventType.SCAN_REQUESTED,)
            if event.variant == WalkupVariant.LEDM_SCAN_TO_COMP
            else (),
        )

    async def _wait(
        self,
        state: PollState,
        event: WalkupEvent,
        timeout_s: float,
        *,
        interval_s: float,
        accept: tuple[WalkupEventType, ...],
        keep_waiting: tuple[WalkupEventType, ...],
    ) -> WalkupEvent | None:
        deadline = time.monotonic() + timeout_s
        if event.variant == WalkupVariant.ESCL:
            while (remaining := deadline - time.monotonic()) > 0:
                nxt = await self.poll_event(state, max(1.0, min(remaining, ESCL_MAX_POLL_S)))
                if nxt is None:
                    continue
                if nxt.type in accept:
                    return nxt
                if nxt.type == WalkupEventType.SCAN_PAGES_COMPLETE:
                    return None
                # HostSelected again (user re-entered the menu) or unknown: keep waiting.
            return None
        if event.comp_event_uri is None:
            return None
        while True:
            etype, raw = await self._read_comp_event(event.comp_event_uri)
            if etype in accept:
                return replace(event, type=etype, raw_type=raw)
            if etype not in keep_waiting:
                return None
            if time.monotonic() + interval_s > deadline:
                return None
            await asyncio.sleep(interval_s)

    # -- settings -------------------------------------------------------------

    async def requested_settings(self, event: WalkupEvent) -> WalkupScanRequest:
        """Settings chosen on the panel for a ``ScanRequested`` event."""
        if event.variant == WalkupVariant.ESCL:
            s = event.settings
            fmt = s.get("DocumentFormatExt") or s.get("DocumentFormat")
            return WalkupScanRequest(
                variant=event.variant,
                destination_uri=event.destination_uri,
                shortcut=event.shortcut,
                input_source=s.get("InputSource"),
                duplex=_to_bool(s.get("Duplex")),
                color_mode=s.get("ColorMode"),
                x_resolution=_to_int(s.get("XResolution")),
                y_resolution=_to_int(s.get("YResolution")),
                document_format=fmt,
                intent=s.get("Intent"),
                brightness=_to_int(s.get("Brightness")),
                contrast=_to_int(s.get("Contrast")),
                content_orientation=s.get("ContentOrientation"),
                context_id=event.destination_uri.rstrip("/").rsplit("/", 1)[-1] or None,
                extra=dict(s),
            )
        r = await self._request("GET", event.destination_uri)
        if r.status_code != 200:
            self._raise_for(r, "read destination settings", lost_on=(404, 410))
        shortcut, plex = _parse_ledm_destination_settings(r.content)
        extra = {k: v for k, v in (("Shortcut", shortcut), ("ScanPlexMode", plex)) if v}
        return WalkupScanRequest(
            variant=event.variant,
            destination_uri=event.destination_uri,
            shortcut=shortcut,
            duplex=_to_bool(plex),
            document_format=_SHORTCUT_FORMATS.get((shortcut or "").lower()),
            extra=extra,
        )
