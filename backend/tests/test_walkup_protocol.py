import asyncio
from pathlib import Path

import httpx
import pytest

from scanbox.walkup.protocol import (
    HpWalkupClient,
    PollState,
    RegisteredDestination,
    WalkupDeviceBusy,
    WalkupEventType,
    WalkupProtocolError,
    WalkupSubscriptionLost,
    WalkupUnsupported,
    WalkupVariant,
    build_escl_subscription,
    parse_escl_event,
    parse_escl_subscriptions,
    parse_event_table,
    parse_ledm_destinations,
    parse_walkup_support,
    recommended_poll_timeout,
)
from tests.walkup_fake import CAPS_FIXTURE, create_walkup_fake

FIX = Path(__file__).parent / "fixtures" / "walkup"
BASE = "http://scanner.test"


def fixture(name: str) -> bytes:
    return (FIX / name).read_bytes()


def make(variant: str, **kw):
    fake = create_walkup_fake(variant, **kw)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=fake), base_url=BASE)
    return fake, client, HpWalkupClient(client, BASE)


# ---------------------------------------------------------------- pure parsers / builders


def test_parse_walkup_support_from_n4000_caps():
    info = parse_walkup_support(CAPS_FIXTURE.read_bytes())
    assert info == {
        "max_destinations": 10,
        "inactivity_timeout_s": 150,
        "subscription_timeout_s": 150,
        "max_shortcuts": 20,
        "pin_support": False,
    }


def test_parse_recorded_8122e_events():
    ev = parse_escl_event(fixture("escl_event_scan_requested.xml"))
    assert ev.type == WalkupEventType.SCAN_REQUESTED
    assert ev.shortcut == "Paperless"
    assert ev.destination_uri == "/eSCL/WalkupSubscriptions/108b7a10-43ef-44b0-97ab-ea2f1274de50"
    assert ev.settings["XResolution"] == "300"
    assert ev.settings["Duplex"] == "false"
    assert ev.settings["ContentOrientation"] == "Portrait"
    assert parse_escl_event(fixture("escl_event_host_selected.xml")).type == WalkupEventType.HOST_SELECTED
    assert (
        parse_escl_event(fixture("escl_event_pages_complete.xml")).type == WalkupEventType.SCAN_PAGES_COMPLETE
    )


def test_unknown_event_is_other():
    xml = b'<scan:WalkupScanEvent xmlns:scan="http://schemas.hp.com/imaging/escl/2011/05/03"><scan:Event>Foo</scan:Event></scan:WalkupScanEvent>'
    ev = parse_escl_event(xml, "/x")
    assert ev.type == WalkupEventType.OTHER and ev.raw_type == "Foo" and ev.destination_uri == "/x"


def test_malformed_xml_raises_protocol_error():
    with pytest.raises(WalkupProtocolError):
        parse_escl_event(b"<not-xml")
    with pytest.raises(WalkupProtocolError):
        parse_escl_event(b"<foo/>")


def test_xxe_is_not_resolved():
    xml = b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]><scan:WalkupScanEvent xmlns:scan="http://schemas.hp.com/imaging/escl/2011/05/03"><scan:Event>&x;</scan:Event></scan:WalkupScanEvent>'
    ev = parse_escl_event(xml)
    assert "root" not in ev.raw_type


def test_subscription_body_matches_hp_client_shape():
    from lxml import etree

    ours = etree.fromstring(
        build_escl_subscription(
            "Laptop (DESKTOP-X)",
            ["Save as PDF", "Save as JPEG", "Email as PDF", "Everyday Scan", "Paperless"],
        )
    )
    theirs = etree.fromstring(fixture("escl_subscription_request.xml"))

    def shape(el):
        return (el.tag, (el.text or "").strip(), [shape(c) for c in el])

    assert shape(ours) == shape(theirs)


def test_parse_synthetic_subscription_listing():
    subs = parse_escl_subscriptions(fixture("escl_subscriptions_list_SYNTHETIC.xml"))
    assert [(s.name, s.uri, s.shortcuts) for s in subs] == [
        ("Scanbox", "/eSCL/WalkupSubscriptions/0f0e5c1a-1111-4c1b-9d55-0123456789ab", ("Document", "Photo")),
        ("OTHER-PC", "/eSCL/WalkupSubscriptions/2b9e7f1c-2222-4c1b-9d55-0123456789ab", ()),
    ]


def test_parse_recorded_ledm_documents():
    events = parse_event_table(fixture("ledm_event_table_comp.xml"))
    scan = [e for e in events if e.category == "ScanEvent"][0]
    assert scan.aging_stamp == "10-457"
    assert ("/WalkupScanToComp/WalkupScanToCompEvent", "wus:WalkupScanToCompEvent") in scan.payloads
    dests = parse_ledm_destinations(fixture("ledm_comp_destinations.xml"), WalkupVariant.LEDM_SCAN_TO_COMP)
    assert dests[0].name == "8d170beb5887"
    old = parse_ledm_destinations(fixture("ledm_walkupscan_destinations.xml"), WalkupVariant.LEDM_WALKUP_SCAN)
    assert old[0].uri == "/WalkupScan/WalkupScanDestinations/1c856ba3-b916-1f08-be4e-2c768ab21113"


def test_recommended_poll_timeout():
    from scanbox.walkup.protocol import WalkupSupport

    assert recommended_poll_timeout(WalkupSupport(subscription_timeout_s=150)) == 90
    assert recommended_poll_timeout(WalkupSupport(subscription_timeout_s=3600)) == 100
    assert recommended_poll_timeout(None) == 60


# ---------------------------------------------------------------- probe


@pytest.mark.parametrize(
    ("variant", "expected"),
    [
        ("escl", (WalkupVariant.ESCL,)),
        ("escl_nolist", (WalkupVariant.ESCL,)),
        ("ledm", (WalkupVariant.LEDM_SCAN_TO_COMP,)),
        ("ledm_walkupscan", (WalkupVariant.LEDM_WALKUP_SCAN,)),
        ("none", ()),
    ],
)
async def test_probe_variants(variant, expected):
    fake, client, wc = make(variant)
    async with client:
        support = await wc.probe()
    assert support.variants == expected
    assert all(m == "GET" for m, _ in fake.requests), "probe must be read-only"
    if variant.startswith("escl"):
        assert (
            support.escl_caps_advertised
            and support.subscription_timeout_s == 150
            and support.max_shortcuts == 20
        )
    if variant == "ledm":
        assert support.user_action_timeout_s == 60 and support.max_destinations == 15


async def test_register_on_unsupported_device_raises():
    _, client, wc = make("none")
    async with client:
        with pytest.raises(WalkupUnsupported):
            await wc.register("Scanbox", "nas")


# ---------------------------------------------------------------- eSCL flow


async def test_escl_end_to_end():
    fake, client, wc = make("escl")
    async with client:
        await wc.probe()
        dest = await wc.register("Scanbox", "nas", shortcuts=["Document", "Photo"])
        assert dest.variant == WalkupVariant.ESCL and dest.uri.startswith("/eSCL/WalkupSubscriptions/")
        state = PollState(dest)

        assert await wc.poll_event(state, 0.2) is None  # 204 on idle

        await fake.select_host("Scanbox")
        ev = await wc.poll_event(state, 1)
        assert ev.type == WalkupEventType.HOST_SELECTED

        async def later():
            await asyncio.sleep(0.05)
            await fake.start_scan(
                "Scanbox",
                "Photo",
                XResolution=600,
                YResolution=600,
                Duplex=True,
                ColorMode="RGB24",
                InputSource="Feeder",
            )

        task = asyncio.create_task(later())
        req_ev = await wc.wait_for_scan_request(state, ev, 2)
        await task
        assert req_ev.type == WalkupEventType.SCAN_REQUESTED
        req = await wc.requested_settings(req_ev)
        assert req.shortcut == "Photo"
        assert (req.x_resolution, req.y_resolution, req.duplex, req.color_mode, req.input_source) == (
            600,
            600,
            True,
            "RGB24",
            "Feeder",
        )
        assert req.context_id == dest.id

        await fake.complete_session("Scanbox")
        assert await wc.wait_for_next_page(state, req_ev, 1) is None

        await wc.unregister(dest)
        assert fake.subs == {}
        await wc.unregister(dest)  # already gone -> no error


async def test_escl_register_is_idempotent_with_listing():
    fake, client, wc = make("escl")
    async with client:
        a = await wc.register("Scanbox", "nas", shortcuts=["Document"])
        b = await wc.register("Scanbox", "nas", shortcuts=["Document"])
        assert a.uri == b.uri and len(fake.subs) == 1
        # Shortcut change -> delete + recreate (shortcuts are immutable on the device).
        c = await wc.register("Scanbox", "nas", shortcuts=["Document", "Photo"])
        assert c.uri != a.uri and len(fake.subs) == 1
        assert [d.name for d in await wc.list_destinations()] == ["Scanbox"]


async def test_escl_register_without_listing_uses_known_uri():
    fake, client, wc = make("escl_nolist")
    async with client:
        await wc.probe()
        with pytest.raises(WalkupUnsupported):
            await wc.list_destinations()
        a = await wc.register("Scanbox", "nas", shortcuts=["Document"])
        b = await wc.register("Scanbox", "nas", shortcuts=["Document"], known_uri=a.uri)
        assert len(fake.subs) == 1
        assert ("DELETE", a.uri) in fake.requests
        assert b.uri != a.uri


async def test_escl_too_many_shortcuts():
    _, client, wc = make("escl")
    async with client:
        await wc.probe()
        with pytest.raises(ValueError):
            await wc.register("Scanbox", "nas", shortcuts=[f"s{i}" for i in range(21)])


async def test_escl_reboot_raises_subscription_lost_and_reregister_works():
    fake, client, wc = make("escl")
    async with client:
        dest = await wc.register("Scanbox", "nas", shortcuts=["Document"])
        state = PollState(dest)
        fake.reboot()
        with pytest.raises(WalkupSubscriptionLost) as ei:
            await wc.poll_event(state, 0.2)
        assert ei.value.status == 403
        new = await wc.register("Scanbox", "nas", shortcuts=["Document"], known_uri=dest.uri)
        state = PollState(new)
        await fake.press_scan("Scanbox", "Document")
        assert (await wc.poll_event(state, 1)).type == WalkupEventType.HOST_SELECTED
        assert (await wc.poll_event(state, 1)).type == WalkupEventType.SCAN_REQUESTED


async def test_escl_busy_and_gone_statuses():
    fake, client, wc = make("escl")
    async with client:
        dest = await wc.register("Scanbox", "nas", shortcuts=["Document"])
        state = PollState(dest)
        fake.fail_next_events = [503, 410, 500]
        with pytest.raises(WalkupDeviceBusy) as ei:
            await wc.poll_event(state, 0.2)
        assert ei.value.retry_after_s == 1.0
        with pytest.raises(WalkupSubscriptionLost):
            await wc.poll_event(state, 0.2)
        with pytest.raises(WalkupProtocolError) as ei2:
            await wc.poll_event(state, 0.2)
        assert not isinstance(ei2.value, WalkupSubscriptionLost) and ei2.value.status == 500


async def test_transport_errors_are_typed():
    from scanbox.walkup.protocol import WalkupUnreachable

    def boom(request):
        raise httpx.ConnectError("refused", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(boom)) as client:
        wc = HpWalkupClient(client, BASE)
        support = await wc.probe()  # probe never raises on transport errors
        assert not support.supported and support.endpoints["/eSCL/WalkupSubscriptions"] is None
        with pytest.raises(WalkupUnreachable):
            await wc.poll_event(
                PollState(RegisteredDestination(WalkupVariant.ESCL, "/eSCL/WalkupSubscriptions/x", "n")), 1
            )


# ---------------------------------------------------------------- LEDM flows


async def test_ledm_scan_to_comp_end_to_end():
    fake, client, wc = make("ledm")
    async with client:
        support = await wc.probe()
        assert support.preferred == WalkupVariant.LEDM_SCAN_TO_COMP
        dest = await wc.register("Scanbox", "nas")
        again = await wc.register("Scanbox", "nas")
        assert dest.uri == again.uri and len(fake.dests) == 1
        state = PollState(dest)
        assert await wc.poll_event(state, 0.3) is None  # primes on historic table, then 304

        await fake.select_host("Scanbox")
        ev = await wc.poll_event(state, 1)
        assert (
            ev.type == WalkupEventType.HOST_SELECTED
            and ev.comp_event_uri == "/WalkupScanToComp/WalkupScanToCompEvent"
        )

        async def later():
            await asyncio.sleep(0.1)
            await fake.start_scan("Scanbox", "SavePDF", Duplex=True)

        task = asyncio.create_task(later())
        req_ev = await wc.wait_for_scan_request(state, ev, 3, interval_s=0.05)
        await task
        assert req_ev.type == WalkupEventType.SCAN_REQUESTED
        req = await wc.requested_settings(req_ev)
        assert (req.shortcut, req.duplex, req.document_format) == ("SavePDF", True, "application/pdf")

        # After the job: panel still on ScanRequested -> keep waiting; then new page.
        async def new_page():
            await asyncio.sleep(0.1)
            await fake.request_new_page("Scanbox")

        task = asyncio.create_task(new_page())
        nxt = await wc.wait_for_next_page(state, req_ev, 3, interval_s=0.05)
        await task
        assert nxt.type == WalkupEventType.SCAN_NEW_PAGE_REQUESTED
        await fake.complete_session("Scanbox")
        assert await wc.wait_for_next_page(state, nxt, 1, interval_s=0.05) is None

        # The same event is not reported twice.
        assert await wc.poll_event(state, 0.2) is None
        await wc.unregister(dest)
        assert fake.dests == {}


async def test_ledm_ignores_events_for_other_destinations():
    fake, client, wc = make("ledm")
    async with client:
        mine = await wc.register("Scanbox", "nas")
        await wc.register("Other", "pc")
        state = PollState(mine)
        assert await wc.poll_event(state, 0.2) is None
        await fake.select_host("Other")
        assert await wc.poll_event(state, 0.5) is None


async def test_ledm_walkupscan_first_gen():
    fake, client, wc = make("ledm_walkupscan")
    async with client:
        dest = await wc.register("Scanbox", "nas")
        state = PollState(dest)
        assert await wc.poll_event(state, 0.2) is None
        await fake.press_scan("Scanbox", "SaveJPEG")
        ev = None
        for _ in range(3):
            ev = await wc.poll_event(state, 0.5)
            if ev is not None:
                break
        assert ev.type == WalkupEventType.SCAN_REQUESTED
        req = await wc.requested_settings(ev)
        assert (req.shortcut, req.document_format, req.duplex) == ("SaveJPEG", "image/jpeg", False)
