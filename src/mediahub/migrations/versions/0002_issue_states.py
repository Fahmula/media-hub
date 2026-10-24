"""issue states

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-24 16:02:39.226686
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "issue_states",
        sa.Column("fingerprint", sa.String(length=128), nullable=False),
        sa.Column("source_key", sa.String(length=80), nullable=False),
        sa.Column("first_seen", sa.DateTime(), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(), nullable=True),
        sa.Column("muted_until", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("fingerprint"),
    )
    with op.batch_alter_table("issue_states", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_issue_states_source_key"), ["source_key"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("issue_states", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_issue_states_source_key"))

    op.drop_table("issue_states")
