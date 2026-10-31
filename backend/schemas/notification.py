"""Pydantic schemas for push subscriptions and staff broadcasts."""
from __future__ import annotations

from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator, model_validator

from backend.schemas.room import SLUG_PATTERN

MAX_TEAMS_PER_SUBSCRIPTION = 30
_MAX_ENDPOINT_LEN = 2048

#: The server POSTs to whatever endpoint a (public, unauthenticated) client
#: registers, so only the browsers' real push services are accepted — this
#: stops the endpoint field being used to make the server call arbitrary URLs.
#: Chrome/Edge-on-Android/Brave (FCM), Firefox, Safari (macOS/iOS), Edge (WNS).
ALLOWED_PUSH_HOST_SUFFIXES = (
    ".googleapis.com",
    ".push.services.mozilla.com",
    ".push.apple.com",
    ".notify.windows.com",
)


class PushKeys(BaseModel):
    p256dh: str = Field(..., min_length=1, max_length=255)
    auth: str = Field(..., min_length=1, max_length=255)


class PushSubscriptionIn(BaseModel):
    endpoint: str = Field(..., max_length=_MAX_ENDPOINT_LEN)
    keys: PushKeys

    @field_validator("endpoint")
    @classmethod
    def _https_only(cls, v: str) -> str:
        parsed = urlparse(v)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not host:
            raise ValueError("endpoint must be an https:// push service URL")
        if not any(host.endswith(suffix) for suffix in ALLOWED_PUSH_HOST_SUFFIXES):
            raise ValueError("endpoint is not a recognised browser push service")
        return v


class TeamSubscribe(PushSubscriptionIn):
    room: str = Field(..., pattern=SLUG_PATTERN)
    teams: list[str] = Field(..., min_length=1, max_length=MAX_TEAMS_PER_SUBSCRIPTION)
    #: Alert categories to receive (`arrival`, `late`, `score`, `announcement`);
    #: omitted = all of them. Staff broadcasts are always delivered.
    categories: list[str] | None = Field(default=None, max_length=10)

    @field_validator("categories")
    @classmethod
    def _known_categories(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        allowed = ("arrival", "late", "score", "announcement")
        bad = [c for c in v if c not in allowed]
        if bad:
            raise ValueError(f"Unknown categories: {', '.join(bad)}")
        return sorted(set(v))

    @field_validator("teams")
    @classmethod
    def _normalise(cls, v: list[str]) -> list[str]:
        out: list[str] = []
        for t in v:
            t = t.strip().upper()
            if t and t not in out:
                out.append(t)
        if not out:
            raise ValueError("Select at least one team")
        return out


class StaffSubscribe(PushSubscriptionIn):
    categories: list[str] | None = Field(default=None, max_length=10)


class EndpointRef(BaseModel):
    endpoint: str = Field(..., max_length=_MAX_ENDPOINT_LEN)


class SubscriptionStatus(BaseModel):
    subscribed: bool
    room: str | None = None
    teams: list[str] = Field(default_factory=list)
    #: Categories the subscriber receives (null = all).
    categories: list[str] | None = None


class VapidKey(BaseModel):
    public_key: str


class BroadcastRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=100)
    message: str = Field(..., min_length=1, max_length=500)
    #: None = every room (and every team subscriber).
    rooms: list[str] | None = Field(default=None, max_length=100)
    push: bool = True
    modal: bool = True


class BroadcastResult(BaseModel):
    rooms: int
    pushed: int


class NotificationStats(BaseModel):
    push_configured: bool
    team_subscriptions: int
    staff_subscriptions: int
    subscriptions_by_room: dict[str, int]


class TeamAlertRequest(BaseModel):
    """Staff alert addressed to specific teams (issue #44)."""

    teams: list[str] = Field(..., min_length=1, max_length=MAX_TEAMS_PER_SUBSCRIPTION)
    reason: Literal["pit_admin", "lost_and_found", "custom"]
    #: Required for `custom`; for the presets it is an optional extra line.
    message: str | None = Field(default=None, max_length=300)
    push: bool = True

    @field_validator("teams")
    @classmethod
    def _normalise(cls, v: list[str]) -> list[str]:
        out: list[str] = []
        for t in v:
            t = t.strip().upper()
            if t and t not in out:
                out.append(t)
        if not out:
            raise ValueError("Select at least one team")
        return out

    @field_validator("message")
    @classmethod
    def _strip(cls, v: str | None) -> str | None:
        return (v or "").strip() or None

    @model_validator(mode="after")
    def _custom_needs_message(self) -> "TeamAlertRequest":
        if self.reason == "custom" and not self.message:
            raise ValueError("A message is required for a custom alert")
        return self


class TeamAlertResult(BaseModel):
    rooms: int
    pushed: int
    #: Teams that no room display and no phone subscription could reach, so
    #: staff know to find them another way.
    unreached_teams: list[str]
