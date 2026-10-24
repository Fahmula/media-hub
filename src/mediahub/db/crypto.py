"""Key management for stored credentials and signed values.

One master secret (``MEDIAHUB_SECRET_KEY`` or ``/config/secret.key``) is expanded with HKDF
into independent sub-keys:

* ``fernet``  – encrypts service credentials and peer tokens at rest
* ``signing`` – HMAC for values handed to the browser (e.g. image references)
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


class DecryptionError(RuntimeError):
    """Stored ciphertext could not be decrypted (wrong or rotated secret key)."""


def _derive(master: bytes, info: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(master)


class Keyring:
    def __init__(self, master: bytes) -> None:
        if len(master) < 32:
            raise ValueError("secret key must be at least 32 bytes")
        self._fernet = Fernet(base64.urlsafe_b64encode(_derive(master, b"mediahub/fernet/v1")))
        self._signing_key = _derive(master, b"mediahub/signing/v1")

    def encrypt_json(self, value: Any) -> str:
        return self._fernet.encrypt(json.dumps(value, separators=(",", ":")).encode()).decode()

    def decrypt_json(self, token: str) -> Any:
        try:
            return json.loads(self._fernet.decrypt(token.encode()))
        except InvalidToken as exc:
            raise DecryptionError("stored credentials cannot be decrypted with this key") from exc

    def sign(self, data: str) -> str:
        mac = hmac.new(self._signing_key, data.encode(), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(mac[:18]).decode().rstrip("=")

    def verify(self, data: str, signature: str) -> bool:
        return hmac.compare_digest(self.sign(data), signature)


def load_master_secret(path: Path, override: str | None = None) -> bytes:
    """Return the master secret, generating ``path`` (mode 0600) on first run."""
    if override:
        if len(override) < 16:
            raise ValueError("MEDIAHUB_SECRET_KEY must be at least 16 characters")
        return hashlib.sha256(override.encode()).digest()
    if path.exists():
        raw = path.read_text().strip()
        if not raw:
            raise ValueError(f"{path} is empty")
        return base64.urlsafe_b64decode(raw.encode())
    path.parent.mkdir(parents=True, exist_ok=True)
    master = secrets.token_bytes(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(base64.urlsafe_b64encode(master).decode())
    return master


_active: Keyring | None = None


def set_keyring(keyring: Keyring) -> None:
    global _active
    _active = keyring


def get_keyring() -> Keyring:
    if _active is None:
        raise RuntimeError("keyring not configured")
    return _active
