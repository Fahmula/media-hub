from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import inspect, text

from mediahub.db.app_settings import ensure_identity
from mediahub.db.models import ServiceInstance
from mediahub.db.session import make_engine, make_sessionmaker, run_migrations, session_scope


def test_migrations_and_identity(tmp_path: Path) -> None:
    engine = make_engine(f"sqlite:///{tmp_path / 'db.sqlite'}")
    run_migrations(engine)
    run_migrations(engine)  # idempotent
    tables = set(inspect(engine).get_table_names())
    assert {"users", "auth_sessions", "service_instances", "peers", "peer_grants"} <= tables

    factory = make_sessionmaker(engine)
    with session_scope(factory) as db:
        first = ensure_identity(db)
    with session_scope(factory) as db:
        assert ensure_identity(db) == first


def test_secrets_encrypted_at_rest(tmp_path: Path) -> None:
    engine = make_engine(f"sqlite:///{tmp_path / 'db.sqlite'}")
    run_migrations(engine)
    factory = make_sessionmaker(engine)
    with session_scope(factory) as db:
        db.add(
            ServiceInstance(
                service_type="sonarr",
                name="TV",
                base_url="http://sonarr:8989",
                secrets={"api_key": "plaintext-key"},
                last_success_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
        )
    with engine.connect() as conn:
        raw = conn.execute(text("SELECT secrets FROM service_instances")).scalar_one()
    assert "plaintext-key" not in raw
    with factory() as db:
        inst = db.query(ServiceInstance).one()
        assert inst.secrets == {"api_key": "plaintext-key"}
        assert inst.last_success_at is not None and inst.last_success_at.tzinfo is UTC
