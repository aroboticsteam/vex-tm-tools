"""Pydantic schemas mirroring `backend.models.team` (plan §8 / §11 Teams)."""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


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
    website_url: str | None = None
    logo_s3_key: str | None = None
    inspection_status: str = "NOT_STARTED"
    updated_at: datetime


class TeamProfileUpdate(BaseModel):
    """PUT body for `/api/v1/teams/<number>` — edits pit_location/notes only
    (plan §5.13 Teams page scope)."""

    model_config = ConfigDict(extra="forbid")

    pit_location: str | None = Field(default=None, max_length=100)
    extra_notes: str | None = None
    #: http(s) link shown on the public team page; an empty string clears it.
    website_url: str | None = Field(default=None, max_length=500)
    #: Checklist inspection state (issue #43): NOT_STARTED, IN_PROGRESS or PASSED.
    inspection_status: Literal["NOT_STARTED", "IN_PROGRESS", "PASSED"] | None = None

    @field_validator("website_url")
    @classmethod
    def _http_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if value and not re.match(r"^https?://[^\s]+$", value, re.IGNORECASE):
            raise ValueError("website_url must be an http:// or https:// link")
        return value


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


class TeamChecklistItem(BaseModel):
    """One team's checklist (issue #43): inspection plus the two videos."""

    team_number: str
    inspection: str
    bot_video: bool
    team_video: bool
    done: int
    total: int
    complete: bool
