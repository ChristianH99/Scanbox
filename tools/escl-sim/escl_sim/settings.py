"""Parsing and validation of eSCL ``scan:ScanSettings`` request bodies."""

from __future__ import annotations

from dataclasses import dataclass, field

from lxml import etree

from .caps import Capabilities, InputCaps

# A4 in 300ths of an inch (210 x 297 mm), used when the request has no ScanRegion.
A4_REGION = (2480, 3508)

TRUE_VALUES = {"true", "1", "yes"}

# Document formats we can actually produce. Everything else listed in the
# capabilities is answered with JPEG (see docs/SIMULATOR.md, "guesses").
JPEG_LIKE = {"image/jpeg", "application/octet-stream", "application/vnd.hp.continuous-perforated-sheet+jpeg"}


class SettingsError(Exception):
    """Invalid ScanSettings. ``status`` is the HTTP status the device would answer with."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class Region:
    """A scan region in 300ths of an inch."""

    width: int
    height: int
    x_offset: int = 0
    y_offset: int = 0


@dataclass(frozen=True)
class ScanSettings:
    input_source: str = "Feeder"
    duplex: bool = False
    color_mode: str = "RGB24"
    x_resolution: int = 300
    y_resolution: int = 300
    document_format: str = "image/jpeg"
    intent: str | None = None
    content_type: str | None = None
    region: Region | None = None
    blank_page_detection: bool = False
    blank_page_removal: bool = False
    multipick_detection: bool | None = None
    multipick_action: str | None = None
    auto_crop: bool = False
    brightness: int | None = None
    contrast: int | None = None
    threshold: int | None = None
    compression_factor: int | None = None
    blank_page_sensitivity: int | None = None
    version: str | None = None
    raw: dict[str, str] = field(default_factory=dict, compare=False, hash=False)

    @property
    def drop_blank_pages(self) -> bool:
        return self.blank_page_detection or self.blank_page_removal

    @property
    def is_pdf(self) -> bool:
        return self.document_format == "application/pdf"

    @property
    def dpi(self) -> int:
        return self.x_resolution

    def summary(self) -> dict:
        return {
            "input_source": self.input_source,
            "duplex": self.duplex,
            "color_mode": self.color_mode,
            "resolution": self.x_resolution,
            "document_format": self.document_format,
            "region": None if self.region is None else self.region.__dict__,
            "blank_page_removal": self.drop_blank_pages,
            "multipick_detection": self.multipick_detection,
            "multipick_action": self.multipick_action,
            "auto_crop": self.auto_crop,
        }


def _local(el) -> str:
    return etree.QName(el).localname


def _int(name: str, value: str) -> int:
    try:
        return int(value.strip())
    except ValueError:
        raise SettingsError(400, f"{name}: not an integer: {value!r}") from None


def _bool(value: str) -> bool:
    return value.strip().lower() in TRUE_VALUES


def parse_scan_settings(body: bytes) -> ScanSettings:
    """Parse a ScanSettings document. Malformed XML / wrong root → 400."""
    if not body or not body.strip():
        raise SettingsError(400, "empty request body")
    parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)
    try:
        root = etree.fromstring(body, parser)
    except etree.XMLSyntaxError as exc:
        raise SettingsError(400, f"malformed XML: {exc}") from None
    if _local(root) != "ScanSettings":
        raise SettingsError(400, f"expected ScanSettings, got {_local(root)}")

    raw: dict[str, str] = {}
    values: dict = {}
    regions: list[Region] = []
    doc_format = None
    doc_format_ext = None
    for el in root:
        if not isinstance(el.tag, str):
            continue  # comments / PIs
        name = _local(el)
        text = (el.text or "").strip()
        raw[name] = text
        match name:
            case "Version":
                values["version"] = text
            case "InputSource":
                values["input_source"] = text
            case "Duplex":
                values["duplex"] = _bool(text)
            case "ColorMode":
                values["color_mode"] = text
            case "XResolution":
                values["x_resolution"] = _int(name, text)
            case "YResolution":
                values["y_resolution"] = _int(name, text)
            case "DocumentFormat":
                doc_format = text
            case "DocumentFormatExt":
                doc_format_ext = text
            case "Intent":
                values["intent"] = text
            case "ContentType":
                values["content_type"] = text
            case "BlankPageDetection":
                values["blank_page_detection"] = _bool(text)
            case "BlankPageDetectionAndRemoval":
                values["blank_page_removal"] = _bool(text)
            case "MultipickDetection":
                values["multipick_detection"] = _bool(text)
            case "MultipickDetectionAction":
                values["multipick_action"] = text
            case "AutoCrop":
                values["auto_crop"] = _bool(text)
            case "Brightness":
                values["brightness"] = _int(name, text)
            case "Contrast":
                values["contrast"] = _int(name, text)
            case "Threshold":
                values["threshold"] = _int(name, text)
            case "CompressionFactor":
                values["compression_factor"] = _int(name, text)
            case "BlankPageSensitivity":
                values["blank_page_sensitivity"] = _int(name, text)
            case "ScanRegions":
                for reg in el:
                    if not isinstance(reg.tag, str) or _local(reg) != "ScanRegion":
                        continue
                    parts = {_local(c): (c.text or "").strip() for c in reg if isinstance(c.tag, str)}
                    units = parts.get("ContentRegionUnits", "escl:ThreeHundredthsOfInches")
                    if not units.endswith("ThreeHundredthsOfInches"):
                        raise SettingsError(409, f"unsupported ContentRegionUnits {units!r}")
                    try:
                        regions.append(
                            Region(
                                width=_int("Width", parts["Width"]),
                                height=_int("Height", parts["Height"]),
                                x_offset=_int("XOffset", parts.get("XOffset", "0")),
                                y_offset=_int("YOffset", parts.get("YOffset", "0")),
                            )
                        )
                    except KeyError as exc:
                        raise SettingsError(400, f"ScanRegion without {exc.args[0]}") from None
            case _:
                pass  # unknown elements are ignored, like a tolerant device
    # DocumentFormatExt wins over pwg:DocumentFormat (eSCL 2.x semantics).
    fmt = doc_format_ext or doc_format
    if fmt:
        values["document_format"] = fmt
    if regions:
        values["region"] = regions[0]
    raw["_region_count"] = str(len(regions))
    values["raw"] = raw
    return ScanSettings(**values)


def validate(settings: ScanSettings, caps: Capabilities, *, bw_jpeg_behaviour: str) -> InputCaps:
    """Check settings against the capabilities. Unsupported values → 409 Conflict."""
    src = settings.input_source
    if src == "Feeder":
        icaps = caps.duplex if settings.duplex else caps.simplex
        if icaps is None:
            raise SettingsError(409, "duplex not supported")
    elif src == "Platen":
        if caps.platen is None:
            raise SettingsError(409, "InputSource Platen not supported by this device")
        icaps = caps.platen
    else:
        raise SettingsError(409, f"unsupported InputSource {src!r}")

    if settings.color_mode not in icaps.color_modes:
        raise SettingsError(409, f"unsupported ColorMode {settings.color_mode!r}")
    if (settings.x_resolution, settings.y_resolution) not in icaps.resolutions:
        raise SettingsError(409, f"unsupported resolution {settings.x_resolution}x{settings.y_resolution}")
    if settings.document_format not in icaps.document_formats:
        raise SettingsError(409, f"unsupported DocumentFormat {settings.document_format!r}")
    if settings.intent is not None and settings.intent not in icaps.intents:
        raise SettingsError(409, f"unsupported Intent {settings.intent!r}")
    if settings.content_type is not None and settings.content_type not in icaps.content_types:
        raise SettingsError(409, f"unsupported ContentType {settings.content_type!r}")
    if int(settings.raw.get("_region_count", "0")) > icaps.max_scan_regions:
        raise SettingsError(409, "too many ScanRegions")
    if settings.region is not None:
        r = settings.region
        if r.width < icaps.min_width or r.height < icaps.min_height:
            raise SettingsError(409, f"ScanRegion {r.width}x{r.height} below minimum")
        if r.x_offset < 0 or r.y_offset < 0:
            raise SettingsError(409, "negative ScanRegion offset")
    if settings.multipick_action is not None and settings.multipick_action not in caps.multipick_actions:
        raise SettingsError(409, f"unsupported MultipickDetectionAction {settings.multipick_action!r}")
    for attr, key in (
        ("brightness", "Brightness"),
        ("contrast", "Contrast"),
        ("threshold", "Threshold"),
        ("compression_factor", "CompressionFactor"),
        ("blank_page_sensitivity", "BlankPageSensitivity"),
    ):
        value = getattr(settings, attr)
        rng = caps.ranges.get(key)
        if value is not None and rng is not None and not (rng.min <= value <= rng.max):
            raise SettingsError(409, f"{key} {value} outside {rng.min}..{rng.max}")
    if (
        settings.color_mode == "BlackAndWhite1"
        and settings.document_format in JPEG_LIKE
        and bw_jpeg_behaviour == "reject"
    ):
        raise SettingsError(409, "BlackAndWhite1 cannot be delivered as JPEG")
    return icaps


def effective_region(settings: ScanSettings, icaps: InputCaps) -> Region:
    """The region actually scanned: default A4, clamped to the device maximum."""
    r = settings.region or Region(*A4_REGION)
    width = min(r.width, icaps.max_width)
    height = min(r.height, icaps.max_height)
    x_off = min(r.x_offset, max(0, icaps.max_width - width))
    return Region(width=width, height=height, x_offset=x_off, y_offset=r.y_offset)
