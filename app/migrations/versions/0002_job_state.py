"""job_state: progress of derived-data background jobs (see app/jobs.py)

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa

from app.migrations.helpers import _create_or_extend

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    _create_or_extend(
        "job_state",
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("processed", sa.Integer(), nullable=False),
        sa.Column("detail", sa.String(length=255), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("name"),
    )


def downgrade() -> None:
    raise NotImplementedError("forward-only: restore the pre-migration backup to roll back")
