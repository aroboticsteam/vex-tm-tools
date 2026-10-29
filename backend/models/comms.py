"""ORM models for desk <-> iPad comms (issue #8): stations (an iPad with a
role), canned messages, agendas, and messages sent back to the desk."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from backend.core.db import Base

ROLE_EMCEE = "emcee"
ROLE_FLOOR_MANAGER = "floor_manager"
ROLE_CAMERA = "camera"
ROLES = (ROLE_EMCEE, ROLE_FLOOR_MANAGER, ROLE_CAMERA)


class Station(Base):
    __tablename__ = "stations"

    slug: Mapped[str] = mapped_column(String(50), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    #: When set, the station page/API need this key (`?k=` / `X-Station-Key`);
    #: NULL = legacy open link (the slug alone). Rotating it revokes old links.
    access_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: Optional second factor on top of the key: `salt$scrypt-hash` of a PIN.
    pin_hash: Mapped[str | None] = mapped_column(String(200), nullable=True)
    #: Bumped whenever the key or PIN changes, invalidating issued unlock tokens.
    key_version: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    @property
    def secured(self) -> bool:
        return self.access_key is not None

    @property
    def has_pin(self) -> bool:
        return self.pin_hash is not None


class CommsPreset(Base):
    __tablename__ = "comms_presets"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    label: Mapped[str] = mapped_column(String(60), nullable=False)
    message: Mapped[str] = mapped_column(String(500), nullable=False)
    flash: Mapped[bool] = mapped_column(Boolean, server_default="false")
    #: Limits which roles the preset is offered for; NULL = every role.
    role: Mapped[str | None] = mapped_column(String(20), nullable=True)
    position: Mapped[int] = mapped_column(Integer, server_default="0")


class AgendaItem(Base):
    __tablename__ = "agenda_items"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    scope: Mapped[str] = mapped_column(String(10), nullable=False)  # "day" | "finals"
    start_time: Mapped[str] = mapped_column(String(5), nullable=False)  # "HH:MM"
    end_time: Mapped[str | None] = mapped_column(String(5), nullable=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    position: Mapped[int] = mapped_column(Integer, server_default="0")
    #: Minutes before `start_time` to announce this item to the rooms. NULL =
    #: use the notifications default; 0 = never announce this item.
    announce_lead_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)


class StationMessage(Base):
    """A message an emcee/floor manager typed to the desk operator."""

    __tablename__ = "station_messages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    station_slug: Mapped[str] = mapped_column(
        String(50), ForeignKey("stations.slug", ondelete="CASCADE"), nullable=False, index=True
    )
    text: Mapped[str] = mapped_column(String(500), nullable=False)
    acknowledged: Mapped[bool] = mapped_column(Boolean, server_default="false")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
