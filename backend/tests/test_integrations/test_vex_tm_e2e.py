"""Real end-to-end test: the actual async `vex_tm` Integration class talking
wire-level HTTP + WebSocket to `backend.tests.mock_tm_server` (Wave 2a item 4).

Confirms:
- OAuth2 client-credentials auth succeeds against the mock server.
- The WebSocket connects with a genuinely-verified HMAC signature.
- An event pushed by the mock server is received and republished as a
  correctly-shaped `EventBusMessage` on `qecomp:events` (fakeredis pub/sub).
- `call_service(...)` produces the correct outbound WS command, observed by
  the mock server's `received_commands` log.
"""
from __future__ import annotations

import asyncio
import json

import fakeredis.aioredis
import pytest
import uvicorn

from backend.modules.integrations.vex_tm.integration import VexTmIntegration
from backend.tests.mock_tm_server import create_app, push_event

pytestmark = pytest.mark.asyncio

HOST = "127.0.0.1"
PORT = 18181


@pytest.fixture
async def mock_server():
    app = create_app()
    config = uvicorn.Config(app, host=HOST, port=PORT, log_level="warning", lifespan="off")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(0.02)
    else:
        pytest.fail("mock TM server did not start in time")

    yield app

    server.should_exit = True
    await asyncio.wait_for(task, timeout=5)


@pytest.fixture
async def redis_client():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


@pytest.fixture
async def vex_tm_instance(mock_server, redis_client):
    config = {
        "base_url": f"http://{HOST}:{PORT}",
        "client_id": "test-client-id",
        "client_secret": "test-client-secret",
        "api_key": "test-api-key-for-hmac-signing",
        "field_set_id": 1,
        "poll_interval_seconds": 3600,  # keep schedule polling out of the way
        "auth_url": f"http://{HOST}:{PORT}/oauth2/token",
        "tags": ["fs1", "vex_tm"],
    }
    instance = VexTmIntegration("vex_tm.e2e_test", config, redis_client, None)
    await instance.setup()
    # Wait for the WS receive loop to actually establish a connection.
    for _ in range(100):
        if instance._ws is not None:
            break
        await asyncio.sleep(0.02)
    else:
        pytest.fail("vex_tm integration did not connect its WebSocket in time")

    yield instance

    await instance.teardown()


async def test_auth_succeeds_against_mock_server(vex_tm_instance, redis_client):
    token = await vex_tm_instance._get_token()
    assert token == "mock-bearer-token-abc123"
    cached = await redis_client.get("qecomp:tm:vex_tm.e2e_test:token")
    assert cached == "mock-bearer-token-abc123"


async def test_websocket_connects_with_valid_hmac_signature(vex_tm_instance):
    # If the mock server's signature check had failed, setup() would never
    # have observed a connected websocket (the fixture would have failed).
    assert vex_tm_instance._ws is not None


async def test_ws_event_from_mock_server_published_as_event_bus_message(
    mock_server, vex_tm_instance, redis_client
):
    pubsub = redis_client.pubsub()
    await pubsub.subscribe("qecomp:events")
    # Drain the subscribe confirmation message.
    await pubsub.get_message(timeout=1)

    fake_event = {
        "type": "matchStarted",
        "fieldID": 1,
        "matchNum": 5,
        "round": "QUAL",
        "redTeams": ["1234A"],
        "blueTeams": ["5678B"],
        "divisionId": 1,
    }
    sent_to = await push_event(mock_server, 1, fake_event)
    assert sent_to == 1

    message = None
    for _ in range(100):
        message = await pubsub.get_message(timeout=0.5)
        if message and message["type"] == "message":
            break
    else:
        pytest.fail("did not receive published EventBusMessage in time")

    assert message is not None
    body = json.loads(message["data"])
    assert body["entity_id"] == "vex_tm.e2e_test"
    assert body["entity_tags"] == ["fs1", "vex_tm"]
    assert body["type"] == "matchStarted"
    assert body["payload"] == fake_event
    assert isinstance(body["timestamp"], (int, float))

    await pubsub.unsubscribe("qecomp:events")
    await pubsub.aclose()


async def test_call_service_start_match_reaches_mock_server(mock_server, vex_tm_instance):
    await vex_tm_instance.call_service("start_match", {})
    await asyncio.sleep(0.1)  # let the mock server's receive loop process it
    assert {"cmd": "start"} in mock_server.state.received_commands


async def test_call_service_set_audience_display_reaches_mock_server(mock_server, vex_tm_instance):
    await vex_tm_instance.call_service("set_audience_display", {"display": "SCORE"})
    await asyncio.sleep(0.1)
    assert {"cmd": "setAudienceDisplay", "display": "SCORE"} in mock_server.state.received_commands


async def test_rest_get_against_mock_server_uses_correct_signature(vex_tm_instance):
    data = await vex_tm_instance._get("/api/divisions")
    assert data == {"divisions": [{"name": "Science", "id": 1}, {"name": "Technology", "id": 2}]}
