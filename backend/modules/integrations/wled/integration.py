"""Async WLED integration client (issue #7).

Drives WLED LED strips (the lights around the fields) through WLED's JSON
API (`POST /json/state`). Plain HTTP via `httpx`, so nothing blocks the
event loop.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import httpx

from backend.core.exceptions import IntegrationError
from backend.modules.integrations.base import Integration

logger = logging.getLogger(__name__)

DEFAULT_PORT = 80
REQUEST_TIMEOUT_SECONDS = 5.0
MAX_FLASHES = 20
MIN_FLASH_INTERVAL_MS = 50
DEFAULT_FLASH_COLOR = "#FFFFFF"

_HEX_RE = re.compile(r"^#?([0-9a-fA-F]{6})$")


def parse_color(value: Any) -> list[int]:
    """`#RRGGBB`/`RRGGBB`, `[r, g, b]`, or `{"r","g","b"}` -> `[r, g, b]`."""
    if isinstance(value, str):
        match = _HEX_RE.match(value.strip())
        if not match:
            raise ValueError(f"Invalid colour {value!r}; expected hex like #FF0000")
        raw = match.group(1)
        return [int(raw[i : i + 2], 16) for i in (0, 2, 4)]
    if isinstance(value, dict) and all(k in value for k in "rgb"):
        value = [value["r"], value["g"], value["b"]]
    if isinstance(value, (list, tuple)) and len(value) == 3:
        try:
            rgb = [int(c) for c in value]
        except (TypeError, ValueError):
            raise ValueError(f"Invalid colour {value!r}") from None
        if all(0 <= c <= 255 for c in rgb):
            return rgb
    raise ValueError(f"Invalid colour {value!r}; expected hex like #FF0000")


def _int_in_range(data: dict[str, Any], name: str, low: int, high: int) -> int:
    if data.get(name) is None:
        raise ValueError(f"'{name}' is required")
    try:
        value = int(data[name])
    except (TypeError, ValueError):
        raise ValueError(f"'{name}' must be an integer") from None
    if not low <= value <= high:
        raise ValueError(f"'{name}' must be between {low} and {high}")
    return value


class WledIntegration(Integration):
    def __init__(self, entity_id: str, config: dict[str, Any], redis: Any, db_pool: Any) -> None:
        super().__init__(entity_id, config, redis, db_pool)
        self._http: httpx.AsyncClient | None = None
        self._flash_lock = asyncio.Lock()
        self._last_state: dict[str, Any] = {}

    async def setup(self) -> None:
        host = self.config.get("host")
        if not host:
            raise IntegrationError(f"wled[{self.entity_id}]: 'host' is required")
        port = int(self.config.get("port") or DEFAULT_PORT)
        self._http = httpx.AsyncClient(base_url=f"http://{host}:{port}", timeout=REQUEST_TIMEOUT_SECONDS)
        try:
            await self._request("GET", "/json/info")
        except Exception as exc:
            await self._close()
            raise IntegrationError(f"wled[{self.entity_id}]: cannot reach {host}:{port}: {exc}") from exc

    async def _close(self) -> None:
        http, self._http = self._http, None
        if http is not None:
            try:
                await http.aclose()
            except Exception:
                logger.exception("Error closing WLED client for %s", self.entity_id)

    async def teardown(self) -> None:
        await self._close()

    async def _request(self, method: str, path: str, json: dict[str, Any] | None = None) -> Any:
        if self._http is None:
            raise IntegrationError(f"wled[{self.entity_id}]: not connected")
        try:
            resp = await self._http.request(method, path, json=json)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as exc:
            raise IntegrationError(f"wled[{self.entity_id}]: request failed: {exc}") from exc

    async def _post_state(self, state: dict[str, Any]) -> dict[str, Any]:
        await self._request("POST", "/json/state", state)
        self._last_state.update({k: v for k, v in state.items() if k in ("on", "bri", "ps")})
        return {"ok": True, "applied": state}

    async def call_service(self, service: str, data: dict[str, Any]) -> dict[str, Any]:
        data = data or {}
        if service == "set_power":
            if "on" not in data:
                raise ValueError("set_power requires 'on'")
            return await self._post_state({"on": bool(data["on"])})
        if service == "set_brightness":
            return await self._post_state({"on": True, "bri": _int_in_range(data, "brightness", 0, 255)})
        if service == "set_color":
            if data.get("color") is None:
                raise ValueError("set_color requires 'color'")
            return await self._post_state({"on": True, "seg": [{"col": [parse_color(data["color"])]}]})
        if service == "set_preset":
            return await self._post_state({"ps": _int_in_range(data, "preset", 1, 250)})
        if service == "set_effect":
            seg: dict[str, Any] = {"fx": _int_in_range(data, "effect", 0, 255)}
            for name, key in (("speed", "sx"), ("intensity", "ix")):
                if data.get(name) is not None:
                    seg[key] = _int_in_range(data, name, 0, 255)
            return await self._post_state({"on": True, "seg": [seg]})
        if service == "flash":
            return await self._flash(data)
        raise ValueError(f"Unknown service '{service}' for wled integration")

    async def _flash(self, data: dict[str, Any]) -> dict[str, Any]:
        """Blinks the strip `count` times in `color`, then restores the
        prior state. Serialised so overlapping flashes can't capture each
        other's mid-flash state as the one to restore."""
        colour = parse_color(data.get("color") or DEFAULT_FLASH_COLOR)
        count = _int_in_range({"count": data.get("count", 3)}, "count", 1, MAX_FLASHES)
        interval = _int_in_range({"i": data.get("interval_ms", 250)}, "i", MIN_FLASH_INTERVAL_MS, 5000) / 1000
        async with self._flash_lock:
            previous = await self._request("GET", "/json/state")
            restore = {k: previous[k] for k in ("on", "bri", "seg") if k in previous}
            if "seg" in restore:
                restore["seg"] = [
                    {k: s[k] for k in ("id", "col", "fx", "sx", "ix", "pal") if k in s} for s in restore["seg"]
                ]
            try:
                for _ in range(count):
                    await self._request(
                        "POST", "/json/state", {"on": True, "bri": 255, "seg": [{"col": [colour], "fx": 0}]}
                    )
                    await asyncio.sleep(interval)
                    await self._request("POST", "/json/state", {"on": False})
                    await asyncio.sleep(interval)
            finally:
                try:
                    await self._request("POST", "/json/state", restore)
                except IntegrationError:
                    logger.exception("Could not restore WLED state for %s after flash", self.entity_id)
        return {"ok": True, "flashes": count}

    async def get_state(self) -> dict[str, Any]:
        state: dict[str, Any] = {"entity_id": self.entity_id, "connected": self._http is not None}
        if self._http is not None:
            try:
                live = await self._request("GET", "/json/state")
                state.update({"on": live.get("on"), "brightness": live.get("bri"), "preset": live.get("ps")})
                return state
            except Exception:
                logger.debug("WLED state fetch failed for %s", self.entity_id, exc_info=True)
        state.update(
            {"on": self._last_state.get("on"), "brightness": self._last_state.get("bri"), "preset": self._last_state.get("ps")}
        )
        return state


IntegrationClass = WledIntegration
