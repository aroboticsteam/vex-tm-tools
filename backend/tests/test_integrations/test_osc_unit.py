"""Unit tests for the generic OSC integration, using a real local OSC UDP
server for a wire-level check."""
from __future__ import annotations

import asyncio

import fakeredis.aioredis
import pytest
from pythonosc.dispatcher import Dispatcher
from pythonosc.osc_server import AsyncIOOSCUDPServer

from backend.core.exceptions import IntegrationError
from backend.modules.integrations.osc.integration import OscIntegration

pytestmark = pytest.mark.asyncio
PORT = 18741


@pytest.fixture
async def redis_client():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


@pytest.fixture
async def server():
    received: list[tuple[str, tuple]] = []
    dispatcher = Dispatcher()
    dispatcher.set_default_handler(lambda addr, *args: received.append((addr, args)))
    srv = AsyncIOOSCUDPServer(("127.0.0.1", PORT), dispatcher, asyncio.get_running_loop())
    transport, _ = await srv.create_serve_endpoint()
    yield received
    transport.close()


async def _wait(received, n):
    for _ in range(100):
        if len(received) >= n:
            return
        await asyncio.sleep(0.02)


async def test_setup_requires_ip(redis_client):
    with pytest.raises(IntegrationError):
        await OscIntegration("o", {}, redis_client, None).setup()


async def test_send_message_on_the_wire(redis_client, server):
    integ = OscIntegration("o", {"ip": "127.0.0.1", "port": PORT}, redis_client, None)
    await integ.setup()
    result = await integ.call_service("send", {"address": "/cue/1/go", "args": [1, 2.5, "x", True]})
    assert result == {"ok": True, "sent": 1, "address": "/cue/1/go"}
    await _wait(server, 1)
    assert server[0][0] == "/cue/1/go"
    assert server[0][1][:3] == (1, 2.5, "x")
    assert (await integ.get_state())["last_message"]["address"] == "/cue/1/go"
    await integ.teardown()
    assert (await integ.get_state())["connected"] is False


async def test_send_without_args_and_scalar_arg(redis_client, server):
    integ = OscIntegration("o", {"ip": "127.0.0.1", "port": PORT}, redis_client, None)
    await integ.setup()
    await integ.call_service("send", {"address": "/go"})
    await integ.call_service("send", {"address": "/level", "args": 7})
    await _wait(server, 2)
    assert server[0] == ("/go", ())
    assert server[1] == ("/level", (7,))


async def test_send_bundle_in_order(redis_client, server):
    integ = OscIntegration("o", {"ip": "127.0.0.1", "port": PORT}, redis_client, None)
    await integ.setup()
    result = await integ.call_service(
        "send_bundle", {"messages": [{"address": "/a", "args": [1]}, {"address": "/b"}]}
    )
    assert result == {"ok": True, "sent": 2}
    await _wait(server, 2)
    assert [m[0] for m in server] == ["/a", "/b"]


@pytest.mark.parametrize(
    "service,data",
    [
        ("send", {}),
        ("send", {"address": "noslash"}),
        ("send", {"address": "/x", "args": [{"a": 1}]}),
        ("send_bundle", {"messages": []}),
        ("send_bundle", {"messages": ["bad"]}),
        ("send_bundle", {"messages": [{"address": "/ok"}, {"address": "bad"}]}),
        ("nope", {}),
    ],
)
async def test_invalid_input(redis_client, service, data):
    integ = OscIntegration("o", {"ip": "127.0.0.1", "port": PORT}, redis_client, None)
    await integ.setup()
    with pytest.raises(ValueError):
        await integ.call_service(service, data)
    assert integ._last_message is None  # a bad bundle sends nothing


async def test_not_connected(redis_client):
    with pytest.raises(IntegrationError):
        await OscIntegration("o", {"ip": "1.1.1.1"}, redis_client, None).call_service("send", {"address": "/x"})
