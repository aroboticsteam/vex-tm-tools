"""Pydantic schemas for the capture agent API (issues #42, #38, #45)."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

#: Clip lengths the deck offers; the API accepts anything in (0, MAX_MARK_SECONDS].
MAX_MARK_SECONDS = 30


class MomentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    number: int
    length_s: float
    status: str
    end_offset_ms: int | None = None
    ready_ms: int | None = None
    requested_at: datetime
    error: str | None = None


class RecordingRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    match_key: str
    match_type: str
    match_number: str
    teams: list[str] = Field(default_factory=list)
    alliances: dict[str, Any] | None = None
    status: str
    started_at: datetime
    stopped_at: datetime | None = None
    duration_s: float | None = None
    size_bytes: int | None = None
    error: str | None = None
    moments: list[MomentRead] = Field(default_factory=list)


class StartRecordingRequest(BaseModel):
    #: Either `match_key`+`round`+`match_number`-style fields or a raw TM `match` tuple.
    match: dict[str, Any] | None = None
    match_key: str | None = Field(default=None, max_length=200)
    round: str | None = Field(default=None, max_length=40)
    instance: int | None = None
    match_num: int | None = None
    red_teams: list[Any] = Field(default_factory=list, max_length=8)
    blue_teams: list[Any] = Field(default_factory=list, max_length=8)


class MarkRequest(BaseModel):
    length_s: float = Field(..., gt=0, le=MAX_MARK_SECONDS)


class UploadRequest(BaseModel):
    kind: Literal["match", "moment"]
    recording_id: uuid.UUID
    moment_id: uuid.UUID | None = None


class UploadGrant(BaseModel):
    upload_id: uuid.UUID
    kind: Literal["match", "moment"]
    url: str
    method: str = "PUT"
    headers: dict[str, str] = Field(default_factory=dict)
    object_key: str
    expires_in: int


class UploadComplete(BaseModel):
    kind: Literal["match", "moment"]
    #: Milliseconds from the mark to the clip being playable, as measured by the agent.
    ready_ms: int | None = Field(default=None, ge=0)
    duration_s: float | None = Field(default=None, ge=0)


class AgentStatus(BaseModel):
    connected: bool
    agent: dict[str, Any] | None = None
    recording: RecordingRead | None = None


class WipeResult(BaseModel):
    recordings: int
    objects_deleted: int


class ReplayPlay(BaseModel):
    kind: Literal["moment", "recording"]
    id: uuid.UUID
    rate: float = Field(default=0.5, ge=0.05, le=4)
    loop: bool = False


class ReplayControl(BaseModel):
    action: Literal["pause", "resume", "rate", "seek", "stop", "restart"]
    value: float | None = None
