"""Unit tests for the PTZ (VISCA-over-IP) integration against a real local
UDP socket standing in for the camera."""
from __future__ import annotations

import asyncio
import socket
import struct

import fakeredis.aioredis
import pytest

from backend.core.exceptions import IntegrationError
from backend.modules.integrations.ptz import integration as ptz
from backend.modules.integrations.ptz.integration import PtzIntegration

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def redis_client():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


@pytest.fixture
def camera():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.setblocking(False)
    yield sock
    sock.close()


async def _recv(sock, n=1):
    loop = asyncio.get_running_loop()
    out = []
    for _ in range(n):
        out.append(await asyncio.wait_for(loop.sock_recv(sock, 1024), 2))
    return out


def _payload(packet: bytes) -> bytes:
    ptype, length, _seq = struct.unpack(">HHI", packet[:8])
    assert ptype == 0x0100 and length == len(packet) - 8
    return packet[8:]


async def _connected(redis_client, camera, **extra):
    cfg = {"host": "127.0.0.1", "port": camera.getsockname()[1], **extra}
    integ = PtzIntegration("ptz", cfg, redis_client, None)
    await integ.setup()
    (reset,) = await _recv(camera)  # sequence reset sent on connect
    assert struct.unpack(">HH", reset[:4]) == (0x0200, 1)
    return integ


def test_packet_builders():
    assert ptz.preset_command(1, 5, store=False) == bytes.fromhex("8101043f0205ff")
    assert ptz.preset_command(1, 5, store=True) == bytes.fromhex("8101043f0105ff")
    assert ptz.home_command(2) == bytes.fromhex("82010604ff")
    assert ptz.stop_command(1) == bytes.fromhex("8101060101010303ff")
    assert ptz.move_command(1, "left", 8) == bytes.fromhex("81010601080801 03ff".replace(" ", ""))
    assert ptz.move_command(1, "up_right", 24) == bytes.fromhex("8101060118140201ff")
    assert ptz.zoom_command(1, "in") == bytes.fromhex("81010407 02ff".replace(" ", ""))
    assert ptz.zoom_command(1, "out", 3) == bytes.fromhex("8101040733ff")
    assert ptz.zoom_command(1, "in", 7) == bytes.fromhex("8101040727ff")
    assert ptz.zoom_command(1, "stop") == bytes.fromhex("8101040700ff")
    assert ptz.wrap_visca(b"\x81", 5) == bytes.fromhex("01000001") + (5).to_bytes(4, "big") + b"\x81"


@pytest.mark.parametrize(
    "fn,args",
    [
        (ptz.preset_command, (1, 255, False)),
        (ptz.preset_command, (1, "x", False)),
        (ptz.preset_command, (8, 1, False)),
        (ptz.move_command, (1, "sideways", 5)),
        (ptz.move_command, (1, "left", 0)),
        (ptz.move_command, (1, "left", 25)),
        (ptz.zoom_command, (1, "in", 8)),
        (ptz.zoom_command, (1, "sideways", 1)),
    ],
)
def test_builders_validate(fn, args):
    with pytest.raises(ValueError):
        fn(*args)


async def test_setup_requires_host(redis_client):
    with pytest.raises(IntegrationError):
        await PtzIntegration("p", {}, redis_client, None).setup()


async def test_commands_over_udp_with_incrementing_sequence(redis_client, camera):
    integ = await _connected(redis_client, camera, camera_address=2)
    await integ.call_service("recall_preset", {"preset": 3})
    await integ.call_service("home", {})
    p1, p2 = await _recv(camera, 2)
    assert _payload(p1) == bytes.fromhex("8201043f0203ff")
    assert _payload(p2) == bytes.fromhex("82010604ff")
    assert struct.unpack(">I", p1[4:8])[0] == 0 and struct.unpack(">I", p2[4:8])[0] == 1
    assert (await integ.get_state())["last_command"] == "home"


async def test_move_zoom_stop(redis_client, camera):
    integ = await _connected(redis_client, camera)
    await integ.call_service("move", {"direction": "down_left", "speed": 10})
    await integ.call_service("zoom", {"direction": "in", "speed": 2})
    await integ.call_service("stop", {})
    packets = [_payload(p) for p in await _recv(camera, 4)]
    assert packets[0] == bytes.fromhex("81010601" "0a0a" "0102" "ff")
    assert packets[1] == bytes.fromhex("8101040722ff")
    assert packets[2] == bytes.fromhex("8101040700ff")  # stop zoom
    assert packets[3] == bytes.fromhex("8101060101010303ff")  # stop pan/tilt


async def test_service_validation_and_errors(redis_client, camera):
    integ = await _connected(redis_client, camera)
    with pytest.raises(ValueError):
        await integ.call_service("recall_preset", {})
    with pytest.raises(ValueError):
        await integ.call_service("move", {"direction": "x"})
    with pytest.raises(ValueError):
        await integ.call_service("nope", {})
    await integ.teardown()
    with pytest.raises(IntegrationError):
        await integ.call_service("home", {})
    assert (await integ.get_state())["connected"] is False
