"""availability ("health") of a spot's NZB articles

Revision ID: 0005
Revises: 0004
"""

import sqlalchemy as sa

from app.migrations.helpers import add_column_if_missing

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    add_column_if_missing(
        "spots", sa.Column("health", sa.Integer(), nullable=False, server_default=sa.text("0"))
    )
    add_column_if_missing(
        "spots", sa.Column("health_pct", sa.Integer(), nullable=False, server_default=sa.text("-1"))
    )
    add_column_if_missing(
        "spots", sa.Column("health_checked_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    raise NotImplementedError("forward-only: restore the pre-migration backup to roll back")
