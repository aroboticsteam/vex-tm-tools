"""Capture agent server side (issues #42, #38, #45): keys, service, REST, integration.

Real Postgres and MinIO (presigned PUT really uploads), fakeredis for presence.
"""
from __future__ import annotations

import uuid

import fakeredis
import httpx
import pytest
from fakeredis import aioredis as fake_aioredis
from sqlalchemy import delete, select

from backend.core.db import async_session_factory, engine as app_engine
from backend.core.dependencies import ALL_PERMISSIONS, CurrentPrincipal, get_current_principal
from backend.core.exceptions import IntegrationError
from backend.core.redis import get_redis, redis_client
from backend.main import app
from backend.models.capture import CaptureMoment, CaptureRecording
from backend.modules.capture import hub, keys, service
from backend.modules.integrations.capture.integration import CaptureIntegration
from backend.modules.media import s3

pytestmark = pytest.mark.asyncio

#: Unique per test run so concurrent suites never share rows or object keys.
RUN = uuid.uuid4().hex[:6]


def _match(num: int, round_: str = "QUAL", teams=("1234A", "5678B")) -> dict:
    return {
        "match": {"division": 1, "session": 0, "round": round_, "instance": 1, "match": num},
        "red_teams": [teams[0]],
        "blue_teams": [{"team_number": teams[1].lower()}],
    }


@pytest.fixture(autouse=True)
async def _reset_pool():
    await app_engine.dispose()
    yield
    await app_engine.dispose()
    await redis_client.connection_pool.disconnect()


@pytest.fixture
async def redis():
    client = fake_aioredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)
    yield client
    await client.aclose()


@pytest.fixture
async def agent(redis):
    await hub.set_presence(redis, {"agent_id": "test-agent", "state": "idle"})
    return redis


@pytest.fixture(autouse=True)
async def _clean():
    yield
    await service.wipe(async_session_factory)


# ── keys ──────────────────────────────────────────────────────────────


async def test_keys_layout():
    assert keys.match_type_slug("QUAL") == "QUAL"
    assert keys.match_type_slug("Practice") == "PRACTICE"
    assert keys.match_type_slug(None) == "MATCH"
    assert keys.match_number_slug("QUAL", 1, 36) == "36"
    assert keys.match_number_slug("SF", 2, 1) == "2-1"
    assert keys.recording_key("QUAL", "36") == "QUAL/36/match.mp4"
    assert keys.moment_key("SF", "2-1", 3) == "SF/2-1/segments/moment-3.mp4"


async def test_parse_match_tuple_and_teams():
    info = service.parse_match(_match(36))
    assert info["match_key"] == "1:0:QUAL:1:36"
    assert (info["match_type"], info["match_number"]) == ("QUAL", "36")
    assert info["teams"] == ["1234A", "5678B"]
    assert info["alliances"] == {"red": ["1234A"], "blue": ["5678B"]}


async def test_parse_match_requires_round_and_number():
    with pytest.raises(ValueError):
        service.parse_match({"red_teams": ["1A"]})


# ── start / stop / mark ───────────────────────────────────────────────


async def test_start_requires_agent(redis):
    with pytest.raises(IntegrationError):
        await service.start_recording(redis, async_session_factory, _match(9001))


async def test_start_stop_and_commands(agent):
    result = await service.start_recording(agent, async_session_factory, _match(9002))
    rec = result["recording"]
    assert rec["status"] == "RECORDING" and rec["teams"] == ["1234A", "5678B"]
    stopped = await service.stop_recording(agent, async_session_factory)
    assert stopped["recording"]["status"] == "STOPPED"
    # idempotent
    assert (await service.stop_recording(agent, async_session_factory))["recording"] is None


async def test_start_stops_previous_recording(agent):
    first = (await service.start_recording(agent, async_session_factory, _match(9003)))["recording"]
    await service.start_recording(agent, async_session_factory, _match(9004))
    async with async_session_factory() as session:
        row = await session.get(CaptureRecording, uuid.UUID(first["id"]))
        assert row.status == "STOPPED"


async def test_mark_numbers_moments_in_order_and_rejects_when_idle(agent):
    with pytest.raises(service.CaptureConflict):
        await service.mark_moment(agent, async_session_factory, 5)
    await service.start_recording(agent, async_session_factory, _match(9005))
    numbers = [(await service.mark_moment(agent, async_session_factory, n))["moment"]["number"] for n in (3, 5, 10)]
    assert numbers == [1, 2, 3]
    with pytest.raises(ValueError):
        await service.mark_moment(agent, async_session_factory, 0)
    with pytest.raises(ValueError):
        await service.mark_moment(agent, async_session_factory, 31)
    await service.stop_recording(agent, async_session_factory)
    with pytest.raises(service.CaptureConflict):
        await service.mark_moment(agent, async_session_factory, 5)


async def test_mark_requires_agent(redis):
    with pytest.raises(IntegrationError):
        await service.mark_moment(redis, async_session_factory, 5)


async def test_restarting_a_match_resets_its_take(agent):
    await service.start_recording(agent, async_session_factory, _match(9006))
    await service.mark_moment(agent, async_session_factory, 5)
    await service.stop_recording(agent, async_session_factory)
    again = (await service.start_recording(agent, async_session_factory, _match(9006)))["recording"]
    async with async_session_factory() as session:
        moments = (await session.execute(select(CaptureMoment).where(CaptureMoment.recording_id == uuid.UUID(again["id"])))).scalars().all()
        assert moments == []


# ── agent messages ────────────────────────────────────────────────────


async def test_agent_messages_update_state(agent):
    rec = (await service.start_recording(agent, async_session_factory, _match(9007)))["recording"]
    moment = (await service.mark_moment(agent, async_session_factory, 5))["moment"]
    await service.handle_agent_message(agent, async_session_factory, {"type": "moment_cut", "moment_id": moment["id"], "end_offset_ms": 12345})
    await service.handle_agent_message(agent, async_session_factory, {"type": "recording_stopped", "recording_id": rec["id"], "duration_s": 75.5})
    async with async_session_factory() as session:
        m = await session.get(CaptureMoment, uuid.UUID(moment["id"]))
        r = await session.get(CaptureRecording, uuid.UUID(rec["id"]))
        assert (m.status, m.end_offset_ms) == ("CUT", 12345)
        assert (r.status, r.duration_s) == ("STOPPED", 75.5)
    await service.handle_agent_message(agent, async_session_factory, {"type": "error", "moment_id": moment["id"], "detail": "ffmpeg exited 1"})
    async with async_session_factory() as session:
        m = await session.get(CaptureMoment, uuid.UUID(moment["id"]))
        assert (m.status, m.error) == ("ERROR", "ffmpeg exited 1")


# ── uploads (real MinIO) ──────────────────────────────────────────────


async def _put(grant: dict, body: bytes) -> None:
    async with httpx.AsyncClient() as http:
        resp = await http.put(grant["url"], content=body, headers=grant["headers"])
        assert resp.status_code == 200, resp.text


async def test_match_and_moment_upload_flow(agent):
    rec = (await service.start_recording(agent, async_session_factory, _match(9008)))["recording"]
    moment = (await service.mark_moment(agent, async_session_factory, 5))["moment"]
    await service.stop_recording(agent, async_session_factory)
    rid = uuid.UUID(rec["id"])

    grant = await service.request_upload(async_session_factory, "match", rid, None)
    assert grant["object_key"] == "QUAL/9008/match.mp4"
    with pytest.raises(service.CaptureConflict):  # nothing uploaded yet
        await service.complete_upload(agent, async_session_factory, grant["upload_id"], "match", None, None)
    await _put(grant, b"x" * 2048)
    done = await service.complete_upload(agent, async_session_factory, grant["upload_id"], "match", None, 70.0)
    assert done["recording"]["status"] == "READY"

    mgrant = await service.request_upload(async_session_factory, "moment", rid, uuid.UUID(moment["id"]))
    assert mgrant["object_key"] == "QUAL/9008/segments/moment-1.mp4"
    await _put(mgrant, b"y" * 512)
    mdone = await service.complete_upload(agent, async_session_factory, mgrant["upload_id"], "moment", 4200, None)
    assert mdone["moment"]["status"] == "READY" and mdone["moment"]["ready_ms"] == 4200

    async with async_session_factory() as session:
        row = await session.get(CaptureRecording, rid)
        assert (row.size_bytes, row.duration_s, row.object_key) == (2048, 70.0, "QUAL/9008/match.mp4")


async def test_upload_for_unknown_ids(agent):
    with pytest.raises(LookupError):
        await service.request_upload(async_session_factory, "match", uuid.uuid4(), None)
    rec = (await service.start_recording(agent, async_session_factory, _match(9009)))["recording"]
    with pytest.raises(LookupError):
        await service.request_upload(async_session_factory, "moment", uuid.UUID(rec["id"]), uuid.uuid4())


async def test_wipe_removes_rows_and_objects(agent):
    rec = (await service.start_recording(agent, async_session_factory, _match(9010)))["recording"]
    await service.stop_recording(agent, async_session_factory)
    grant = await service.request_upload(async_session_factory, "match", uuid.UUID(rec["id"]), None)
    await _put(grant, b"z" * 100)
    await service.complete_upload(agent, async_session_factory, grant["upload_id"], "match", None, None)
    assert await s3.object_exists("QUAL/9010/match.mp4")
    result = await service.wipe(async_session_factory, "qual")
    assert result["recordings"] >= 1 and result["objects_deleted"] >= 1
    assert not await s3.object_exists("QUAL/9010/match.mp4")


# ── REST ──────────────────────────────────────────────────────────────


@pytest.fixture
async def client(redis):
    async def _redis():
        yield redis

    state = {"permissions": {ALL_PERMISSIONS}}

    async def _principal():
        return CurrentPrincipal(subject="t", permissions=state["permissions"]) if state["permissions"] is not None else None

    app.dependency_overrides[get_redis] = _redis
    app.dependency_overrides[get_current_principal] = _principal
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            c.state = state  # type: ignore[attr-defined]
            yield c
    finally:
        app.dependency_overrides.pop(get_redis, None)
        app.dependency_overrides.pop(get_current_principal, None)


async def test_rest_flow(client, agent):
    status = (await client.get("/api/v1/capture/status")).json()
    assert status["connected"] is True and status["recording"] is None

    started = await client.post("/api/v1/capture/recordings/start", json=_match(9011))
    assert started.status_code == 200, started.text
    status = (await client.get("/api/v1/capture/status")).json()
    assert status["recording"]["match_key"] == "1:0:QUAL:1:9011"

    marked = await client.post("/api/v1/capture/moments", json={"length_s": 5})
    assert marked.status_code == 200 and marked.json()["moment"]["number"] == 1
    assert (await client.post("/api/v1/capture/moments", json={"length_s": 99})).status_code == 422

    assert (await client.post("/api/v1/capture/recordings/stop")).status_code == 200
    assert (await client.post("/api/v1/capture/moments", json={"length_s": 5})).status_code == 409

    by_team = (await client.get("/api/v1/capture/recordings", params={"team": "5678b"})).json()
    assert [r["match_number"] for r in by_team] == ["9011"] and by_team[0]["moments"][0]["number"] == 1
    assert (await client.get("/api/v1/capture/recordings", params={"team": "0000Z"})).json() == []
    rid = by_team[0]["id"]
    assert (await client.get(f"/api/v1/capture/recordings/{rid}")).status_code == 200
    assert (await client.get(f"/api/v1/capture/recordings/{uuid.uuid4()}")).status_code == 404

    # not uploaded yet -> no playback URL, public video 409
    assert (await client.get(f"/api/v1/capture/recordings/{rid}/url")).status_code == 409
    assert (await client.get(f"/api/v1/public/recordings/{rid}/video")).status_code == 409


async def test_rest_agent_absent_is_503(client, redis):
    resp = await client.post("/api/v1/capture/recordings/start", json=_match(9012))
    assert resp.status_code == 503


async def test_rest_permissions(client, agent):
    client.state["permissions"] = None
    assert (await client.get("/api/v1/capture/status")).status_code == 401
    client.state["permissions"] = {"analysis:view"}
    assert (await client.get("/api/v1/capture/status")).status_code == 200
    assert (await client.post("/api/v1/capture/moments", json={"length_s": 5})).status_code == 403
    assert (await client.post("/api/v1/capture/uploads", json={"kind": "match", "recording_id": str(uuid.uuid4())})).status_code == 403
    client.state["permissions"] = {"capture:upload"}
    assert (await client.post("/api/v1/capture/uploads", json={"kind": "match", "recording_id": str(uuid.uuid4())})).status_code == 404
    assert (await client.get("/api/v1/capture/status")).status_code == 403


async def test_rest_upload_and_public_redirect(client, agent):
    rid = (await client.post("/api/v1/capture/recordings/start", json=_match(9013))).json()["recording"]["id"]
    await client.post("/api/v1/capture/recordings/stop")
    grant = (await client.post("/api/v1/capture/uploads", json={"kind": "match", "recording_id": rid})).json()
    await _put(grant, b"v" * 300)
    done = await client.post(f"/api/v1/capture/uploads/{grant['upload_id']}/complete", json={"kind": "match", "duration_s": 61})
    assert done.status_code == 200
    url = (await client.get(f"/api/v1/capture/recordings/{rid}/url")).json()
    assert url["label"] == "QUAL 9013" and "QUAL/9013/match.mp4" in url["url"]
    redirect = await client.get(f"/api/v1/public/recordings/{rid}/video")
    assert redirect.status_code == 302 and "QUAL/9013/match.mp4" in redirect.headers["location"]
    async with httpx.AsyncClient() as http:
        assert (await http.get(url["url"])).content == b"v" * 300

    wiped = await client.delete("/api/v1/capture/recordings", params={"match_type": "QUAL"})
    assert wiped.status_code == 200 and wiped.json()["recordings"] >= 1


# ── integration ───────────────────────────────────────────────────────


async def test_integration_services_and_status(agent, redis):
    statuses: list[str] = []

    async def hook(entity_id: str, status: str) -> None:
        statuses.append(status)

    integ = CaptureIntegration("capture.test", {}, agent, None)
    integ._status_hook = hook
    await integ.setup()
    try:
        assert statuses == ["CONNECTED"]
        await integ.call_service("start_recording", _match(9014))
        out = await integ.call_service("mark_moment", {"seconds": 5})
        assert out["moment"]["number"] == 1
        assert (await integ.call_service("stop_recording", {}))["recording"]["status"] == "STOPPED"
        assert (await integ.get_state())["connected"] is True
        with pytest.raises(IntegrationError):
            await integ.call_service("mark_moment", {"seconds": "abc"})
        with pytest.raises(ValueError):
            await integ.call_service("nope", {})
        await hub.clear_presence(agent)
        await integ._refresh_status()
        assert statuses[-1] == "DEGRADED"
        with pytest.raises(IntegrationError):
            await integ.call_service("start_recording", _match(9015))
    finally:
        await integ.teardown()


async def test_rest_replay_play_and_control(client, agent):
    rid = (await client.post("/api/v1/capture/recordings/start", json=_match(9014))).json()["recording"]["id"]
    await client.post("/api/v1/capture/recordings/stop")
    body = {"kind": "recording", "id": rid}
    assert (await client.post("/api/v1/capture/replay/main/play", json=body)).status_code == 409  # not uploaded
    assert (await client.post("/api/v1/capture/replay/bad id!/play", json=body)).status_code in (404, 422)

    grant = (await client.post("/api/v1/capture/uploads", json={"kind": "match", "recording_id": rid})).json()
    await _put(grant, b"v" * 300)
    await client.post(f"/api/v1/capture/uploads/{grant['upload_id']}/complete", json={"kind": "match", "duration_s": 61})

    played = await client.post("/api/v1/capture/replay/main/play", json=body)
    assert played.status_code == 200, played.text
    state = (await client.get("/api/v1/capture/replay/main")).json()
    assert state["playing"] is True and state["rate"] == 0.5 and "QUAL/9014/match.mp4" in state["url"]

    assert (await client.post("/api/v1/capture/replay/main/control", json={"action": "pause"})).json()["state"]["paused"] is True
    assert (await client.post("/api/v1/capture/replay/main/control", json={"action": "rate", "value": 0.25})).json()["state"]["rate"] == 0.25
    assert (await client.post("/api/v1/capture/replay/main/control", json={"action": "rate"})).status_code == 422
    assert (await client.post("/api/v1/capture/replay/main/control", json={"action": "nope"})).status_code == 422
    assert (await client.post("/api/v1/capture/replay/main/control", json={"action": "stop"})).json()["state"] == {"playing": False}
    assert (await client.post("/api/v1/capture/replay/main/control", json={"action": "pause"})).status_code == 409


async def test_rest_replay_permissions(client, agent):
    client.state["permissions"] = {"analysis:view"}
    assert (await client.get("/api/v1/capture/replay/main")).status_code == 200
    assert (await client.post("/api/v1/capture/replay/main/control", json={"action": "stop"})).status_code == 403
    assert (await client.post("/api/v1/capture/replay/main/play", json={"kind": "recording", "id": str(uuid.uuid4())})).status_code == 403
