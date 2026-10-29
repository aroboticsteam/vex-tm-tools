"""Validation for room + push-subscription schemas."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.schemas.notification import StaffSubscribe, TeamSubscribe
from backend.schemas.room import RoomCreate, normalize_stream_url

KEYS = {"p256dh": "k", "auth": "a"}


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "https://www.youtube.com/embed/dQw4w9WgXcQ"),
        ("https://youtu.be/dQw4w9WgXcQ?t=5", "https://www.youtube.com/embed/dQw4w9WgXcQ"),
        ("https://www.youtube.com/live/dQw4w9WgXcQ", "https://www.youtube.com/embed/dQw4w9WgXcQ"),
        ("https://www.youtube.com/embed/dQw4w9WgXcQ", "https://www.youtube.com/embed/dQw4w9WgXcQ"),
        ("https://player.twitch.tv/?channel=x&parent=example.org", "https://player.twitch.tv/?channel=x&parent=example.org"),
        ("  ", None),
        (None, None),
    ],
)
def test_normalize_stream_url(raw, expected):
    assert normalize_stream_url(raw) == expected


@pytest.mark.parametrize("bad", ["javascript:alert(1)", "ftp://x/y", "not a url", "https://www.youtube.com/watch?v=<script>"])
def test_normalize_stream_url_rejects(bad):
    with pytest.raises(ValueError):
        normalize_stream_url(bad)


def test_room_create_cleans_teams_and_validates_slug():
    room = RoomCreate(slug="room-1", name="Room 1", teams=[" 10d ", "10D", "", "99a"])
    assert room.teams == ["10D", "99A"]
    for bad_slug in ["Room 1", "-x", "a/b", "x" * 51, ""]:
        with pytest.raises(ValidationError):
            RoomCreate(slug=bad_slug, name="n")


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://fcm.googleapis.com/fcm/send/abc",
        "https://updates.push.services.mozilla.com/wpush/v2/abc",
        "https://web.push.apple.com/abc",
        "https://wns2-par02p.notify.windows.com/w/?token=abc",
    ],
)
def test_known_push_services_accepted(endpoint):
    sub = TeamSubscribe(endpoint=endpoint, keys=KEYS, room="r", teams=["1a", "1A"])
    assert sub.teams == ["1A"]


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://fcm.googleapis.com/x",
        "https://evil.example.com/hook",
        "https://googleapis.com.evil.example/x",
        "https://169.254.169.254/latest/meta-data",
        "https://localhost/x",
    ],
)
def test_other_endpoints_rejected_to_prevent_ssrf(endpoint):
    with pytest.raises(ValidationError):
        StaffSubscribe(endpoint=endpoint, keys=KEYS)


def test_team_subscribe_requires_teams():
    with pytest.raises(ValidationError):
        TeamSubscribe(endpoint="https://fcm.googleapis.com/x", keys=KEYS, room="r", teams=[])
    with pytest.raises(ValidationError):
        TeamSubscribe(endpoint="https://fcm.googleapis.com/x", keys=KEYS, room="r", teams=["  "])
