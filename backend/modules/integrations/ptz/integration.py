"""PTZ camera integration over VISCA-over-IP (issue #7).

VISCA-over-IP wraps each VISCA command in an 8-byte header
(`payload type 0x0100`, payload length, 32-bit sequence number) and sends
it as a UDP datagram, port 52381 by default. Commands are fire-and-forget
here; the camera's ACK/completion replies are read (best-effort) only to
surface errors.

NOTE: compatibility with the BirdDog X1 is *not verified* against real
hardware — it should work if the camera has VISCA over IP enabled, but
that has to be confirmed on site. The packet builders are pure functions
so they can be checked against any camera's documentation.
"""
from __future__ import annotations

import asyncio
import logging
import socket
import struct
from typing import Any

from backend.core.exceptions import IntegrationError
from backend.modules.integrations.base import Integration

logger = logging.getLogger(__name__)

DEFAULT_PORT = 52381
MAX_PAN_SPEED = 0x18
MAX_TILT_SPEED = 0x14
MAX_PRESET = 254

_PAYLOAD_VISCA_COMMAND = 0x0100
_PAYLOAD_RESET_SEQUENCE = 0x0200

# direction -> (pan code, tilt code); 01/02 = left|right (up|down), 03 = stop.
DIRECTIONS: dict[str, tuple[int, int]] = {
    "left": (0x01, 0x03),
    "right": (0x02, 0x03),
    "up": (0x03, 0x01),
    "down": (0x03, 0x02),
    "up_left": (0x01, 0x01),
    "up_right": (0x02, 0x01),
    "down_left": (0x01, 0x02),
    "down_right": (0x02, 0x02),
}


def wrap_visca(payload: bytes, sequence: int, payload_type: int = _PAYLOAD_VISCA_COMMAND) -> bytes:
    """Prepends the VISCA-over-IP header."""
    return struct.pack(">HHI", payload_type, len(payload), sequence & 0xFFFFFFFF) + payload


def _header(camera: int) -> int:
    if not 1 <= camera <= 7:
        raise ValueError("camera_address must be between 1 and 7")
    return 0x80 | camera


def _clamp(value: Any, low: int, high: int, name: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"'{name}' must be an integer") from None
    if not low <= number <= high:
        raise ValueError(f"'{name}' must be between {low} and {high}")
    return number


def preset_command(camera: int, preset: Any, store: bool) -> bytes:
    p = _clamp(preset, 0, MAX_PRESET, "preset")
    return bytes([_header(camera), 0x01, 0x04, 0x3F, 0x01 if store else 0x02, p, 0xFF])


def move_command(camera: int, direction: str, speed: Any = 8) -> bytes:
    codes = DIRECTIONS.get(str(direction))
    if codes is None:
        raise ValueError(f"direction must be one of: {', '.join(DIRECTIONS)}")
    s = _clamp(speed, 1, MAX_PAN_SPEED, "speed")
    return bytes([_header(camera), 0x01, 0x06, 0x01, s, min(s, MAX_TILT_SPEED), codes[0], codes[1], 0xFF])


def stop_command(camera: int) -> bytes:
    return bytes([_header(camera), 0x01, 0x06, 0x01, 0x01, 0x01, 0x03, 0x03, 0xFF])


def home_command(camera: int) -> bytes:
    return bytes([_header(camera), 0x01, 0x06, 0x04, 0xFF])


def zoom_command(camera: int, direction: str, speed: Any = 0) -> bytes:
    s = _clamp(speed, 0, 7, "speed")
    if direction == "stop":
        code = 0x00
    elif direction == "in":
        code = 0x02 if s == 0 else 0x20 | s
    elif direction == "out":
        code = 0x03 if s == 0 else 0x30 | s
    else:
        raise ValueError("zoom direction must be 'in', 'out' or 'stop'")
    return bytes([_header(camera), 0x01, 0x04, 0x07, code, 0xFF])


class PtzIntegration(Integration):
    def __init__(self, entity_id: str, config: dict[str, Any], redis: Any, db_pool: Any) -> None:
        super().__init__(entity_id, config, redis, db_pool)
        self._sock: socket.socket | None = None
        self._target: tuple[str, int] | None = None
        self._camera = 1
        self._sequence = 0
        self._last_command: str | None = None

    async def setup(self) -> None:
        host = self.config.get("host")
        if not host:
            raise IntegrationError(f"ptz[{self.entity_id}]: 'host' is required")
        port = int(self.config.get("port") or DEFAULT_PORT)
        self._camera = int(self.config.get("camera_address") or 1)
        _header(self._camera)  # validates
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setblocking(False)
        self._sock, self._target = sock, (host, port)
        # Reset the camera's expected sequence number so ours starts at 0.
        await self._send_raw(wrap_visca(b"\x01", 0, _PAYLOAD_RESET_SEQUENCE))

    async def teardown(self) -> None:
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except Exception:
                logger.exception("Error closing PTZ socket for %s", self.entity_id)

    async def _send_raw(self, packet: bytes) -> None:
        assert self._sock is not None and self._target is not None
        try:
            await asyncio.get_running_loop().sock_sendto(self._sock, packet, self._target)
        except OSError as exc:
            raise IntegrationError(f"ptz[{self.entity_id}]: send failed: {exc}") from exc

    async def _command(self, name: str, payload: bytes) -> dict[str, Any]:
        packet = wrap_visca(payload, self._sequence)
        self._sequence = (self._sequence + 1) & 0xFFFFFFFF
        await self._send_raw(packet)
        self._last_command = name
        return {"ok": True, "command": name, "packet": packet.hex()}

    async def call_service(self, service: str, data: dict[str, Any]) -> dict[str, Any]:
        data = data or {}
        if service not in {"recall_preset", "store_preset", "move", "stop", "home", "zoom"}:
            raise ValueError(f"Unknown service '{service}' for ptz integration")
        if self._sock is None:
            raise IntegrationError(f"ptz[{self.entity_id}]: not connected")
        cam = self._camera
        if service in ("recall_preset", "store_preset"):
            if data.get("preset") is None:
                raise ValueError(f"{service} requires 'preset'")
            return await self._command(service, preset_command(cam, data["preset"], store=service == "store_preset"))
        if service == "move":
            return await self._command(service, move_command(cam, data.get("direction", ""), data.get("speed", 8)))
        if service == "stop":
            # Stop both pan/tilt and zoom.
            await self._command("stop", zoom_command(cam, "stop"))
            return await self._command("stop", stop_command(cam))
        if service == "home":
            return await self._command(service, home_command(cam))
        return await self._command(service, zoom_command(cam, data.get("direction", ""), data.get("speed", 0)))

    async def get_state(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "connected": self._sock is not None,
            "target": f"{self._target[0]}:{self._target[1]}" if self._target else None,
            "last_command": self._last_command,
        }


IntegrationClass = PtzIntegration
