"""Match recordings and instant-replay moments made by the capture agent
(issues #42, #38, #45).

The agent records each match locally and uploads it to the object store at
`{match_type}/{match_number}/match.mp4`; moments (clips) go under
`{match_type}/{match_number}/segments/moment-{n}.mp4`.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.core.db import Base

#: A recording is RECORDING while the agent captures, STOPPED once recording
#: ended but the file is still only on the agent, READY once uploaded.
RECORDING_STATUSES = ("RECORDING", "STOPPED", "READY", "ERROR")
#: A moment is REQUESTED until the agent cuts it, CUT while it uploads, READY once playable.
MOMENT_STATUSES = ("REQUESTED", "CUT", "READY", "ERROR")


class CaptureRecording(Base):
    __tablename__ = "capture_recordings"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    #: `division:session:round:instance:match` (see `notifications.logic.match_key`).
    match_key: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    match_type: Mapped[str] = mapped_column(String(40), nullable=False)
    match_number: Mapped[str] = mapped_column(String(40), nullable=False)
    #: Every team in the match, upper-case, used to find a team's recordings.
    teams: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default="{}")
    #: `{"red": [...], "blue": [...]}` as scheduled.
    alliances: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(String(20), server_default="RECORDING", nullable=False)
    #: Set once the file is in the object store.
    object_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    started_at: Mapped[datetime] = mapped_column(server_default=func.now())
    stopped_at: Mapped[datetime | None] = mapped_column(nullable=True)
    duration_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    error: Mapped[str | None] = mapped_column(String(500), nullable=True)

    moments: Mapped[list["CaptureMoment"]] = relationship(
        back_populates="recording", cascade="all, delete-orphan", order_by="CaptureMoment.number"
    )

    __table_args__ = (
        Index("ix_capture_recordings_teams", "teams", postgresql_using="gin"),
        Index("ix_capture_recordings_started_at", "started_at"),
    )


class CaptureMoment(Base):
    __tablename__ = "capture_moments"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    recording_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("capture_recordings.id", ondelete="CASCADE"), nullable=False
    )
    #: 1-based, in mark order within the recording ("moment-1", "moment-2", ...).
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    length_s: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(String(20), server_default="REQUESTED", nullable=False)
    #: Mark position on the agent's own recording clock (end of the clip).
    end_offset_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Measured time from mark to the clip being playable, reported by the agent.
    ready_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    object_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    requested_at: Mapped[datetime] = mapped_column(server_default=func.now())
    error: Mapped[str | None] = mapped_column(String(500), nullable=True)

    recording: Mapped[CaptureRecording] = relationship(back_populates="moments")

    __table_args__ = (UniqueConstraint("recording_id", "number", name="uq_capture_moment_number"),)
