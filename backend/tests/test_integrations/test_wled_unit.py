"""Unit tests for the WLED integration, against an in-process mock WLED
device (httpx.MockTransport implementing the bits of the JSON API we use)."""
from __future__ import annotations

import json
from typing import Any

import fakeredis.aioredis
import httpx
import pytest

from backend.core.exceptions import IntegrationError
from backend.modules.integrations.wled.integration import WledIntegration, parse_color

pytestmark = pytest.mark.asyncio


class MockWled:
    def __init__(self) -> None:
        self.state: dict[str, Any] = {"on": True, "bri": 120, "seg": [{"id": 0, "col": [[0, 0, 255]], "fx": 5}]}
        self.posts: list[dict[str, Any]] = []
        self.fail = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.fail:
            return httpx.Response(500)
        if request.url.path == "/json/info":
            return httpx.Response(200, json={"ver": "0.15"})
        if request.url.path == "/json/state":
            if request.method == "POST":
                body = json.loads(request.content)
                self.posts.append(body)
                self.state.update(body)
                return httpx.Response(200, json={"success": True})
            return httpx.Response(200, json=self.state)
        return httpx.Response(404)


@pytest.fixture
async def redis_client():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


@pytest.fixture
async def wled(redis_client):
    device = MockWled()
    integ = WledIntegration("wled-1", {"host": "wled.local"}, redis_client, None)
    integ._http = httpx.AsyncClient(base_url="http://wled.local:80", transport=httpx.MockTransport(device.handler))
    yield integ, device
    await integ.teardown()


def test_parse_color_variants():
    assert parse_color("#FF8000") == [255, 128, 0]
    assert parse_color("00ff00") == [0, 255, 0]
    assert parse_color([1, 2, 3]) == [1, 2, 3]
    assert parse_color({"r": 9, "g": 8, "b": 7}) == [9, 8, 7]


@pytest.mark.parametrize("bad", ["red", "#12345", [1, 2], [1, 2, 300], 5, None])
def test_parse_color_rejects_invalid(bad):
    with pytest.raises(ValueError):
        parse_color(bad)


async def test_setup_requires_host(redis_client):
    with pytest.raises(IntegrationError):
        await WledIntegration("w", {}, redis_client, None).setup()


async def test_setup_unreachable_raises(redis_client, monkeypatch):
    integ = WledIntegration("w", {"host": "127.0.0.1", "port": 1}, redis_client, None)
    with pytest.raises(IntegrationError):
        await integ.setup()
    assert integ._http is None


async def test_setup_connects_to_device(redis_client, monkeypatch):
    device = MockWled()
    real = httpx.AsyncClient

    def factory(**kw):
        return real(transport=httpx.MockTransport(device.handler), **kw)

    monkeypatch.setattr("backend.modules.integrations.wled.integration.httpx.AsyncClient", factory)
    integ = WledIntegration("w", {"host": "wled.local"}, redis_client, None)
    await integ.setup()
    assert (await integ.get_state())["connected"] is True
    await integ.teardown()
    assert (await integ.get_state())["connected"] is False


async def test_set_power_and_brightness(wled):
    integ, device = wled
    await integ.call_service("set_power", {"on": False})
    await integ.call_service("set_brightness", {"brightness": 200})
    assert device.posts == [{"on": False}, {"on": True, "bri": 200}]
    state = await integ.get_state()
    assert state["brightness"] == 200 and state["on"] is True


async def test_brightness_out_of_range(wled):
    integ, _ = wled
    with pytest.raises(ValueError):
        await integ.call_service("set_brightness", {"brightness": 300})
    with pytest.raises(ValueError):
        await integ.call_service("set_brightness", {})


async def test_set_color_preset_effect(wled):
    integ, device = wled
    await integ.call_service("set_color", {"color": "#FF0000"})
    await integ.call_service("set_preset", {"preset": 4})
    await integ.call_service("set_effect", {"effect": 9, "speed": 100, "intensity": 50})
    assert device.posts[0] == {"on": True, "seg": [{"col": [[255, 0, 0]]}]}
    assert device.posts[1] == {"ps": 4}
    assert device.posts[2] == {"on": True, "seg": [{"fx": 9, "sx": 100, "ix": 50}]}
    with pytest.raises(ValueError):
        await integ.call_service("set_preset", {"preset": 0})
    with pytest.raises(ValueError):
        await integ.call_service("set_color", {})


async def test_flash_blinks_then_restores(wled, monkeypatch):
    integ, device = wled
    monkeypatch.setattr("backend.modules.integrations.wled.integration.asyncio.sleep", _no_sleep)
    result = await integ.call_service("flash", {"color": "#FFFFFF", "count": 2, "interval_ms": 100})
    assert result == {"ok": True, "flashes": 2}
    # 2 x (on, off) then the restore of the original state.
    assert len(device.posts) == 5
    assert device.posts[0]["seg"][0]["col"] == [[255, 255, 255]]
    assert device.posts[1] == {"on": False}
    restore = device.posts[-1]
    assert restore["on"] is True and restore["bri"] == 120
    assert restore["seg"] == [{"id": 0, "col": [[0, 0, 255]], "fx": 5}]


async def _no_sleep(_):
    return None


async def test_flash_restores_after_failure(wled, monkeypatch):
    integ, device = wled
    monkeypatch.setattr("backend.modules.integrations.wled.integration.asyncio.sleep", _no_sleep)
    calls = {"n": 0}
    original = device.handler

    def flaky(request):
        if request.method == "POST":
            calls["n"] += 1
            if calls["n"] == 2:  # fail mid-flash; the restore POST must still be attempted
                return httpx.Response(500)
        return original(request)

    integ._http = httpx.AsyncClient(base_url="http://wled.local:80", transport=httpx.MockTransport(flaky))
    with pytest.raises(IntegrationError):
        await integ.call_service("flash", {"count": 3})
    assert calls["n"] >= 3


async def test_flash_validation(wled):
    integ, _ = wled
    for bad in ({"count": 0}, {"count": 999}, {"interval_ms": 1}, {"color": "nope"}):
        with pytest.raises(ValueError):
            await integ.call_service("flash", bad)


async def test_device_error_becomes_integration_error(wled):
    integ, device = wled
    device.fail = True
    with pytest.raises(IntegrationError):
        await integ.call_service("set_power", {"on": True})
    state = await integ.get_state()  # must never raise
    assert state["connected"] is True


async def test_unknown_service_and_not_connected(redis_client):
    integ = WledIntegration("w", {"host": "h"}, redis_client, None)
    with pytest.raises(ValueError):
        await integ.call_service("nope", {})
    with pytest.raises(IntegrationError):
        await integ.call_service("set_power", {"on": True})
