"""seed the `competition` system_settings key (VEX program: V5RC or VIQRC)

Revision ID: d1e2f3a4b5c6
Revises: c1d2e3f4a5b6
Create Date: 2026-11-10 00:00:00.000000

Makes the VEX program explicit. Default `V5RC`; existing rows are kept.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "d1e2f3a4b5c6"
down_revision: Union[str, None] = "c1d2e3f4a5b6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        INSERT INTO system_settings (key, value) VALUES
          ('competition', '{"program": "V5RC"}')
        ON CONFLICT DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute("DELETE FROM system_settings WHERE key = 'competition'")
