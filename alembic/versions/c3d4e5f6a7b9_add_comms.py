"""add stations, comms presets, agenda items and station messages (issue #8)

Revision ID: c3d4e5f6a7b9
Revises: b2c3d4e5f6a8
Create Date: 2026-10-29 02:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c3d4e5f6a7b9"
down_revision: Union[str, None] = "b2c3d4e5f6a8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ROLE_ADDITIONS: dict[str, list[str]] = {
    "qecomp-operator": ["comms:read", "comms:send", "comms:manage"],
    "qecomp-staff": ["comms:read"],
    "qecomp-viewer": ["comms:read"],
}


def upgrade() -> None:
    op.create_table(
        "stations",
        sa.Column("slug", sa.String(50), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "comms_presets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.func.gen_random_uuid()),
        sa.Column("label", sa.String(60), nullable=False),
        sa.Column("message", sa.String(500), nullable=False),
        sa.Column("flash", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("role", sa.String(20), nullable=True),
        sa.Column("position", sa.Integer(), server_default="0", nullable=False),
    )
    op.create_table(
        "agenda_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.func.gen_random_uuid()),
        sa.Column("scope", sa.String(10), nullable=False),
        sa.Column("start_time", sa.String(5), nullable=False),
        sa.Column("end_time", sa.String(5), nullable=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("details", sa.Text(), nullable=True),
        sa.Column("position", sa.Integer(), server_default="0", nullable=False),
    )
    op.create_table(
        "station_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.func.gen_random_uuid()),
        sa.Column("station_slug", sa.String(50), sa.ForeignKey("stations.slug", ondelete="CASCADE"), nullable=False),
        sa.Column("text", sa.String(500), nullable=False),
        sa.Column("acknowledged", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_station_messages_station_slug", "station_messages", ["station_slug"])

    role_permissions = sa.table(
        "role_permissions", sa.column("authentik_group", sa.String), sa.column("permission", sa.String)
    )
    rows = [
        {"authentik_group": group, "permission": permission}
        for group, permissions in _ROLE_ADDITIONS.items()
        for permission in permissions
    ]
    op.execute(
        postgresql.insert(role_permissions)
        .values(rows)
        .on_conflict_do_nothing(index_elements=["authentik_group", "permission"])
    )


def downgrade() -> None:
    role_permissions = sa.table(
        "role_permissions", sa.column("authentik_group", sa.String), sa.column("permission", sa.String)
    )
    for group, permissions in _ROLE_ADDITIONS.items():
        op.execute(
            role_permissions.delete().where(
                sa.and_(role_permissions.c.authentik_group == group, role_permissions.c.permission.in_(permissions))
            )
        )
    op.drop_index("ix_station_messages_station_slug", table_name="station_messages")
    op.drop_table("station_messages")
    op.drop_table("agenda_items")
    op.drop_table("comms_presets")
    op.drop_table("stations")
