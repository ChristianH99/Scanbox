"""The /_sim control API and the SimState Python API."""

from __future__ import annotations

from escl_sim import PRESETS, SimState

from .conftest import create_job, fetch_all


async def test_state_and_reset(client, state: SimState):
    r = await client.get("/_sim/state")
    body = r.json()
    assert body["adf_state"] == "ScannerAdfLoaded" and body["feeder_count"] == 3
    uri = await create_job(client, dpi=75)
    await fetch_all(client, uri)
    body = (await client.get("/_sim/state")).json()
    assert body["feeder_count"] == 0 and body["jobs"][0]["state"] == "Completed"
    assert ["POST", "/eSCL/ScanJobs", 201] in body["requests"]
    body = (await client.post("/_sim/reset")).json()
    assert body["feeder_count"] == 3 and body["jobs"] == []


async def test_load_preset_pages_empty(client):
    r = await client.post("/_sim/load", json={"stack": "long_stack"})
    assert r.json()["feeder_count"] == 30
    r = await client.post("/_sim/load", json={"pages": [{"kind": "qr", "qr": "A"}, {"kind": "blank", "back": {"kind": "text"}}]})
    assert r.json()["feeder_count"] == 2
    r = await client.post("/_sim/load", json={"pages": [{"kind": "text"}], "append": True})
    assert r.json()["feeder_count"] == 3
    r = await client.post("/_sim/load", json={"empty": True})
    assert r.json()["adf_state"] == "ScannerAdfEmpty"
    assert (await client.post("/_sim/load", json={"stack": "nope"})).status_code == 422
    assert (await client.post("/_sim/load", json={"pages": [{"kind": "weird"}]})).status_code == 422
    assert (await client.post("/_sim/load", json={"pages": [{"kind": "text", "bogus": 1}]})).status_code == 422


async def test_faults_api(client, state: SimState):
    r = await client.post("/_sim/faults", json={"jam_after_pages": 1, "latency_ms": 0})
    assert r.json()["jam_after_pages"] == 1
    assert state.faults.jam_after_pages == 1
    assert (await client.post("/_sim/faults", json={"nonsense": 1})).status_code == 422
    uri = await create_job(client, dpi=75)
    pages, _ = await fetch_all(client, uri)
    assert len(pages) == 1
    body = (await client.get("/_sim/state")).json()
    assert body["adf_state"] == "ScannerAdfJam" and len(body["paper_path"]) == 1
    body = (await client.post("/_sim/clear_jam", json={"reinsert": True})).json()
    assert body["condition"] is None and body["feeder_count"] == 2
    await client.post("/_sim/faults", json={"hatch_open": True})
    assert (await client.delete("/_sim/faults")).json()["hatch_open"] is False


async def test_config_api(client, state: SimState):
    r = await client.post("/_sim/config", json={"bw_jpeg_behaviour": "reject", "background": [10, 20, 30]})
    assert r.json()["bw_jpeg_behaviour"] == "reject"
    assert state.config.background == (10, 20, 30)


async def test_presets_listed_and_loadable(client):
    body = (await client.get("/_sim/presets")).json()
    required = {"letter_de", "invoice_qr", "multi_qr", "with_blanks", "skewed", "duplex_mixed",
                "separator_batch", "photo", "long_stack"}
    assert required <= set(body) == set(PRESETS)
    assert len(body["long_stack"]) == 30
    assert body["invoice_qr"][0]["qr"] == "INV-2024-0042"


def test_python_api():
    st = SimState(stack="invoice_qr", bw_jpeg_behaviour="reject")
    assert len(st.feeder) == 3 and st.config.bw_jpeg_behaviour == "reject"
    st.set_faults(jam_after_pages=2, sticky=True)
    assert st.faults.sticky
    st.load_empty()
    assert st.adf_state == "ScannerAdfEmpty"
    st.reset()
    assert len(st.feeder) == 3 and st.faults.jam_after_pages is None
    assert SimState(stack=None).feeder == []
