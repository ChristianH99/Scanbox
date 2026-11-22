"""Fault injection: jam, multipick, slow, drop, offline, busy, hatch, latency."""

from __future__ import annotations

import asyncio
import socket
import time

import httpx
import pytest
import uvicorn
from escl_sim import SimState, create_app

from .conftest import create_job, fetch_all, scan_settings
from .test_protocol import adf, job_info, status


def uuid_of(uri: str) -> str:
    return uri.rsplit("/", 1)[1]


async def test_jam_after_two_pages(client, state: SimState):
    state.load("long_stack")
    state.faults.jam_after_pages = 2
    uri = await create_job(client, dpi=75)
    pages, last = await fetch_all(client, uri)
    assert len(pages) == 2
    assert last.status_code == 404  # default abort_status (see SIMULATOR.md)
    root = await status(client)
    assert adf(root) == "ScannerAdfJam"
    assert root.findtext("{http://www.pwg.org/schemas/2010/12/sm}State") == "Stopped"
    info = job_info(root, uuid_of(uri))
    assert info["state"] == "Aborted"
    assert info["reasons"] == ["AbortedBySystem", "MediaJam"]
    assert info["completed"] == 2
    assert state.faults.jam_after_pages is None  # one-shot
    # jammed: new jobs refused until the paper path is cleared
    assert (await client.post("/eSCL/ScanJobs", content=scan_settings())).status_code == 409
    assert len(state.feeder) == 27
    state.clear_jam()  # operator puts the jammed sheet back on top
    assert len(state.feeder) == 28
    assert adf(await status(client)) == "ScannerAdfLoaded"
    await create_job(client, dpi=75)


async def test_jam_abort_status_configurable(client, state: SimState):
    state.config.abort_status = 500
    state.faults.jam_after_pages = 0
    uri = await create_job(client, dpi=75)
    r = await client.get(f"{uri}/NextDocument")
    assert r.status_code == 500


async def test_sticky_jam_every_job(client, state: SimState):
    state.load("long_stack")
    state.set_faults(jam_after_pages=1, sticky=True)
    for _ in range(2):
        uri = await create_job(client, dpi=75)
        pages, _ = await fetch_all(client, uri)
        assert len(pages) == 1
        state.clear_jam(reinsert=False)


async def test_multipick_continue_job(client, state: SimState):
    state.load([{"kind": "text", "seed": i} for i in range(4)])
    state.faults.multipick_after_pages = 1
    uri = await create_job(
        client, dpi=75,
        extra="<scan:MultipickDetection>true</scan:MultipickDetection>"
              "<scan:MultipickDetectionAction>ContinueJob</scan:MultipickDetectionAction>",
    )
    first = await client.get(f"{uri}/NextDocument")
    assert first.status_code == 200
    second = await client.get(f"{uri}/NextDocument")  # the double-fed sheet is delivered...
    assert second.status_code == 200
    info = job_info(await status(client), uuid_of(uri))
    assert info["state"] == "Processing" and "MultipickDetected" in info["reasons"]
    rest, last = await fetch_all(client, uri)
    assert len(rest) == 1 and last.status_code == 404  # ...but the sheet stuck to it was never scanned
    info = job_info(await status(client), uuid_of(uri))
    assert info["state"] == "Completed"
    assert info["reasons"] == ["JobCompletedSuccessfully", "MultipickDetected"]
    assert [s.seed for s in state.lost_sheets] == [2]


async def test_multipick_stop_job(client, state: SimState):
    state.load([{"kind": "text", "seed": i} for i in range(4)])
    state.faults.multipick_after_pages = 1
    uri = await create_job(
        client, dpi=75,
        extra="<scan:MultipickDetection>true</scan:MultipickDetection>"
              "<scan:MultipickDetectionAction>StopJob</scan:MultipickDetectionAction>",
    )
    pages, last = await fetch_all(client, uri)
    assert len(pages) == 1 and last.status_code == 404
    root = await status(client)
    assert adf(root) == "ScannerAdfMultipickDetected"
    info = job_info(root, uuid_of(uri))
    assert info["state"] == "Aborted" and info["reasons"] == ["AbortedBySystem", "MultipickDetected"]
    state.clear_jam()
    assert [s.seed for s in state.feeder] == [1, 2, 3]


async def test_multipick_undetected_when_disabled(client, state: SimState):
    state.load([{"kind": "text", "seed": i} for i in range(3)])
    state.faults.multipick_after_pages = 0
    uri = await create_job(client, dpi=75, extra="<scan:MultipickDetection>false</scan:MultipickDetection>")
    pages, _ = await fetch_all(client, uri)
    assert len(pages) == 2  # one sheet silently lost
    info = job_info(await status(client), uuid_of(uri))
    assert info["reasons"] == ["JobCompletedSuccessfully"]


async def test_slow_mode_retry_after(client, state: SimState):
    state.faults.slow_ms = 300
    uri = await create_job(client, dpi=75)
    r = await client.get(f"{uri}/NextDocument")
    assert r.status_code == 503
    assert r.headers["Retry-After"] == "1"
    assert (await client.get(f"{uri}/NextDocument")).status_code == 503  # still scanning
    await asyncio.sleep(0.35)
    r = await client.get(f"{uri}/NextDocument")
    assert r.status_code == 200 and r.content[:2] == b"\xff\xd8"
    assert (await client.get(f"{uri}/NextDocument")).status_code == 503  # next page starts scanning


async def test_slow_mode_block(client, state: SimState):
    state.set_faults(slow_ms=150, slow_style="block")
    uri = await create_job(client, dpi=75)
    t = time.perf_counter()
    r = await client.get(f"{uri}/NextDocument")
    assert r.status_code == 200
    assert time.perf_counter() - t >= 0.14


async def test_latency(client, state: SimState):
    state.faults.latency_ms = 120
    t = time.perf_counter()
    assert (await client.get("/eSCL/ScannerStatus")).status_code == 200
    assert time.perf_counter() - t >= 0.11
    t = time.perf_counter()
    await client.get("/_sim/state")  # control API is never delayed
    assert time.perf_counter() - t < 0.1


async def test_drop_connection_asgi(client, state: SimState):
    state.faults.drop_connection_after_pages = 1
    uri = await create_job(client, dpi=75)
    assert (await client.get(f"{uri}/NextDocument")).status_code == 200
    with pytest.raises(httpx.TransportError):
        await client.get(f"{uri}/NextDocument")
    # default drop_mode=lose_page: the device thinks page 2 was sent and continues with page 3
    r = await client.get(f"{uri}/NextDocument")
    assert r.status_code == 200
    job = state.jobs[uuid_of(uri)]
    assert [d["sheet"] for d in job.delivered] == [1, 3]
    assert [d["sheet"] for d in job.lost] == [2]


async def test_drop_connection_redeliver(client, state: SimState):
    state.config.drop_mode = "redeliver"
    state.faults.drop_connection_after_pages = 0
    uri = await create_job(client, dpi=75)
    with pytest.raises(httpx.TransportError):
        await client.get(f"{uri}/NextDocument")
    pages, _ = await fetch_all(client, uri)
    assert len(pages) == 3


async def test_offline_one_shot_and_sticky(client, state: SimState):
    state.faults.offline = True
    with pytest.raises(httpx.ConnectError):
        await client.get("/eSCL/ScannerStatus")
    assert (await client.get("/eSCL/ScannerStatus")).status_code == 200
    state.set_faults(offline=True, sticky=True)
    for _ in range(3):
        with pytest.raises(httpx.ConnectError):
            await client.get("/eSCL/ScannerCapabilities")
    assert (await client.get("/_sim/state")).status_code == 200  # control API unaffected
    state.faults.offline = False
    assert (await client.get("/eSCL/ScannerStatus")).status_code == 200


async def test_offline_503_mode(client, state: SimState):
    state.config.offline_mode = "503"
    state.faults.offline = True
    assert (await client.get("/eSCL/ScannerStatus")).status_code == 503


async def test_busy_fault_one_shot(client, state: SimState):
    state.faults.busy = True
    root = await status(client)
    assert root.findtext("{http://www.pwg.org/schemas/2010/12/sm}State") == "Processing"
    assert (await client.post("/eSCL/ScanJobs", content=scan_settings())).status_code == 503
    await create_job(client)


async def test_fail_create_status(client, state: SimState):
    state.faults.fail_create_status = 500
    assert (await client.post("/eSCL/ScanJobs", content=scan_settings())).status_code == 500
    await create_job(client)


async def test_hatch_open(client, state: SimState):
    state.faults.hatch_open = True
    assert adf(await status(client)) == "ScannerAdfHatchOpen"
    assert (await client.post("/eSCL/ScanJobs", content=scan_settings())).status_code == 409
    state.faults.hatch_open = False
    uri = await create_job(client, dpi=75)
    assert (await client.get(f"{uri}/NextDocument")).status_code == 200
    state.faults.hatch_open = True  # opened mid-job
    assert (await client.get(f"{uri}/NextDocument")).status_code == 404
    info = job_info(await status(client), uuid_of(uri))
    assert info["state"] == "Aborted" and "DoorOpen" in info["reasons"]


# ---------------------------------------------------------------- real HTTP server (standalone mode)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
async def server(state: SimState):
    port = _free_port()
    config = uvicorn.Config(create_app(state, offline_mode="503"), host="127.0.0.1", port=port, log_level="critical")
    srv = uvicorn.Server(config)
    task = asyncio.create_task(srv.serve())
    for _ in range(100):
        if srv.started:
            break
        await asyncio.sleep(0.02)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    await task


async def test_drop_connection_real_server(server, state: SimState):
    state.faults.drop_connection_after_pages = 0
    async with httpx.AsyncClient(base_url=server) as c:
        r = await c.post("/eSCL/ScanJobs", content=scan_settings(dpi=150))
        assert r.status_code == 201
        assert r.headers["Location"].startswith(server + "/eSCL/ScanJobs/")
        with pytest.raises(httpx.TransportError):
            await c.get(r.headers["Location"] + "/NextDocument")
    async with httpx.AsyncClient(base_url=server) as c:
        r2 = await c.get(r.headers["Location"] + "/NextDocument")
        assert r2.status_code == 200
        state.faults.offline = True
        assert (await c.get("/eSCL/ScannerStatus")).status_code == 503
