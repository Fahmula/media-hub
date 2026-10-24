"""The application context: long-lived objects created in the lifespan and shared by routes."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from mediahub.config import Settings
from mediahub.db.app_settings import ServerIdentity
from mediahub.db.crypto import Keyring
from mediahub.monitoring.service import MonitoringService


@dataclass
class AppContext:
    settings: Settings
    engine: Engine
    sessions: sessionmaker[Session]
    keyring: Keyring
    identity: ServerIdentity
    monitor: MonitoringService


def get_context(request: Request) -> AppContext:
    return request.app.state.ctx
