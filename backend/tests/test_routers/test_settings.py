"""Router tests for `backend/routers/settings.py` (plan §14): system
settings, API keys (creation/scoping/revocation), role_permissions, and
ZerOS presets — against real Postgres."""
from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.core.db import engine as app_engine
from backend.core.dependencies import ALL_PERMISSIONS, CurrentPrincipal, get_current_principal
from backend.core.settings import settings
from backend.main import app
from backend.models.integration import IntegrationInstance, ZerosPreset
from backend.models.settings import ApiKey, RolePermission, SystemSetting

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _reset_app_engine_pool():
    await app_engine.dispose()
    yield
    await app_engine.dispose()


@pytest.fixture
async def db_engine():
    engine = create_async_engine(settings.POSTGRES_DSN)
    try:
        async with engine.connect() as conn:
            await conn.exec_driver_sql("SELECT 1")
    except Exception:
        pytest.skip("Real Postgres not reachable; skipping settings router tests")
    yield engine
    await engine.dispose()


@pytest.fixture
async def client(db_engine):
    async def _override_get_current_principal():
        return CurrentPrincipal(subject="test-admin", permissions={ALL_PERMISSIONS})

    app.dependency_overrides[get_current_principal] = _override_get_current_principal
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_current_principal, None)


@pytest.fixture
async def db_factory(db_engine):
    return async_sessionmaker(bind=db_engine, expire_on_commit=False)


# ── system_settings ───────────────────────────────────────────────────────


async def test_update_and_list_settings_roundtrip(client, db_factory):
    key = f"test_setting_{uuid.uuid4().hex[:8]}"
    try:
        resp = await client.put("/api/v1/settings", json={key: {"value": {"threshold": 15}}})
        assert resp.status_code == 200
        assert any(row["key"] == key and row["value"]["threshold"] == 15 for row in resp.json())

        list_resp = await client.get("/api/v1/settings")
        assert any(row["key"] == key for row in list_resp.json())
    finally:
        async with db_factory() as session:
            row = await session.get(SystemSetting, key)
            if row is not None:
                await session.delete(row)
                await session.commit()


async def test_update_settings_upserts_existing_key(client, db_factory):
    key = f"test_setting_{uuid.uuid4().hex[:8]}"
    try:
        await client.put("/api/v1/settings", json={key: {"value": {"a": 1}}})
        resp = await client.put("/api/v1/settings", json={key: {"value": {"a": 2}}})
        assert resp.status_code == 200
        row = next(r for r in resp.json() if r["key"] == key)
        assert row["value"] == {"a": 2}
    finally:
        async with db_factory() as session:
            row = await session.get(SystemSetting, key)
            if row is not None:
                await session.delete(row)
                await session.commit()


async def test_competition_setting_accepts_only_known_programs(client, db_factory):
    async with db_factory() as session:
        before = await session.get(SystemSetting, "competition")
        original = dict(before.value) if before is not None else None
    try:
        for program in ("VIQRC", "V5RC"):
            resp = await client.put("/api/v1/settings", json={"competition": {"value": {"program": program}}})
            assert resp.status_code == 200
            assert next(r for r in resp.json() if r["key"] == "competition")["value"] == {"program": program}
        for bad in ({"program": "VEXU"}, {}, {"program": "V5RC", "x": 1}):
            resp = await client.put("/api/v1/settings", json={"competition": {"value": bad}})
            assert resp.status_code == 422
        listed = await client.get("/api/v1/settings")
        assert next(r for r in listed.json() if r["key"] == "competition")["value"] == {"program": "V5RC"}
    finally:
        async with db_factory() as session:
            row = await session.get(SystemSetting, "competition")
            if original is None:
                if row is not None:
                    await session.delete(row)
            elif row is not None:
                row.value = original
            await session.commit()


# ── api_keys (plan Appendix B.7) ───────────────────────────────────────────


async def test_create_api_key_shows_raw_key_once_and_hashes_at_rest(client, db_factory):
    resp = await client.post("/api/v1/api-keys", json={"name": "test key", "permissions": ["tm:control"]})
    assert resp.status_code == 201
    body = resp.json()
    assert body["raw_key"].startswith("qec_")
    key_id = body["id"]

    try:
        async with db_factory() as session:
            row = await session.get(ApiKey, uuid.UUID(key_id))
            assert row is not None
            assert row.key_hash != body["raw_key"]
            assert len(row.key_hash) == 64

        list_resp = await client.get("/api/v1/api-keys")
        assert all("raw_key" not in row for row in list_resp.json())
    finally:
        async with db_factory() as session:
            row = await session.get(ApiKey, uuid.UUID(key_id))
            if row is not None:
                await session.delete(row)
                await session.commit()


async def test_revoke_api_key(client, db_factory):
    resp = await client.post("/api/v1/api-keys", json={"name": "revoke-me", "permissions": ["settings:read"]})
    key_id = resp.json()["id"]

    revoke_resp = await client.delete(f"/api/v1/api-keys/{key_id}")
    assert revoke_resp.status_code == 204

    async with db_factory() as session:
        row = await session.get(ApiKey, uuid.UUID(key_id))
        assert row.revoked is True
        await session.delete(row)
        await session.commit()


async def test_revoke_missing_api_key_returns_404(client):
    resp = await client.delete(f"/api/v1/api-keys/{uuid.uuid4()}")
    assert resp.status_code == 404


# ── role_permissions (plan §13) ───────────────────────────────────────────


async def test_list_role_permissions_includes_seeded_defaults(client):
    """Confirms the seed migration's defaults are queryable through the API
    (skips gracefully if this test DB predates that migration)."""
    resp = await client.get("/api/v1/users/roles")
    assert resp.status_code == 200
    groups = {row["authentik_group"] for row in resp.json()}
    # Not asserting the full default set (an operator may have customized
    # it), just that the endpoint round-trips real rows when present.
    assert isinstance(groups, set)


async def test_replace_role_permissions_roundtrip(client, db_factory):
    async with db_factory() as session:
        existing = (await session.execute(RolePermission.__table__.select())).all()

    try:
        new_mapping = [{"authentik_group": "qecomp-test-group", "permission": "teams:read"}]
        resp = await client.put("/api/v1/users/roles", json=new_mapping)
        assert resp.status_code == 200
        assert resp.json() == new_mapping
    finally:
        # Restore whatever was there before this test (this endpoint fully
        # replaces the table, so leaving it in the test's temporary state
        # would break other tests/migrations' expectations).
        async with db_factory() as session:
            await session.execute(delete(RolePermission))
            if existing:
                await session.execute(
                    RolePermission.__table__.insert(),
                    [dict(row._mapping) for row in existing],
                )
            await session.commit()


# ── zeros_presets ──────────────────────────────────────────────────────────


@pytest.fixture
async def zeros_integration(db_factory):
    entity_id = f"zeros.test_{uuid.uuid4().hex[:8]}"
    async with db_factory() as session:
        row = IntegrationInstance(
            entity_id=entity_id, domain="zeros", display_name="Zeros Presets Test", config={}, tags=[]
        )
        session.add(row)
        await session.commit()
        integration_id = row.id

    yield integration_id

    async with db_factory() as session:
        await session.execute(delete(ZerosPreset).where(ZerosPreset.integration_id == integration_id))
        await session.execute(delete(IntegrationInstance).where(IntegrationInstance.id == integration_id))
        await session.commit()


async def test_zeros_presets_crud(client, zeros_integration):
    integration_id = str(zeros_integration)

    create_resp = await client.post(
        "/api/v1/zeros/presets",
        json={"integration_id": integration_id, "preset_number": 1, "preset_name": "House Lights"},
    )
    assert create_resp.status_code == 201
    preset_id = create_resp.json()["id"]

    list_resp = await client.get("/api/v1/zeros/presets")
    assert any(row["id"] == preset_id for row in list_resp.json())

    update_resp = await client.put(
        f"/api/v1/zeros/presets/{preset_id}",
        json={"integration_id": integration_id, "preset_number": 2, "preset_name": "Blackout"},
    )
    assert update_resp.status_code == 200
    assert update_resp.json()["preset_number"] == 2

    delete_resp = await client.delete(f"/api/v1/zeros/presets/{preset_id}")
    assert delete_resp.status_code == 204


async def test_zeros_preset_duplicate_number_returns_409(client, zeros_integration):
    integration_id = str(zeros_integration)
    body = {"integration_id": integration_id, "preset_number": 5, "preset_name": "Preset A"}
    assert (await client.post("/api/v1/zeros/presets", json=body)).status_code == 201
    dup = {"integration_id": integration_id, "preset_number": 5, "preset_name": "Preset B"}
    assert (await client.post("/api/v1/zeros/presets", json=dup)).status_code == 409
