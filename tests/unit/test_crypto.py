from __future__ import annotations

import stat
from pathlib import Path

import pytest

from mediahub.db.crypto import DecryptionError, Keyring, load_master_secret


def test_round_trip() -> None:
    kr = Keyring(b"a" * 32)
    token = kr.encrypt_json({"api_key": "s3cret"})
    assert "s3cret" not in token
    assert kr.decrypt_json(token) == {"api_key": "s3cret"}


def test_wrong_key_raises() -> None:
    token = Keyring(b"a" * 32).encrypt_json({"x": 1})
    with pytest.raises(DecryptionError):
        Keyring(b"b" * 32).decrypt_json(token)


def test_sign_verify() -> None:
    kr = Keyring(b"a" * 32)
    sig = kr.sign("item/123")
    assert kr.verify("item/123", sig)
    assert not kr.verify("item/124", sig)
    assert not Keyring(b"b" * 32).verify("item/123", sig)


def test_master_secret_generated_once_with_0600(tmp_path: Path) -> None:
    path = tmp_path / "secret.key"
    first = load_master_secret(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert load_master_secret(path) == first
    assert len(first) == 32


def test_master_secret_override(tmp_path: Path) -> None:
    path = tmp_path / "secret.key"
    assert load_master_secret(path, "a-long-enough-secret") == load_master_secret(
        path, "a-long-enough-secret"
    )
    assert not path.exists()
    with pytest.raises(ValueError):
        load_master_secret(path, "short")
