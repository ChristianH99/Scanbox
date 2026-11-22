from __future__ import annotations

import hashlib
import os

import pytest
from pydantic import ValidationError

from scanbox.destinations.base import DeliveryError, RecoverAction
from scanbox.destinations.paperless import PaperlessConfig, PaperlessDestination, test_paperless
from tests.mocks import make_artifact, make_env, make_job, make_pdf
from tests.mocks.paperless import create_paperless_mock, paperless_client_factory

TOKEN = "secret-token"  # noqa: S105


@pytest.fixture
def dest():
    d = PaperlessDestination()
    d.task_poll_interval_s = 0.01
    d.task_poll_timeout_s = 2
    return d


@pytest.fixture
def app():
    return create_paperless_mock(token=TOKEN)


@pytest.fixture
def mock(app):
    return app.state.mock


@pytest.fixture
def pdf(tmp_path):
    return make_pdf(tmp_path / "Invoice 2026.pdf", pages=2)


def api_env(tmp_path, app, token=TOKEN, **admin):
    return make_env(
        tmp_path,
        {"paperless.url": "http://paperless.test", **admin},
        {"paperless.token": token} if token else {},
        http_client_factory=paperless_client_factory(app),
    )


def by_name(coll, name):
    return next(i for i, x in coll.items() if x["name"] == name)


async def test_api_upload_with_all_metadata(dest, app, mock, pdf, tmp_path):
    env = api_env(tmp_path, app)
    cfg = PaperlessConfig(
        title_template="{title} ({qr})",
        tags=["Inbox", "scan"],  # case-insensitive match
        correspondent="ACME Corp",
        document_type="Invoice",
        qr_mapping="asn",
    )
    res = await dest.deliver(make_job(pdf, qr="00123"), cfg, env)
    up = mock.uploads[0]
    assert up["filename"] == "Invoice 2026.pdf"
    assert up["title"] == "Invoice ACME (00123)"
    assert up["created"].startswith("2026-03-14T09:26:53")
    assert up["tags"] == [by_name(mock.tags, "Inbox"), by_name(mock.tags, "Scan")]
    assert up["correspondent"] == by_name(mock.correspondents, "ACME Corp")
    assert up["document_type"] == by_name(mock.document_types, "Invoice")
    assert up["archive_serial_number"] == 123
    doc_id = res.detail["document_id"]
    assert doc_id in mock.documents and res.detail["task_status"] == "SUCCESS"
    assert res.detail["task_id"] and res.detail["asn"] == 123
    assert res.detail["paperless_version"] == mock.version
    assert f"#{doc_id}" in res.message


async def test_non_numeric_qr_falls_back_to_custom_field(dest, app, mock, pdf, tmp_path):
    cfg = PaperlessConfig(qr_mapping="asn", asn_fallback="custom_field", qr_custom_field="scan id")
    res = await dest.deliver(make_job(pdf, qr="INV-42"), cfg, api_env(tmp_path, app))
    up = mock.uploads[0]
    assert "archive_serial_number" not in up
    assert up["custom_fields"] == {by_name(mock.custom_fields, "Scan ID"): "INV-42"}
    doc = mock.documents[res.detail["document_id"]]
    assert doc["custom_fields"] == [{"field": by_name(mock.custom_fields, "Scan ID"), "value": "INV-42"}]


async def test_non_numeric_qr_falls_back_to_tag(dest, app, mock, pdf, tmp_path):
    cfg = PaperlessConfig(qr_mapping="asn", asn_fallback="tag", tags=["Inbox"])
    await dest.deliver(make_job(pdf, qr="INV-42"), cfg, api_env(tmp_path, app))
    new_tag = by_name(mock.tags, "INV-42")
    assert mock.uploads[0]["tags"] == [by_name(mock.tags, "Inbox"), new_tag]


async def test_non_numeric_qr_no_fallback(dest, app, mock, pdf, tmp_path):
    res = await dest.deliver(make_job(pdf, qr="ABC"), PaperlessConfig(), api_env(tmp_path, app))
    assert "archive_serial_number" not in mock.uploads[0] and res.detail["asn_fallback"] == "none"


async def test_legacy_custom_fields_patched_after_creation(dest, mock, pdf, tmp_path):
    app = create_paperless_mock(token=TOKEN, legacy_custom_fields=True)
    m = app.state.mock
    cfg = PaperlessConfig(qr_mapping="custom_field", qr_custom_field="Scan ID", wait_for_task=False)
    res = await dest.deliver(make_job(pdf, qr="X-1"), cfg, api_env(tmp_path, app))
    doc = m.documents[res.detail["document_id"]]
    assert doc["custom_fields"] == [{"field": by_name(m.custom_fields, "Scan ID"), "value": "X-1"}]
    assert "PATCH" in " ".join(m.requests)


async def test_tag_resolution(dest, app, mock, pdf, tmp_path):
    env = api_env(tmp_path, app)
    with pytest.raises(DeliveryError) as ei:
        await dest.deliver(make_job(pdf), PaperlessConfig(tags=["Nope"]), env)
    assert ei.value.code == "paperless_unknown_tag" and not ei.value.retryable
    assert mock.uploads == []
    await dest.deliver(make_job(pdf), PaperlessConfig(tags=["Nope"], create_missing_tags=True), env)
    assert mock.uploads[0]["tags"] == [by_name(mock.tags, "Nope")]
    with pytest.raises(DeliveryError) as ei:
        await dest.deliver(make_job(pdf), PaperlessConfig(correspondent="Nobody"), env)
    assert ei.value.code == "paperless_unknown_correspondent"


async def test_duplicate_task_is_success(dest, app, mock, pdf, tmp_path):
    env = api_env(tmp_path, app)
    first = await dest.deliver(make_job(pdf), PaperlessConfig(), env)
    res = await dest.deliver(make_job(pdf), PaperlessConfig(), env)
    assert res.detail["duplicate"] is True
    assert res.detail["document_id"] == first.detail["document_id"]
    assert len(mock.documents) == 1


async def test_retry_attempt_checks_checksum_first(dest, pdf, tmp_path):
    # Paperless 3 accepts duplicates by default -> a retried delivery must not upload twice.
    app = create_paperless_mock(token=TOKEN, reject_duplicates=False)
    m = app.state.mock
    env = api_env(tmp_path, app)
    first = await dest.deliver(make_job(pdf), PaperlessConfig(), env)
    res = await dest.deliver(make_job(pdf, attempt=2), PaperlessConfig(), env)
    assert len(m.uploads) == 1 and len(m.documents) == 1
    assert res.detail["document_id"] == first.detail["document_id"]


async def test_consume_failure_not_retryable(dest, app, mock, pdf, tmp_path):
    mock.consume_error = "Error while consuming document: corrupt PDF"
    with pytest.raises(DeliveryError) as ei:
        await dest.deliver(make_job(pdf), PaperlessConfig(), api_env(tmp_path, app))
    assert ei.value.code == "paperless_consume_failed" and "corrupt PDF" in ei.value.message
    assert not ei.value.retryable


async def test_no_wait(dest, app, mock, pdf, tmp_path):
    res = await dest.deliver(make_job(pdf), PaperlessConfig(wait_for_task=False), api_env(tmp_path, app))
    assert res.detail["task_id"] in mock.tasks and "document_id" not in res.detail


async def test_invalid_token_not_retryable(dest, app, pdf, tmp_path):
    env = api_env(tmp_path, app, token="wrong")
    with pytest.raises(DeliveryError) as ei:
        await dest.deliver(make_job(pdf), PaperlessConfig(), env)
    assert ei.value.code == "paperless_auth_failed" and not ei.value.retryable
    ok, msg = await test_paperless(env)
    assert not ok and "token" in msg.lower()


async def test_missing_config(dest, app, pdf, tmp_path):
    with pytest.raises(DeliveryError) as ei:
        await dest.deliver(make_job(pdf), PaperlessConfig(), api_env(tmp_path, app, token=None))
    assert ei.value.code == "paperless_not_configured" and not ei.value.retryable
    env = make_env(tmp_path, {}, {"paperless.token": TOKEN})
    with pytest.raises(DeliveryError) as ei:
        await dest.deliver(make_job(pdf), PaperlessConfig(), env)
    assert "paperless.url" in ei.value.message and not ei.value.retryable
    assert await dest.validate(PaperlessConfig(), env)


@pytest.mark.parametrize("status", [500, 429, 503])
async def test_server_errors_retryable(dest, app, mock, pdf, tmp_path, status):
    mock.fail_status = status
    with pytest.raises(DeliveryError) as ei:
        await dest.deliver(make_job(pdf), PaperlessConfig(), api_env(tmp_path, app))
    assert ei.value.retryable


async def test_offline_retryable(dest, app, mock, pdf, tmp_path):
    mock.offline = True
    with pytest.raises(DeliveryError) as ei:
        await dest.deliver(make_job(pdf), PaperlessConfig(), api_env(tmp_path, app))
    assert ei.value.retryable and ei.value.code == "paperless_unavailable"


async def test_integer_custom_field_rejects_text(dest, app, mock, pdf, tmp_path):
    cfg = PaperlessConfig(qr_mapping="custom_field", qr_custom_field="Number")
    with pytest.raises(DeliveryError) as ei:
        await dest.deliver(make_job(pdf, qr="NaN"), cfg, api_env(tmp_path, app))
    assert ei.value.code == "paperless_bad_custom_field" and not ei.value.retryable
    await dest.deliver(make_job(pdf, qr="17"), cfg, api_env(tmp_path, app))
    assert mock.uploads[0]["custom_fields"] == {by_name(mock.custom_fields, "Number"): 17}


async def test_400_message_is_surfaced(dest, app, mock, pdf, tmp_path, monkeypatch):
    # A negative ASN is rejected by the mock with a 400 field error.
    job = make_job(pdf)
    env = api_env(tmp_path, app)
    orig = dest._metadata

    async def bad_meta(job, cfg, api):
        fields, custom, info = await orig(job, cfg, api)
        return [*fields, ("archive_serial_number", "-5")], custom, info

    monkeypatch.setattr(dest, "_metadata", bad_meta)
    with pytest.raises(DeliveryError) as ei:
        await dest.deliver(job, PaperlessConfig(), env)
    assert ei.value.code == "paperless_rejected" and not ei.value.retryable
    assert "archive_serial_number" in ei.value.message


async def test_api_version_fallback_and_test_connection(dest, pdf, tmp_path):
    app = create_paperless_mock(token=TOKEN, max_api_version=5, version="2.1.0")
    env = api_env(tmp_path, app)
    ok, msg = await test_paperless(env)
    assert ok and "2.1.0" in msg and "API v5" in msg
    res = await dest.deliver(make_job(pdf), PaperlessConfig(), env)
    assert res.detail["document_id"]


async def test_api_v10_paginated_tasks(dest, pdf, tmp_path):
    app = create_paperless_mock(token=TOKEN, max_api_version=10)
    from scanbox.destinations import paperless as mod

    old = mod.API_VERSION
    mod.API_VERSION = 10
    try:
        res = await dest.deliver(make_job(pdf), PaperlessConfig(), api_env(tmp_path, app))
    finally:
        mod.API_VERSION = old
    assert res.detail["task_status"] == "SUCCESS"


async def test_skip_ocr_sends_image_only_pdf(dest, app, mock, pdf, tmp_path):
    img = make_artifact(make_pdf(tmp_path / "img.pdf", pages=2, text="other"), filename="Invoice 2026.pdf")
    await dest.deliver(
        make_job(pdf, image_only_pdf=img), PaperlessConfig(skip_ocr=True), api_env(tmp_path, app)
    )
    assert mock.uploads[0]["size"] == img.size != pdf.stat().st_size


async def test_recover_api(dest, app, mock, pdf, tmp_path):
    env = api_env(tmp_path, app)
    job = make_job(pdf)
    cfg = PaperlessConfig()
    assert await dest.recover(job, cfg, env, None) == RecoverAction.REDELIVER
    res = await dest.deliver(job, cfg, env)
    assert await dest.recover(job, cfg, env, None) == RecoverAction.DONE  # checksum found
    assert await dest.recover(job, cfg, env, {"task_id": res.detail["task_id"]}) == RecoverAction.DONE
    mock.offline = True
    assert await dest.recover(job, cfg, env, None) == RecoverAction.REDELIVER


def test_config_validation():
    with pytest.raises(ValidationError):
        PaperlessConfig(qr_mapping="custom_field")
    with pytest.raises(ValidationError):
        PaperlessConfig(asn_fallback="custom_field")
    with pytest.raises(ValidationError):
        PaperlessConfig(consume_subdir="../etc")
    with pytest.raises(ValidationError):
        PaperlessConfig(consume_subdir="/abs")


# ----------------------------------------------------------------------- consume mode


def consume_env(tmp_path, root):
    return make_env(tmp_path, {"paperless.consume_dir": str(root)})


async def test_consume_atomic_write_and_conflict(dest, pdf, tmp_path):
    root = tmp_path / "consume"
    root.mkdir()
    env = consume_env(tmp_path, root)
    cfg = PaperlessConfig(mode="consume", consume_subdir="alice/ inbox ")
    res = await dest.deliver(make_job(pdf), cfg, env)
    target = root / "alice" / "inbox"
    first = target / "Invoice 2026.pdf"
    assert first.read_bytes() == pdf.read_bytes()
    assert res.detail["sha256"] == hashlib.sha256(pdf.read_bytes()).hexdigest()
    # conflict -> suffix, never overwrite
    other = make_pdf(tmp_path / "other.pdf", pages=1, text="different")
    await dest.deliver(make_job(other, filename="Invoice 2026.pdf"), cfg, env)
    assert first.read_bytes() == pdf.read_bytes()
    assert (target / "Invoice 2026 (2).pdf").read_bytes() == other.read_bytes()
    # no temp files left behind
    assert sorted(p.name for p in target.iterdir()) == ["Invoice 2026 (2).pdf", "Invoice 2026.pdf"]


async def test_consume_traversal_rejected(dest, pdf, tmp_path):
    root = tmp_path / "consume"
    root.mkdir()
    env = consume_env(tmp_path, root)
    cfg = PaperlessConfig(mode="consume")
    for bad in ("../../x", "a/../../x", "/etc", "C:/Windows", "..\\x"):
        with pytest.raises(DeliveryError) as ei:
            await dest.deliver(make_job(pdf), cfg.model_copy(update={"consume_subdir": bad}), env)
        assert ei.value.code == "paperless_bad_subdir" and not ei.value.retryable
    # malicious filename is sanitised into the target dir
    await dest.deliver(make_job(pdf, filename="../../evil.pdf"), cfg, env)
    assert [p.name for p in root.iterdir()] == ["_.._evil.pdf"]
    assert not (tmp_path / "evil.pdf").exists()


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
async def test_consume_symlink_escape_rejected(dest, pdf, tmp_path):
    root = tmp_path / "consume"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(DeliveryError):
        await dest.deliver(
            make_job(pdf), PaperlessConfig(mode="consume", consume_subdir="link"), consume_env(tmp_path, root)
        )
    assert list(outside.iterdir()) == []


async def test_consume_missing_dir(dest, pdf, tmp_path):
    with pytest.raises(DeliveryError) as ei:
        await dest.deliver(make_job(pdf), PaperlessConfig(mode="consume"), make_env(tmp_path, {}))
    assert not ei.value.retryable
    with pytest.raises(DeliveryError) as ei:
        await dest.deliver(
            make_job(pdf), PaperlessConfig(mode="consume"), consume_env(tmp_path, tmp_path / "nope")
        )
    assert ei.value.retryable


async def test_consume_recover(dest, pdf, tmp_path):
    root = tmp_path / "consume"
    root.mkdir()
    env = consume_env(tmp_path, root)
    cfg = PaperlessConfig(mode="consume", consume_subdir="scans")
    job = make_job(pdf)
    assert await dest.recover(job, cfg, env, None) == RecoverAction.REDELIVER
    await dest.deliver(job, cfg, env)
    assert await dest.recover(job, cfg, env, None) == RecoverAction.DONE
    # a hidden temp file with the same content does not count
    for p in (root / "scans").iterdir():
        p.rename(p.with_name(".partial"))
    assert await dest.recover(job, cfg, env, None) == RecoverAction.REDELIVER
