"""Generated page content: sizes, colour modes, QR codes, determinism."""

from __future__ import annotations

import asyncio
import shutil
import subprocess

import numpy as np
import pytest
import zxingcpp
from escl_sim import PageSpec, SimState, imaging, pages
from PIL import Image

from .conftest import create_job, fetch_all, image, scan_settings


async def first_page(client, **kw) -> Image.Image:
    uri = await create_job(client, **kw)
    r = await client.get(f"{uri}/NextDocument")
    assert r.status_code == 200
    return image(r.content)


@pytest.mark.parametrize(
    ("dpi", "region", "size"),
    [
        (300, (2480, 3508, 0, 0), (2480, 3508)),
        (150, (2480, 3508, 0, 0), (1240, 1754)),
        (75, (2480, 3508, 0, 0), (620, 877)),
        (200, (2550, 3300, 0, 0), (1700, 2200)),  # US letter
        (150, (3000, 3508, 0, 0), (1275, 1754)),  # width clamped to MaxWidth 2550
        (150, None, (1240, 1754)),  # default region A4
    ],
)
async def test_resolution_to_pixel_size(client, dpi, region, size):
    img = await first_page(client, dpi=dpi, region=region)
    assert img.size == size
    assert img.info.get("dpi") == pytest.approx((dpi, dpi), abs=0.5)
    assert img.info.get("jfif_density") == (dpi, dpi)


async def test_color_modes(client, state: SimState):
    assert (await first_page(client, color="RGB24", dpi=75)).mode == "RGB"
    state.reset()
    assert (await first_page(client, color="Grayscale8", dpi=75)).mode == "L"


async def test_auto_color_detection(client, state: SimState):
    state.load("photo")
    uri = await create_job(client, color="scan:AutoColorDetection", dpi=75)
    pages_, _ = await fetch_all(client, uri)
    assert [image(p).mode for p in pages_] == ["RGB", "L"]


async def test_bw_gray_thresholded(client):
    img = await first_page(client, color="BlackAndWhite1", dpi=150)
    assert img.mode == "L"
    arr = np.asarray(img)
    extreme = ((arr < 40) | (arr > 215)).mean()
    assert extreme > 0.99
    assert (arr < 40).mean() > 0.005  # there is text


async def test_bw_reject(client, state: SimState):
    state.config.bw_jpeg_behaviour = "reject"
    r = await client.post("/eSCL/ScanJobs", content=scan_settings(color="BlackAndWhite1"))
    assert r.status_code == 409
    # PDF can carry 1-bit data, so it is still accepted
    r = await client.post("/eSCL/ScanJobs", content=scan_settings(color="BlackAndWhite1", fmt="application/pdf"))
    assert r.status_code == 201


async def test_pdf_output(client):
    uri = await create_job(client, fmt="application/pdf", dpi=75)
    r = await client.get(f"{uri}/NextDocument")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.content.startswith(b"%PDF")


def decode_qrs(img: Image.Image):
    return zxingcpp.read_barcodes(img, formats=zxingcpp.BarcodeFormat.QRCode)


def centre(barcode) -> tuple[float, float]:
    p = barcode.position
    xs = [p.top_left.x, p.top_right.x, p.bottom_left.x, p.bottom_right.x]
    ys = [p.top_left.y, p.top_right.y, p.bottom_left.y, p.bottom_right.y]
    return sum(xs) / 4, sum(ys) / 4


@pytest.mark.parametrize("dpi", [150, 300])
async def test_qr_decodable_in_top_right_zone(client, state: SimState, dpi):
    state.load("invoice_qr")
    img = await first_page(client, dpi=dpi, color="Grayscale8")
    hits = decode_qrs(img)
    assert [h.text for h in hits] == ["INV-2024-0042"]
    cx, cy = centre(hits[0])
    w, h = img.size
    # architecture §8 top_right zone = {x:0.6, y:0, w:0.4, h:0.3}
    assert cx / w > 0.6 and cy / h < 0.3
    # ~2.5 cm (allow integer module scaling)
    side = abs(hits[0].position.top_right.x - hits[0].position.top_left.x) / dpi * 25.4
    assert 12 < side < 26


async def test_multi_qr_positions(client, state: SimState):
    state.load("multi_qr")
    img = await first_page(client, dpi=150)
    hits = {h.text: centre(h) for h in decode_qrs(img)}
    w, h = img.size
    assert set(hits) == {"ZONE-123", "OTHER-999"}
    assert hits["ZONE-123"][0] / w > 0.6 and hits["ZONE-123"][1] / h < 0.3
    assert hits["OTHER-999"][0] / w < 0.4 and hits["OTHER-999"][1] / h > 0.7


async def test_qr_still_decodable_when_skewed(client, state: SimState):
    state.load("skewed")
    state.feeder = state.feeder[3:]  # the skewed QR page (-6°)
    img = await first_page(client, dpi=200)
    assert [h.text for h in decode_qrs(img)] == ["SKEW-1"]


async def test_separator_batch_values(client, state: SimState):
    state.load("separator_batch")
    uri = await create_job(client, dpi=150)
    pages_, _ = await fetch_all(client, uri)
    values = [[h.text for h in decode_qrs(image(p))] for p in pages_]
    assert values == [["DOC-A"], [], ["DOC-B"], [], []]


def test_background_visible_for_autocrop():
    spec = PageSpec(kind="text", skew_deg=4)
    p = imaging.OutputParams(dpi=75, region=(2550, 3600, 0, 0), bed_width=2550, background=(200, 200, 198),
                             color_mode="RGB24", document_format="image/jpeg", quality=80, threshold=128,
                             brightness=None, contrast=None, auto_crop=False, samples_dir=pages.DEFAULT_SAMPLES_DIR)
    img, bbox = imaging.compose_scan(spec, p)
    arr = np.asarray(img)
    assert tuple(arr[0, 0]) == (200, 200, 198)  # corner shows scanner background
    assert tuple(arr[arr.shape[0] // 2, arr.shape[1] // 2]) == pages.PAPER_RGB
    cropped = imaging.encode_page(spec, p.__class__(**{**p.__dict__, "auto_crop": True}))
    assert (cropped.width, cropped.height) == (bbox[2] - bbox[0], bbox[3] - bbox[1])


def test_rendering_is_deterministic():
    spec = PageSpec(kind="qr", qr="X-1", seed=5, skew_deg=2.5)
    a = np.asarray(pages.render_paper(spec, 100)).copy()
    pages.render_paper.cache_clear()
    b = np.asarray(pages.render_paper(spec, 100))
    assert np.array_equal(a, b)


def test_rotation_upside_down():
    up = np.asarray(pages.render_paper(PageSpec(kind="text", seed=9), 75))
    down = np.asarray(pages.render_paper(PageSpec(kind="text", seed=9, rotation=180), 75))
    assert np.array_equal(up[::-1, ::-1], down)
    side = pages.render_paper(PageSpec(kind="text", seed=9, rotation=90), 75)
    assert side.size == pages.render_paper(PageSpec(kind="text", seed=9), 75).size


async def test_file_page(client, state: SimState, tmp_path):
    src = Image.new("RGB", (400, 560), (255, 0, 0))
    path = tmp_path / "red.jpg"
    src.save(path)
    state.load([{"kind": "file", "path": str(path)}])
    img = await first_page(client, dpi=75)
    r, g, b = img.getpixel((img.width // 2, img.height // 2))
    assert r > 200 and g < 60 and b < 60


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract not installed")
async def test_text_is_ocrable(client, tmp_path):
    img = await first_page(client, dpi=300, color="Grayscale8")
    path = tmp_path / "p.png"
    img.save(path)
    out = (await asyncio.to_thread(
        subprocess.run, ["tesseract", str(path), "-", "-l", "deu+eng" if _has_deu() else "eng"],
        capture_output=True, text=True, timeout=60, check=False)).stdout
    assert "Sehr geehrte Damen und Herren" in out


def _has_deu() -> bool:
    out = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True, check=False).stdout
    return "deu" in out.split()
