"""buttons: icon and confirm-before-press (issue #25)

Revision ID: e6f7a8b9c1d2
Revises: d5e6f7a8b9c1
Create Date: 2026-10-29 12:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e6f7a8b9c1d2"
down_revision: Union[str, None] = "d5e6f7a8b9c1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("buttons", sa.Column("icon", sa.String(8), nullable=True))
    op.add_column("buttons", sa.Column("confirm", sa.Boolean(), server_default="false", nullable=False))


def downgrade() -> None:
    op.drop_column("buttons", "confirm")
    op.drop_column("buttons", "icon")
