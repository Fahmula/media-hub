"""FastAPI application factory and lifespan."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from mediahub import __version__
from mediahub.config import Settings
from mediahub.context import AppContext
from mediahub.db.app_settings import ensure_identity
from mediahub.db.crypto import Keyring, load_master_secret, set_keyring
from mediahub.db.session import make_engine, make_sessionmaker, run_migrations, session_scope
from mediahub.logging import configure_logging, get_logger
from mediahub.runtime_lock import InstanceLock
from mediahub.web.routes import health

log = get_logger(__name__)


def _build_context(settings: Settings) -> AppContext:
    settings.config_dir.mkdir(parents=True, exist_ok=True)
    override = settings.secret_key.get_secret_value() if settings.secret_key else None
    keyring = Keyring(load_master_secret(settings.secret_key_path, override))
    set_keyring(keyring)

    engine = make_engine(settings.database_url)
    run_migrations(engine)
    sessions = make_sessionmaker(engine)
    with session_scope(sessions) as db:
        identity = ensure_identity(db)

    return AppContext(
        settings=settings,
        engine=engine,
        sessions=sessions,
        keyring=keyring,
        identity=identity,
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    configure_logging(settings.log_level, settings.log_format)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        lock = InstanceLock(settings.lock_path)
        lock.acquire()
        ctx = _build_context(settings)
        app.state.ctx = ctx
        log.info(
            "startup",
            version=__version__,
            server_id=ctx.identity.server_id,
            server_name=ctx.identity.server_name,
            config_dir=str(settings.config_dir),
        )
        try:
            yield
        finally:
            ctx.engine.dispose()
            lock.release()
            log.info("shutdown")

    app = FastAPI(
        title="MediaHub",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.include_router(health.router)
    return app
