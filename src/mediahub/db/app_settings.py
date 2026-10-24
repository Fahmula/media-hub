"""Key/value application settings (server identity, UI preferences)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from mediahub.db.models import AppSetting

SERVER_ID = "server_id"
SERVER_NAME = "server_name"
DEFAULT_SERVER_NAME = "MediaHub"


@dataclass(frozen=True, slots=True)
class ServerIdentity:
    server_id: str
    server_name: str


def get_setting(session: Session, key: str, default: Any = None) -> Any:
    row = session.get(AppSetting, key)
    return row.value if row is not None else default


def set_setting(session: Session, key: str, value: Any) -> None:
    row = session.get(AppSetting, key)
    if row is None:
        session.add(AppSetting(key=key, value=value))
    else:
        row.value = value


def ensure_identity(session: Session, default_name: str | None = None) -> ServerIdentity:
    """Return this installation's identity, creating a stable UUID on first start."""
    server_id = get_setting(session, SERVER_ID)
    if not server_id:
        server_id = str(uuid.uuid4())
        set_setting(session, SERVER_ID, server_id)
    name = get_setting(session, SERVER_NAME)
    if not name:
        name = (default_name or DEFAULT_SERVER_NAME).strip()[:64] or DEFAULT_SERVER_NAME
        set_setting(session, SERVER_NAME, name)
    return ServerIdentity(server_id=server_id, server_name=name)
