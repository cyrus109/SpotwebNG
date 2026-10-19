"""watchlists: saved searches that notify or send, and their hit log

Revision ID: 0006
Revises: 0005
"""

import sqlalchemy as sa

from app.migrations.helpers import _create_indexes, _create_or_extend

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    now = sa.text("(CURRENT_TIMESTAMP)")
    _create_or_extend(
        "watches",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("enabled", sa.Integer(), nullable=False),
        sa.Column("query", sa.String(255), nullable=False),
        sa.Column("tree", sa.String(512), nullable=False),
        sa.Column("min_size", sa.BigInteger(), nullable=False),
        sa.Column("max_size", sa.BigInteger(), nullable=False),
        sa.Column("action", sa.String(12), nullable=False),
        sa.Column("last_id", sa.Integer(), nullable=False),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("hits", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=now, nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    _create_or_extend(
        "watch_hits",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("watch_id", sa.Integer(), nullable=False),
        sa.Column("message_id", sa.String(255), nullable=False),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("outcome", sa.String(120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=now, nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    _create_indexes(
        "watch_hits",
        [
            ("ix_watch_hits_watch_id", ["watch_id"], False),
            ("ix_watch_hits_message_id", ["message_id"], False),
        ],
    )


def downgrade() -> None:
    raise NotImplementedError("forward-only: restore the pre-migration backup to roll back")
