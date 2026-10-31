"""Only the authenticated `/ws/events` feed gets the broadcast-all firehose;
public pages (room, station, overlay, prompter) get their own bucket only.

Two layers: (1) wiring tests drive each real endpoint (DB/Redis lookups
stubbed) and check which sockets end up in the broadcast-all set; (2)
behaviour tests dispatch an internal event through a `ConnectionManager` and
check what each page type's socket receives.
"""
from __future__ import annotations

import types

import pytest
from starlette.testclient import TestClient

from backend.main import create_app
from backend.routers import ws as ws_mod
from backend.routers.ws import ConnectionManager


class FakeWs:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def accept(self) -> None:
        pass

    async def send_json(self, message: dict) -> None:
        self.sent.append(message)


INTERNAL = {
    "entity_id": "integration.tm",
    "type": "integration_status",
    "timestamp": 1.0,
    "payload": {"status": "connected", "host": "10.0.0.5"},
}


@pytest.fixture
def mgr() -> ConnectionManager:
    return ConnectionManager(redis=object())


async def test_connect_defaults_to_not_receiving_broadcast_all(mgr):
    public, events = FakeWs(), FakeWs()
    await mgr.connect("room:r1", public)
    await mgr.connect("events", events, receive_all=True)
    await mgr._dispatch(INTERNAL)
    assert public.sent == []
    assert [m["type"] for m in events.sent] == ["integration_status"]


@pytest.mark.parametrize(
    "bucket",
    ["room:r1", "station:emcee-1", "overlay.f1", "timer.main"],  # room, station, overlay, prompter
)
async def test_public_socket_never_sees_internal_events(mgr, bucket):
    public = FakeWs()
    await mgr.connect(bucket, public)
    for etype in ("integration_status", "match_started", "timer_state", "capture_command", "test_event"):
        await mgr._dispatch({**INTERNAL, "entity_id": "somewhere.else", "type": etype})
    assert public.sent == []


async def test_public_sockets_still_get_their_dedicated_messages(mgr):
    room, station, overlay = FakeWs(), FakeWs(), FakeWs()
    await mgr.connect("room:r1", room)
    await mgr.connect("station:emcee-1", station)
    await mgr.connect("overlay.f1", overlay)

    await mgr._dispatch(
        {"entity_id": "notifications", "type": "room_notice", "timestamp": 1.0,
         "payload": {"id": "n1", "rooms": ["r1"], "title": "T", "message": "M"}}
    )
    await mgr._dispatch(
        {"entity_id": "comms", "type": "station_message", "timestamp": 1.0,
         "payload": {"stations": ["emcee-1"], "id": "m1", "text": "Hi", "flash": False, "sent_at": 1.0}}
    )
    await mgr._dispatch(
        {"entity_id": "overlay.f1", "type": "overlay_show_team", "timestamp": 1.0, "payload": {"team": "1234A"}}
    )
    assert [m["type"] for m in room.sent] == ["notice"]
    assert [m["type"] for m in station.sent] == ["message"]
    assert [m["type"] for m in overlay.sent] == ["show_team"]


async def test_prompter_bucket_still_gets_its_own_timer_events(mgr):
    prompter, other = FakeWs(), FakeWs()
    mgr.get_timer_manager = lambda: types.SimpleNamespace(get_state=lambda _e: {"phase": "running"})
    await mgr.connect("timer.main", prompter)
    await mgr.connect("timer.other", other)
    await mgr._dispatch({"entity_id": "timer.main", "type": "timer_started", "timestamp": 1.0, "payload": {}})
    assert [m["type"] for m in prompter.sent] == ["timer_started"]
    assert prompter.sent[0]["payload"] == {"phase": "running"}
    assert other.sent == []


# ── wiring: which real endpoints join the broadcast-all set ────────────


class _Session:
    def __init__(self, row):
        self._row = row

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, *_a, **_k):
        return self._row

    async def execute(self, *_a, **_k):
        return types.SimpleNamespace(scalar_one_or_none=lambda: self._row)


@pytest.fixture
def stubbed(monkeypatch):
    row = types.SimpleNamespace(slug="x", access_key=None, tags=[], token_nonce="n")
    monkeypatch.setattr(ws_mod, "async_session_factory", lambda: _Session(row))
    monkeypatch.setattr(ws_mod.station_access, "check_access", lambda *_a, **_k: None)
    monkeypatch.setattr(ws_mod, "validate_prompter_token", lambda *_a, **_k: True)

    async def _noop(*_a, **_k):
        return None

    async def _cues(_eid):
        return {}

    monkeypatch.setattr(ws_mod.station_presence, "mark_connected", _noop)
    monkeypatch.setattr(ws_mod.station_presence, "touch", _noop)
    monkeypatch.setattr(ws_mod.station_presence, "mark_disconnected", _noop, raising=False)
    monkeypatch.setattr(ws_mod, "_cues_snapshot", _cues)
    monkeypatch.setattr(ws_mod, "_ws_is_authenticated", _authed)
    return row


async def _authed(_ws):
    return True


@pytest.mark.parametrize(
    ("url", "expect_all"),
    [
        ("/ws/events", True),
        ("/ws/room/r1", False),
        ("/ws/station/emcee-1", False),
        ("/ws/overlay/overlay.f1", False),
        ("/ws/prompter/timer.main?token=t", False),
    ],
)
def test_only_events_socket_joins_broadcast_all(stubbed, monkeypatch, url, expect_all):
    before = set(ws_mod.manager._all)
    with TestClient(create_app()) as client:
        # the lifespan wires the real timer-manager getter; stub it afterwards
        fake_timers = types.SimpleNamespace(get_state=lambda _e: {})
        monkeypatch.setattr(ws_mod.manager, "get_timer_manager", lambda: fake_timers)
        with client.websocket_connect(url) as ws:
            if url.startswith("/ws/prompter"):
                ws.receive_json()  # timer_state
                ws.receive_json()  # cues
            joined = set(ws_mod.manager._all) - before
            assert (len(joined) == 1) is expect_all
