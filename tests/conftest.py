from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mediahub.config import LogFormat, Settings
from mediahub.db.crypto import Keyring, set_keyring
from mediahub.main import create_app


@pytest.fixture(autouse=True)
def _keyring() -> None:
    set_keyring(Keyring(b"k" * 32))


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(config_dir=tmp_path / "config", log_format=LogFormat.CONSOLE)


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings), base_url="http://testserver") as c:
        yield c
