"""Secret encryption at rest and token hashing."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


class SecretBox:
    """Fernet encryption with a key derived from the app secret (HKDF-SHA256)."""

    def __init__(self, secret_key: str) -> None:
        if len(secret_key) < 16:
            raise ValueError("SCANBOX_SECRET_KEY must be at least 16 characters")
        derived = HKDF(algorithm=hashes.SHA256(), length=32, salt=b"scanbox-secrets-v1", info=b"fernet").derive(
            secret_key.encode("utf-8")
        )
        self._fernet = Fernet(base64.urlsafe_b64encode(derived))
        self._mac_key = HKDF(algorithm=hashes.SHA256(), length=32, salt=b"scanbox-mac-v1", info=b"mac").derive(
            secret_key.encode("utf-8")
        )

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str) -> str | None:
        try:
            return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError):
            return None

    def sign(self, data: str) -> str:
        return hmac.new(self._mac_key, data.encode("utf-8"), hashlib.sha256).hexdigest()


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def random_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def constant_time_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))
