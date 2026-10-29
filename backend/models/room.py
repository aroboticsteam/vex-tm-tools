"""ORM models for rooms and their allocated teams.

A room is a named group of teams (e.g. a judging/waiting room) that has a
public display page (`/room/<slug>`), optionally embedding a livestream, and
that match alerts and staff broadcasts can be targeted at.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.core.db import Base


class Room(Base):
    __tablename__ = "rooms"

    slug: Mapped[str] = mapped_column(String(50), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    stream_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class RoomTeam(Base):
    __tablename__ = "room_teams"

    room_slug: Mapped[str] = mapped_column(
        String(50), ForeignKey("rooms.slug", ondelete="CASCADE"), primary_key=True
    )
    team_number: Mapped[str] = mapped_column(String(20), primary_key=True)
