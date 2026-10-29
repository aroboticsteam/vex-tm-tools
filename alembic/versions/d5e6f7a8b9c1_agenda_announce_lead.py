"""agenda items: per-item announcement lead time (issue #22)

Revision ID: d5e6f7a8b9c1
Revises: c3d4e5f6a7b9
Create Date: 2026-10-29 10:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d5e6f7a8b9c1"
down_revision: Union[str, None] = "c3d4e5f6a7b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("agenda_items", sa.Column("announce_lead_minutes", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("agenda_items", "announce_lead_minutes")
