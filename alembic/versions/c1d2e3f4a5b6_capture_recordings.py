"""capture agent: recordings and replay moments (issues #42, #38, #45)

Revision ID: c1d2e3f4a5b6
Revises: b9c1d2e3f4a5
Create Date: 2026-10-30 20:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c1d2e3f4a5b6"
down_revision: Union[str, None] = "b9c1d2e3f4a5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: Seeded like the comms permissions: the agent key gets `capture:upload` by hand.
_ROLE_PERMISSIONS = {
    "qecomp-operator": ["capture:control", "analysis:view"],
    "qecomp-video": ["capture:control", "analysis:view"],
    "qecomp-staff": ["analysis:view"],
}


def upgrade() -> None:
    op.create_table(
        "capture_recordings",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("match_key", sa.String(200), nullable=False, unique=True),
        sa.Column("match_type", sa.String(40), nullable=False),
        sa.Column("match_number", sa.String(40), nullable=False),
        sa.Column("teams", postgresql.ARRAY(sa.Text()), server_default="{}"),
        sa.Column("alliances", postgresql.JSONB(), nullable=True),
        sa.Column("status", sa.String(20), server_default="RECORDING", nullable=False),
        sa.Column("object_key", sa.String(512), nullable=True),
        sa.Column("started_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("stopped_at", sa.DateTime(), nullable=True),
        sa.Column("duration_s", sa.Float(), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("error", sa.String(500), nullable=True),
    )
    op.create_index("ix_capture_recordings_teams", "capture_recordings", ["teams"], postgresql_using="gin")
    op.create_index("ix_capture_recordings_started_at", "capture_recordings", ["started_at"])
    op.create_table(
        "capture_moments",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column(
            "recording_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("capture_recordings.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("length_s", sa.Float(), nullable=False),
        sa.Column("status", sa.String(20), server_default="REQUESTED", nullable=False),
        sa.Column("end_offset_ms", sa.Integer(), nullable=True),
        sa.Column("ready_ms", sa.Integer(), nullable=True),
        sa.Column("object_key", sa.String(512), nullable=True),
        sa.Column("requested_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("error", sa.String(500), nullable=True),
        sa.UniqueConstraint("recording_id", "number", name="uq_capture_moment_number"),
    )
    role_permissions = sa.table(
        "role_permissions", sa.column("authentik_group", sa.String), sa.column("permission", sa.String)
    )
    for group, permissions in _ROLE_PERMISSIONS.items():
        for permission in permissions:
            op.execute(
                postgresql.insert(role_permissions)
                .values(authentik_group=group, permission=permission)
                .on_conflict_do_nothing()
            )


def downgrade() -> None:
    role_permissions = sa.table(
        "role_permissions", sa.column("authentik_group", sa.String), sa.column("permission", sa.String)
    )
    for group, permissions in _ROLE_PERMISSIONS.items():
        op.execute(
            role_permissions.delete().where(
                sa.and_(role_permissions.c.authentik_group == group, role_permissions.c.permission.in_(permissions))
            )
        )
    op.drop_table("capture_moments")
    op.drop_index("ix_capture_recordings_started_at", table_name="capture_recordings")
    op.drop_index("ix_capture_recordings_teams", table_name="capture_recordings")
    op.drop_table("capture_recordings")
