"""API keys: scope (full / download-only) and a request counter

Revision ID: 0007
Revises: 0006
"""

import sqlalchemy as sa

from app.migrations.helpers import add_column_if_missing

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    add_column_if_missing(
        "api_users",
        sa.Column("scope", sa.String(12), nullable=False, server_default=sa.text("'full'")),
    )
    add_column_if_missing(
        "api_users",
        sa.Column("requests", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )


def downgrade() -> None:
    raise NotImplementedError("forward-only: restore the pre-migration backup to roll back")
