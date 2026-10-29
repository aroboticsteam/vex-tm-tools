"""Router tests for `backend/routers/button_pages.py` against real Postgres,
with a real `AutomationEngine` pressing buttons against fake integrations."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend import loader as loader_module
from backend.core.settings import settings
from backend.models.button_page import ButtonPage
from backend.modules.automation.engine import AutomationEngine

pytestmark = pytest.mark.asyncio

GOOD_YAML = "- service: wled.flash\n  target: wled.field1\n  data:\n    count: 2\n"


@dataclass
class FakeInstance:
    entity_id: str
    tags: list[str] = field(default_factory=list)
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    async def call_service(self, service: str, data: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((service, data))
        return {"ok": True}

    async def get_state(self) -> dict[str, Any]:
        return {}


@pytest.fixture
async def session_factory():
    engine = create_async_engine(settings.POSTGRES_DSN, future=True)
    try:
        async with engine.connect() as conn:
            await conn.exec_driver_sql("SELECT 1 FROM button_pages LIMIT 1")
    except Exception:
        await engine.dispose()
        pytest.skip("Real Postgres (migrated) not reachable; skipping button page tests")
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    async with factory() as session:
        await session.execute(delete(ButtonPage).where(ButtonPage.name.like("pytest-%")))
        await session.commit()
    await engine.dispose()


def _make_client(session_factory, permissions):
    from backend.core.db import get_db
    from backend.core.dependencies import CurrentPrincipal, get_current_principal
    from backend.main import create_app

    app = create_app()

    async def _db():
        async with session_factory() as session:
            yield session

    async def _principal():
        return CurrentPrincipal(subject="tester", permissions=set(permissions))

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_principal] = _principal
    return app


@pytest.fixture
async def instance(monkeypatch):
    inst = FakeInstance(entity_id="wled.field1")
    monkeypatch.setattr(loader_module, "get_instance", lambda eid: inst if eid == inst.entity_id else None)
    monkeypatch.setattr(loader_module, "get_instances_by_tag", lambda tag: [])
    monkeypatch.setattr(loader_module, "get_all_instances", lambda: [inst])
    return inst


@pytest.fixture
async def admin(session_factory, instance):
    app = _make_client(session_factory, {"*"})
    app.state.automation_engine = AutomationEngine(redis=object(), session_factory=object())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


def _name() -> str:
    return f"pytest-{uuid.uuid4().hex[:8]}"


async def test_page_and_button_crud(admin):
    resp = await admin.post("/api/v1/button-pages", json={"name": _name(), "columns": 3})
    assert resp.status_code == 201
    page = resp.json()

    resp = await admin.post(f"/api/v1/button-pages/{page['id']}/buttons", json={"label": "Flash", "action_yaml": GOOD_YAML})
    assert resp.status_code == 201
    button = resp.json()
    assert button["color"] == "#374151"

    pages = (await admin.get("/api/v1/button-pages")).json()
    mine = next(p for p in pages if p["id"] == page["id"])
    assert [b["id"] for b in mine["buttons"]] == [button["id"]]

    resp = await admin.put(f"/api/v1/button-pages/buttons/{button['id']}", json={"label": "Flash!", "color": "#ff0000"})
    assert resp.status_code == 200 and resp.json()["label"] == "Flash!" and resp.json()["color"] == "#ff0000"
    resp = await admin.put(f"/api/v1/button-pages/{page['id']}", json={"columns": 6})
    assert resp.json()["columns"] == 6

    assert (await admin.delete(f"/api/v1/button-pages/buttons/{button['id']}")).status_code == 204
    assert (await admin.delete(f"/api/v1/button-pages/{page['id']}")).status_code == 204
    assert (await admin.put(f"/api/v1/button-pages/{page['id']}", json={"columns": 2})).status_code == 404


async def test_deleting_page_removes_its_buttons(admin):
    page = (await admin.post("/api/v1/button-pages", json={"name": _name()})).json()
    button = (
        await admin.post(f"/api/v1/button-pages/{page['id']}/buttons", json={"label": "A", "action_yaml": GOOD_YAML})
    ).json()
    await admin.delete(f"/api/v1/button-pages/{page['id']}")
    assert (await admin.delete(f"/api/v1/button-pages/buttons/{button['id']}")).status_code == 404


async def test_duplicate_page_name_conflicts(admin):
    name = _name()
    assert (await admin.post("/api/v1/button-pages", json={"name": name})).status_code == 201
    assert (await admin.post("/api/v1/button-pages", json={"name": name})).status_code == 409


@pytest.mark.parametrize(
    "body",
    [
        {"label": "", "action_yaml": GOOD_YAML},
        {"label": "x", "action_yaml": GOOD_YAML, "color": "red"},
        {"label": "x", "action_yaml": "not: [valid"},
        {"label": "x", "action_yaml": "- service: {{ unbalanced\n"},
    ],
)
async def test_invalid_button_rejected(admin, body):
    page = (await admin.post("/api/v1/button-pages", json={"name": _name()})).json()
    resp = await admin.post(f"/api/v1/button-pages/{page['id']}/buttons", json=body)
    assert resp.status_code in (400, 422)


async def test_button_on_missing_page_404(admin):
    resp = await admin.post(f"/api/v1/button-pages/{uuid.uuid4()}/buttons", json={"label": "x", "action_yaml": GOOD_YAML})
    assert resp.status_code == 404


async def test_press_runs_actions(admin, instance):
    page = (await admin.post("/api/v1/button-pages", json={"name": _name()})).json()
    button = (
        await admin.post(f"/api/v1/button-pages/{page['id']}/buttons", json={"label": "Flash", "action_yaml": GOOD_YAML})
    ).json()
    resp = await admin.post(f"/api/v1/button-pages/buttons/{button['id']}/press")
    assert resp.status_code == 200
    assert resp.json()["status"] == "success"
    assert instance.calls == [("flash", {"count": 2})]


async def test_press_reports_failure(admin):
    page = (await admin.post("/api/v1/button-pages", json={"name": _name()})).json()
    yaml_text = "- service: wled.flash\n  target: wled.gone\n  data: {}\n"
    button = (
        await admin.post(f"/api/v1/button-pages/{page['id']}/buttons", json={"label": "Bad", "action_yaml": yaml_text})
    ).json()
    resp = await admin.post(f"/api/v1/button-pages/buttons/{button['id']}/press")
    assert resp.status_code == 200 and resp.json()["status"] == "failed"
    assert resp.json()["failed_action_index"] == 0


async def test_press_unknown_button_404_and_no_engine_503(session_factory, instance):
    app = _make_client(session_factory, {"*"})
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        page = (await client.post("/api/v1/button-pages", json={"name": _name()})).json()
        button = (
            await client.post(f"/api/v1/button-pages/{page['id']}/buttons", json={"label": "x", "action_yaml": GOOD_YAML})
        ).json()
        assert (await client.post(f"/api/v1/button-pages/buttons/{uuid.uuid4()}/press")).status_code == 404
        # No engine on app.state (passive node, no leader to proxy to) -> 503.
        assert (await client.post(f"/api/v1/button-pages/buttons/{button['id']}/press")).status_code == 503


async def test_permissions_enforced(session_factory, instance):
    admin_app = _make_client(session_factory, {"*"})
    async with AsyncClient(transport=ASGITransport(app=admin_app), base_url="http://test") as admin_client:
        page = (await admin_client.post("/api/v1/button-pages", json={"name": _name()})).json()
        button = (
            await admin_client.post(
                f"/api/v1/button-pages/{page['id']}/buttons", json={"label": "x", "action_yaml": GOOD_YAML}
            )
        ).json()

    async def client_with(perms):
        app = _make_client(session_factory, perms)
        app.state.automation_engine = AutomationEngine(redis=object(), session_factory=object())
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    async with await client_with({"buttons:read"}) as viewer:
        assert (await viewer.get("/api/v1/button-pages")).status_code == 200
        assert (await viewer.post("/api/v1/button-pages", json={"name": _name()})).status_code == 403
        assert (await viewer.post(f"/api/v1/button-pages/buttons/{button['id']}/press")).status_code == 403
    async with await client_with({"buttons:press"}) as presser:
        assert (await presser.get("/api/v1/button-pages")).status_code == 403
        assert (await presser.put(f"/api/v1/button-pages/buttons/{button['id']}", json={"label": "y"})).status_code == 403
        assert (await presser.post(f"/api/v1/button-pages/buttons/{button['id']}/press")).status_code == 200
    async with await client_with(set()) as nobody:
        assert (await nobody.post(f"/api/v1/button-pages/buttons/{button['id']}/press")).status_code == 403
