"""Art-Net (DMX over IP) sender (issue #39).

Drives lighting consoles that take Art-Net input, in particular MagicQ
virtual sliders via a Node 1 Universe interface. The integration owns one
512-channel universe buffer; services change the buffer and a keep-alive loop
re-sends it a few times a second (Art-Net nodes drop output when frames stop).
Everything is non-blocking asyncio UDP; nothing runs on a worker thread.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from backend.core.exceptions import IntegrationError
from backend.modules.integrations.base import Integration

logger = logging.getLogger(__name__)

DEFAULT_PORT = 6454
DEFAULT_REFRESH_HZ = 4
MAX_REFRESH_HZ = 44  # DMX512 maximum refresh rate
CHANNELS = 512
FADE_TICK_S = 1 / 30
MAX_FADE_MS = 10 * 60 * 1000
_OP_DMX = 0x5000
_PROTOCOL_VERSION = 14
_HEADER = b"Art-Net\x00"


def build_artdmx(sequence: int, universe: int, data: bytes) -> bytes:
    """Build one ArtDmx packet. `data` is 2..512 bytes, padded to even length."""
    if len(data) > CHANNELS:
        raise ValueError(f"ArtDmx carries at most {CHANNELS} channels")
    if len(data) % 2:
        data = data + b"\x00"
    if len(data) < 2:
        data = data + b"\x00" * (2 - len(data))
    return (
        _HEADER
        + _OP_DMX.to_bytes(2, "little")
        + _PROTOCOL_VERSION.to_bytes(2, "big")
        + bytes([sequence & 0xFF, 0])
        + (universe & 0x7FFF).to_bytes(2, "little")  # SubUni then Net
        + len(data).to_bytes(2, "big")
        + data
    )


def _channel(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= CHANNELS:
        raise ValueError(f"channel must be an integer 1-{CHANNELS}")
    return value


def _level(data: dict[str, Any]) -> int:
    """Resolve `level` (0-255) or `percent` (0-100) to a DMX value."""
    level, percent = data.get("level"), data.get("percent")
    if (level is None) == (percent is None):
        raise ValueError("provide exactly one of 'level' (0-255) or 'percent' (0-100)")
    if level is not None:
        if isinstance(level, bool) or not isinstance(level, int) or not 0 <= level <= 255:
            raise ValueError("level must be an integer 0-255")
        return level
    if isinstance(percent, bool) or not isinstance(percent, (int, float)) or not 0 <= percent <= 100:
        raise ValueError("percent must be a number 0-100")
    return round(percent * 255 / 100)


class _Protocol(asyncio.DatagramProtocol):
    def error_received(self, exc: Exception) -> None:  # pragma: no cover - OS dependent
        logger.warning("artnet: UDP error: %s", exc)


class ArtNetIntegration(Integration):
    def __init__(self, entity_id: str, config: dict[str, Any], redis: Any, db_pool: Any) -> None:
        super().__init__(entity_id, config, redis, db_pool)
        self._transport: asyncio.DatagramTransport | None = None
        self._levels = bytearray(CHANNELS)
        self._sequence = 0
        self._universe = 0
        self._refresh_s = 1 / DEFAULT_REFRESH_HZ
        self._refresh_task: asyncio.Task | None = None
        self._fades: dict[int, asyncio.Task] = {}
        self._last_sent: float | None = None
        self._frames_sent = 0

    async def setup(self) -> None:
        ip = self.config.get("ip")
        if not ip:
            raise IntegrationError(f"artnet[{self.entity_id}]: 'ip' is required")
        port = int(self.config.get("port") or DEFAULT_PORT)
        universe = int(self.config.get("universe") or 0)
        if not 0 <= universe <= 32767:
            raise IntegrationError(f"artnet[{self.entity_id}]: universe must be 0-32767")
        hz = int(self.config.get("refresh_hz") or DEFAULT_REFRESH_HZ)
        if not 1 <= hz <= MAX_REFRESH_HZ:
            raise IntegrationError(f"artnet[{self.entity_id}]: refresh_hz must be 1-{MAX_REFRESH_HZ}")
        self._universe = universe
        self._refresh_s = 1 / hz
        try:
            transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
                _Protocol, remote_addr=(ip, port), allow_broadcast=True
            )
        except OSError as exc:
            raise IntegrationError(f"artnet[{self.entity_id}]: cannot open UDP socket to {ip}:{port}: {exc}") from exc
        self._transport = transport
        self._send_frame()
        self._refresh_task = asyncio.create_task(self._refresh_loop())

    async def teardown(self) -> None:
        tasks = [t for t in (self._refresh_task, *self._fades.values()) if t is not None]
        self._refresh_task = None
        self._fades.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except BaseException:  # noqa: BLE001 - teardown must never raise
                pass
        if self._transport is not None:
            try:
                self._transport.close()
            except Exception:  # noqa: BLE001
                logger.exception("artnet[%s]: error closing socket", self.entity_id)
            self._transport = None

    def _send_frame(self) -> None:
        if self._transport is None:
            raise IntegrationError(f"artnet[{self.entity_id}]: not connected")
        self._sequence = self._sequence % 255 + 1  # 1..255; 0 would disable sequencing
        packet = build_artdmx(self._sequence, self._universe, bytes(self._levels))
        try:
            self._transport.sendto(packet)
        except OSError as exc:
            raise IntegrationError(f"artnet[{self.entity_id}]: send failed: {exc}") from exc
        self._last_sent = time.time()
        self._frames_sent += 1

    async def _refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(self._refresh_s)
            try:
                self._send_frame()
            except IntegrationError as exc:
                logger.warning("%s", exc)

    async def _run_fade(self, channel: int, target: int, duration_ms: int) -> None:
        start_level = self._levels[channel - 1]
        started = time.monotonic()
        duration = duration_ms / 1000
        try:
            while True:
                progress = min((time.monotonic() - started) / duration, 1.0)
                self._levels[channel - 1] = round(start_level + (target - start_level) * progress)
                self._send_frame()
                if progress >= 1.0:
                    return
                await asyncio.sleep(FADE_TICK_S)
        except IntegrationError as exc:
            logger.warning("%s", exc)
        finally:
            if self._fades.get(channel) is asyncio.current_task():
                del self._fades[channel]

    def _cancel_fade(self, channel: int) -> None:
        task = self._fades.pop(channel, None)
        if task is not None:
            task.cancel()

    async def call_service(self, service: str, data: dict[str, Any]) -> dict[str, Any]:
        data = data or {}
        if service not in ("set_channels", "set_slider", "fade", "blackout"):
            raise ValueError(f"Unknown service '{service}' for artnet integration")
        if self._transport is None:
            raise IntegrationError(f"artnet[{self.entity_id}]: not connected")

        if service == "blackout":
            for channel in list(self._fades):
                self._cancel_fade(channel)
            self._levels = bytearray(CHANNELS)
            self._send_frame()
            return {"ok": True}

        if service == "set_channels":
            start = _channel(data.get("start"))
            values = data.get("values")
            if not isinstance(values, list) or not values:
                raise ValueError("'values' must be a non-empty list of integers 0-255")
            if start - 1 + len(values) > CHANNELS:
                raise ValueError(f"values run past channel {CHANNELS}")
            if not all(isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 255 for v in values):
                raise ValueError("values must be integers 0-255")
            for offset in range(len(values)):
                self._cancel_fade(start + offset)
            self._levels[start - 1 : start - 1 + len(values)] = bytes(values)
            self._send_frame()
            return {"ok": True, "start": start, "count": len(values)}

        channel = _channel(data.get("channel"))
        target = _level(data)
        if service == "set_slider":
            self._cancel_fade(channel)
            self._levels[channel - 1] = target
            self._send_frame()
            return {"ok": True, "channel": channel, "level": target}

        duration_ms = data.get("duration_ms")
        if isinstance(duration_ms, bool) or not isinstance(duration_ms, int) or not 0 <= duration_ms <= MAX_FADE_MS:
            raise ValueError(f"duration_ms must be an integer 0-{MAX_FADE_MS}")
        self._cancel_fade(channel)
        if duration_ms == 0:
            self._levels[channel - 1] = target
            self._send_frame()
            return {"ok": True, "channel": channel, "level": target}
        self._fades[channel] = asyncio.create_task(self._run_fade(channel, target, duration_ms))
        return {"ok": True, "channel": channel, "target": target, "duration_ms": duration_ms}

    async def get_state(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "connected": self._transport is not None,
            "universe": self._universe,
            "active_channels": {i + 1: v for i, v in enumerate(self._levels) if v},
            "fading": sorted(self._fades),
            "frames_sent": self._frames_sent,
            "last_sent": self._last_sent,
        }


IntegrationClass = ArtNetIntegration
