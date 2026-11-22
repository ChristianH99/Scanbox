"""eSCL XML: capabilities/status parsing and ScanSettings building.

Parsing is namespace-aware but tolerant: unknown elements are ignored and
missing optional elements yield ``None`` so new firmware doesn't break us.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from lxml import etree

NS = {
    "scan": "http://schemas.hp.com/imaging/escl/2011/05/03",
    "pwg": "http://www.pwg.org/schemas/2010/12/sm",
}
SCAN = "{%s}" % NS["scan"]
PWG = "{%s}" % NS["pwg"]

_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False, remove_comments=True)


def parse_xml(data: bytes | str) -> etree._Element:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return etree.fromstring(data.strip(), parser=_PARSER)


def _text(el: etree._Element | None, path: str) -> str | None:
    if el is None:
        return None
    found = el.find(path, NS)
    if found is None or found.text is None:
        return None
    return found.text.strip()


def _int(el: etree._Element | None, path: str) -> int | None:
    t = _text(el, path)
    try:
        return int(t) if t is not None else None
    except ValueError:
        return None


def _bool(el: etree._Element | None, path: str) -> bool | None:
    t = _text(el, path)
    if t is None:
        return None
    return t.lower() in ("true", "1", "yes")


@dataclass
class Range:
    min: int
    max: int
    normal: int
    step: int = 1


@dataclass
class InputCaps:
    min_width: int | None = None
    max_width: int | None = None
    min_height: int | None = None
    max_height: int | None = None
    color_modes: list[str] = field(default_factory=list)
    content_types: list[str] = field(default_factory=list)
    document_formats: list[str] = field(default_factory=list)
    resolutions: list[int] = field(default_factory=list)
    intents: list[str] = field(default_factory=list)
    binary_renderings: list[str] = field(default_factory=list)


@dataclass
class ScannerCapabilities:
    version: str | None = None
    make_and_model: str | None = None
    serial_number: str | None = None
    uuid: str | None = None
    admin_uri: str | None = None
    icon_uri: str | None = None
    platen: InputCaps | None = None
    adf_simplex: InputCaps | None = None
    adf_duplex: InputCaps | None = None
    feeder_capacity: int | None = None
    adf_options: list[str] = field(default_factory=list)
    auto_crop: bool = False
    auto_exposure: bool = False
    blank_page_detection: bool = False
    multipick_actions: list[str] = field(default_factory=list)
    carrier_sheet: bool = False
    walkup_supported: bool = False
    ranges: dict[str, Range] = field(default_factory=dict)  # Brightness, Contrast, …

    @property
    def sources(self) -> list[str]:
        out = []
        if self.adf_simplex:
            out.append("adf_simplex")
        if self.adf_duplex:
            out.append("adf_duplex")
        if self.platen:
            out.append("platen")
        return out

    def input_caps(self, source: str) -> InputCaps | None:
        return {"adf_simplex": self.adf_simplex, "adf_duplex": self.adf_duplex, "platen": self.platen}.get(source)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["sources"] = self.sources
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ScannerCapabilities:
        d = dict(d)
        d.pop("sources", None)
        for k in ("platen", "adf_simplex", "adf_duplex"):
            if d.get(k) is not None:
                d[k] = InputCaps(**d[k])
        d["ranges"] = {k: Range(**v) for k, v in (d.get("ranges") or {}).items()}
        return cls(**d)


_RANGE_ELEMENTS = {
    "Brightness": "BrightnessSupport",
    "Contrast": "ContrastSupport",
    "Gamma": "GammaSupport",
    "Highlight": "HighlightSupport",
    "Shadow": "ShadowSupport",
    "Sharpen": "SharpenSupport",
    "NoiseRemoval": "NoiseRemovalSupport",
    "Threshold": "ThresholdSupport",
    "CompressionFactor": "CompressionFactorSupport",
    "BlankPageSensitivity": "BlankPageSensitivitySupport",
}


def _input_caps(el: etree._Element | None) -> InputCaps | None:
    if el is None:
        return None
    caps = InputCaps(
        min_width=_int(el, "scan:MinWidth"),
        max_width=_int(el, "scan:MaxWidth"),
        min_height=_int(el, "scan:MinHeight"),
        max_height=_int(el, "scan:MaxHeight"),
    )
    for prof in el.iterfind("scan:SettingProfiles/scan:SettingProfile", NS):
        caps.color_modes += [c.text.strip() for c in prof.iterfind("scan:ColorModes/scan:ColorMode", NS) if c.text]
        caps.content_types += [
            c.text.strip() for c in prof.iterfind("scan:ContentTypes/pwg:ContentType", NS) if c.text
        ]
        fmts = [c.text.strip() for c in prof.iterfind("scan:DocumentFormats/pwg:DocumentFormat", NS) if c.text]
        fmts += [c.text.strip() for c in prof.iterfind("scan:DocumentFormats/scan:DocumentFormatExt", NS) if c.text]
        caps.document_formats += fmts
        for r in prof.iterfind("scan:SupportedResolutions/scan:DiscreteResolutions/scan:DiscreteResolution", NS):
            x = _int(r, "scan:XResolution")
            y = _int(r, "scan:YResolution")
            if x and x == y:
                caps.resolutions.append(x)
        caps.binary_renderings += [
            c.text.strip() for c in prof.iterfind("scan:BinaryRenderings/scan:BinaryRendering", NS) if c.text
        ]
    caps.intents = [c.text.strip() for c in el.iterfind("scan:SupportedIntents/scan:Intent", NS) if c.text]

    def dedupe(seq: list) -> list:
        return list(dict.fromkeys(seq))

    caps.color_modes = dedupe(caps.color_modes)
    caps.content_types = dedupe(caps.content_types)
    caps.document_formats = dedupe(caps.document_formats)
    caps.resolutions = sorted(set(caps.resolutions))
    caps.binary_renderings = dedupe(caps.binary_renderings)
    return caps


def parse_capabilities(data: bytes | str) -> ScannerCapabilities:
    root = parse_xml(data)
    adf = root.find("scan:Adf", NS)
    caps = ScannerCapabilities(
        version=_text(root, "pwg:Version"),
        make_and_model=_text(root, "pwg:MakeAndModel"),
        serial_number=_text(root, "pwg:SerialNumber"),
        uuid=_text(root, "scan:UUID"),
        admin_uri=_text(root, "scan:AdminURI"),
        icon_uri=_text(root, "scan:IconURI"),
        platen=_input_caps(root.find("scan:Platen/scan:PlatenInputCaps", NS)),
        adf_simplex=_input_caps(adf.find("scan:AdfSimplexInputCaps", NS)) if adf is not None else None,
        adf_duplex=_input_caps(adf.find("scan:AdfDuplexInputCaps", NS)) if adf is not None else None,
        feeder_capacity=_int(adf, "scan:FeederCapacity") if adf is not None else None,
        adf_options=[o.text.strip() for o in adf.iterfind("scan:AdfOptions/scan:AdfOption", NS) if o.text]
        if adf is not None
        else [],
        auto_crop=bool(_bool(root, "scan:AutoCrop")),
        auto_exposure=bool(_bool(root, "scan:AutoExposure")),
        blank_page_detection=bool(_bool(root, "scan:BlankPageDetection")),
        multipick_actions=[
            a.text.strip()
            for a in root.iterfind("scan:MultipickDetectionActionSupport/scan:MultipickDetectionAction", NS)
            if a.text
        ],
        carrier_sheet=bool(_bool(root, "scan:CarrierSheetSupport")),
        walkup_supported=root.find("scan:WalkupScanSupport", NS) is not None,
    )
    # Duplex ADF without separate duplex caps: reuse simplex caps.
    if caps.adf_duplex is None and caps.adf_simplex and "Duplex" in caps.adf_options:
        caps.adf_duplex = caps.adf_simplex
    for name, el_name in _RANGE_ELEMENTS.items():
        el = root.find(f"scan:{el_name}", NS)
        if el is not None:
            mn, mx, nm = _int(el, "scan:Min"), _int(el, "scan:Max"), _int(el, "scan:Normal")
            if mn is not None and mx is not None:
                caps.ranges[name] = Range(mn, mx, nm if nm is not None else mn, _int(el, "scan:Step") or 1)
    return caps


# ---------------------------------------------------------------------------- status

ADF_STATES = {
    "ScannerAdfLoaded",
    "ScannerAdfEmpty",
    "ScannerAdfJam",
    "ScannerAdfDoorOpen",
    "ScannerAdfHatchOpen",
    "ScannerAdfMispick",
    "ScannerAdfMultipickDetected",
    "ScannerAdfProcessing",
    "ScannerAdfDuplexPageTooShort",
    "ScannerAdfDuplexPageTooLong",
    "ScannerAdfInputTrayFailed",
    "ScannerAdfInputTrayOverloaded",
}


@dataclass
class JobInfo:
    uri: str
    uuid: str | None
    state: str | None
    reasons: list[str]
    images_completed: int | None
    images_to_transfer: int | None
    age: int | None


@dataclass
class ScannerStatus:
    state: str  # Idle | Processing | Testing | Stopped | Down
    adf_state: str | None
    jobs: list[JobInfo]

    def job(self, job_uri: str) -> JobInfo | None:
        tail = job_uri.rstrip("/").rsplit("/", 1)[-1]
        for j in self.jobs:
            if j.uri.rstrip("/").endswith(tail) or j.uuid == tail:
                return j
        return None


def parse_status(data: bytes | str) -> ScannerStatus:
    root = parse_xml(data)
    jobs = []
    for j in root.iterfind("scan:Jobs/scan:JobInfo", NS):
        jobs.append(
            JobInfo(
                uri=_text(j, "pwg:JobUri") or "",
                uuid=_text(j, "pwg:JobUuid"),
                state=_text(j, "pwg:JobState"),
                reasons=[r.text.strip() for r in j.iterfind("pwg:JobStateReasons/pwg:JobStateReason", NS) if r.text],
                images_completed=_int(j, "pwg:ImagesCompleted"),
                images_to_transfer=_int(j, "pwg:ImagesToTransfer"),
                age=_int(j, "scan:Age"),
            )
        )
    return ScannerStatus(
        state=_text(root, "pwg:State") or "Unknown",
        adf_state=_text(root, "scan:AdfState"),
        jobs=jobs,
    )


# ---------------------------------------------------------------------------- settings


@dataclass
class ScanSettings:
    """What we send in POST /eSCL/ScanJobs. Units: 1/300 inch for the region."""

    resolution: int = 300
    color_mode: str = "RGB24"
    duplex: bool = False
    input_source: str = "Feeder"
    document_format: str = "image/jpeg"
    width: int = 2480  # A4
    height: int = 3508
    x_offset: int = 0
    y_offset: int = 0
    intent: str | None = "Document"
    content_type: str | None = None
    version: str = "2.6"
    # Optional scanner-side features – only emitted when not None.
    blank_page_removal: bool | None = None
    blank_page_sensitivity: int | None = None
    multipick_detection: bool | None = None
    multipick_action: str | None = None
    auto_crop: bool | None = None
    auto_exposure: bool | None = None
    carrier_sheet: bool | None = None
    brightness: int | None = None
    contrast: int | None = None
    gamma: int | None = None
    highlight: int | None = None
    shadow: int | None = None
    sharpen: int | None = None
    noise_removal: int | None = None
    threshold: int | None = None
    compression_factor: int | None = None
    binary_rendering: str | None = None


def build_scan_settings(s: ScanSettings) -> bytes:
    """Serialise ScanSettings following the element order of the eSCL schema."""
    nsmap = {"scan": NS["scan"], "pwg": NS["pwg"]}
    root = etree.Element(SCAN + "ScanSettings", nsmap=nsmap)

    def add(parent: etree._Element, tag: str, value: Any) -> etree._Element:
        el = etree.SubElement(parent, tag)
        if value is not None:
            el.text = str(value).lower() if isinstance(value, bool) else str(value)
        return el

    add(root, PWG + "Version", s.version)
    if s.intent:
        add(root, SCAN + "Intent", s.intent)
    regions = etree.SubElement(root, PWG + "ScanRegions")
    region = etree.SubElement(regions, PWG + "ScanRegion")
    add(region, PWG + "Height", s.height)
    add(region, PWG + "ContentRegionUnits", "escl:ThreeHundredthsOfInches")
    add(region, PWG + "Width", s.width)
    add(region, PWG + "XOffset", s.x_offset)
    add(region, PWG + "YOffset", s.y_offset)
    add(root, PWG + "DocumentFormat", s.document_format)
    if s.content_type:
        add(root, SCAN + "ContentType", s.content_type)
    add(root, PWG + "InputSource", s.input_source)
    add(root, SCAN + "XResolution", s.resolution)
    add(root, SCAN + "YResolution", s.resolution)
    add(root, SCAN + "ColorMode", s.color_mode)
    if s.binary_rendering:
        add(root, SCAN + "BinaryRendering", s.binary_rendering)
    if s.input_source == "Feeder":
        add(root, SCAN + "Duplex", s.duplex)
    for attr, tag in (
        ("brightness", "Brightness"),
        ("compression_factor", "CompressionFactor"),
        ("contrast", "Contrast"),
        ("gamma", "Gamma"),
        ("highlight", "Highlight"),
        ("noise_removal", "NoiseRemoval"),
        ("shadow", "Shadow"),
        ("sharpen", "Sharpen"),
        ("threshold", "Threshold"),
    ):
        v = getattr(s, attr)
        if v is not None:
            add(root, SCAN + tag, v)
    if s.blank_page_removal is not None:
        add(root, SCAN + "BlankPageDetection", s.blank_page_removal)
        add(root, SCAN + "BlankPageDetectionAndRemoval", s.blank_page_removal)
        if s.blank_page_removal and s.blank_page_sensitivity is not None:
            add(root, SCAN + "BlankPageSensitivity", s.blank_page_sensitivity)
    if s.multipick_detection is not None:
        add(root, SCAN + "MultipickDetection", s.multipick_detection)
        if s.multipick_detection and s.multipick_action:
            add(root, SCAN + "MultipickDetectionAction", s.multipick_action)
    if s.auto_crop is not None:
        add(root, SCAN + "AutoCrop", s.auto_crop)
    if s.auto_exposure is not None:
        add(root, SCAN + "AutoExposure", s.auto_exposure)
    if s.carrier_sheet is not None:
        add(root, SCAN + "CarrierSheet", s.carrier_sheet)
    add(root, SCAN + "DocumentFormatExt", s.document_format)
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True)
