"""Pydantic schemas for desk <-> iPad comms (issue #8)."""
from __future__ import annotations

import re
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.models.comms import ROLES

SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]{0,49}$"
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
SCREENS = ("messages", "countdown", "agenda_day", "agenda_finals", "timer")
SCOPES = ("day", "finals")


def _check_role(value: str | None) -> str | None:
    if value is not None and value not in ROLES:
        raise ValueError(f"role must be one of {', '.join(ROLES)}")
    return value


class StationBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    role: str

    _role = field_validator("role")(_check_role)


class StationCreate(StationBase):
    slug: str = Field(..., pattern=SLUG_PATTERN)


class StationUpdate(StationBase):
    pass


class StationRead(StationBase):
    model_config = ConfigDict(from_attributes=True)

    slug: str
    #: Link requires the station key (and the PIN too when `has_pin`).
    secured: bool = False
    has_pin: bool = False
    #: Presence: an open page is connected right now / when one last pinged.
    connected: bool = False
    last_seen: float | None = None


class PresetBase(BaseModel):
    label: str = Field(..., min_length=1, max_length=60)
    message: str = Field(..., min_length=1, max_length=500)
    flash: bool = False
    role: str | None = None
    position: int = Field(default=0, ge=0, le=1000)

    _role = field_validator("role")(_check_role)


class PresetRead(PresetBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID


class AgendaBase(BaseModel):
    scope: str
    start_time: str
    end_time: str | None = None
    title: str = Field(..., min_length=1, max_length=200)
    details: str | None = Field(default=None, max_length=2000)
    #: Omitted = slot the item in by start time (create) / keep as is (update).
    position: int | None = Field(default=None, ge=0, le=1000)
    announce_lead_minutes: int | None = Field(default=None, ge=0, le=240)

    @field_validator("scope")
    @classmethod
    def _scope(cls, value: str) -> str:
        if value not in SCOPES:
            raise ValueError(f"scope must be one of {', '.join(SCOPES)}")
        return value

    @field_validator("start_time", "end_time")
    @classmethod
    def _time(cls, value: str | None) -> str | None:
        if value is not None and not _TIME_RE.match(value):
            raise ValueError("times must be HH:MM (24-hour)")
        return value


class AgendaRead(AgendaBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    position: int = 0


class AgendaReorder(BaseModel):
    scope: str
    ids: list[UUID] = Field(..., min_length=1, max_length=500)

    @field_validator("scope")
    @classmethod
    def _scope(cls, value: str) -> str:
        if value not in SCOPES:
            raise ValueError(f"scope must be one of {', '.join(SCOPES)}")
        return value


class AgendaImport(BaseModel):
    """Bulk add: one item per line, `HH:MM[-HH:MM] Title [| details]`."""

    scope: str
    text: str = Field(..., min_length=1, max_length=20000)
    replace: bool = False
    dry_run: bool = False

    @field_validator("scope")
    @classmethod
    def _scope(cls, value: str) -> str:
        if value not in SCOPES:
            raise ValueError(f"scope must be one of {', '.join(SCOPES)}")
        return value


class AgendaImportError(BaseModel):
    line: int
    error: str


class AgendaImportResult(BaseModel):
    created: int
    errors: list[AgendaImportError] = Field(default_factory=list)
    items: list[AgendaBase] = Field(default_factory=list)


class SendRequest(BaseModel):
    """Send to explicit stations and/or every station of the given roles;
    both empty/omitted means every station."""

    stations: list[str] | None = Field(default=None, max_length=100)
    roles: list[str] | None = Field(default=None, max_length=3)
    text: str = Field(..., min_length=1, max_length=500)
    flash: bool = False

    @field_validator("roles")
    @classmethod
    def _roles(cls, value: list[str] | None) -> list[str] | None:
        for role in value or []:
            _check_role(role)
        return value


class ScreenRequest(BaseModel):
    stations: list[str] | None = Field(default=None, max_length=100)
    roles: list[str] | None = Field(default=None, max_length=3)
    screen: str

    @field_validator("screen")
    @classmethod
    def _screen(cls, value: str) -> str:
        if value not in SCREENS:
            raise ValueError(f"screen must be one of {', '.join(SCREENS)}")
        return value

    @model_validator(mode="after")
    def _roles_valid(self) -> "ScreenRequest":
        for role in self.roles or []:
            _check_role(role)
        return self


class SendResult(BaseModel):
    stations: list[str]


class InboundMessage(BaseModel):
    text: str = Field(..., min_length=1, max_length=500)

    @field_validator("text")
    @classmethod
    def _strip(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("text must not be blank")
        return value


class StationMessageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    station_slug: str
    text: str
    acknowledged: bool
    created_at: datetime


class PublicStation(BaseModel):
    slug: str
    name: str
    role: str
    screens: list[str]
    screen: str
    last_message: dict | None = None
    #: The running timer cue, if any, and the server clock to sync it against.
    timer: "TimerState | None" = None
    server_time: float = 0.0


class NextMatch(BaseModel):
    available: bool = False
    label: str | None = None
    division: str | None = None
    scheduled_at: float | None = None
    red: list[dict] = []
    blue: list[dict] = []


class LinkResult(BaseModel):
    """Returned once when a station link is (re)generated; the key is only
    ever shown to `comms:manage` users."""

    access_key: str
    path: str


class PinRequest(BaseModel):
    #: Digits only, 4-8 long; null removes the PIN.
    pin: str | None = Field(default=None, min_length=4, max_length=8, pattern=r"^\d+$")


class UnlockRequest(BaseModel):
    pin: str = Field(..., min_length=1, max_length=20)


class UnlockResult(BaseModel):
    token: str
    expires_in: int


class TimerRequest(BaseModel):
    """Stagetimer-style cue pushed to stations. `start` needs a mode (and a
    duration for countdowns); the other actions act on the running timer."""

    stations: list[str] | None = Field(default=None, max_length=100)
    roles: list[str] | None = Field(default=None, max_length=3)
    action: str
    mode: str = "countdown"
    duration_seconds: int | None = Field(default=None, ge=1, le=86400)
    label: str = Field(default="", max_length=60)
    #: Countdown colours: amber at/below `warn`, red at/below `critical`.
    warn_seconds: int = Field(default=120, ge=0, le=86400)
    critical_seconds: int = Field(default=30, ge=0, le=86400)
    #: For `adjust`: add (or, negative, remove) seconds on a countdown.
    adjust_seconds: int | None = Field(default=None, ge=-3600, le=3600)

    @model_validator(mode="after")
    def _check(self) -> "TimerRequest":
        if self.action not in ("start", "pause", "resume", "stop", "adjust"):
            raise ValueError("action must be start, pause, resume, stop or adjust")
        if self.mode not in ("countdown", "countup"):
            raise ValueError("mode must be countdown or countup")
        if self.action == "start" and self.mode == "countdown" and self.duration_seconds is None:
            raise ValueError("a countdown needs duration_seconds")
        if self.action == "adjust" and not self.adjust_seconds:
            raise ValueError("adjust needs a non-zero adjust_seconds")
        if self.critical_seconds > self.warn_seconds:
            raise ValueError("critical_seconds cannot exceed warn_seconds")
        return self


class TimerState(BaseModel):
    id: str
    mode: str
    label: str = ""
    duration: int | None = None
    warn: int = 120
    critical: int = 30
    #: Seconds already run before the current run started.
    elapsed_before: float = 0
    #: Epoch seconds the current run started; null while paused.
    running_since: float | None = None


PublicStation.model_rebuild()
