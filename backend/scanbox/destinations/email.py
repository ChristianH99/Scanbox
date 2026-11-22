"""E-mail destination (SMTP via aiosmtplib).

See docs/DESTINATIONS.md for settings, size handling and resumable multi-part sending.

Progress / resume contract (optional, used when the delivery runner provides it):

* ``env.extras["progress"]``: callable (sync or async) receiving a dict that the runner
  persists as the delivery's partial result. It is called before and after each mail.
* ``env.extras["previous_result"]``: the last persisted dict for this delivery. ``deliver``
  skips parts listed in ``sent_parts`` when the split plan (``plan``) is unchanged.

``recover`` receives the same dict as ``previous``.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import math
import re
import ssl
from collections.abc import Iterable
from dataclasses import dataclass
from email.headerregistry import Address
from email.message import EmailMessage
from email.policy import SMTP as SMTP_POLICY
from email.utils import formatdate, parseaddr
from typing import Any, ClassVar, Literal

import aiosmtplib
from pydantic import BaseModel, Field, field_validator

from scanbox.destinations import pdfsplit
from scanbox.destinations.base import (
    Artifact,
    DeliveryEnv,
    DeliveryError,
    DeliveryJob,
    DeliveryResult,
    RecoverAction,
)
from scanbox.destinations.templating import render_template

DEFAULT_SUBJECT = "Scan: {title}"
DEFAULT_BODY = "Attached: {filename} ({pages} pages), scanned {date} {time} by {user}.\n\n-- Scanbox\n"

# Conservative: "25 MB" limits (Gmail etc.) are applied to the whole encoded message, and
# we count decimal megabytes so a 1,048,576-based server limit is also satisfied.
_MB = 1_000_000
# Room left for headers that the server adds (Received:, DKIM, ...).
_SAFETY_BYTES = 64 * 1024

_ADDR_RE = re.compile(
    r"^[^@\s<>()\[\],;:\"]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+$"
)


def is_valid_address(addr: str) -> bool:
    name, parsed = parseaddr(addr)
    return bool(parsed) and parsed == addr.strip() and bool(_ADDR_RE.match(parsed)) and len(parsed) <= 254


class EmailConfig(BaseModel):
    recipients: list[str] = Field(default_factory=list)
    use_default_recipients: bool = True
    subject_template: str | None = None
    body_template: str | None = None
    attach_text: bool = False
    oversize: Literal["compress_then_split", "compress", "split", "fail"] = "compress_then_split"

    @field_validator("recipients")
    @classmethod
    def _check_recipients(cls, v: list[str]) -> list[str]:
        out = []
        for addr in v:
            addr = addr.strip()
            if not is_valid_address(addr):
                raise ValueError(f"invalid e-mail address: {addr!r}")
            out.append(addr)
        return out


@dataclass(frozen=True)
class SmtpSettings:
    host: str
    port: int
    security: str
    username: str | None
    password: str | None
    from_address: str
    from_name: str | None
    max_bytes: int
    timeout: float
    verify_tls: bool


def load_smtp_settings(env: DeliveryEnv) -> SmtpSettings:
    s = env.settings
    host = (s.admin("smtp.host") or "").strip()
    from_address = (s.admin("smtp.from_address") or "").strip()
    missing = [k for k, v in (("smtp.host", host), ("smtp.from_address", from_address)) if not v]
    if missing:
        raise DeliveryError(
            "smtp_not_configured",
            f"E-mail is not configured: admin setting(s) {', '.join(missing)} missing.",
            retryable=False,
        )
    security = (s.admin("smtp.security") or "starttls").lower()
    if security not in ("starttls", "tls", "none"):
        raise DeliveryError("smtp_not_configured", f"Invalid smtp.security {security!r}.", retryable=False)
    try:
        port = int(s.admin("smtp.port") or (465 if security == "tls" else 587))
        max_mb = float(s.admin("smtp.max_attachment_mb") or 25)
        timeout = float(s.admin("smtp.timeout_s") or 30)
    except (TypeError, ValueError) as e:
        raise DeliveryError("smtp_not_configured", f"Invalid SMTP setting: {e}", retryable=False) from e
    verify = s.admin("smtp.verify_tls", True)
    return SmtpSettings(
        host=host,
        port=port,
        security=security,
        username=(s.admin("smtp.username") or "").strip() or None,
        password=s.admin("smtp.password") or None,
        from_address=from_address,
        from_name=(s.admin("smtp.from_name") or "").strip() or None,
        max_bytes=int(max_mb * _MB),
        timeout=timeout,
        verify_tls=verify not in (False, "false", "0", 0),
    )


def _smtp_client(cfg: SmtpSettings) -> aiosmtplib.SMTP:
    kw: dict[str, Any] = {
        "hostname": cfg.host,
        "port": cfg.port,
        "timeout": cfg.timeout,
        "validate_certs": cfg.verify_tls,
    }
    if cfg.security == "tls":
        kw.update(use_tls=True, start_tls=False)
    elif cfg.security == "starttls":
        kw.update(use_tls=False, start_tls=True)
    else:
        kw.update(use_tls=False, start_tls=False)
    return aiosmtplib.SMTP(**kw)


def classify_smtp_error(exc: BaseException) -> DeliveryError:
    """Map aiosmtplib / socket errors to DeliveryError with the right retry semantics."""
    if isinstance(exc, DeliveryError):
        return exc
    cause = exc.__cause__ or exc.__context__
    for e in (exc, cause):
        if isinstance(e, ssl.SSLCertVerificationError):
            return DeliveryError(
                "smtp_tls_error", f"TLS certificate verification failed: {e}", retryable=False
            )
    if isinstance(exc, aiosmtplib.SMTPAuthenticationError):
        retry = 400 <= exc.code < 500
        return DeliveryError(
            "smtp_auth_failed", f"SMTP authentication failed ({exc.code} {exc.message})", retryable=retry
        )
    if isinstance(exc, aiosmtplib.SMTPRecipientsRefused):
        codes = [r.code for r in exc.recipients]
        detail = "; ".join(f"{r.recipient}: {r.code} {r.message}" for r in exc.recipients)
        retry = bool(codes) and all(400 <= c < 500 for c in codes)
        return DeliveryError("recipients_refused", f"All recipients refused: {detail}", retryable=retry)
    if isinstance(exc, aiosmtplib.SMTPNotSupported):
        return DeliveryError("smtp_not_supported", f"SMTP server: {exc}", retryable=False)
    if isinstance(
        exc, (aiosmtplib.SMTPConnectError, aiosmtplib.SMTPTimeoutError, aiosmtplib.SMTPServerDisconnected)
    ):
        return DeliveryError("smtp_unavailable", f"SMTP server unreachable: {exc}", retryable=True)
    if isinstance(exc, aiosmtplib.SMTPResponseException):
        if exc.code in (552, 523):
            return DeliveryError(
                "attachment_too_large", f"SMTP server rejected the message size ({exc.code} {exc.message})"
            )
        retry = 400 <= exc.code < 500
        return DeliveryError("smtp_error", f"SMTP error {exc.code}: {exc.message}", retryable=retry)
    if isinstance(exc, ssl.SSLError):
        return DeliveryError("smtp_tls_error", f"TLS error: {exc}", retryable=False)
    if isinstance(exc, (OSError, TimeoutError, asyncio.TimeoutError, aiosmtplib.SMTPException)):
        return DeliveryError("smtp_unavailable", f"SMTP connection failed: {exc}", retryable=True)
    return DeliveryError("smtp_error", f"Unexpected SMTP error: {exc!r}", retryable=True)


async def _connect(cfg: SmtpSettings) -> aiosmtplib.SMTP:
    client = _smtp_client(cfg)
    try:
        await client.connect()
        if cfg.username:
            await client.login(cfg.username, cfg.password or "")
    except BaseException:
        client.close()
        raise
    return client


async def _quit(client: aiosmtplib.SMTP) -> None:
    try:
        await client.quit()
    except Exception:  # noqa: BLE001,S110 - best effort
        client.close()


def _message_id(idempotency_key: str, part: int, total: int, from_address: str) -> str:
    digest = hashlib.sha256(idempotency_key.encode()).hexdigest()[:32]
    domain = from_address.rpartition("@")[2] or "scanbox.local"
    return f"<scanbox.{digest}.{part}of{total}@{domain}>"


def _dedupe(addrs: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out = []
    for a in addrs:
        a = a.strip()
        if a and a.lower() not in seen:
            seen.add(a.lower())
            out.append(a)
    return out


def resolve_recipients(job: DeliveryJob, cfg: EmailConfig, env: DeliveryEnv) -> list[str]:
    addrs = list(cfg.recipients)
    if cfg.use_default_recipients:
        defaults = env.settings.user("email.default_recipients") or []
        if isinstance(defaults, str):
            defaults = [x for x in re.split(r"[,;\s]+", defaults) if x]
        addrs += [a for a in defaults if is_valid_address(a)]
    if not addrs and job.user_email and is_valid_address(job.user_email):
        addrs = [job.user_email]
    addrs = _dedupe(addrs)
    if not addrs:
        raise DeliveryError("no_recipients", "No e-mail recipients configured.", retryable=False)
    return addrs


@dataclass
class _Part:
    data: bytes
    filename: str


class EmailDestination:
    type: ClassVar[str] = "email"
    Config: ClassVar[type[BaseModel]] = EmailConfig

    async def validate(self, cfg: BaseModel, env: DeliveryEnv) -> list[str]:
        assert isinstance(cfg, EmailConfig)
        problems = []
        try:
            load_smtp_settings(env)
        except DeliveryError as e:
            problems.append(e.message)
        has_defaults = cfg.use_default_recipients and env.settings.user("email.default_recipients")
        if not cfg.recipients and not has_defaults:
            problems.append("No recipients: add recipients or set default recipients in your settings.")
        return problems

    # ------------------------------------------------------------------ building

    def _texts(self, job: DeliveryJob, cfg: EmailConfig, env: DeliveryEnv) -> tuple[str, str]:
        subject_t = cfg.subject_template or env.settings.user("email.subject_template") or DEFAULT_SUBJECT
        body_t = cfg.body_template or env.settings.user("email.body_template") or DEFAULT_BODY
        tv = job.template_vars()
        subject = " ".join(render_template(subject_t, tv).split())  # no header injection via newlines
        return subject, render_template(body_t, tv)

    def _text_attachment(self, job: DeliveryJob, cfg: EmailConfig) -> tuple[str, str] | None:
        if not cfg.attach_text:
            return None
        txt = next((a for a in job.artifacts if a.kind == "txt"), None)
        if txt is not None and txt.path.exists():
            return txt.filename, txt.path.read_text(encoding="utf-8", errors="replace")
        if job.ocr_text:
            stem = job.filename.rsplit(".", 1)[0]
            return f"{stem}.txt", job.ocr_text
        return None

    def _build(
        self,
        *,
        smtp: SmtpSettings,
        job: DeliveryJob,
        recipients: list[str],
        subject: str,
        body: str,
        pdf: _Part | None,
        text: tuple[str, str] | None,
        part: int,
        total: int,
    ) -> EmailMessage:
        msg = EmailMessage(policy=SMTP_POLICY)
        msg["From"] = str(Address(display_name=smtp.from_name or "", addr_spec=smtp.from_address))
        msg["To"] = ", ".join(recipients)
        msg["Subject"] = subject if total == 1 else f"{subject} [{part}/{total}]"
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = _message_id(job.idempotency_key, part, total, smtp.from_address)
        msg["X-Scanbox-Delivery"] = job.idempotency_key
        if total > 1:
            body = f"Part {part} of {total}.\n\n{body}"
        msg.set_content(body)
        if pdf is not None:
            msg.add_attachment(pdf.data, maintype="application", subtype="pdf", filename=pdf.filename)
        if text is not None:
            msg.add_attachment(text[1], subtype="plain", charset="utf-8", filename=text[0])
        return msg

    @staticmethod
    def _size(msg: EmailMessage) -> int:
        return len(msg.as_bytes())

    async def _plan(
        self, job: DeliveryJob, cfg: EmailConfig, env: DeliveryEnv, smtp: SmtpSettings, recipients: list[str]
    ) -> tuple[list[EmailMessage], dict[str, Any]]:
        subject, body = self._texts(job, cfg, env)
        text = self._text_attachment(job, cfg)
        limit = smtp.max_bytes - _SAFETY_BYTES
        info: dict[str, Any] = {"compressed": False}

        def single(art: Artifact) -> list[EmailMessage]:
            pdf = _Part(art.path.read_bytes(), job.filename)
            return [
                self._build(
                    smtp=smtp,
                    job=job,
                    recipients=recipients,
                    subject=subject,
                    body=body,
                    pdf=pdf,
                    text=text,
                    part=1,
                    total=1,
                )
            ]

        art = job.primary
        msgs = single(art)
        if self._size(msgs[0]) <= limit:
            return msgs, {**info, "sha256": art.sha256}

        mode = cfg.oversize
        if mode in ("compress_then_split", "compress") and job.recompress is not None:
            art = await job.recompress("ebook")
            info["compressed"] = True
            msgs = single(art)
            if self._size(msgs[0]) <= limit:
                return msgs, {**info, "sha256": art.sha256}
        if mode in ("compress", "fail"):
            raise DeliveryError(
                "attachment_too_large",
                f"Message is {self._size(msgs[0]) / _MB:.1f} MB encoded; limit is {smtp.max_bytes / _MB:.1f} MB.",
            )

        # Split: work out the raw PDF budget per part from the encoded overhead.
        shell = self._build(
            smtp=smtp,
            job=job,
            recipients=recipients,
            subject=subject + " [99/99]",
            body="Part 99 of 99.\n\n" + body,
            pdf=_Part(b"", job.filename),
            text=text,
            part=1,
            total=99,
        )
        overhead = self._size(shell)
        encoded_budget = limit - overhead
        raw_budget = math.floor(encoded_budget * 3 / 4 * 76 / 78) - 1024
        if raw_budget <= 1024:
            raise DeliveryError("attachment_too_large", "max_attachment_mb is too small to send anything.")
        try:
            parts = await asyncio.to_thread(pdfsplit.plan_split, art.path, raw_budget)
        except pdfsplit.PageTooLargeError as e:
            raise DeliveryError(
                "attachment_too_large", f"Cannot split below the size limit: {e}", retryable=False
            ) from e
        total = len(parts)
        stem = job.filename.rsplit(".", 1)[0]
        msgs = []
        for i, (_s, _e, data) in enumerate(parts, start=1):
            name = f"{stem}_part{i}of{total}.pdf" if total > 1 else job.filename
            m = self._build(
                smtp=smtp,
                job=job,
                recipients=recipients,
                subject=subject,
                body=body,
                pdf=_Part(data, name),
                text=text if i == 1 else None,
                part=i,
                total=total,
            )
            if self._size(m) > smtp.max_bytes:
                raise DeliveryError("attachment_too_large", f"Part {i}/{total} still exceeds the limit.")
            msgs.append(m)
        pages = [[s + 1, e] for s, e, _ in parts]
        return msgs, {**info, "sha256": art.sha256, "pages": pages}

    # ------------------------------------------------------------------ sending

    @staticmethod
    async def _report(env: DeliveryEnv, state: dict[str, Any]) -> None:
        cb = env.extras.get("progress")
        if cb is None:
            return
        res = cb(dict(state))
        if inspect.isawaitable(res):
            await res

    async def deliver(self, job: DeliveryJob, cfg: BaseModel, env: DeliveryEnv) -> DeliveryResult:
        assert isinstance(cfg, EmailConfig)
        smtp = load_smtp_settings(env)
        recipients = resolve_recipients(job, cfg, env)
        msgs, info = await self._plan(job, cfg, env, smtp, recipients)
        total = len(msgs)
        plan = f"{info['sha256']}:{total}"

        previous = env.extras.get("previous_result") or {}
        sent: list[int] = []
        if previous.get("plan") == plan:
            sent = sorted({int(x) for x in previous.get("sent_parts", [])})
        state: dict[str, Any] = {
            "plan": plan,
            "parts": total,
            "sent_parts": sent,
            "in_flight": None,
            "message_ids": [m["Message-ID"] for m in msgs],
            "recipients": recipients,
        }
        refused: dict[str, str] = {}
        todo = [i for i in range(1, total + 1) if i not in sent]
        if todo:
            try:
                client = await _connect(smtp)
            except Exception as e:  # noqa: BLE001
                raise classify_smtp_error(e) from e
            try:
                for i in todo:
                    state["in_flight"] = i
                    await self._report(env, state)
                    try:
                        errors, _resp = await client.send_message(
                            msgs[i - 1], sender=smtp.from_address, recipients=recipients
                        )
                    except Exception as e:  # noqa: BLE001
                        err = classify_smtp_error(e)
                        # A definite SMTP rejection means the part was not accepted.
                        if isinstance(e, aiosmtplib.SMTPResponseException | aiosmtplib.SMTPRecipientsRefused):
                            state["in_flight"] = None
                        await self._report(env, state)
                        raise err from e
                    for rcpt, (code, text) in (errors or {}).items():
                        refused[rcpt] = f"{code} {text}"
                    state["sent_parts"] = [*state["sent_parts"], i]
                    state["in_flight"] = None
                    await self._report(env, state)
            finally:
                await _quit(client)

        detail = {
            **state,
            "compressed": info["compressed"],
            "page_ranges": info.get("pages"),
            "refused": refused or None,
        }
        what = "1 e-mail" if total == 1 else f"{total} e-mails (split)"
        msg = f"Sent {what} to {', '.join(recipients)}"
        if refused:
            msg += f" (refused: {', '.join(refused)})"
        return DeliveryResult(message=msg, detail=detail)

    async def recover(
        self, job: DeliveryJob, cfg: BaseModel, env: DeliveryEnv, previous: dict[str, Any] | None
    ) -> RecoverAction:
        """E-mail cannot be un-sent, so only resume when progress proves nothing was in flight."""
        if not previous or "sent_parts" not in previous or "parts" not in previous:
            return RecoverAction.NEEDS_ATTENTION
        if previous.get("in_flight") is not None:
            return RecoverAction.NEEDS_ATTENTION
        if len(set(previous["sent_parts"])) >= int(previous["parts"]):
            return RecoverAction.DONE
        return RecoverAction.REDELIVER


async def test_smtp(env: DeliveryEnv) -> tuple[bool, str]:
    """Connect (+ TLS + login) without sending mail. For the admin "test connection" button."""
    try:
        smtp = load_smtp_settings(env)
        client = await _connect(smtp)
    except DeliveryError as e:
        return False, e.message
    except Exception as e:  # noqa: BLE001
        return False, classify_smtp_error(e).message
    await _quit(client)
    auth = f", authenticated as {smtp.username}" if smtp.username else ""
    return True, f"Connected to {smtp.host}:{smtp.port} ({smtp.security}{auth})"


test_smtp.__test__ = False  # type: ignore[attr-defined]  # not a pytest test


async def send_test_mail(env: DeliveryEnv, to: str) -> None:
    """Send a short test e-mail; raises DeliveryError on failure."""
    if not is_valid_address(to):
        raise DeliveryError("invalid_recipient", f"Invalid e-mail address: {to!r}")
    smtp = load_smtp_settings(env)
    msg = EmailMessage(policy=SMTP_POLICY)
    msg["From"] = str(Address(display_name=smtp.from_name or "", addr_spec=smtp.from_address))
    msg["To"] = to
    msg["Subject"] = "Scanbox test e-mail"
    msg["Date"] = formatdate(localtime=True)
    msg.set_content("This is a test e-mail from Scanbox. Your SMTP settings work.\n")
    try:
        client = await _connect(smtp)
    except Exception as e:  # noqa: BLE001
        raise classify_smtp_error(e) from e
    try:
        await client.send_message(msg, sender=smtp.from_address, recipients=[to])
    except Exception as e:  # noqa: BLE001
        raise classify_smtp_error(e) from e
    finally:
        await _quit(client)
