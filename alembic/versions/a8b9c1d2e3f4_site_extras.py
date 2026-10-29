"""team logo/website and match recording links (issue #28)

Revision ID: a8b9c1d2e3f4
Revises: f7a8b9c1d2e3
Create Date: 2026-10-29 16:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a8b9c1d2e3f4"
down_revision: Union[str, None] = "f7a8b9c1d2e3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("team_profiles", sa.Column("website_url", sa.String(500), nullable=True))
    op.add_column("team_profiles", sa.Column("logo_s3_key", sa.String(512), nullable=True))
    op.create_table(
        "match_recordings",
        sa.Column("match_key", sa.String(200), primary_key=True),
        sa.Column("url", sa.String(500), nullable=False),
        sa.Column("title", sa.String(100), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("match_recordings")
    op.drop_column("team_profiles", "logo_s3_key")
    op.drop_column("team_profiles", "website_url")
