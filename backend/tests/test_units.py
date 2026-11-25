"""Unit tests: filenames, QR zone/selection, eSCL XML + client, state machine, imageops."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import cv2
import httpx
import numpy as np
import pytest
import qrcode

from scanbox.escl.client import EsclClient, ScannerOffline, normalise_base_url
from scanbox.escl.xml import (
    NS,
    ScanSettings,
    build_scan_settings,
    parse_capabilities,
    parse_status,
    parse_xml,
)
from scanbox.filenames import TemplateVars, build_filename, sanitize_component, sanitize_subpath, unique_path
from scanbox.jobs import states as st
from scanbox.processing import imageops as ops
from scanbox.processing.qr import QrHit, detect_qr, reading_order, select_hit
from scanbox.profiles import ProfileSettings, QrOptions, Zone, validate_against_caps

FIXTURE = Path(__file__).resolve().parents[2] / "docs" / "fixtures" / "scanner-capabilities.xml"


# ------------------------------------------------------------------ filenames
def tv(**kw) -> TemplateVars:  # noqa: ANN003
    base = {"created": datetime(2026, 3, 4, 5, 6, 7), "qr": "INV-42", "title": "Rechnung", "counter": 7,
            "profile": "Dokument", "user": "alice", "pages": 3}
    base.update(kw)
    return TemplateVars(**base)


def test_filename_tokens() -> None:
    assert build_filename("{date}_{title}", tv(), "pdf") == "2026-03-04_Rechnung.pdf"
    assert build_filename("{datetime}", tv(), "pdf") == "2026-03-04_05-06-07.pdf"
    assert build_filename("{date:%Y%m%d}-{counter}", tv(), "pdf") == "20260304-0007.pdf"
    assert build_filename("{qr}_{pages}p", tv(), "pdf") == "INV-42_3p.pdf"
    assert build_filename("{title}", tv(), "pdf", add_qr=True) == "INV-42_Rechnung.pdf"
    assert build_filename("{qr}_{title}", tv(), "pdf", add_qr=True) == "INV-42_Rechnung.pdf"
    # empty token leaves no dangling separators
    assert build_filename("{qr}_{title}", tv(qr=None), "pdf") == "Rechnung.pdf"
    assert build_filename("{unknown}{nothing}", tv(), "pdf") == "scan.pdf"


@pytest.mark.parametrize("bad", ["../../etc/passwd", "a/b\\c", "..", "CON", "nul.txt", "x\x00y", " .hidden"])
def test_filename_sanitising(bad: str) -> None:
    name = build_filename("{title}", tv(title=bad), "pdf")
    assert "/" not in name and "\\" not in name and "\x00" not in name
    assert not name.startswith(".")
    assert name.split(".")[0].upper() not in ("CON", "NUL")


def test_filename_length_limit_utf8() -> None:
    name = build_filename("{title}", tv(title="Ä" * 300), "pdf")
    assert len(name.encode()) <= 155 and name.endswith(".pdf")


def test_qr_value_path_traversal_in_filename() -> None:
    assert "/" not in build_filename("{qr}", tv(qr="../../x"), "pdf")


def test_subpath() -> None:
    assert str(sanitize_subpath("a/b:c/d")).replace("\\", "/") == "a/b_c/d"
    for bad in ("../x", "/abs", "C:\\x", "a/../../b"):
        with pytest.raises(ValueError):
            sanitize_subpath(bad)
    assert sanitize_component("  name.  ") == "name"


def test_unique_path(tmp_path: Path) -> None:
    (tmp_path / "a.pdf").write_text("x")
    (tmp_path / "a (2).pdf").write_text("x")
    assert unique_path(tmp_path, "a.pdf").name == "a (3).pdf"
    assert unique_path(tmp_path, "b.pdf").name == "b.pdf"


# ------------------------------------------------------------------ QR
def _page_with_qr(codes: list[tuple[str, float, float]], w: int = 1240, h: int = 1754, angle: float = 0) -> np.ndarray:
    page = np.full((h, w), 255, np.uint8)
    for value, rx, ry in codes:
        img = qrcode.make(value, box_size=6, border=2).convert("L")
        arr = np.array(img)
        x, y = int(rx * w), int(ry * h)
        page[y:y + arr.shape[0], x:x + arr.shape[1]] = arr
    if angle:
        page = ops.rotate_fine(page, angle)
    return page


def test_qr_zone_filters_codes() -> None:
    page = _page_with_qr([("ZONE-1", 0.75, 0.05), ("OTHER", 0.05, 0.8)])
    hits = detect_qr(page, Zone(x=0.6, y=0, w=0.4, h=0.3))
    assert [h.value for h in hits] == ["ZONE-1"]
    assert 0.6 < hits[0].cx < 1.0 and hits[0].cy < 0.3
    assert {h.value for h in detect_qr(page, Zone())} == {"ZONE-1", "OTHER"}


def test_qr_tolerates_rotation_and_noise() -> None:
    page = _page_with_qr([("SKEW-7", 0.7, 0.05)], angle=4)
    rng = np.random.default_rng(1)
    noise = rng.integers(0, 40, page.shape, dtype=np.uint8)
    noisy = cv2.subtract(page, noise)
    low_contrast = cv2.convertScaleAbs(noisy, alpha=0.5, beta=100)
    hits = detect_qr(low_contrast, Zone(x=0.5, y=0, w=0.5, h=0.35))
    assert [h.value for h in hits] == ["SKEW-7"]


def test_qr_selection_rules() -> None:
    hits = [QrHit("B", 0.8, 0.1, 0.05, 0.05), QrHit("A", 0.6, 0.08, 0.1, 0.1), QrHit("C", 0.6, 0.3, 0.05, 0.05)]
    assert [h.value for h in reading_order(hits)] == ["A", "B", "C"]
    assert select_hit(hits, QrOptions(selection="reading_order")).value == "A"
    assert select_hit(hits, QrOptions(selection="largest")).value == "A"
    assert select_hit(hits, QrOptions(selection="regex", regex="^C$")).value == "C"
    assert select_hit(hits, QrOptions(regex="^Z")) is None


def test_zone_validation() -> None:
    z = Zone(x=0.9, y=0.9, w=0.5, h=0.5)
    assert z.w == pytest.approx(0.1) and z.h == pytest.approx(0.1)
    with pytest.raises(ValueError):
        Zone(x=0.995, y=0, w=0.5, h=0.5)
    with pytest.raises(ValueError):
        QrOptions(regex="([unclosed")


# ------------------------------------------------------------------ eSCL XML
def test_parse_real_capabilities() -> None:
    caps = parse_capabilities(FIXTURE.read_bytes())
    assert caps.make_and_model == "HP ScanJet Pro N4000 snw1"
    assert caps.sources == ["adf_simplex", "adf_duplex"]
    assert caps.platen is None
    assert caps.adf_simplex.resolutions == [75, 150, 200, 240, 300, 400, 500, 600]
    assert "BlackAndWhite1" in caps.adf_duplex.color_modes
    assert caps.multipick_actions == ["ContinueJob", "StopJob"]
    assert caps.ranges["Brightness"].max == 2000 and caps.ranges["Threshold"].normal == 128
    assert caps.walkup_supported and caps.blank_page_detection and caps.auto_crop
    assert caps.feeder_capacity == 50


def test_profiles_validated_against_caps() -> None:
    caps = parse_capabilities(FIXTURE.read_bytes())
    assert validate_against_caps(ProfileSettings(), caps) == []
    assert validate_against_caps(ProfileSettings(resolution=1200), caps)
    bad = ProfileSettings.model_validate({"scanner": {"brightness": 5000}})
    assert any("Brightness" in p for p in validate_against_caps(bad, caps))


def test_scan_settings_xml() -> None:
    xml = build_scan_settings(ScanSettings(resolution=300, color_mode="Grayscale8", duplex=True,
                                           blank_page_removal=True, context_id="abc"))
    root = parse_xml(xml)
    assert root.findtext("scan:XResolution", namespaces=NS) == "300"
    assert root.findtext("scan:Duplex", namespaces=NS) == "true"
    assert root.findtext("pwg:InputSource", namespaces=NS) == "Feeder"
    assert root.findtext("scan:ContextID", namespaces=NS) == "abc"
    assert root.findtext("pwg:ScanRegions/pwg:ScanRegion/pwg:Width", namespaces=NS) == "2480"
    # optional extras are omitted unless set
    assert root.find("scan:Brightness", NS) is None


def test_parse_status() -> None:
    xml = f"""<?xml version="1.0"?><scan:ScannerStatus xmlns:scan="{NS['scan']}" xmlns:pwg="{NS['pwg']}">
      <pwg:Version>2.9</pwg:Version><pwg:State>Idle</pwg:State><scan:AdfState>ScannerAdfJam</scan:AdfState>
      <scan:Jobs><scan:JobInfo><pwg:JobUri>/eSCL/ScanJobs/abc</pwg:JobUri><pwg:JobUuid>abc</pwg:JobUuid>
      <pwg:JobState>Aborted</pwg:JobState><pwg:JobStateReasons><pwg:JobStateReason>AbortedBySystem</pwg:JobStateReason>
      </pwg:JobStateReasons></scan:JobInfo></scan:Jobs></scan:ScannerStatus>"""
    stt = parse_status(xml)
    assert stt.adf_state == "ScannerAdfJam"
    assert stt.job("http://x/eSCL/ScanJobs/abc").state == "Aborted"


def test_xml_rejects_entities() -> None:
    evil = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]><x>&e;</x>'
    root = parse_xml(evil)
    assert "root:" not in (root.text or "")


@pytest.mark.parametrize("addr,expected", [
    ("192.168.1.5", "http://192.168.1.5"),
    ("scanner.lan:8080", "http://scanner.lan:8080"),
    ("https://scanner.lan/eSCL/", "https://scanner.lan"),
    ("http://[fe80::1]:80", "http://[fe80::1]:80"),
])
def test_normalise_address(addr: str, expected: str) -> None:
    assert normalise_base_url(addr) == expected


@pytest.mark.parametrize("bad", ["", "ftp://x", "http://"])
def test_normalise_address_invalid(bad: str) -> None:
    with pytest.raises(ValueError):
        normalise_base_url(bad)


async def test_client_rewrites_location_host() -> None:
    """Scanners return Location with their mDNS name; we must keep talking to the configured address."""
    c = EsclClient("http://10.0.0.9:8080")
    assert c.resolve_job_uri("http://HP000000.local:8080/eSCL/ScanJobs/x") == "http://10.0.0.9:8080/eSCL/ScanJobs/x"
    assert c.resolve_job_uri("/eSCL/ScanJobs/y") == "http://10.0.0.9:8080/eSCL/ScanJobs/y"


async def test_client_retries_then_offline() -> None:
    calls = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("refused", request=req)

    from scanbox.escl.client import RetryPolicy

    c = EsclClient("http://x", client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                   retry=RetryPolicy(attempts=3, base_delay=0.01))
    with pytest.raises(ScannerOffline):
        await c.get_status()
    assert calls == 3


# ------------------------------------------------------------------ state machine
def test_state_machine_transitions() -> None:
    assert st.PROCESSING in st.TRANSITIONS[st.STORED]
    assert st.DONE not in st.TRANSITIONS[st.QUEUED]
    assert st.TRANSITIONS[st.DISCARDED] == set()
    for src, dsts in st.TRANSITIONS.items():
        assert src in st.ALL_STATES and dsts <= set(st.ALL_STATES)


# ------------------------------------------------------------------ imageops
def _text_page(angle: float) -> np.ndarray:
    page = np.full((1400, 1000), 255, np.uint8)
    for i in range(25):
        cv2.putText(page, "Lorem ipsum dolor sit amet consectetur", (80, 120 + i * 45),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.9, 0, 2)
    return ops.rotate_fine(page, angle)


@pytest.mark.parametrize("angle", [-4.0, 0.0, 2.5, 7.0])
def test_deskew_detects_angle(angle: float) -> None:
    detected = ops.detect_skew(_text_page(angle))
    assert detected == pytest.approx(-angle, abs=0.4)


def test_blank_detection() -> None:
    blank = np.full((1400, 1000), 250, np.uint8)
    rng = np.random.default_rng(0)
    specks = blank.copy()
    for _ in range(30):
        y, x = rng.integers(100, 1300), rng.integers(100, 900)
        specks[y, x] = 0
    assert ops.ink_coverage(blank) < 0.001
    assert ops.ink_coverage(specks) < 0.004
    assert ops.ink_coverage(_text_page(0)) > 0.01


def test_auto_crop_finds_paper() -> None:
    img = np.full((1200, 1000), 120, np.uint8)
    img[100:1100, 150:850] = 250
    rect = ops.find_paper_rect(img)
    assert rect is not None
    x, y, w, h = rect
    assert abs(x - 150) < 10 and abs(y - 100) < 10 and abs(w - 700) < 15 and abs(h - 1000) < 15
    assert ops.find_paper_rect(np.full((1200, 1000), 250, np.uint8)) is None


def test_enhance_bw() -> None:
    out = ops.enhance(_text_page(0), ops.Enhance(color="bw"))
    assert out.ndim == 2 and set(np.unique(out).tolist()) <= {0, 255}
