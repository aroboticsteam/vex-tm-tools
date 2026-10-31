"""Unit tests for backend.loader (fakeredis + a fake Postgres session
factory — no real database needed). Covers discovery, spin-up/teardown,
DEGRADED + retry-with-backoff, hot-reload via config_change, and the
frozen public API (get_instance / get_instances_by_tag / get_all_instances
/ get_instance_status).
"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any

import fakeredis.aioredis
import pytest

import backend.loader as loader_module
from backend.core.security import encrypt
from backend.modules.integrations.base import Integration

pytestmark = pytest.mark.asyncio


@dataclass
class FakeRow:
    entity_id: str
    domain: str
    config: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    enabled: bool = True
    id: uuid.UUID = field(default_factory=uuid.uuid4)


class FakeScalars:
    def __init__(self, rows: list[FakeRow]) -> None:
        self._rows = rows

    def all(self):
        return list(self._rows)


class FakeResult:
    def __init__(self, rows: list[FakeRow]) -> None:
        self._rows = rows

    def scalars(self):
        return FakeScalars(self._rows)


class FakeSession:
    def __init__(self, rows_provider) -> None:
        self._rows_provider = rows_provider

    async def __aenter__(self) -> "FakeSession":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def execute(self, _statement):
        return FakeResult(self._rows_provider())


class FakeSessionFactory:
    """Mimics `async_sessionmaker` well enough for the loader: calling it
    returns a fresh async-context-manager session backed by whatever rows
    the test has currently configured."""

    def __init__(self, rows: list[FakeRow] | None = None) -> None:
        self.rows: list[FakeRow] = rows or []

    def __call__(self) -> FakeSession:
        return FakeSession(lambda: self.rows)


class FakeGoodIntegration(Integration):
    setup_calls = 0
    teardown_calls = 0
    instances: list["FakeGoodIntegration"] = []

    def __init__(self, entity_id, config, redis, db_pool):
        super().__init__(entity_id, config, redis, db_pool)
        FakeGoodIntegration.instances.append(self)

    async def setup(self):
        FakeGoodIntegration.setup_calls += 1

    async def teardown(self):
        FakeGoodIntegration.teardown_calls += 1

    async def call_service(self, service, data):
        return {}

    async def get_state(self):
        return {}


class FakeFailingIntegration(Integration):
    fail_until_attempt = 1  # setup() succeeds starting from this call count
    attempts = 0

    async def setup(self):
        type(self).attempts += 1
        if type(self).attempts < type(self).fail_until_attempt:
            raise RuntimeError("simulated setup failure")

    async def teardown(self):
        pass

    async def call_service(self, service, data):
        return {}

    async def get_state(self):
        return {}


@pytest.fixture
async def redis_client():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


@pytest.fixture(autouse=True)
def reset_registry():
    FakeGoodIntegration.setup_calls = 0
    FakeGoodIntegration.teardown_calls = 0
    FakeGoodIntegration.instances = []
    FakeFailingIntegration.attempts = 0
    FakeFailingIntegration.fail_until_attempt = 1
    loader_module.INTEGRATION_REGISTRY.clear()
    loader_module._MANIFESTS.clear()
    loader_module.INTEGRATION_REGISTRY["fake_good"] = FakeGoodIntegration
    loader_module.INTEGRATION_REGISTRY["fake_failing"] = FakeFailingIntegration
    loader_module._MANIFESTS["fake_good"] = {
        "config_schema": {"secret_field": {"secret": True}, "plain_field": {"secret": False}}
    }
    loader_module._MANIFESTS["fake_failing"] = {"config_schema": {}}
    yield
    loader_module.INTEGRATION_REGISTRY.clear()
    loader_module._MANIFESTS.clear()


def test_discover_finds_all_domains():
    loader_module._discover()
    assert set(loader_module.INTEGRATION_REGISTRY.keys()) == {
        "vex_tm",
        "spotify",
        "atem",
        "zeros",
        "obs",
        "wled",
        "osc",
        "ptz",
        "artnet",
        "capture",
    }
    for domain, manifest in loader_module._MANIFESTS.items():
        assert "config_schema" in manifest


async def test_load_all_spins_up_enabled_instances(redis_client):
    factory = FakeSessionFactory(
        [FakeRow(entity_id="fake_good.one", domain="fake_good", tags=["t1"])]
    )
    ldr = loader_module.Loader(redis=redis_client, session_factory=factory)
    await ldr.load_all()
    try:
        assert FakeGoodIntegration.setup_calls == 1
        assert ldr.get_instance("fake_good.one") is not None
        assert ldr.get_instance_status("fake_good.one") == "CONNECTED"
        status = await redis_client.get("qecomp:integration:fake_good.one:status")
        assert status == "CONNECTED"
    finally:
        await ldr.teardown_all()


async def test_disabled_rows_are_not_spun_up(redis_client):
    factory = FakeSessionFactory([FakeRow(entity_id="fake_good.off", domain="fake_good", enabled=False)])
    ldr = loader_module.Loader(redis=redis_client, session_factory=factory)
    await ldr.load_all()
    try:
        assert ldr.get_instance("fake_good.off") is None
        assert FakeGoodIntegration.setup_calls == 0
    finally:
        await ldr.teardown_all()


async def test_secret_config_fields_are_decrypted(redis_client):
    plaintext = "super-secret-value"
    factory = FakeSessionFactory(
        [
            FakeRow(
                entity_id="fake_good.secret",
                domain="fake_good",
                config={"secret_field": encrypt(plaintext), "plain_field": "not-secret"},
            )
        ]
    )
    ldr = loader_module.Loader(redis=redis_client, session_factory=factory)
    await ldr.load_all()
    try:
        instance = ldr.get_instance("fake_good.secret")
        assert instance.config["secret_field"] == plaintext
        assert instance.config["plain_field"] == "not-secret"
    finally:
        await ldr.teardown_all()


async def test_unknown_domain_marks_disconnected_without_crashing(redis_client):
    factory = FakeSessionFactory([FakeRow(entity_id="mystery.one", domain="does_not_exist")])
    ldr = loader_module.Loader(redis=redis_client, session_factory=factory)
    await ldr.load_all()  # must not raise
    try:
        assert ldr.get_instance_status("mystery.one") == "DISCONNECTED"
    finally:
        await ldr.teardown_all()


async def test_setup_failure_marks_degraded_and_retries_in_background(redis_client):
    FakeFailingIntegration.fail_until_attempt = 2  # fail once, succeed on retry
    factory = FakeSessionFactory([FakeRow(entity_id="fake_failing.one", domain="fake_failing")])
    ldr = loader_module.Loader(redis=redis_client, session_factory=factory)
    await ldr.load_all()
    try:
        assert ldr.get_instance_status("fake_failing.one") == "DEGRADED"
        assert ldr.get_instance("fake_failing.one") is None
        assert "fake_failing.one" in ldr._retry_tasks

        # The retry loop's first backoff is ~1s; give it a little headroom.
        for _ in range(100):
            if ldr.get_instance_status("fake_failing.one") == "CONNECTED":
                break
            await asyncio.sleep(0.05)
        assert ldr.get_instance_status("fake_failing.one") == "CONNECTED"
        assert ldr.get_instance("fake_failing.one") is not None
    finally:
        await ldr.teardown_all()


async def test_get_instances_by_tag(redis_client):
    factory = FakeSessionFactory(
        [
            FakeRow(entity_id="fake_good.a", domain="fake_good", tags=["fs1", "fieldAudio"]),
            FakeRow(entity_id="fake_good.b", domain="fake_good", tags=["fs2"]),
        ]
    )
    ldr = loader_module.Loader(redis=redis_client, session_factory=factory)
    await ldr.load_all()
    try:
        tagged = ldr.get_instances_by_tag("fieldAudio")
        assert [i.entity_id for i in tagged] == ["fake_good.a"]
        assert len(ldr.get_all_instances()) == 2
    finally:
        await ldr.teardown_all()


async def test_reconcile_tears_down_removed_and_spins_up_new(redis_client):
    factory = FakeSessionFactory([FakeRow(entity_id="fake_good.a", domain="fake_good")])
    ldr = loader_module.Loader(redis=redis_client, session_factory=factory)
    await ldr.load_all()
    try:
        assert ldr.get_instance("fake_good.a") is not None

        # Simulate a config change: "a" removed, "b" added.
        factory.rows = [FakeRow(entity_id="fake_good.b", domain="fake_good")]
        await ldr.reconcile()

        assert ldr.get_instance("fake_good.a") is None
        assert ldr.get_instance_status("fake_good.a") == "DISCONNECTED"
        assert ldr.get_instance("fake_good.b") is not None
        assert FakeGoodIntegration.teardown_calls == 1
    finally:
        await ldr.teardown_all()


async def test_reconcile_respins_instance_when_config_changes(redis_client):
    factory = FakeSessionFactory([FakeRow(entity_id="fake_good.a", domain="fake_good", tags=["x"])])
    ldr = loader_module.Loader(redis=redis_client, session_factory=factory)
    await ldr.load_all()
    try:
        first_instance = ldr.get_instance("fake_good.a")

        factory.rows = [FakeRow(entity_id="fake_good.a", domain="fake_good", tags=["y"])]
        await ldr.reconcile()

        second_instance = ldr.get_instance("fake_good.a")
        assert second_instance is not None
        assert second_instance is not first_instance
        assert second_instance.tags == ["y"]
        assert FakeGoodIntegration.teardown_calls == 1
        assert FakeGoodIntegration.setup_calls == 2
    finally:
        await ldr.teardown_all()


async def test_reconcile_is_noop_when_nothing_changed(redis_client):
    factory = FakeSessionFactory([FakeRow(entity_id="fake_good.a", domain="fake_good")])
    ldr = loader_module.Loader(redis=redis_client, session_factory=factory)
    await ldr.load_all()
    try:
        first_instance = ldr.get_instance("fake_good.a")
        await ldr.reconcile()
        assert ldr.get_instance("fake_good.a") is first_instance
        assert FakeGoodIntegration.setup_calls == 1
        assert FakeGoodIntegration.teardown_calls == 0
    finally:
        await ldr.teardown_all()


async def test_config_change_pubsub_triggers_reconcile(redis_client):
    factory = FakeSessionFactory([FakeRow(entity_id="fake_good.a", domain="fake_good")])
    ldr = loader_module.Loader(redis=redis_client, session_factory=factory)
    await ldr.load_all()
    try:
        assert ldr.get_instance("fake_good.b") is None
        factory.rows = [FakeRow(entity_id="fake_good.b", domain="fake_good")]
        await asyncio.sleep(0.1)  # let the background pubsub listener actually subscribe
        await redis_client.publish("qecomp:config_change", "reload")

        for _ in range(100):
            if ldr.get_instance("fake_good.b") is not None:
                break
            await asyncio.sleep(0.02)
        assert ldr.get_instance("fake_good.b") is not None
        assert ldr.get_instance("fake_good.a") is None
    finally:
        await ldr.teardown_all()


async def test_teardown_all_cancels_retry_tasks_and_tears_down_instances(redis_client):
    factory = FakeSessionFactory(
        [
            FakeRow(entity_id="fake_good.a", domain="fake_good"),
            FakeRow(entity_id="fake_failing.b", domain="fake_failing"),
        ]
    )
    FakeFailingIntegration.fail_until_attempt = 999  # never succeeds
    ldr = loader_module.Loader(redis=redis_client, session_factory=factory)
    await ldr.load_all()

    assert "fake_failing.b" in ldr._retry_tasks
    assert ldr.get_instance_status("fake_failing.b") == "DEGRADED"
    await ldr.teardown_all()

    assert ldr._retry_tasks == {}
    assert ldr._instances == {}
    assert FakeGoodIntegration.teardown_calls == 1
    # The degraded (never-connected) entity must not be left reporting a
    # stale DEGRADED status once the loader has stopped managing it.
    assert ldr.get_instance_status("fake_failing.b") == "DISCONNECTED"
    redis_status = await redis_client.get("qecomp:integration:fake_failing.b:status")
    assert redis_status == "DISCONNECTED"


async def test_module_level_public_api_delegates_to_most_recent_loader(redis_client):
    factory = FakeSessionFactory([FakeRow(entity_id="fake_good.a", domain="fake_good", tags=["z"])])
    ldr = loader_module.Loader(redis=redis_client, session_factory=factory)
    await ldr.load_all()
    try:
        assert loader_module.get_instance("fake_good.a") is ldr.get_instance("fake_good.a")
        assert loader_module.get_instances_by_tag("z") == ldr.get_instances_by_tag("z")
        assert loader_module.get_all_instances() == ldr.get_all_instances()
        assert loader_module.get_instance_status("fake_good.a") == "CONNECTED"
        assert loader_module.get_instance_status("does.not.exist") == "DISCONNECTED"
    finally:
        await ldr.teardown_all()
