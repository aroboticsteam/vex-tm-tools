"""Shared fixtures for the capture tests."""
from __future__ import annotations

import pytest
import redis.asyncio as aioredis

from backend.core.redis import redis_client
from backend.core.settings import settings


@pytest.fixture(autouse=True)
def _fresh_global_redis_pool():
    """Starlette's TestClient runs the app in its own event loop and can leave
    the shared `redis_client` pool holding a socket bound to that (closed) loop;
    a later async test using the same client then fails with "attached to a
    different loop". Swap in a clean pool after every test here."""
    yield
    redis_client.connection_pool = aioredis.ConnectionPool.from_url(
        settings.REDIS_URL, encoding="utf-8", decode_responses=True
    )
