"""comms: secured station links (key + optional PIN), agenda ordering by position (issue #27)

Revision ID: f7a8b9c1d2e3
Revises: e6f7a8b9c1d2
Create Date: 2026-10-29 14:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f7a8b9c1d2e3"
down_revision: Union[str, None] = "e6f7a8b9c1d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("stations", sa.Column("access_key", sa.String(64), nullable=True))
    op.add_column("stations", sa.Column("pin_hash", sa.String(200), nullable=True))
    op.add_column("stations", sa.Column("key_version", sa.Integer(), server_default="0", nullable=False))
    # Agenda order becomes explicit (drag-and-drop): number each scope's items
    # in their current chronological order so nothing visibly moves.
    op.execute(
        """
        UPDATE agenda_items a SET position = r.rn
        FROM (
            SELECT id, ROW_NUMBER() OVER (PARTITION BY scope ORDER BY start_time, position, id) - 1 AS rn
            FROM agenda_items
        ) r
        WHERE a.id = r.id
        """
    )


def downgrade() -> None:
    op.drop_column("stations", "key_version")
    op.drop_column("stations", "pin_hash")
    op.drop_column("stations", "access_key")
