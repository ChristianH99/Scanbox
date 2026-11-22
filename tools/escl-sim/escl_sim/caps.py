"""Scanner capabilities: the verbatim fixture plus a parsed view used for validation."""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources

from lxml import etree

NS_SCAN = "http://schemas.hp.com/imaging/escl/2011/05/03"
NS_PWG = "http://www.pwg.org/schemas/2010/12/sm"
NSMAP = {"scan": NS_SCAN, "pwg": NS_PWG}

# 300ths of an inch per mm
U_PER_MM = 300 / 25.4


@lru_cache(maxsize=1)
def capabilities_bytes() -> bytes:
    """The real device's ScannerCapabilities document, byte-for-byte."""
    return resources.files("escl_sim").joinpath("data/scanner-capabilities.xml").read_bytes()


@dataclass(frozen=True)
class Range:
    min: int
    max: int
    normal: int


@dataclass(frozen=True)
class InputCaps:
    min_width: int
    max_width: int
    min_height: int
    max_height: int
    max_scan_regions: int
    color_modes: frozenset[str]
    content_types: frozenset[str]
    document_formats: frozenset[str]
    resolutions: frozenset[tuple[int, int]]
    intents: frozenset[str]


@dataclass(frozen=True)
class Capabilities:
    version: str
    make_and_model: str
    simplex: InputCaps
    duplex: InputCaps | None
    platen: InputCaps | None
    ranges: dict[str, Range] = field(default_factory=dict)
    multipick_actions: frozenset[str] = frozenset()
    feeder_capacity: int = 50


def _txt(el, path: str) -> str | None:
    found = el.find(path, NSMAP)
    return found.text.strip() if found is not None and found.text else None


def _input_caps(el) -> InputCaps:
    def ints(path: str) -> int:
        return int(_txt(el, path) or 0)

    profile = el.find("scan:SettingProfiles/scan:SettingProfile", NSMAP)
    res = frozenset(
        (int(_txt(r, "scan:XResolution")), int(_txt(r, "scan:YResolution")))
        for r in profile.iterfind(
            "scan:SupportedResolutions/scan:DiscreteResolutions/scan:DiscreteResolution", NSMAP
        )
    )
    formats = {e.text.strip() for e in profile.iterfind("scan:DocumentFormats/*", NSMAP) if e.text}
    return InputCaps(
        min_width=ints("scan:MinWidth"),
        max_width=ints("scan:MaxWidth"),
        min_height=ints("scan:MinHeight"),
        max_height=ints("scan:MaxHeight"),
        max_scan_regions=ints("scan:MaxScanRegions") or 1,
        color_modes=frozenset(e.text.strip() for e in profile.iterfind("scan:ColorModes/scan:ColorMode", NSMAP)),
        content_types=frozenset(e.text.strip() for e in profile.iterfind("scan:ContentTypes/pwg:ContentType", NSMAP)),
        document_formats=frozenset(formats),
        resolutions=res,
        intents=frozenset(e.text.strip() for e in el.iterfind("scan:SupportedIntents/scan:Intent", NSMAP)),
    )


@lru_cache(maxsize=1)
def capabilities() -> Capabilities:
    root = etree.fromstring(capabilities_bytes())
    simplex = root.find("scan:Adf/scan:AdfSimplexInputCaps", NSMAP)
    duplex = root.find("scan:Adf/scan:AdfDuplexInputCaps", NSMAP)
    platen = root.find("scan:Platen/scan:PlatenInputCaps", NSMAP)
    ranges: dict[str, Range] = {}
    for el in root:
        name = etree.QName(el).localname
        if name.endswith("Support") and el.find("scan:Min", NSMAP) is not None:
            ranges[name[: -len("Support")]] = Range(
                int(float(_txt(el, "scan:Min"))),
                int(float(_txt(el, "scan:Max"))),
                int(float(_txt(el, "scan:Normal"))),
            )
    actions = frozenset(
        e.text.strip()
        for e in root.iterfind("scan:MultipickDetectionActionSupport/scan:MultipickDetectionAction", NSMAP)
    )
    return Capabilities(
        version=_txt(root, "pwg:Version") or "2.9",
        make_and_model=_txt(root, "pwg:MakeAndModel") or "",
        simplex=_input_caps(simplex),
        duplex=_input_caps(duplex) if duplex is not None else None,
        platen=_input_caps(platen) if platen is not None else None,
        ranges=ranges,
        multipick_actions=actions,
        feeder_capacity=int(_txt(root, "scan:Adf/scan:FeederCapacity") or 50),
    )
