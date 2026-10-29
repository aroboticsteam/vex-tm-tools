"""Which station pages are open right now (issue #27).

Each open page holds a websocket and pings every ~25 s. A per-station
connection counter (with a TTL that every ping refreshes, so a crashed pod's
count heals itself) says whether anyone is connected; a timestamp says when
a page was last heard from.
"""
from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

CONNS_KEY = "qecomp:station:{slug}:conns"
SEEN_KEY = "qecomp:station:{slug}:seen"
_CONNS_TTL = 90
_SEEN_TTL = 7 * 24 * 3600


async def mark_connected(redis: Any, slug: str) -> None:
    try:
        await redis.incr(CONNS_KEY.format(slug=slug))
        await redis.expire(CONNS_KEY.format(slug=slug), _CONNS_TTL)
        await redis.set(SEEN_KEY.format(slug=slug), time.time(), ex=_SEEN_TTL)
    except Exception:
        logger.warning("Could not record station connect for %s", slug, exc_info=True)


async def touch(redis: Any, slug: str) -> None:
    try:
        await redis.expire(CONNS_KEY.format(slug=slug), _CONNS_TTL)
        await redis.set(SEEN_KEY.format(slug=slug), time.time(), ex=_SEEN_TTL)
    except Exception:
        logger.warning("Could not record station ping for %s", slug, exc_info=True)


async def mark_disconnected(redis: Any, slug: str) -> None:
    try:
        remaining = await redis.decr(CONNS_KEY.format(slug=slug))
        if remaining <= 0:
            await redis.delete(CONNS_KEY.format(slug=slug))
        await redis.set(SEEN_KEY.format(slug=slug), time.time(), ex=_SEEN_TTL)
    except Exception:
        logger.warning("Could not record station disconnect for %s", slug, exc_info=True)


async def read_presence(redis: Any, slugs: list[str]) -> dict[str, tuple[bool, float | None]]:
    """`{slug: (connected, last_seen_epoch)}`; all-offline if Redis is down."""
    if not slugs:
        return {}
    try:
        conns = await redis.mget([CONNS_KEY.format(slug=s) for s in slugs])
        seen = await redis.mget([SEEN_KEY.format(slug=s) for s in slugs])
    except Exception:
        logger.warning("Station presence unavailable", exc_info=True)
        return {s: (False, None) for s in slugs}
    out: dict[str, tuple[bool, float | None]] = {}
    for slug, c, t in zip(slugs, conns, seen):
        out[slug] = (int(c or 0) > 0, float(t) if t else None)
    return out
