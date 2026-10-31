"""The ffmpeg capture agent (`tools/capture_agent/agent.py`, issue #45).

Real ffmpeg on a synthetic `lavfi` source, the real FastAPI app (in-process) for
the control/upload API, real Postgres and MinIO for the presigned PUTs.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import shutil
import sys
import subprocess
import time
import uuid
from pathlib import Path

import fakeredis
import httpx
import pytest
from fakeredis import aioredis as fake_aioredis

from backend.core.db import async_session_factory, engine as app_engine
from backend.core.dependencies import ALL_PERMISSIONS, CurrentPrincipal, get_current_principal
from backend.core.redis import get_redis, redis_client
from backend.main import app
from backend.modules.capture import hub, service
from backend.modules.media import s3

AGENT_PATH = Path(__file__).resolve().parents[3] / "tools" / "capture_agent" / "agent.py"
spec = importlib.util.spec_from_file_location("capture_agent_tool", AGENT_PATH)
agent_mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = agent_mod  # dataclasses resolve annotations through sys.modules
spec.loader.exec_module(agent_mod)  # type: ignore[union-attr]

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed"),
]

SOURCE = "-f lavfi -i testsrc=size=320x240:rate=25"
NUM = 9100 + int(uuid.uuid4().int % 800)


@pytest.fixture(autouse=True)
async def _reset_pool():
    await app_engine.dispose()
    yield
    await service.wipe(async_session_factory)
    await app_engine.dispose()  # last, so no connection outlives this test's event loop
    await redis_client.connection_pool.disconnect()  # the app's pool must not keep this loop's sockets


@pytest.fixture
async def redis():
    client = fake_aioredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)
    await hub.set_presence(client, {"agent_id": "tool-test", "state": "idle"})
    yield client
    await client.aclose()


@pytest.fixture
async def api(redis):
    async def _redis():
        yield redis

    async def _principal():
        return CurrentPrincipal(subject="agent", permissions={ALL_PERMISSIONS})

    app.dependency_overrides[get_redis] = _redis
    app.dependency_overrides[get_current_principal] = _principal
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_redis, None)
        app.dependency_overrides.pop(get_current_principal, None)


def _settings(tmp_path: Path, **kw) -> "agent_mod.Settings":
    import shlex

    return agent_mod.Settings(
        server="http://test", key="qec_x", input_args=shlex.split(SOURCE), work_dir=tmp_path, video_bitrate="1M", **kw
    )


def _duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    return float(out.strip())


async def _wait(predicate, timeout: float = 30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await predicate():
            return
        await asyncio.sleep(0.2)
    raise AssertionError("condition not met in time")


async def test_record_mark_stop_and_upload_end_to_end(tmp_path, redis, api, monkeypatch):
    monkeypatch.setattr(agent_mod, "FLUSH_WAIT_S", 0.5)
    agent = agent_mod.CaptureAgent(_settings(tmp_path), client=api)
    reports: list[dict] = []

    async def send(msg: dict) -> None:
        reports.append(msg)
        await service.handle_agent_message(redis, async_session_factory, msg)

    agent.send = send
    agent.queue.start()
    try:
        started = await service.start_recording(
            redis, async_session_factory,
            {"match": {"division": 1, "session": 0, "round": "QUAL", "instance": 1, "match": NUM}, "red_teams": ["1234A"], "blue_teams": ["5678B"]},
        )
        rid = started["recording"]["id"]
        await agent.handle_command({"type": "start_recording", "recording_id": rid, "match_type": "QUAL", "match_number": str(NUM)})
        assert agent.state == "recording"
        await asyncio.sleep(4)

        marked = await service.mark_moment(redis, async_session_factory, 2)
        mid = marked["moment"]["id"]
        await agent.handle_command({"type": "mark", "recording_id": rid, "moment_id": mid, "number": 1, "length_s": 2})
        await _wait(lambda: _moment_ready(rid, mid))
        # Clip is a ~2 s, independently playable file, uploaded while still recording.
        assert agent.state == "recording"

        await service.stop_recording(redis, async_session_factory)
        await agent.handle_command({"type": "stop_recording", "recording_id": rid})
        assert agent.state == "idle"
        await _wait(lambda: _recording_ready(rid))
    finally:
        await agent.queue.stop()

    types = [r["type"] for r in reports]
    assert "moment_cut" in types and "recording_stopped" in types
    rec = (await api.get(f"/api/v1/capture/recordings/{rid}")).json()
    assert rec["status"] == "READY" and rec["duration_s"] and rec["duration_s"] >= 3.5
    moment = rec["moments"][0]
    assert moment["status"] == "READY" and moment["end_offset_ms"] and moment["ready_ms"] is not None

    clip = next((tmp_path / rid).glob("moment-*.mp4"))
    assert 1.2 <= _duration(clip) <= 3.0
    assert await s3.object_size(f"QUAL/{NUM}/match.mp4") == (tmp_path / rid / "match.mp4").stat().st_size
    assert await s3.object_size(f"QUAL/{NUM}/segments/moment-1.mp4") == clip.stat().st_size
    assert json.loads((tmp_path / "queue.json").read_text()) == []  # nothing left to send


async def _moment_ready(rid: str, mid: str) -> bool:
    async with async_session_factory() as session:
        from backend.models.capture import CaptureMoment

        row = await session.get(CaptureMoment, uuid.UUID(mid))
        return row is not None and row.status == "READY"


async def _recording_ready(rid: str) -> bool:
    async with async_session_factory() as session:
        from backend.models.capture import CaptureRecording

        row = await session.get(CaptureRecording, uuid.UUID(rid))
        return row is not None and row.status == "READY"


async def test_stream_copy_mode_uses_copy_codecs(tmp_path):
    args = agent_mod._encode_args(_settings(tmp_path, reencode=False))
    assert args == ["-c", "copy"]
    enc = agent_mod._encode_args(_settings(tmp_path, reencode=True))
    assert "libx264" in enc and "1M" in enc and enc[enc.index("-g") + 1] == "30"


async def test_start_fails_cleanly_on_bad_input(tmp_path, redis, api):
    settings = agent_mod.Settings("http://test", "k", ["-f", "lavfi", "-i", "nonsense_source=1"], tmp_path)
    agent = agent_mod.CaptureAgent(settings, client=api)
    reports: list[dict] = []

    async def send(msg: dict) -> None:
        reports.append(msg)

    agent.send = send
    await agent.handle_command({"type": "start_recording", "recording_id": str(uuid.uuid4()), "match_type": "QUAL", "match_number": "1"})
    assert agent.state == "idle"
    assert reports and reports[0]["type"] == "error" and "ffmpeg" in reports[0]["detail"]


async def test_mark_when_not_recording_reports_error(tmp_path, api):
    agent = agent_mod.CaptureAgent(_settings(tmp_path), client=api)
    reports: list[dict] = []

    async def send(msg: dict) -> None:
        reports.append(msg)

    agent.send = send
    await agent.handle_command({"type": "mark", "recording_id": "x", "moment_id": "m", "length_s": 3})
    await asyncio.gather(*agent._tasks)  # marks run in the background
    assert reports[0]["type"] == "error" and reports[0]["moment_id"] == "m"


async def test_queue_retries_with_backoff_and_survives_restart(tmp_path):
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/uploads"):
            attempts["n"] += 1
            if attempts["n"] == 1:
                return httpx.Response(503)
            return httpx.Response(200, json={"upload_id": "u1", "url": "http://store.invalid/put", "headers": {}, "object_key": "k"})
        return httpx.Response(200, json={})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://test")
    file = tmp_path / "a.mp4"
    file.write_bytes(b"x" * 10)
    q = agent_mod.UploadQueue(tmp_path, client, retry_delays=(0.05,))
    q.add(agent_mod.UploadJob("match", "rec-1", str(file)))
    q.add(agent_mod.UploadJob("match", "rec-1", str(file)))  # duplicate is ignored
    assert len(q.jobs) == 1 and json.loads(q.file.read_text())[0]["recording_id"] == "rec-1"

    # A fresh process reloads the pending job from disk.
    q2 = agent_mod.UploadQueue(tmp_path, client, retry_delays=(0.05,))
    q2.load()
    assert list(q2.jobs) == ["match:rec-1"]
    await client.aclose()
