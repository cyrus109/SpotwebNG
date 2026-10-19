"""signature material on spots and comments; spotters (block/trust) table

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa

from app.migrations.helpers import _create_indexes, _create_or_extend, add_column_if_missing

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    zero = sa.text("0")
    empty = sa.text("''")
    add_column_if_missing(
        "spots", sa.Column("keyid", sa.Integer(), nullable=False, server_default=sa.text("-1"))
    )
    add_column_if_missing(
        "spots", sa.Column("spotter_id", sa.String(16), nullable=False, server_default=empty)
    )
    add_column_if_missing(
        "spots", sa.Column("verified", sa.Integer(), nullable=False, server_default=zero)
    )
    add_column_if_missing(
        "spots", sa.Column("from_hdr", sa.String(700), nullable=False, server_default=empty)
    )
    _create_indexes(
        "spots",
        [
            ("ix_spots_spotter_id", ["spotter_id"], False),
            ("ix_spots_verified", ["verified"], False),
        ],
    )
    add_column_if_missing(
        "comments", sa.Column("verified", sa.Integer(), nullable=False, server_default=zero)
    )
    add_column_if_missing(
        "comments", sa.Column("spotter_id", sa.String(16), nullable=False, server_default=empty)
    )
    _create_or_extend(
        "spotters",
        sa.Column("spotter_id", sa.String(16), nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("note", sa.String(255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("spotter_id"),
    )


def downgrade() -> None:
    raise NotImplementedError("forward-only: restore the pre-migration backup to roll back")
