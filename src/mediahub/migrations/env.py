"""Alembic environment. Runs against the connection passed in by ``run_migrations`` or, for
CLI use (``uv run alembic ...``), against ``MEDIAHUB_CONFIG_DIR``'s database."""

from __future__ import annotations

from alembic import context
from sqlalchemy import Connection

from mediahub.db.models import Base

target_metadata = Base.metadata


def _run(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,  # SQLite needs batch mode for ALTER TABLE
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


connection = context.config.attributes.get("connection")
if connection is not None:
    _run(connection)
else:
    from mediahub.config import Settings
    from mediahub.db.session import make_engine

    url = context.config.get_main_option("sqlalchemy.url") or Settings().database_url
    with make_engine(url).connect() as conn:
        _run(conn)
        conn.commit()
