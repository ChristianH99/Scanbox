from __future__ import annotations

import io
from email.message import EmailMessage

import pikepdf
import pytest
from pydantic import ValidationError

from scanbox.destinations.base import DeliveryError, RecoverAction
from scanbox.destinations.email import EmailConfig, EmailDestination, send_test_mail, test_smtp
from tests.mocks import make_artifact, make_env, make_job, make_pdf
from tests.mocks.smtp import free_port, make_self_signed_cert, run_smtp_server

dest = EmailDestination()


def smtp_admin(server, **kw):
    base = {
        "smtp.host": server.host if server else "127.0.0.1",
        "smtp.port": server.port if server else free_port(),
        "smtp.security": "none",
        "smtp.from_address": "scanbox@example.org",
        "smtp.from_name": "Scanbox",
        "smtp.timeout_s": 5,
    }
    base.update(kw)
    return base


@pytest.fixture
def pdf(tmp_path):
    return make_pdf(tmp_path / "scan.pdf", pages=2)


@pytest.fixture
def server():
    with run_smtp_server() as s:
        yield s


def pdf_attachments(msg: EmailMessage):
    return [p for p in msg.iter_attachments() if p.get_content_type() == "application/pdf"]


async def test_plain_send(server, pdf, tmp_path):
    env = make_env(tmp_path, smtp_admin(server))
    job = make_job(pdf)
    cfg = EmailConfig(recipients=["bob@example.com"], use_default_recipients=False)
    res = await dest.deliver(job, cfg, env)
    assert len(server.messages) == 1
    m = server.messages[0]
    assert m.rcpt_tos == ["bob@example.com"]
    msg = m.message
    assert msg["Subject"] == "Scan: Invoice ACME"
    assert msg["From"] == "Scanbox <scanbox@example.org>"
    att = pdf_attachments(msg)
    assert len(att) == 1 and att[0].get_filename() == "scan.pdf"
    assert att[0].get_content() == pdf.read_bytes()
    assert "Invoice" not in (msg.get_body(("plain",)).get_content())  # default body uses filename
    assert res.detail["sent_parts"] == [1] and res.detail["parts"] == 1


async def test_recipient_merging_and_templates(server, pdf, tmp_path):
    env = make_env(
        tmp_path,
        smtp_admin(server),
        {
            "email.default_recipients": ["Carol@example.com", "bob@example.com", "not-an-address"],
            "email.subject_template": "U: {title} {qr} {unknown}",
            "email.body_template": "Body {pages} {{x}}",
        },
    )
    cfg = EmailConfig(recipients=["bob@example.com", "dave@example.com"], attach_text=True)
    job = make_job(pdf, qr="INV-1", ocr_text="hello ocr")
    await dest.deliver(job, cfg, env)
    m = server.messages[0]
    assert m.rcpt_tos == ["bob@example.com", "dave@example.com", "Carol@example.com"]
    msg = m.message
    assert msg["Subject"] == "U: Invoice ACME INV-1 {unknown}"
    assert msg.get_body(("plain",)).get_content().strip() == "Body 1 {x}"
    txt = [p for p in msg.iter_attachments() if p.get_content_type() == "text/plain"]
    assert txt[0].get_filename() == "scan.txt" and txt[0].get_content().strip() == "hello ocr"

    # profile template beats user template
    cfg2 = EmailConfig(
        recipients=["bob@example.com"], subject_template="P {title}", use_default_recipients=False
    )
    await dest.deliver(job, cfg2, env)
    assert server.messages[1].message["Subject"] == "P Invoice ACME"


async def test_falls_back_to_user_email(server, pdf, tmp_path):
    env = make_env(tmp_path, smtp_admin(server))
    await dest.deliver(make_job(pdf), EmailConfig(), env)
    assert server.messages[0].rcpt_tos == ["alice@example.com"]
    with pytest.raises(DeliveryError) as ei:
        await dest.deliver(make_job(pdf, user_email=None), EmailConfig(), env)
    assert ei.value.code == "no_recipients" and not ei.value.retryable


def test_config_validates_addresses():
    with pytest.raises(ValidationError):
        EmailConfig(recipients=["bob@example.com", "evil@x.com\r\nBcc: y@z.com"])
    with pytest.raises(ValidationError):
        EmailConfig(recipients=["nope"])


async def test_message_id_deterministic(server, pdf, tmp_path):
    env = make_env(tmp_path, smtp_admin(server))
    cfg = EmailConfig(recipients=["bob@example.com"])
    await dest.deliver(make_job(pdf), cfg, env)
    await dest.deliver(make_job(pdf), cfg, env)
    await dest.deliver(make_job(pdf, idempotency_key="deliver:7:2"), cfg, env)
    ids = [m.message["Message-ID"] for m in server.messages]
    assert ids[0] == ids[1] != ids[2]
    assert ids[0].endswith("@example.org>")


async def test_auth_and_starttls(pdf, tmp_path):
    cert = make_self_signed_cert(tmp_path)
    with run_smtp_server(users={"u": "pw"}, tls_cert=cert, require_starttls=True) as s:
        admin = smtp_admin(
            s,
            **{
                "smtp.security": "starttls",
                "smtp.username": "u",
                "smtp.password": "pw",
                "smtp.verify_tls": False,
            },
        )
        env = make_env(tmp_path, admin)
        ok, msg = await test_smtp(env)
        assert ok, msg
        await dest.deliver(make_job(pdf), EmailConfig(recipients=["bob@example.com"]), env)
        assert len(s.messages) == 1

        # wrong password -> not retryable
        env_bad = make_env(tmp_path, {**admin, "smtp.password": "wrong"})
        with pytest.raises(DeliveryError) as ei:
            await dest.deliver(make_job(pdf), EmailConfig(recipients=["bob@example.com"]), env_bad)
        assert ei.value.code == "smtp_auth_failed" and not ei.value.retryable
        ok, msg = await test_smtp(env_bad)
        assert not ok and "authentication" in msg

        # self-signed cert with verification on -> clear, non-retryable TLS error
        env_verify = make_env(tmp_path, {**admin, "smtp.verify_tls": True})
        with pytest.raises(DeliveryError) as ei:
            await dest.deliver(make_job(pdf), EmailConfig(recipients=["bob@example.com"]), env_verify)
        assert ei.value.code == "smtp_tls_error" and not ei.value.retryable


async def test_plain_auth_without_tls(pdf, tmp_path):
    with run_smtp_server(users={"u": "pw"}) as s:
        env = make_env(tmp_path, smtp_admin(s, **{"smtp.username": "u", "smtp.password": "pw"}))
        await send_test_mail(env, "bob@example.com")
        assert s.messages[0].message["Subject"] == "Scanbox test e-mail"


async def test_server_down_is_retryable(pdf, tmp_path):
    env = make_env(tmp_path, smtp_admin(None))
    with pytest.raises(DeliveryError) as ei:
        await dest.deliver(make_job(pdf), EmailConfig(recipients=["bob@example.com"]), env)
    assert ei.value.retryable and ei.value.code == "smtp_unavailable"
    ok, _ = await test_smtp(env)
    assert not ok


async def test_missing_config_not_retryable(pdf, tmp_path):
    env = make_env(tmp_path, {})
    with pytest.raises(DeliveryError) as ei:
        await dest.deliver(make_job(pdf), EmailConfig(recipients=["bob@example.com"]), env)
    assert ei.value.code == "smtp_not_configured" and not ei.value.retryable
    assert "smtp.host" in ei.value.message
    assert await dest.validate(EmailConfig(recipients=["bob@example.com"]), env)


@pytest.mark.parametrize(("reply", "retryable"), [("451 try again later", True), ("554 rejected", False)])
async def test_smtp_reply_classification(server, pdf, tmp_path, reply, retryable):
    server.handler.data_reply = reply
    env = make_env(tmp_path, smtp_admin(server))
    with pytest.raises(DeliveryError) as ei:
        await dest.deliver(make_job(pdf), EmailConfig(recipients=["bob@example.com"]), env)
    assert ei.value.retryable is retryable


async def test_recipient_refused_permanent(server, pdf, tmp_path):
    server.handler.rcpt_reply = "550 no such user"
    env = make_env(tmp_path, smtp_admin(server))
    with pytest.raises(DeliveryError) as ei:
        await dest.deliver(make_job(pdf), EmailConfig(recipients=["bob@example.com"]), env)
    assert ei.value.code == "recipients_refused" and not ei.value.retryable


@pytest.fixture
def big_pdf(tmp_path):
    # ~3 MB, 6 pages of incompressible image data
    return make_pdf(tmp_path / "big.pdf", pages=6, noise_bytes_per_page=500_000)


def _encoded_limit_mb(mb: float) -> dict:
    return {"smtp.max_attachment_mb": mb}


async def test_split_into_parts(server, big_pdf, tmp_path):
    progress = []
    env = make_env(
        tmp_path, smtp_admin(server, **_encoded_limit_mb(1.2)), extras={"progress": progress.append}
    )
    cfg = EmailConfig(recipients=["bob@example.com"], oversize="split")
    res = await dest.deliver(make_job(big_pdf, page_count=6), cfg, env)
    n = len(server.messages)
    assert n >= 3
    assert res.detail["parts"] == n and res.detail["sent_parts"] == list(range(1, n + 1))
    pages = []
    ids = set()
    for i, m in enumerate(server.messages, start=1):
        assert len(m.data) <= 1_200_000
        msg = m.message
        assert msg["Subject"].endswith(f"[{i}/{n}]")
        ids.add(msg["Message-ID"])
        (att,) = pdf_attachments(msg)
        assert att.get_filename() == f"big_part{i}of{n}.pdf"
        with pikepdf.open(io.BytesIO(att.get_content())) as p:
            pages.append(len(p.pages))
    assert sum(pages) == 6 and len(ids) == n
    assert progress[-1]["in_flight"] is None and progress[0]["in_flight"] == 1


async def test_base64_overhead_is_counted(server, tmp_path):
    # Raw PDF fits the limit, but its base64 encoding does not.
    pdf = make_pdf(tmp_path / "p.pdf", pages=2, noise_bytes_per_page=420_000)
    raw = pdf.stat().st_size
    limit_mb = (raw + 70_000) / 1_000_000
    assert raw * 4 / 3 > limit_mb * 1_000_000
    env = make_env(tmp_path, smtp_admin(server, **_encoded_limit_mb(limit_mb)))
    with pytest.raises(DeliveryError) as ei:
        await dest.deliver(make_job(pdf), EmailConfig(recipients=["bob@example.com"], oversize="fail"), env)
    assert ei.value.code == "attachment_too_large" and not ei.value.retryable


async def test_compress_then_split_uses_recompress(server, big_pdf, tmp_path):
    small = make_artifact(make_pdf(tmp_path / "small.pdf", pages=6), filename="big.pdf")
    calls = []

    async def recompress(preset):
        calls.append(preset)
        return small

    env = make_env(tmp_path, smtp_admin(server, **_encoded_limit_mb(1.2)))
    res = await dest.deliver(
        make_job(big_pdf, recompress=recompress), EmailConfig(recipients=["b@example.com"]), env
    )
    assert calls == ["ebook"] and len(server.messages) == 1 and res.detail["compressed"]


async def test_too_large_page_fails(server, tmp_path):
    pdf = make_pdf(tmp_path / "p.pdf", pages=2, noise_bytes_per_page=600_000)
    env = make_env(tmp_path, smtp_admin(server, **_encoded_limit_mb(0.5)))
    with pytest.raises(DeliveryError) as ei:
        await dest.deliver(make_job(pdf), EmailConfig(recipients=["b@example.com"]), env)
    assert ei.value.code == "attachment_too_large" and not ei.value.retryable
    assert server.messages == []


async def test_resume_skips_sent_parts(server, big_pdf, tmp_path):
    progress = []
    env = make_env(
        tmp_path, smtp_admin(server, **_encoded_limit_mb(1.2)), extras={"progress": progress.append}
    )
    cfg = EmailConfig(recipients=["bob@example.com"], oversize="split")
    job = make_job(big_pdf)
    first = await dest.deliver(job, cfg, env)
    n = first.detail["parts"]
    server.handler.messages.clear()
    # Simulate a crash after part 1 was confirmed.
    previous = {**first.detail, "sent_parts": [1], "in_flight": None}
    assert await dest.recover(job, cfg, env, previous) == RecoverAction.REDELIVER
    env2 = make_env(
        tmp_path, smtp_admin(server, **_encoded_limit_mb(1.2)), extras={"previous_result": previous}
    )
    res = await dest.deliver(job, cfg, env2)
    assert len(server.messages) == n - 1
    assert server.messages[0].message["Subject"].endswith(f"[2/{n}]")
    assert res.detail["sent_parts"] == list(range(1, n + 1))


async def test_recover(pdf, tmp_path):
    env = make_env(tmp_path, smtp_admin(None))
    job = make_job(pdf)
    cfg = EmailConfig(recipients=["b@example.com"])
    assert await dest.recover(job, cfg, env, None) == RecoverAction.NEEDS_ATTENTION
    assert (
        await dest.recover(job, cfg, env, {"parts": 2, "sent_parts": [1], "in_flight": 2})
        == RecoverAction.NEEDS_ATTENTION
    )
    assert (
        await dest.recover(job, cfg, env, {"parts": 2, "sent_parts": [1, 2], "in_flight": None})
        == RecoverAction.DONE
    )
    assert (
        await dest.recover(job, cfg, env, {"parts": 1, "sent_parts": [], "in_flight": None})
        == RecoverAction.REDELIVER
    )
