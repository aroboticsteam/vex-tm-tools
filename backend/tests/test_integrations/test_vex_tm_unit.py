"""Unit tests for the vex_tm integration client (fakeredis + mocked httpx/WS).

Exercises the plan §3.7 fixes in isolation: HMAC signing, host-header port
handling, Redis token caching, and service-command dispatch — without any
real network I/O.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import fakeredis.aioredis
import pytest

from backend.modules.integrations.vex_tm.integration import (
    VexTmIntegration,
    _host_header,
    _sign,
    _string_to_sign,
)

def _make_config(**overrides):
    config = {
        "base_url": "http://tm.example.com:8080",
        "client_id": "cid",
        "client_secret": "csecret",
        "api_key": "  api-key-with-whitespace  ",
        "field_set_id": 1,
        "poll_interval_seconds": 300,
        "tags": ["fs1"],
    }
    config.update(overrides)
    return config


@pytest.fixture
async def redis_client():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


def test_host_header_standard_ports_omit_port():
    assert _host_header("tm.example.com", "https", 443) == "tm.example.com"
    assert _host_header("tm.example.com", "http", 80) == "tm.example.com"


def test_host_header_nonstandard_port_included():
    assert _host_header("tm.example.com", "https", 8443) == "tm.example.com:8443"
    assert _host_header("tm.example.com", "http", 8080) == "tm.example.com:8080"


def test_host_header_no_port():
    assert _host_header("tm.example.com", "https", None) == "tm.example.com"


def test_string_to_sign_includes_query_string():
    sts = _string_to_sign("GET", "/api/matches/1?round=QUAL", "tok", "host", "date")
    assert "/api/matches/1?round=QUAL" in sts
    assert sts.startswith("GET\n")


def test_sign_is_deterministic_hmac_sha256():
    sts = _string_to_sign("GET", "/api/divisions", "tok", "host", "date")
    sig1 = _sign("my-key", sts)
    sig2 = _sign("my-key", sts)
    assert sig1 == sig2
    assert len(sig1) == 64  # hex-encoded SHA-256


async def test_setup_strips_api_key_whitespace(redis_client):
    integration = VexTmIntegration("vex_tm.test", _make_config(), redis_client, None)
    integration._get_token = AsyncMock(return_value="tok")  # avoid real network
    integration._receive_loop = AsyncMock()
    integration._schedule_poll_loop = AsyncMock()

    # Patch out the background tasks so setup() completes instantly.
    import asyncio

    async def _noop():
        await asyncio.sleep(3600)

    integration._receive_loop = _noop
    integration._schedule_poll_loop = _noop

    await integration.setup()
    try:
        assert integration.config["api_key"] == "api-key-with-whitespace"
    finally:
        await integration.teardown()


async def test_token_cached_in_redis_avoids_refetch(redis_client):
    integration = VexTmIntegration("vex_tm.test", _make_config(), redis_client, None)
    await redis_client.set("qecomp:tm:vex_tm.test:token", "cached-token")

    integration._fetch_token = AsyncMock(side_effect=AssertionError("should not refetch"))
    token = await integration._get_token()
    assert token == "cached-token"


async def test_fetch_token_success_caches_in_redis(redis_client):
    integration = VexTmIntegration("vex_tm.test", _make_config(), redis_client, None)
    integration._auth_http = AsyncMock()
    response = MagicMock()
    response.status_code = 200
    response.headers = {}
    response.json.return_value = {"access_token": "new-token", "expires_in": 3600}
    response.raise_for_status = MagicMock()
    integration._auth_http.post = AsyncMock(return_value=response)

    token = await integration._fetch_token(max_attempts=1)
    assert token == "new-token"
    cached = await redis_client.get("qecomp:tm:vex_tm.test:token")
    assert cached == "new-token"


async def test_fetch_token_rate_limited_then_gives_up(redis_client, monkeypatch):
    integration = VexTmIntegration("vex_tm.test", _make_config(), redis_client, None)
    integration._auth_http = AsyncMock()
    response = MagicMock()
    response.status_code = 429
    response.headers = {"Retry-After": "0"}
    integration._auth_http.post = AsyncMock(return_value=response)

    async def fast_sleep(_seconds):
        return None

    monkeypatch.setattr("asyncio.sleep", fast_sleep)

    from backend.core.exceptions import IntegrationError

    with pytest.raises(IntegrationError):
        await integration._fetch_token(max_attempts=2)
    assert integration._auth_http.post.call_count == 2


async def test_call_service_unknown_raises_value_error(redis_client):
    integration = VexTmIntegration("vex_tm.test", _make_config(), redis_client, None)
    integration._ws = AsyncMock()
    with pytest.raises(ValueError):
        await integration.call_service("not_a_real_service", {})


async def test_call_service_without_ws_raises_integration_error(redis_client):
    from backend.core.exceptions import IntegrationError

    integration = VexTmIntegration("vex_tm.test", _make_config(), redis_client, None)
    integration._ws = None
    with pytest.raises(IntegrationError):
        await integration.call_service("start_match", {})


@pytest.mark.parametrize(
    "service,data,expected",
    [
        ("start_match", {}, {"cmd": "start"}),
        ("end_early", {}, {"cmd": "endEarly"}),
        ("abort", {}, {"cmd": "abort"}),
        ("reset", {}, {"cmd": "reset"}),
        ("queue_next_match", {}, {"cmd": "queueNextMatch"}),
        ("queue_prev_match", {}, {"cmd": "queuePrevMatch"}),
        ("set_audience_display", {"display": "SCORE"}, {"cmd": "setAudienceDisplay", "display": "SCORE"}),
        ("queue_skills", {"skills_id": 7}, {"cmd": "queueSkills", "skillsID": 7}),
    ],
)
async def test_call_service_sends_expected_command(redis_client, service, data, expected):
    integration = VexTmIntegration("vex_tm.test", _make_config(), redis_client, None)
    integration._ws = AsyncMock()

    result = await integration.call_service(service, data)

    integration._ws.send.assert_awaited_once()
    (sent_raw,) = integration._ws.send.call_args.args
    assert json.loads(sent_raw) == expected
    assert result == {"sent": expected}


async def test_handle_ws_message_publishes_event_bus_message(redis_client):
    integration = VexTmIntegration("vex_tm.test", _make_config(tags=["fs1", "division_1"]), redis_client, None)

    published = []

    async def fake_publish(channel, message):
        published.append((channel, message))

    integration._redis.publish = fake_publish

    payload = {"type": "matchStarted", "fieldID": 1, "matchNum": 5, "round": "QUAL"}
    await integration._handle_ws_message(json.dumps(payload))

    assert len(published) == 1
    channel, message_json = published[0]
    assert channel == "qecomp:events"
    message = json.loads(message_json)
    assert message["entity_id"] == "vex_tm.test"
    assert message["entity_tags"] == ["fs1", "division_1"]
    assert message["type"] == "matchStarted"
    assert message["payload"] == payload


async def test_handle_ws_message_ignores_invalid_json(redis_client):
    integration = VexTmIntegration("vex_tm.test", _make_config(), redis_client, None)
    integration._redis.publish = AsyncMock(side_effect=AssertionError("should not publish"))
    await integration._handle_ws_message("not json")  # must not raise


async def test_schedule_fetch_caches_in_redis(redis_client):
    integration = VexTmIntegration("vex_tm.test", _make_config(), redis_client, None)

    async def fake_get(endpoint, params=None):
        if endpoint == "/api/divisions":
            return {"divisions": [{"id": 1, "name": "Division 1"}]}
        if endpoint == "/api/matches/1":
            return {"matches": [{"matchNum": 1}]}
        raise AssertionError(f"unexpected endpoint {endpoint}")

    integration._get = fake_get
    await integration._fetch_and_cache_schedule()

    cached = await redis_client.get("qecomp:tm:vex_tm.test:schedule")
    assert cached is not None
    schedule = json.loads(cached)
    assert schedule["divisions"][0]["id"] == 1
    assert schedule["divisions"][0]["matches"] == [{"matchNum": 1}]


async def test_get_state_never_raises_on_redis_failure(redis_client):
    integration = VexTmIntegration("vex_tm.test", _make_config(), redis_client, None)

    async def boom(*_args, **_kwargs):
        raise RuntimeError("redis down")

    integration._redis.get = boom
    state = await integration.get_state()
    assert state["status"] == "UNKNOWN"
    assert state["connected"] is False


async def test_teardown_never_raises_even_if_ws_close_fails(redis_client):
    integration = VexTmIntegration("vex_tm.test", _make_config(), redis_client, None)
    integration._tasks = []
    bad_ws = AsyncMock()
    bad_ws.close = AsyncMock(side_effect=RuntimeError("boom"))
    integration._ws = bad_ws
    await integration.teardown()  # must not raise
    assert integration._ws is None


async def test_match_stopped_triggers_post_match_schedule_refreshes(redis_client, monkeypatch):
    """Final scores land in `/api/matches` shortly after `matchStopped`, so the
    integration re-fetches the schedule a few times instead of waiting for the
    next poll (the notification service reads that cache for score alerts)."""
    import asyncio

    from backend.modules.integrations.vex_tm import integration as mod

    monkeypatch.setattr(mod, "POST_MATCH_REFRESH_DELAYS", (0, 0, 0))
    integration = VexTmIntegration("vex_tm.test", _make_config(), redis_client, None)
    integration._fetch_and_cache_schedule = AsyncMock()

    await integration._handle_ws_message(json.dumps({"type": "matchStopped", "fieldID": 1}))
    await integration._handle_ws_message(json.dumps({"type": "fieldActivated", "fieldID": 1}))  # no refresh
    await integration._refresh_task

    assert integration._fetch_and_cache_schedule.await_count == 3

    # A newer stop supersedes an in-flight sequence and teardown cancels it.
    monkeypatch.setattr(mod, "POST_MATCH_REFRESH_DELAYS", (60,))
    await integration._handle_ws_message(json.dumps({"type": "matchStopped", "fieldID": 1}))
    task = integration._refresh_task
    await integration.teardown()
    await asyncio.sleep(0)
    assert task.cancelled() or task.done()
    assert integration._refresh_task is None


async def test_default_poll_interval_is_one_minute(redis_client):
    from backend.modules.integrations.vex_tm.integration import DEFAULT_POLL_INTERVAL_SECONDS

    assert DEFAULT_POLL_INTERVAL_SECONDS == 60


@pytest.mark.parametrize("service", ["set_audience_display", "queue_skills"])
async def test_call_service_missing_field_is_value_error(redis_client, service):
    integration = VexTmIntegration("vex_tm.test", _make_config(), redis_client, None)
    integration._ws = AsyncMock()
    with pytest.raises(ValueError):
        await integration.call_service(service, {})
    integration._ws.send.assert_not_awaited()
