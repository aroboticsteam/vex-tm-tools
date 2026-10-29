"""Public website data API (issue #10) against real Postgres + fakeredis."""
from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock

import fakeredis
import pytest
from fakeredis import aioredis as fake_aioredis
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.core.settings import settings
from backend.models.comms import AgendaItem
from backend.models.room import Room
from backend.models.settings import SystemSetting
from backend.models.team import TeamProfile
from backend.models.team_clip import TeamClip

pytestmark = pytest.mark.asyncio

PREFIX = "PS"


@pytest.fixture
async def session_factory():
    engine = create_async_engine(settings.POSTGRES_DSN, future=True)
    try:
        async with engine.connect() as conn:
            await conn.exec_driver_sql("SELECT 1 FROM agenda_items LIMIT 1")
    except Exception:
        await engine.dispose()
        pytest.skip("Real Postgres (migrated) not reachable; skipping public site tests")
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    async def wipe():
        async with factory() as session:
            await session.execute(delete(TeamClip).where(TeamClip.team_number.like(f"{PREFIX}%")))
            await session.execute(delete(TeamProfile))
            await session.execute(delete(AgendaItem))
            await session.execute(delete(Room).where(Room.slug.like("ps-%")))
            await session.execute(delete(SystemSetting).where(SystemSetting.key == "site"))
            await session.commit()

    await wipe()
    yield factory
    await wipe()
    await engine.dispose()


@pytest.fixture
async def client(session_factory, monkeypatch):
    from backend.core.db import get_db
    from backend.core.dependencies import get_redis
    from backend.main import create_app
    from backend.routers import team_clips

    redis = fake_aioredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)
    app = create_app()

    async def _db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_redis] = lambda: redis
    monkeypatch.setattr(team_clips.s3, "generate_presigned_url", AsyncMock(side_effect=lambda key: f"https://s3.test/{key}"))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        c.redis, c.factory = redis, session_factory
        yield c
    await redis.aclose()


def raw(num, at, red, blue, state="UNPLAYED", score=None, division=1):
    out = {
        "matchInfo": {
            "timeScheduled": at,
            "state": state,
            "alliances": [{"teams": [{"number": t} for t in red]}, {"teams": [{"number": t} for t in blue]}],
            "matchTuple": {"session": 0, "division": division, "round": "QUAL", "instance": 1, "match": num},
        }
    }
    if score:
        out["finalScore"] = score
    return out


async def seed(client):
    now = time.time()
    async with client.factory() as session:
        session.add_all(
            [
                TeamProfile(
                    team_number=f"{PREFIX}1A",
                    pit_location="M9",
                    robot_name="Bolt",
                    extra_notes="INTERNAL ONLY",
                    cached_stats={
                        "team_name": "Gearsquad", "organisation": "QE School", "location": "London, United Kingdom",
                        "country": "United Kingdom", "division_id": 1, "event": {"name": "QE Battle"},
                        "ranking": {"rank": 1, "wins": 4, "losses": 0, "ties": 0, "avgPoints": 69.5, "highScore": 90},
                        "skills": [{"rank": 2, "number": f"{PREFIX}1A", "totalScore": 40, "driverHighScore": 17, "progHighScore": 23, "driverAttempts": 3, "progAttempts": 1}],
                        "qual_avg": 70.0, "qual_high": 90, "awards": [{"title": "Excellence"}], "previous_rankings": [{"event": {"name": "Comp 1"}, "rank": 3}],
                        "skills_rank": 12,
                    },
                ),
                TeamProfile(team_number=f"{PREFIX}2B", cached_stats={"team_name": "Beta", "division_id": 1, "ranking": {"rank": 2}}),
                TeamProfile(team_number=f"{PREFIX}3C"),  # no stats at all
            ]
        )
        session.add(AgendaItem(scope="day", start_time="09:00", title="Opening"))
        session.add(Room(slug="ps-main", name="Main Hall", stream_url="https://www.youtube.com/embed/abc123"))
        session.add(Room(slug="ps-quiet", name="Quiet Room"))
        session.add(TeamClip(team_number=f"{PREFIX}1A", kind="team", status="DONE", processed_s3_key=f"teams/{PREFIX}1A/clips/team/processed.webm"))
        session.add(
            SystemSetting(
                key="site",
                value={"title": "QE Battle", "next_competition": {"name": "Nationals", "date": "2027-01-10", "teams": [f"{PREFIX}1A"]},
                       "sponsors": [{"name": "Kingston"}], "map": {"image_url": "/m.png", "rooms": [{"name": "M9", "x": 10, "y": 20}]}, "presented_by": "Kingston"},
            )
        )
        await session.commit()
    schedule = {"divisions": [{"id": 1, "matches": [
        raw(1, now - 600, [f"{PREFIX}1A"], [f"{PREFIX}2B"], "SCORED", [50, 30]),
        raw(2, now + 600, [f"{PREFIX}2B"], [f"{PREFIX}1A"]),
    ]}]}
    await client.redis.set("qecomp:tm:vex_tm.t:schedule", json.dumps(schedule))


async def test_overview_with_and_without_config(client):
    empty = (await client.get("/api/v1/public/site/overview")).json()
    assert empty["title"] == "Competition" and empty["stats"]["teams"] == 0 and empty["sponsors"] == [] and empty["next_competition"] is None
    await seed(client)
    o = (await client.get("/api/v1/public/site/overview")).json()
    assert o["title"] == "QE Battle" and o["event"] == "QE Battle"
    assert o["stats"] == {"teams": 3, "matches_played": 1, "matches_total": 2, "awards": 1}
    assert o["next_competition"]["name"] == "Nationals" and o["sponsors"] == [{"name": "Kingston", "url": None, "logo_url": None}]
    assert o["map"]["rooms"][0]["name"] == "M9" and o["presented_by"] == "Kingston"


async def test_event_rankings_skills_matches_agenda_and_programme(client):
    await seed(client)
    d = (await client.get("/api/v1/public/site/event")).json()
    assert [r["team"] for r in d["rankings"]] == [f"{PREFIX}1A", f"{PREFIX}2B"]
    assert d["rankings"][0]["wins"] == 4 and d["rankings"][0]["avg_points"] == 69.5
    assert d["skills"] == [{"rank": 2, "team": f"{PREFIX}1A", "team_name": "Gearsquad", "division": 1, "total": 40, "driver": 17, "prog": 23, "driver_attempts": 3, "prog_attempts": 1}]
    assert [m["label"] for m in d["matches"]] == ["Q1", "Q2"]
    assert d["matches"][0]["score"] == [50, 30] and d["matches"][0]["scored"] and d["matches"][1]["score"] is None
    assert d["divisions"] == {"1": {"programme": "iq"}}
    assert d["agenda"][0]["title"] == "Opening"


async def test_programme_vrc_for_four_team_matches(client):
    await client.redis.set(
        "qecomp:tm:vex_tm.t:schedule",
        json.dumps({"divisions": [{"id": 2, "matches": [raw(1, time.time(), ["A", "B"], ["C", "D"], division=2)]}]}),
    )
    assert (await client.get("/api/v1/public/site/event")).json()["divisions"] == {"2": {"programme": "vrc"}}


async def test_teams_list_handles_missing_stats(client):
    await seed(client)
    teams = (await client.get("/api/v1/public/site/teams")).json()
    by = {t["team_number"]: t for t in teams}
    assert by[f"{PREFIX}1A"]["qual_rank"] == 1 and by[f"{PREFIX}1A"]["skills_rank"] == 2 and by[f"{PREFIX}1A"]["organisation"] == "QE School"
    assert by[f"{PREFIX}3C"]["qual_rank"] is None and by[f"{PREFIX}3C"]["team_name"] is None


async def test_team_detail_schedule_clips_history_and_no_internal_fields(client):
    await seed(client)
    resp = await client.get(f"/api/v1/public/site/teams/{PREFIX}1A")
    assert resp.status_code == 200
    t = resp.json()
    assert "INTERNAL ONLY" not in resp.text and "extra_notes" not in t
    assert t["pit_location"] == "M9" and t["robot_name"] == "Bolt"
    assert t["current"]["qual_rank"] == 1 and t["current"]["skills_total"] == 40 and t["current"]["avg_points"] == 70.0
    assert t["season"]["awards"] == 1 and t["season"]["events_attended"] == 1 and t["season"]["world_skills_rank"] == 12
    assert [m["label"] for m in t["schedule"]] == ["Q1", "Q2"]
    assert t["clips"] == {"bot": None, "team": f"https://s3.test/teams/{PREFIX}1A/clips/team/processed.webm"}


async def test_team_detail_unknown_404_and_sparse_team_ok(client):
    await seed(client)
    assert (await client.get("/api/v1/public/site/teams/NOPE")).status_code == 404
    sparse = (await client.get(f"/api/v1/public/site/teams/{PREFIX}3C")).json()
    assert sparse["schedule"] == [] and sparse["current"]["qual_rank"] is None and sparse["season"]["awards"] == 0


async def test_livestreams_only_rooms_with_streams(client):
    await seed(client)
    assert (await client.get("/api/v1/public/site/livestreams")).json() == [
        {"slug": "ps-main", "name": "Main Hall", "stream_url": "https://www.youtube.com/embed/abc123"}
    ]


async def test_site_page_served(client):
    page = await client.get("/site")
    assert page.status_code == 200 and "text/html" in page.headers["content-type"] and "/api/v1/public/site/" in page.text
