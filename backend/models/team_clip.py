"""ORM model for recorded green-screen clips (issues #1/#12): up to two per
team — the "bot" and the "team" in front of the green screen."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import String, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.core.db import Base


class TeamClip(Base):
    __tablename__ = "team_clips"

    team_number: Mapped[str] = mapped_column(String(20), primary_key=True)
    kind: Mapped[str] = mapped_column(String(10), primary_key=True)
    raw_s3_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    processed_s3_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    status: Mapped[str] = mapped_column(String(20), server_default="NONE")
    error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
