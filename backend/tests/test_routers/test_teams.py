"""Tests for routers/teams.py (plan §14, against real Postgres per the task's
verification requirement — `httpx.AsyncClient` + `ASGITransport` against the
real FastAPI app, matching the pattern in tests/test_core/test_health.py)."""
from __future__ import annotations

import asyncio
import shutil
import subprocess
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import create_async_engine

from backend.core.db import engine as app_engine
from backend.core.settings import settings
from backend.main import app
from backend.models.team import TeamProfile

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _reset_app_engine_pool():
    """`backend.core.db.engine` is a module-level singleton bound to
    whichever event loop was running when its connection pool first opened a
    connection. Since pytest-asyncio gives each test function its own event
    loop by default, reusing a pooled connection from a previous test's loop
    raises `RuntimeError: ... attached to a different loop`. Disposing the
    pool before each test forces fresh connections to be opened lazily
    against the *current* test's loop.
    """
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
        pytest.skip("Real Postgres not reachable; skipping teams router tests")
    yield engine
    await engine.dispose()


@pytest.fixture
async def client():
    from backend.core.dependencies import ALL_PERMISSIONS, CurrentPrincipal, get_current_principal

    # This file tests CRUD/business logic, not RBAC itself (that's covered
    # by `tests/test_core/test_rbac.py`) — bypass auth with an
    # all-permissions principal so requests here don't need a real
    # session/API key.
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
async def seeded_team(db_engine):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    number = f"T{uuid.uuid4().hex[:6]}"
    async with factory() as session:
        session.add(
            TeamProfile(
                team_number=number,
                pit_location="Bay 1",
                bio="A team",
                video_processing_status="NONE",
            )
        )
        await session.commit()

    yield number

    async with factory() as session:
        await session.execute(delete(TeamProfile).where(TeamProfile.team_number == number))
        await session.commit()


async def test_get_team_404_when_missing(client):
    resp = await client.get(f"/api/v1/teams/does-not-exist-{uuid.uuid4().hex}")
    assert resp.status_code == 404


async def test_list_teams_includes_seeded_team(client, seeded_team):
    resp = await client.get("/api/v1/teams")
    assert resp.status_code == 200
    numbers = [t["team_number"] for t in resp.json()]
    assert seeded_team in numbers


async def test_get_team_returns_profile(client, seeded_team):
    resp = await client.get(f"/api/v1/teams/{seeded_team}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["team_number"] == seeded_team
    assert body["pit_location"] == "Bay 1"


async def test_update_team_edits_pit_location_and_notes(client, seeded_team):
    resp = await client.put(
        f"/api/v1/teams/{seeded_team}",
        json={"pit_location": "Bay 42", "extra_notes": "Bring extra batteries"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["pit_location"] == "Bay 42"
    assert body["extra_notes"] == "Bring extra batteries"

    # Persisted.
    resp2 = await client.get(f"/api/v1/teams/{seeded_team}")
    assert resp2.json()["pit_location"] == "Bay 42"


async def test_update_team_404_when_missing(client):
    resp = await client.put(
        f"/api/v1/teams/does-not-exist-{uuid.uuid4().hex}",
        json={"pit_location": "X"},
    )
    assert resp.status_code == 404


async def test_update_team_rejects_oversized_pit_location(client, seeded_team):
    """Regression test (code-review finding): TeamProfileUpdate.pit_location
    must reject values longer than the `String(100)` DB column so an
    oversized value gets a clean 422, not an unhandled DataError at commit."""
    resp = await client.put(
        f"/api/v1/teams/{seeded_team}",
        json={"pit_location": "x" * 101},
    )
    assert resp.status_code == 422


async def test_update_team_rejects_unknown_fields(client, seeded_team):
    resp = await client.put(
        f"/api/v1/teams/{seeded_team}",
        json={"video_360_s3_key": "should-not-be-editable-via-this-endpoint"},
    )
    assert resp.status_code == 422


async def test_video_status_reflects_db_state(client, seeded_team):
    resp = await client.get(f"/api/v1/teams/{seeded_team}/video/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["video_processing_status"] == "NONE"
    assert body["video_360_s3_key"] is None


async def test_video_status_404_when_missing(client):
    resp = await client.get(f"/api/v1/teams/does-not-exist-{uuid.uuid4().hex}/video/status")
    assert resp.status_code == 404


async def test_upload_video_rejects_bad_extension(client, seeded_team):
    resp = await client.post(
        f"/api/v1/teams/{seeded_team}/video",
        files={"file": ("video.avi", b"not a real video", "video/avi")},
        data={"key_colour": "#00B140", "similarity": "0.1", "blend": "0.05"},
    )
    assert resp.status_code == 400


async def test_batch_videos_returns_null_for_unprocessed_teams(client, seeded_team):
    resp = await client.get(f"/api/v1/teams/batch/videos?teams={seeded_team},nonexistent")
    assert resp.status_code == 200
    body = resp.json()
    assert body["videos"][seeded_team] is None
    assert body["videos"]["nonexistent"] is None


async def test_batch_videos_empty_teams_param(client):
    resp = await client.get("/api/v1/teams/batch/videos?teams=")
    assert resp.status_code == 200
    assert resp.json()["videos"] == {}


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg not installed"
)
async def test_upload_video_end_to_end_real_ffmpeg_and_s3(client, seeded_team, tmp_path):
    """Full pipeline through the real HTTP endpoint: upload -> background
    task runs real FFmpeg -> real S3 (MinIO) multipart upload -> DB updated
    to DONE with a fetchable processed video key. Skipped if MinIO/S3 isn't
    configured (checked indirectly via the status settling to DONE/FAILED;
    if S3 is unreachable the background task will mark FAILED and this test
    fails loudly rather than silently skipping, since S3 is expected to be
    up per the task's compose stack)."""
    input_path = tmp_path / "input.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=0x00B140:s=320x240:d=0.3",
            "-vf", "format=yuv420p", str(input_path),
        ],
        check=True, capture_output=True,
    )

    with open(input_path, "rb") as f:
        resp = await client.post(
            f"/api/v1/teams/{seeded_team}/video",
            files={"file": ("input.mp4", f.read(), "video/mp4")},
            data={"key_colour": "#00B140", "similarity": "0.15", "blend": "0.05"},
        )
    assert resp.status_code == 202
    assert resp.json()["video_processing_status"] == "PROCESSING"

    # The background task runs on the app's own event loop after the
    # response is sent; poll the status endpoint until it settles.
    status = "PROCESSING"
    for _ in range(60):
        status_resp = await client.get(f"/api/v1/teams/{seeded_team}/video/status")
        status = status_resp.json()["video_processing_status"]
        if status in ("DONE", "FAILED"):
            break
        await asyncio.sleep(0.5)

    assert status == "DONE", f"video processing did not complete successfully (status={status})"

    final = await client.get(f"/api/v1/teams/{seeded_team}/video/status")
    assert final.json()["video_360_s3_key"] == f"teams/{seeded_team}/video/processed.webm"

    batch = await client.get(f"/api/v1/teams/batch/videos?teams={seeded_team}")
    url = batch.json()["videos"][seeded_team]
    assert url is not None and "processed.webm" in url

    # Clean up the S3 objects this test created.
    from backend.modules.media import s3

    await s3.delete_object(f"teams/{seeded_team}/video/raw.mp4")
    await s3.delete_object(f"teams/{seeded_team}/video/processed.webm")


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
async def test_raw_video_persisted_even_when_processing_fails(seeded_team, tmp_path):
    """Regression test (code-review finding): if FFmpeg processing fails
    after the raw upload already succeeded, `raw_video_s3_key` must still be
    persisted — otherwise the raw video is orphaned in S3 with no DB
    reference, defeating Appendix A.5's "kept in S3 permanently... allows
    re-processing" guarantee. Invokes the background task body directly
    with an out-of-range `similarity` so `process_video` reliably fails
    validation after the (concurrent) raw upload has already completed.
    """
    from backend.core.db import async_session_factory
    from backend.modules.media import s3
    from backend.routers.teams import _process_and_upload_video

    input_path = tmp_path / "input.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=0x00B140:s=320x240:d=0.3",
            "-vf", "format=yuv420p", str(input_path),
        ],
        check=True, capture_output=True,
    )

    raw_key = s3.raw_video_key(seeded_team, "mp4")
    try:
        await _process_and_upload_video(
            team_number=seeded_team,
            input_path=str(input_path),
            ext="mp4",
            key_colour="#00B140",
            similarity=99.0,  # invalid — out of [0.0, 1.0], guaranteed ProcessingError
            blend=0.05,
            tmp_dir=str(tmp_path),
        )

        assert await s3.object_exists(raw_key) is True

        async with async_session_factory() as session:
            team = await session.get(TeamProfile, seeded_team)
            assert team.video_processing_status == "FAILED"
            assert team.raw_video_s3_key == raw_key
    finally:
        await s3.delete_object(raw_key)


async def test_update_inspection_status_and_checklist(client, seeded_team):
    resp = await client.put(f"/api/v1/teams/{seeded_team}", json={"inspection_status": "PASSED"})
    assert resp.status_code == 200
    assert resp.json()["inspection_status"] == "PASSED"
    listing = await client.get("/api/v1/teams/checklist")
    assert listing.status_code == 200
    row = next(r for r in listing.json() if r["team_number"] == seeded_team)
    assert row["inspection"] == "PASSED"
    assert (row["done"], row["total"], row["complete"]) == (1, 3, False)
    assert (row["bot_video"], row["team_video"]) == (False, False)


async def test_inspection_status_rejects_unknown_value(client, seeded_team):
    resp = await client.put(f"/api/v1/teams/{seeded_team}", json={"inspection_status": "MAYBE"})
    assert resp.status_code == 422
    assert (await client.get(f"/api/v1/teams/{seeded_team}")).json()["inspection_status"] == "NOT_STARTED"


async def test_checklist_reflects_done_clips(client, seeded_team, db_engine):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from backend.models.team_clip import TeamClip

    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with factory() as session:
        session.add(TeamClip(team_number=seeded_team, kind="bot", status="DONE"))
        session.add(TeamClip(team_number=seeded_team, kind="team", status="PROCESSING"))
        await session.commit()
    try:
        row = next(r for r in (await client.get("/api/v1/teams/checklist")).json() if r["team_number"] == seeded_team)
        assert (row["bot_video"], row["team_video"], row["done"]) == (True, False, 1)
    finally:
        async with factory() as session:
            await session.execute(delete(TeamClip).where(TeamClip.team_number == seeded_team))
            await session.commit()


async def test_checklist_route_is_not_shadowed_by_team_number(client):
    # "/checklist" must hit the list route, not get_team(number="checklist")
    resp = await client.get("/api/v1/teams/checklist")
    assert resp.status_code == 200 and isinstance(resp.json(), list)
