"""team checklist: inspection status on team_profiles (issue #43)

Revision ID: b9c1d2e3f4a5
Revises: a8b9c1d2e3f4
Create Date: 2026-10-30 18:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b9c1d2e3f4a5"
down_revision: Union[str, None] = "a8b9c1d2e3f4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "team_profiles",
        sa.Column("inspection_status", sa.String(20), server_default="NOT_STARTED", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("team_profiles", "inspection_status")
