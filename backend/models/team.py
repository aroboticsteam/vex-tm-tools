"""ORM model for team_profiles (plan §8)."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from backend.core.db import Base


class TeamProfile(Base):
    __tablename__ = "team_profiles"

    team_number: Mapped[str] = mapped_column(String(20), primary_key=True)
    pit_location: Mapped[str | None] = mapped_column(String(100), nullable=True)
    bio: Mapped[str | None] = mapped_column(Text, nullable=True)
    robot_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # Raw upload retained permanently (per Appendix A.5) so "Re-process" can
    # re-run FFmpeg without requiring a new upload. Not in the plan's §8 DDL
    # listing (which only lists `video_360_s3_key`) but required by A.5's
    # "Both raw and processed keys stored in team_profiles" resolution —
    # added here as an additive column (see alembic migration).
    raw_video_s3_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    video_360_s3_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    video_processing_status: Mapped[str] = mapped_column(String(20), server_default="NONE")
    # Short, sanitized human-readable reason set when video_processing_status
    # transitions to "FAILED" (e.g. "NoSuchBucket: The specified bucket does
    # not exist"), so `/video/status` can surface something actionable
    # instead of just "FAILED" with no detail. Never holds a full traceback
    # or internal filesystem paths (see `_sanitize_processing_error` in
    # backend/routers/teams.py). Cleared whenever a new upload starts.
    video_processing_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    cached_stats: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    extra_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Public team page extras (issue #28), edited by staff.
    website_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    logo_s3_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )
