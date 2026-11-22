"""eSCL protocol behaviour: capabilities, status, job lifecycle, validation."""

from __future__ import annotations

from importlib import resources
from pathlib import Path

import pytest
from lxml import etree

from escl_sim import SimState
from escl_sim.caps import NS_PWG, NS_SCAN

from .conftest import create_job, fetch_all, image, scan_settings

NS = {"scan": NS_SCAN, "pwg": NS_PWG}
FIXTURE = Path(__file__).resolve().parents[3] / "docs" / "fixtures" / "scanner-capabilities.xml"


def parse_status(content: bytes):
    root = etree.fromstring(content)
    assert root.tag == f"{{{NS_SCAN}}}ScannerStatus"
    return root


def job_info(root, uuid: str):
    for info in root.iterfind("scan:Jobs/scan:JobInfo", NS):
        if info.findtext("pwg:JobUuid", namespaces=NS) == uuid:
            return {
                "uri": info.findtext("pwg:JobUri", namespaces=NS),
                "state": info.findtext("pwg:JobState", namespaces=NS),
                "reasons": [e.text for e in info.iterfind("pwg:JobStateReasons/pwg:JobStateReason", NS)],
                "completed": int(info.findtext("pwg:ImagesCompleted", namespaces=NS)),
                "to_transfer": int(info.findtext("pwg:ImagesToTransfer", namespaces=NS)),
                "age": int(info.findtext("scan:Age", namespaces=NS)),
            }
    raise AssertionError(f"job {uuid} not in status")


async def status(client):
    r = await client.get("/eSCL/ScannerStatus")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/xml")
    return parse_status(r.content)


def adf(root) -> str:
    return root.findtext("scan:AdfState", namespaces=NS)


async def test_capabilities_verbatim(client):
    r = await client.get("/eSCL/ScannerCapabilities")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/xml")
    packaged = resources.files("escl_sim").joinpath("data/scanner-capabilities.xml").read_bytes()
    assert r.content == packaged
    if FIXTURE.exists():
        assert r.content == FIXTURE.read_bytes()


async def test_status_idle_namespaces(client):
    root = await status(client)
    assert root.nsmap["scan"] == NS_SCAN and root.nsmap["pwg"] == NS_PWG
    assert root.findtext("pwg:Version", namespaces=NS) == "2.9"
    assert root.findtext("pwg:State", namespaces=NS) == "Idle"
    assert adf(root) == "ScannerAdfLoaded"
    assert root.find("scan:Jobs", NS) is not None


async def test_simplex_job_three_pages_then_404(client, state: SimState):
    uri = await create_job(client)
    assert uri.startswith("http://sim/eSCL/ScanJobs/")
    uuid = uri.rsplit("/", 1)[1]

    root = await status(client)
    info = job_info(root, uuid)
    assert info["state"] == "Processing" and "JobScanning" in info["reasons"]
    assert info["uri"] == f"/eSCL/ScanJobs/{uuid}"
    assert root.findtext("pwg:State", namespaces=NS) == "Processing"
    assert adf(root) == "ScannerAdfProcessing"

    pages, last = await fetch_all(client, uri)
    assert len(pages) == 3
    assert last.status_code == 404
    for p in pages:
        assert p[:3] == b"\xff\xd8\xff"
    root = await status(client)
    info = job_info(root, uuid)
    assert info["state"] == "Completed"
    assert info["reasons"] == ["JobCompletedSuccessfully"]
    assert info["completed"] == 3 and info["to_transfer"] == 0
    assert adf(root) == "ScannerAdfEmpty"
    assert root.findtext("pwg:State", namespaces=NS) == "Idle"
    assert state.feeder == []


async def test_relative_location_and_path_only_client(client, state: SimState):
    state.config.location_style = "relative"
    uri = await create_job(client)
    assert uri.startswith("/eSCL/ScanJobs/")
    r = await client.get(f"{uri}/NextDocument")
    assert r.status_code == 200


async def test_duplex_order(client, state: SimState):
    state.load("duplex_mixed")
    uri = await create_job(client, duplex=True, dpi=75)
    pages, last = await fetch_all(client, uri)
    assert last.status_code == 404
    assert len(pages) == 8
    job = state.jobs[uri.rsplit("/", 1)[1]]
    order = [(d["sheet"], d["side"]) for d in job.delivered]
    assert order == [(1, "front"), (1, "back"), (2, "front"), (2, "back"),
                     (3, "front"), (3, "back"), (4, "front"), (4, "back")]
    assert job.delivered[4]["qr"] == "DUPLEX-7"
    assert [d["kind"] for d in job.delivered[1::2]] == ["text", "blank", "near_blank", "blank"]


async def test_blank_removal_simplex(client, state: SimState):
    state.load("with_blanks")
    uri = await create_job(client, dpi=75, extra="<scan:BlankPageDetectionAndRemoval>true</scan:BlankPageDetectionAndRemoval>")
    pages, last = await fetch_all(client, uri)
    assert len(pages) == 2 and last.status_code == 404
    job = state.jobs[uri.rsplit("/", 1)[1]]
    assert [d["kind"] for d in job.delivered] == ["text", "text"]
    assert job.blank_removed == 2 and job.state == "Completed"


async def test_blank_detection_hp_element_duplex(client, state: SimState):
    state.load("duplex_mixed")
    uri = await create_job(client, duplex=True, dpi=75, extra="<scan:BlankPageDetection>true</scan:BlankPageDetection>")
    pages, _ = await fetch_all(client, uri)
    assert len(pages) == 5


async def test_without_blank_removal_blanks_are_delivered(client, state: SimState):
    state.load("with_blanks")
    uri = await create_job(client, dpi=75)
    pages, _ = await fetch_all(client, uri)
    assert len(pages) == 4


async def test_empty_feeder_409(client, state: SimState):
    state.load_empty()
    root = await status(client)
    assert adf(root) == "ScannerAdfEmpty"
    r = await client.post("/eSCL/ScanJobs", content=scan_settings())
    assert r.status_code == 409


async def test_busy_active_job_503(client):
    await create_job(client)
    r = await client.post("/eSCL/ScanJobs", content=scan_settings())
    assert r.status_code == 503


async def test_cancel_job(client, state: SimState):
    uri = await create_job(client, dpi=75)
    assert (await client.get(f"{uri}/NextDocument")).status_code == 200
    r = await client.delete(uri)
    assert r.status_code == 200
    uuid = uri.rsplit("/", 1)[1]
    info = job_info(await status(client), uuid)
    assert info["state"] == "Canceled" and info["reasons"] == ["JobCanceledByUser"]
    assert (await client.get(f"{uri}/NextDocument")).status_code == 404
    assert len(state.feeder) == 2  # unfed sheets stay in the tray
    assert (await client.delete(uri)).status_code == 200  # idempotent, like sane-airscan cleanup expects
    assert (await client.delete("/eSCL/ScanJobs/nope")).status_code == 404
    # scanner is free again
    await create_job(client)


async def test_unknown_job_404(client):
    assert (await client.get("/eSCL/ScanJobs/does-not-exist/NextDocument")).status_code == 404


@pytest.mark.parametrize(
    ("kw", "expected"),
    [
        ({"source": "Platen"}, 409),
        ({"source": "Camera"}, 409),
        ({"dpi": 1200}, 409),
        ({"dpi": 100}, 409),
        ({"fmt": "image/png"}, 409),
        ({"color": "RGB48"}, 409),
        ({"region": (10, 3508, 0, 0)}, 409),
        ({"extra": "<scan:Brightness>5000</scan:Brightness>"}, 409),
        ({"extra": "<scan:MultipickDetectionAction>Explode</scan:MultipickDetectionAction>"}, 409),
        ({"extra": "<scan:XResolution>abc</scan:XResolution>"}, 400),
    ],
)
async def test_invalid_settings_rejected(client, kw, expected):
    r = await client.post("/eSCL/ScanJobs", content=scan_settings(**kw))
    assert r.status_code == expected, r.text


async def test_malformed_xml_400(client):
    assert (await client.post("/eSCL/ScanJobs", content=b"<scan:ScanSettings")).status_code == 400
    assert (await client.post("/eSCL/ScanJobs", content=b"<Foo/>")).status_code == 400
    assert (await client.post("/eSCL/ScanJobs", content=b"")).status_code == 400


async def test_minimal_settings_use_defaults(client):
    body = (b'<scan:ScanSettings xmlns:scan="http://schemas.hp.com/imaging/escl/2011/05/03" '
            b'xmlns:pwg="http://www.pwg.org/schemas/2010/12/sm"><pwg:Version>2.0</pwg:Version>'
            b"<pwg:InputSource>Feeder</pwg:InputSource></scan:ScanSettings>")
    r = await client.post("/eSCL/ScanJobs", content=body)
    assert r.status_code == 201
    page = await client.get(r.headers["Location"] + "/NextDocument")
    img = image(page.content)
    assert img.size == (2480, 3508) and img.mode == "RGB"  # A4 at 300 dpi, RGB24


async def test_scan_image_info(client):
    uri = await create_job(client, dpi=75)
    await client.get(f"{uri}/NextDocument")
    r = await client.get(f"{uri}/ScanImageInfo")
    assert r.status_code == 200
    root = etree.fromstring(r.content)
    assert root.findtext("scan:ActualWidth", namespaces=NS) == "620"
    assert root.findtext("scan:ActualBytesPerLine", namespaces=NS) == str(620 * 3)


async def test_job_timeout_aborts(client, state: SimState):
    now = [1000.0]
    state.clock = lambda: now[0]
    uri = await create_job(client, dpi=75)
    now[0] += state.config.job_timeout_s + 1
    info = job_info(await status(client), uri.rsplit("/", 1)[1])
    assert info["state"] == "Aborted" and "JobTimedOut" in info["reasons"]
    await create_job(client, dpi=75)  # scanner free again


async def test_job_history_and_age(client, state: SimState):
    now = [50.0]
    state.clock = lambda: now[0]
    uri = await create_job(client, dpi=75)
    await fetch_all(client, uri)
    now[0] += 7
    info = job_info(await status(client), uri.rsplit("/", 1)[1])
    assert info["age"] == 7
