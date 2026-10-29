"""add button pages and buttons (issue #11)

Revision ID: a1b2c3d4e5f7
Revises: e1f2a3b4c5d6
Create Date: 2026-10-29 00:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a1b2c3d4e5f7"
down_revision: Union[str, None] = "e1f2a3b4c5d6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ROLE_ADDITIONS: dict[str, list[str]] = {
    "qecomp-operator": ["buttons:read", "buttons:press", "buttons:manage"],
    "qecomp-video": ["buttons:read", "buttons:press"],
    "qecomp-viewer": ["buttons:read"],
}


def upgrade() -> None:
    op.create_table(
        "button_pages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.func.gen_random_uuid()),
        sa.Column("name", sa.String(100), nullable=False, unique=True),
        sa.Column("position", sa.Integer(), server_default="0", nullable=False),
        sa.Column("columns", sa.Integer(), server_default="4", nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "buttons",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.func.gen_random_uuid()),
        sa.Column(
            "page_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("button_pages.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("label", sa.String(100), nullable=False),
        sa.Column("color", sa.String(7), server_default="#374151", nullable=False),
        sa.Column("position", sa.Integer(), server_default="0", nullable=False),
        sa.Column("action_yaml", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_buttons_page_id", "buttons", ["page_id"])

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
    op.drop_index("ix_buttons_page_id", table_name="buttons")
    op.drop_table("buttons")
    op.drop_table("button_pages")
