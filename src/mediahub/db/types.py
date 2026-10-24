"""Custom column types."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, Text
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator

from mediahub.db.crypto import DecryptionError, get_keyring

# Returned instead of raising when stored ciphertext can't be decrypted (secret key changed),
# so rows still load and the UI can ask for the credentials to be re-entered.
UNDECRYPTABLE = "__undecryptable__"


class UTCDateTime(TypeDecorator[datetime]):
    """Stores naive UTC in SQLite and always returns timezone-aware UTC datetimes."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime given to UTCDateTime")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        return value.replace(tzinfo=UTC) if value is not None else None


class EncryptedJSON(TypeDecorator[dict[str, Any]]):
    """A JSON object encrypted with the active keyring (Fernet) before it reaches SQLite."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: dict[str, Any] | None, dialect: Dialect) -> str | None:
        return None if value is None else get_keyring().encrypt_json(value)

    def process_result_value(self, value: str | None, dialect: Dialect) -> dict[str, Any] | None:
        if value is None:
            return None
        try:
            return get_keyring().decrypt_json(value)
        except DecryptionError:
            return {UNDECRYPTABLE: True}
