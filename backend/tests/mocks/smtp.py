"""Capturing SMTP server for tests (aiosmtpd in a background thread)."""

from __future__ import annotations

import datetime as dt
import email
import email.policy
import ipaddress
import socket
import ssl
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path

from aiosmtpd.controller import Controller
from aiosmtpd.smtp import AuthResult, LoginPassword


@dataclass
class CapturedMail:
    mail_from: str
    rcpt_tos: list[str]
    data: bytes

    @property
    def message(self) -> EmailMessage:
        return email.message_from_bytes(self.data, policy=email.policy.default)  # type: ignore[return-value]


@dataclass
class CapturingHandler:
    messages: list[CapturedMail] = field(default_factory=list)
    # Reply code to return for DATA, e.g. "451 try later" / "554 rejected" (None = accept).
    data_reply: str | None = None
    rcpt_reply: str | None = None

    async def handle_RCPT(self, server, session, envelope, address, rcpt_options):  # noqa: N802
        if self.rcpt_reply:
            return self.rcpt_reply
        envelope.rcpt_tos.append(address)
        return "250 OK"

    async def handle_DATA(self, server, session, envelope):  # noqa: N802
        if self.data_reply:
            return self.data_reply
        self.messages.append(
            CapturedMail(
                envelope.mail_from, list(envelope.rcpt_tos), envelope.original_content or envelope.content
            )
        )
        return "250 Message accepted for delivery"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_self_signed_cert(dir_: Path) -> tuple[Path, Path]:
    """Create a self-signed cert/key for localhost/127.0.0.1 with `cryptography`."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = dir_ / "cert.pem", dir_ / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
    )
    return cert_path, key_path


@dataclass
class SmtpServer:
    host: str
    port: int
    handler: CapturingHandler
    controller: Controller

    @property
    def messages(self) -> list[CapturedMail]:
        return self.handler.messages


@contextmanager
def run_smtp_server(
    *,
    users: dict[str, str] | None = None,
    tls_cert: tuple[Path, Path] | None = None,
    require_starttls: bool = False,
    data_size_limit: int = 100 * 1024 * 1024,
) -> Iterator[SmtpServer]:
    """Start a capturing SMTP server on 127.0.0.1:<random>.

    ``users`` enables AUTH (PLAIN/LOGIN) with these credentials; ``tls_cert`` enables STARTTLS.
    """
    handler = CapturingHandler()
    kwargs: dict = {"data_size_limit": data_size_limit}
    if users is not None:

        def authenticator(server, session, envelope, mechanism, auth_data):
            if isinstance(auth_data, LoginPassword):
                ok = users.get(auth_data.login.decode()) == auth_data.password.decode()
                return AuthResult(success=ok, handled=False)
            return AuthResult(success=False, handled=False)

        kwargs.update(authenticator=authenticator, auth_require_tls=tls_cert is not None)
    if tls_cert is not None:
        ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ctx.load_cert_chain(str(tls_cert[0]), str(tls_cert[1]))
        kwargs.update(tls_context=ctx, require_starttls=require_starttls)
    port = free_port()
    controller = Controller(handler, hostname="127.0.0.1", port=port, **kwargs)
    controller.start()
    try:
        yield SmtpServer("127.0.0.1", port, handler, controller)
    finally:
        controller.stop()
