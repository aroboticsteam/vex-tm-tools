"""Tests for routers/team_clips.py: upload -> real ffmpeg keying -> (mocked)
S3, status reporting, the public overlay feed, and permissions."""
from __future__ import annotations

import shutil
import subprocess
import uuid
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.core.db import engine as app_engine
from backend.core.exceptions import ProcessingError
from backend.core.settings import settings
from backend.models.team import TeamProfile
from backend.models.team_clip import TeamClip

pytestmark = pytest.mark.asyncio

FFMPEG = shutil.which("ffmpeg") is not None


@pytest.fixture(autouse=True)
async def _reset_pool():
    await app_engine.dispose()
    yield
    await app_engine.dispose()


@pytest.fixture
async def factory():
    engine = create_async_engine(settings.POSTGRES_DSN)
    try:
        async with engine.connect() as conn:
            await conn.exec_driver_sql("SELECT 1 FROM team_clips LIMIT 1")
    except Exception:
        await engine.dispose()
        pytest.skip("Real Postgres (migrated) not reachable; skipping clip tests")
    yield async_sessionmaker(bind=engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def team(factory):
    number = f"C{uuid.uuid4().hex[:6]}"
    yield number
    async with factory() as session:
        await session.execute(delete(TeamClip).where(TeamClip.team_number == number))
        await session.execute(delete(TeamProfile).where(TeamProfile.team_number == number))
        await session.commit()


@pytest.fixture
def s3_mock(monkeypatch):
    from backend.routers import team_clips

    upload = AsyncMock()
    monkeypatch.setattr(team_clips.s3, "upload_multipart", upload)
    monkeypatch.setattr(team_clips.s3, "generate_presigned_url", AsyncMock(side_effect=lambda key: f"https://s3.test/{key}"))
    return upload


def _client(permissions):
    from backend.core.dependencies import CurrentPrincipal, get_current_principal
    from backend.main import app

    async def _principal():
        return CurrentPrincipal(subject="tester", permissions=set(permissions))

    app.dependency_overrides[get_current_principal] = _principal
    return app, AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture
async def admin():
    from backend.core.dependencies import get_current_principal

    app, client = _client({"*"})
    async with client:
        yield client
    app.dependency_overrides.pop(get_current_principal, None)


@pytest.fixture(scope="module")
def green_clip(tmp_path_factory):
    path = tmp_path_factory.mktemp("clips") / "green.mp4"
    if FFMPEG:
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=0x00B140:s=640x640:r=60:d=0.5", "-pix_fmt", "yuv420p", str(path)],
            check=True,
            capture_output=True,
        )
    return path


async def test_clips_status_empty_then_unknown_kind(admin, team):
    resp = await admin.get(f"/api/v1/teams/{team}/clips")
    assert resp.status_code == 200
    assert resp.json()["clips"]["bot"]["status"] == "NONE" and resp.json()["clips"]["team"]["status"] == "NONE"
    resp = await admin.post(f"/api/v1/teams/{team}/clips/robot", files={"file": ("a.mp4", b"x", "video/mp4")})
    assert resp.status_code == 404


async def test_rejects_bad_extension(admin, team, s3_mock):
    resp = await admin.post(f"/api/v1/teams/{team}/clips/bot", files={"file": ("a.exe", b"x", "application/octet-stream")})
    assert resp.status_code == 400
    s3_mock.assert_not_awaited()


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not installed")
async def test_upload_processes_both_kinds_independently(admin, team, s3_mock, green_clip, factory):
    data = green_clip.read_bytes()
    for kind in ("bot", "team"):
        resp = await admin.post(
            f"/api/v1/teams/{team}/clips/{kind}", files={"file": ("rec.mp4", data, "video/mp4")}
        )
        assert resp.status_code == 202

    clips = (await admin.get(f"/api/v1/teams/{team}/clips")).json()["clips"]
    assert clips["bot"]["status"] == "DONE" and clips["team"]["status"] == "DONE"
    assert clips["bot"]["processed_s3_key"] == f"teams/{team}/clips/bot/processed.webm"
    assert clips["team"]["processed_s3_key"] == f"teams/{team}/clips/team/processed.webm"
    uploaded = {call.args[1] for call in s3_mock.await_args_list}
    assert f"teams/{team}/clips/bot/raw.mp4" in uploaded and f"teams/{team}/clips/team/processed.webm" in uploaded


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not installed")
async def test_processing_failure_is_reported(admin, team, s3_mock):
    resp = await admin.post(f"/api/v1/teams/{team}/clips/bot", files={"file": ("rec.mp4", b"not a video", "video/mp4")})
    assert resp.status_code == 202
    clip = (await admin.get(f"/api/v1/teams/{team}/clips")).json()["clips"]["bot"]
    assert clip["status"] == "FAILED" and clip["error"]


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not installed")
async def test_reupload_replaces_failed_state(admin, team, s3_mock, green_clip):
    await admin.post(f"/api/v1/teams/{team}/clips/bot", files={"file": ("rec.mp4", b"junk", "video/mp4")})
    await admin.post(f"/api/v1/teams/{team}/clips/bot", files={"file": ("rec.mp4", green_clip.read_bytes(), "video/mp4")})
    clip = (await admin.get(f"/api/v1/teams/{team}/clips")).json()["clips"]["bot"]
    assert clip["status"] == "DONE" and clip["error"] is None


async def test_public_overlay_feed(admin, team, factory, s3_mock):
    async with factory() as session:
        session.add(
            TeamProfile(
                team_number=team,
                robot_name="Bolt",
                cached_stats={
                    "team_name": "Gearsquad",
                    "organisation": "QE School",
                    "location": "London, United Kingdom",
                    "ranking": {"rank": 3},
                    "skills_rank": 2,
                },
            )
        )
        session.add(TeamClip(team_number=team, kind="team", status="DONE", processed_s3_key=f"teams/{team}/clips/team/processed.webm"))
        await session.commit()
    # Public: no auth principal needed beyond the (overridden) default.
    resp = await admin.get(f"/api/v1/public/overlay/teams?teams={team},NOPE")
    assert resp.status_code == 200
    info = resp.json()["teams"][team]
    assert info["team_name"] == "Gearsquad" and info["organisation"] == "QE School"
    assert info["country"] == "United Kingdom" and info["qual_rank"] == 3 and info["skills_rank"] == 2
    assert info["team_url"] == f"https://s3.test/teams/{team}/clips/team/processed.webm"
    assert info["bot_url"] is None
    assert resp.json()["teams"]["NOPE"]["team_name"] is None


async def test_public_overlay_bot_falls_back_to_legacy_video(admin, team, factory, s3_mock):
    async with factory() as session:
        session.add(
            TeamProfile(team_number=team, video_360_s3_key=f"teams/{team}/video/processed.webm", video_processing_status="DONE")
        )
        await session.commit()
    info = (await admin.get(f"/api/v1/public/overlay/teams?teams={team}")).json()["teams"][team]
    assert info["bot_url"] == f"https://s3.test/teams/{team}/video/processed.webm"


async def test_public_overlay_limits_and_presign_failures(admin, team, factory, monkeypatch, s3_mock):
    assert (await admin.get("/api/v1/public/overlay/teams?teams=" + ",".join(f"T{i}" for i in range(13)))).status_code == 422
    async with factory() as session:
        session.add(TeamClip(team_number=team, kind="bot", status="DONE", processed_s3_key="k"))
        session.add(TeamProfile(team_number=team))
        await session.commit()
    from backend.routers import team_clips

    monkeypatch.setattr(team_clips.s3, "generate_presigned_url", AsyncMock(side_effect=ProcessingError("boom")))
    info = (await admin.get(f"/api/v1/public/overlay/teams?teams={team}")).json()["teams"][team]
    assert info["bot_url"] is None


async def test_upload_requires_video_upload_permission(team, s3_mock):
    from backend.core.dependencies import get_current_principal

    app, client = _client({"teams:read"})
    try:
        async with client:
            resp = await client.post(f"/api/v1/teams/{team}/clips/bot", files={"file": ("a.mp4", b"x", "video/mp4")})
            assert resp.status_code == 403
            assert (await client.get(f"/api/v1/teams/{team}/clips")).status_code == 200
    finally:
        app.dependency_overrides.pop(get_current_principal, None)
