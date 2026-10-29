"""ORM models for button pages (issue #11): pages of pressable buttons,
each running a YAML action chain (same format as scripts/automations)."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from backend.core.db import Base


class ButtonPage(Base):
    __tablename__ = "button_pages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    position: Mapped[int] = mapped_column(Integer, server_default="0")
    columns: Mapped[int] = mapped_column(Integer, server_default="4")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Button(Base):
    __tablename__ = "buttons"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    page_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("button_pages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    label: Mapped[str] = mapped_column(String(100), nullable=False)
    color: Mapped[str] = mapped_column(String(7), nullable=False, server_default="#374151")
    position: Mapped[int] = mapped_column(Integer, server_default="0")
    action_yaml: Mapped[str] = mapped_column(Text, nullable=False)
    #: A short emoji/glyph shown above the label.
    icon: Mapped[str | None] = mapped_column(String(8), nullable=True)
    #: The UI asks "are you sure?" before running (risky actions).
    confirm: Mapped[bool] = mapped_column(Boolean, server_default="false", nullable=False)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
