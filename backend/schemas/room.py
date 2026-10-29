"""Pydantic schemas for rooms (admin CRUD + the public room page)."""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator

SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]{0,49}$"
MAX_TEAMS_PER_ROOM = 300

_YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}
_YOUTUBE_ID = re.compile(r"^[A-Za-z0-9_-]{6,20}$")


def normalize_stream_url(value: str | None) -> str | None:
    """Validates a livestream URL for iframe embedding and turns YouTube
    watch/live/short links into their `/embed/` form. Only http(s) is allowed
    (this value ends up in an `<iframe src>` on a public page)."""
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("stream_url must be an http(s) URL")
    host = (parsed.hostname or "").lower()
    video_id: str | None = None
    if host in _YOUTUBE_HOSTS:
        path = parsed.path.rstrip("/")
        if path == "/watch":
            video_id = (parse_qs(parsed.query).get("v") or [None])[0]
        elif path.startswith(("/live/", "/shorts/")):
            video_id = path.split("/", 2)[2]
        elif path.startswith("/embed/"):
            return value
    elif host == "youtu.be":
        video_id = parsed.path.lstrip("/").split("/")[0] or None
    if video_id:
        if not _YOUTUBE_ID.match(video_id):
            raise ValueError("Unrecognised YouTube video id")
        return f"https://www.youtube.com/embed/{video_id}"
    return value


def _clean_teams(teams: list[str]) -> list[str]:
    cleaned: list[str] = []
    for team in teams:
        t = team.strip().upper()
        if not t:
            continue
        if len(t) > 20:
            raise ValueError(f"Team number too long: {t[:21]!r}")
        if t not in cleaned:
            cleaned.append(t)
    return cleaned


class RoomBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    stream_url: str | None = Field(default=None, max_length=2000)
    teams: list[str] = Field(default_factory=list, max_length=MAX_TEAMS_PER_ROOM)

    @field_validator("stream_url")
    @classmethod
    def _check_stream_url(cls, v: str | None) -> str | None:
        return normalize_stream_url(v)

    @field_validator("teams")
    @classmethod
    def _check_teams(cls, v: list[str]) -> list[str]:
        return _clean_teams(v)


class RoomCreate(RoomBase):
    slug: str = Field(..., pattern=SLUG_PATTERN)


class RoomUpdate(RoomBase):
    pass


class RoomRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    slug: str
    name: str
    stream_url: str | None = None
    teams: list[str] = Field(default_factory=list)


class PublicRoom(BaseModel):
    """What the unauthenticated room/notify pages may see."""

    slug: str
    name: str
    stream_url: str | None = None
    teams: list[str] = Field(default_factory=list)
