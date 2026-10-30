"""Unit tests for the Art-Net integration, using a real local UDP listener
for wire-level checks."""
from __future__ import annotations

import asyncio

import fakeredis.aioredis
import pytest

from backend.core.exceptions import IntegrationError
from backend.modules.integrations.artnet.integration import (
    ArtNetIntegration,
    build_artdmx,
)

pytestmark = pytest.mark.asyncio
PORT = 18746


class _Listener(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.packets: list[bytes] = []

    def datagram_received(self, data: bytes, addr) -> None:
        self.packets.append(data)


@pytest.fixture
async def redis_client():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


@pytest.fixture
async def listener():
    transport, protocol = await asyncio.get_running_loop().create_datagram_endpoint(
        _Listener, local_addr=("127.0.0.1", PORT)
    )
    yield protocol
    transport.close()


async def _make(redis_client, **cfg):
    integ = ArtNetIntegration("a", {"ip": "127.0.0.1", "port": PORT, **cfg}, redis_client, None)
    await integ.setup()
    return integ


async def _wait(listener, n, timeout=2.0):
    for _ in range(int(timeout / 0.01)):
        if len(listener.packets) >= n:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"expected {n} packets, got {len(listener.packets)}")


def _dmx(packet: bytes) -> bytes:
    return packet[18:]


async def test_build_artdmx_layout():
    pkt = build_artdmx(7, 0x1234, bytes([1, 2, 3]))
    assert pkt[:8] == b"Art-Net\x00"
    assert pkt[8:10] == b"\x00\x50"  # OpDmx, little endian
    assert pkt[10:12] == b"\x00\x0e"  # protocol version 14
    assert pkt[12] == 7  # sequence
    assert pkt[14:16] == b"\x34\x12"  # universe, little endian
    assert pkt[16:18] == b"\x00\x04"  # length padded to even, big endian
    assert pkt[18:] == bytes([1, 2, 3, 0])


async def test_build_artdmx_rejects_too_long_and_pads_short():
    with pytest.raises(ValueError):
        build_artdmx(1, 0, bytes(513))
    assert build_artdmx(1, 0, b"")[16:18] == b"\x00\x02"


async def test_setup_validates_config(redis_client):
    with pytest.raises(IntegrationError):
        await ArtNetIntegration("a", {}, redis_client, None).setup()
    with pytest.raises(IntegrationError):
        await ArtNetIntegration("a", {"ip": "127.0.0.1", "universe": 40000}, redis_client, None).setup()
    with pytest.raises(IntegrationError):
        await ArtNetIntegration("a", {"ip": "127.0.0.1", "refresh_hz": 100}, redis_client, None).setup()


async def test_setup_sends_initial_frame_and_universe(redis_client, listener):
    integ = await _make(redis_client, universe=3)
    await _wait(listener, 1)
    assert listener.packets[0][14:16] == b"\x03\x00"
    assert _dmx(listener.packets[0]) == bytes(512)
    await integ.teardown()


async def test_set_slider_level_and_percent(redis_client, listener):
    integ = await _make(redis_client)
    assert await integ.call_service("set_slider", {"channel": 5, "level": 200}) == {"ok": True, "channel": 5, "level": 200}
    await _wait(listener, 2)
    assert _dmx(listener.packets[-1])[4] == 200
    result = await integ.call_service("set_slider", {"channel": 6, "percent": 50})
    assert result["level"] == 128
    state = await integ.get_state()
    assert state["active_channels"] == {5: 200, 6: 128}
    assert state["connected"] is True
    await integ.teardown()
    assert (await integ.get_state())["connected"] is False


async def test_set_slider_validation(redis_client, listener):
    integ = await _make(redis_client)
    for bad in (
        {"channel": 0, "level": 1},
        {"channel": 513, "level": 1},
        {"channel": True, "level": 1},
        {"channel": 1},
        {"channel": 1, "level": 1, "percent": 1},
        {"channel": 1, "level": 256},
        {"channel": 1, "level": -1},
        {"channel": 1, "percent": 101},
        {"channel": 1, "level": "5"},
    ):
        with pytest.raises(ValueError):
            await integ.call_service("set_slider", bad)
    await integ.teardown()


async def test_set_channels_run(redis_client, listener):
    integ = await _make(redis_client)
    result = await integ.call_service("set_channels", {"start": 10, "values": [1, 2, 3]})
    assert result == {"ok": True, "start": 10, "count": 3}
    await _wait(listener, 2)
    assert _dmx(listener.packets[-1])[9:12] == bytes([1, 2, 3])
    for bad in (
        {"start": 511, "values": [1, 2, 3]},
        {"start": 1, "values": []},
        {"start": 1, "values": [256]},
        {"start": 1, "values": ["a"]},
        {"start": 1, "values": [True]},
        {"start": 1},
    ):
        with pytest.raises(ValueError):
            await integ.call_service("set_channels", bad)
    await integ.teardown()


async def test_blackout_clears_everything_and_cancels_fades(redis_client, listener):
    integ = await _make(redis_client)
    await integ.call_service("set_channels", {"start": 1, "values": [255, 255]})
    await integ.call_service("fade", {"channel": 3, "level": 255, "duration_ms": 5000})
    assert (await integ.get_state())["fading"] == [3]
    await integ.call_service("blackout", {})
    assert (await integ.get_state())["fading"] == []
    assert (await integ.get_state())["active_channels"] == {}
    await asyncio.sleep(0.1)
    assert _dmx(listener.packets[-1]) == bytes(512)
    await integ.teardown()


async def test_fade_reaches_target_and_is_monotonic(redis_client, listener):
    integ = await _make(redis_client)
    await integ.call_service("fade", {"channel": 1, "level": 100, "duration_ms": 200})
    await asyncio.sleep(0.4)
    values = [_dmx(p)[0] for p in listener.packets]
    assert values[-1] == 100
    assert values == sorted(values)
    assert len(set(values)) > 3  # intermediate steps were sent
    assert (await integ.get_state())["fading"] == []
    await integ.teardown()


async def test_fade_zero_duration_is_immediate_and_new_fade_replaces_old(redis_client, listener):
    integ = await _make(redis_client)
    assert (await integ.call_service("fade", {"channel": 2, "level": 50, "duration_ms": 0}))["level"] == 50
    await integ.call_service("fade", {"channel": 2, "level": 255, "duration_ms": 10000})
    await integ.call_service("fade", {"channel": 2, "level": 0, "duration_ms": 100})
    await asyncio.sleep(0.3)
    assert _dmx(listener.packets[-1])[1] == 0
    with pytest.raises(ValueError):
        await integ.call_service("fade", {"channel": 2, "level": 1, "duration_ms": -1})
    with pytest.raises(ValueError):
        await integ.call_service("fade", {"channel": 2, "level": 1})
    await integ.teardown()


async def test_manual_set_cancels_running_fade(redis_client, listener):
    integ = await _make(redis_client)
    await integ.call_service("fade", {"channel": 4, "level": 255, "duration_ms": 10000})
    await integ.call_service("set_slider", {"channel": 4, "level": 7})
    await asyncio.sleep(0.15)
    assert _dmx(listener.packets[-1])[3] == 7
    assert (await integ.get_state())["fading"] == []
    await integ.teardown()


async def test_keepalive_resends_frames_with_rising_sequence(redis_client, listener):
    integ = await _make(redis_client, refresh_hz=20)
    await _wait(listener, 4)
    seqs = [p[12] for p in listener.packets[:4]]
    assert seqs == [1, 2, 3, 4]
    await integ.teardown()
    count = len(listener.packets)
    await asyncio.sleep(0.15)
    assert len(listener.packets) == count  # no frames after teardown


async def test_sequence_wraps_without_hitting_zero(redis_client, listener):
    integ = await _make(redis_client)
    integ._sequence = 254
    for _ in range(3):
        integ._send_frame()
    await _wait(listener, 4)
    tail = [p[12] for p in listener.packets[-3:]]
    assert tail == [255, 1, 2]
    await integ.teardown()


async def test_unknown_service_and_not_connected(redis_client):
    integ = ArtNetIntegration("a", {"ip": "127.0.0.1", "port": PORT}, redis_client, None)
    with pytest.raises(ValueError):
        await integ.call_service("nope", {})
    with pytest.raises(IntegrationError):
        await integ.call_service("blackout", {})
    await integ.teardown()  # safe before setup


async def test_every_integration_domain_has_a_service_permission():
    from backend.loader import INTEGRATIONS_DIR
    from backend.routers.integrations import _DOMAIN_PERMISSIONS

    domains = {p.parent.name for p in INTEGRATIONS_DIR.glob("*/manifest.yaml")}
    assert "artnet" in domains
    assert domains <= set(_DOMAIN_PERMISSIONS)
    assert _DOMAIN_PERMISSIONS["artnet"] == "vfx:control"


async def test_service_schema_rejects_missing_required_fields():
    from backend.core.service_validation import ServiceValidationError, validate_service_data

    validate_service_data("artnet", "blackout", {})
    validate_service_data("artnet", "set_slider", {"channel": 1, "level": 5})
    with pytest.raises(ServiceValidationError):
        validate_service_data("artnet", "set_slider", {"level": 5})
    with pytest.raises(ServiceValidationError):
        validate_service_data("artnet", "fade", {"channel": 1, "level": 5})
