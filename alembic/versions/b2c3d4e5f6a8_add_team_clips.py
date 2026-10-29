"""add team_clips (issues #1/#12)

Revision ID: b2c3d4e5f6a8
Revises: a1b2c3d4e5f7
Create Date: 2026-10-29 01:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b2c3d4e5f6a8"
down_revision: Union[str, None] = "a1b2c3d4e5f7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "team_clips",
        sa.Column("team_number", sa.String(20), primary_key=True),
        sa.Column("kind", sa.String(10), primary_key=True),
        sa.Column("raw_s3_key", sa.String(512), nullable=True),
        sa.Column("processed_s3_key", sa.String(512), nullable=True),
        sa.Column("status", sa.String(20), server_default="NONE", nullable=False),
        sa.Column("error", sa.String(500), nullable=True),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("team_clips")
