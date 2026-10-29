"""Pydantic schemas mirroring `backend.models.team` (plan §8 / §11 Teams)."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class TeamProfileRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    team_number: str
    pit_location: str | None = None
    bio: str | None = None
    robot_name: str | None = None
    video_360_s3_key: str | None = None
    video_processing_status: str = "NONE"
    cached_stats: dict[str, Any] | None = None
    extra_notes: str | None = None
    updated_at: datetime


class TeamProfileUpdate(BaseModel):
    """PUT body for `/api/v1/teams/<number>` — edits pit_location/notes only
    (plan §5.13 Teams page scope)."""

    model_config = ConfigDict(extra="forbid")

    pit_location: str | None = Field(default=None, max_length=100)
    extra_notes: str | None = None


class VideoProcessingStatus(BaseModel):
    team_number: str
    video_processing_status: str
    video_360_s3_key: str | None = None
    error: str | None = None


class VideoUploadAccepted(BaseModel):
    team_number: str
    video_processing_status: str = "PROCESSING"


class TeamVideoUrl(BaseModel):
    team_number: str
    url: str | None = Field(
        default=None,
        description="Presigned processed-video URL, or null if no processed video exists.",
    )


class BatchVideoUrlsResponse(BaseModel):
    videos: dict[str, str | None]


class TeamClipStatus(BaseModel):
    team_number: str
    kind: str
    status: str = "NONE"
    processed_s3_key: str | None = None
    error: str | None = None


class TeamClipsRead(BaseModel):
    team_number: str
    clips: dict[str, TeamClipStatus]


class OverlayTeam(BaseModel):
    """Display-safe team info for the public overlay endpoint."""

    team_number: str
    team_name: str | None = None
    organisation: str | None = None
    location: str | None = None
    country: str | None = None
    qual_rank: int | None = None
    skills_rank: int | None = None
    robot_name: str | None = None
    bot_url: str | None = None
    team_url: str | None = None


class OverlayTeamsResponse(BaseModel):
    teams: dict[str, OverlayTeam]
    #: Opt-in (system setting `overlay.show_robot_name`): robot names need
    #: VEX Events approval before they are put on stream.
    show_robot_name: bool = False
