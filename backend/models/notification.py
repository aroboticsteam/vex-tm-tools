"""ORM models for Web Push subscriptions and the notification dedupe log."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from backend.core.db import Base

#: `push_subscriptions.kind` values.
KIND_TEAM = "team"
KIND_STAFF = "staff"


class PushSubscription(Base):
    __tablename__ = "push_subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    # The push service URL doubles as the capability the client holds to
    # change/remove its own subscription, so it is unique and never returned.
    endpoint: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    p256dh: Mapped[str] = mapped_column(String(255), nullable=False)
    auth: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[str] = mapped_column(String(10), nullable=False, server_default=KIND_TEAM)
    room_slug: Mapped[str | None] = mapped_column(
        String(50), ForeignKey("rooms.slug", ondelete="SET NULL"), nullable=True
    )
    # Staff only: OIDC subject + which alert categories they opted into.
    user_subject: Mapped[str | None] = mapped_column(String(100), nullable=True)
    categories: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)
    failure_count: Mapped[int] = mapped_column(Integer, server_default="0")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    last_success_at: Mapped[datetime | None] = mapped_column(nullable=True)


class PushSubscriptionTeam(Base):
    __tablename__ = "push_subscription_teams"

    subscription_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("push_subscriptions.id", ondelete="CASCADE"), primary_key=True
    )
    team_number: Mapped[str] = mapped_column(String(20), primary_key=True)


class NotificationLog(Base):
    """One row per alert already sent (e.g. `matches:1:QUAL:0:1:12`), so a
    restart or leader failover never re-sends it."""

    __tablename__ = "notification_log"

    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
