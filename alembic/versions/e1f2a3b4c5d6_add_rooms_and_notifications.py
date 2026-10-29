"""add rooms, push subscriptions and notification log (issue #5)

Revision ID: e1f2a3b4c5d6
Revises: c5d6e7f8a9b0
Create Date: 2026-10-20 00:00:00.000000

Rooms (named team allocations with a public display page + optional
livestream), Web Push subscriptions (team-facing, scoped to a room, and
staff), and a dedupe log so alerts are sent once. Also extends the
`notifications` system_settings row with the team-alert knobs, and seeds
the new `rooms:*` / `notifications:*` permissions into the default roles
(plus a `qecomp-staff` role). Existing ntfy keys in that row are preserved.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e1f2a3b4c5d6"
down_revision: Union[str, None] = "c5d6e7f8a9b0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ROLE_ADDITIONS: dict[str, list[str]] = {
    "qecomp-operator": ["rooms:read", "rooms:manage", "notifications:receive", "notifications:broadcast"],
    "qecomp-staff": ["rooms:read", "teams:read", "notifications:receive"],
    "qecomp-viewer": ["rooms:read"],
}

_TEAM_ALERT_DEFAULTS = (
    '{"team_alerts_enabled": true, "lead_matches_x": 3, "lead_minutes_y": 15, '
    '"scores_enabled": true, "scores_excluded_rounds": ["F"]}'
)


def upgrade() -> None:
    op.create_table(
        "rooms",
        sa.Column("slug", sa.String(50), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("stream_url", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "room_teams",
        sa.Column("room_slug", sa.String(50), sa.ForeignKey("rooms.slug", ondelete="CASCADE"), primary_key=True),
        sa.Column("team_number", sa.String(20), primary_key=True),
    )
    op.create_index("ix_room_teams_team_number", "room_teams", ["team_number"])

    op.create_table(
        "push_subscriptions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.func.gen_random_uuid()),
        sa.Column("endpoint", sa.Text(), nullable=False, unique=True),
        sa.Column("p256dh", sa.String(255), nullable=False),
        sa.Column("auth", sa.String(255), nullable=False),
        sa.Column("kind", sa.String(10), nullable=False, server_default="team"),
        sa.Column("room_slug", sa.String(50), sa.ForeignKey("rooms.slug", ondelete="SET NULL"), nullable=True),
        sa.Column("user_subject", sa.String(100), nullable=True),
        sa.Column("categories", postgresql.JSONB(), nullable=True),
        sa.Column("user_agent", sa.String(255), nullable=True),
        sa.Column("failure_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("last_success_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_push_subscriptions_room_slug", "push_subscriptions", ["room_slug"])

    op.create_table(
        "push_subscription_teams",
        sa.Column(
            "subscription_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("push_subscriptions.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("team_number", sa.String(20), primary_key=True),
    )
    op.create_index("ix_push_subscription_teams_team_number", "push_subscription_teams", ["team_number"])

    op.create_table(
        "notification_log",
        sa.Column("key", sa.String(200), primary_key=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )

    # Merge the team-alert defaults into the existing ntfy row (existing
    # keys win), creating the row if some environment never got c3d4e5f6a7b8.
    op.execute(
        f"""
        INSERT INTO system_settings (key, value) VALUES
          ('notifications', '{_TEAM_ALERT_DEFAULTS}'::jsonb)
        ON CONFLICT (key) DO UPDATE
          SET value = '{_TEAM_ALERT_DEFAULTS}'::jsonb || system_settings.value
        """
    )

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
                sa.and_(
                    role_permissions.c.authentik_group == group,
                    role_permissions.c.permission.in_(permissions),
                )
            )
        )
    op.execute(
        "UPDATE system_settings SET value = value - 'team_alerts_enabled' - 'lead_matches_x' "
        "- 'lead_minutes_y' - 'scores_enabled' - 'scores_excluded_rounds' WHERE key = 'notifications'"
    )
    op.drop_table("notification_log")
    op.drop_index("ix_push_subscription_teams_team_number", table_name="push_subscription_teams")
    op.drop_table("push_subscription_teams")
    op.drop_index("ix_push_subscriptions_room_slug", table_name="push_subscriptions")
    op.drop_table("push_subscriptions")
    op.drop_index("ix_room_teams_team_number", table_name="room_teams")
    op.drop_table("room_teams")
    op.drop_table("rooms")
