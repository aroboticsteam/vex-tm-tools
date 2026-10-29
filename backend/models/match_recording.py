"""Links from completed matches to their recordings (issue #28)."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import String, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.core.db import Base


class MatchRecording(Base):
    __tablename__ = "match_recordings"

    #: `division:session:round:instance:match` — see `notifications.logic.match_key`.
    match_key: Mapped[str] = mapped_column(String(200), primary_key=True)
    url: Mapped[str] = mapped_column(String(500), nullable=False)
    title: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
