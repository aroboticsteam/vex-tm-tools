"""Tests for backend.modules.scraper.Scraper (plan §14 test_scraper.py).

Uses the real compose Postgres + Redis (skipped if unreachable) with mocked
httpx calls to the TM REST API and Robot Events API — matches the plan's
test file description ("TM API fetch, Redis cache hit/miss, Robot Events
caching, rate limit handling, skills match exclusion").
"""
from __future__ import annotations

import json
import time
import uuid

import pytest
import redis.asyncio as redis
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.core.settings import settings
from backend.models.integration import IntegrationInstance
from backend.models.settings import SystemSetting
from backend.models.team import TeamProfile
from backend.modules.scraper.scraper import Scraper, _team_cache_key
from backend.schemas.events import EventBusMessage

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def real_redis():
    client = redis.from_url(settings.REDIS_URL, decode_responses=True)
    try:
        await client.ping()
    except Exception:
        pytest.skip("Real Redis not reachable; skipping scraper tests")
    yield client
    await client.aclose()


@pytest.fixture
async def db_engine():
    engine = create_async_engine(settings.POSTGRES_DSN)
    try:
        async with engine.connect() as conn:
            await conn.exec_driver_sql("SELECT 1")
    except Exception:
        pytest.skip("Real Postgres not reachable; skipping scraper tests")
    yield engine
    await engine.dispose()


@pytest.fixture
async def session_factory(db_engine):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    return async_sessionmaker(bind=db_engine, expire_on_commit=False)


@pytest.fixture
async def scraper(real_redis, session_factory):
    s = Scraper(redis=real_redis, session_factory=session_factory)
    yield s
    await s._re_http.aclose()
    await s._tm_http.aclose()


@pytest.fixture
async def cleanup_team(session_factory):
    created_numbers: list[str] = []

    def _track(number: str) -> str:
        created_numbers.append(number)
        return number

    yield _track

    async with session_factory() as session:
        for number in created_numbers:
            await session.execute(delete(TeamProfile).where(TeamProfile.team_number == number))
        await session.commit()


class _FakeResponse:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


async def test_invalidate_cache_removes_redis_key(scraper, real_redis):
    number = f"T{uuid.uuid4().hex[:6]}"
    await real_redis.set(_team_cache_key(number), json.dumps({"team_number": number}))
    await scraper.invalidate_cache(number)
    assert await real_redis.get(_team_cache_key(number)) is None


async def test_fetch_team_cache_hit_skips_tm_fetch(scraper, real_redis, mocker):
    number = f"T{uuid.uuid4().hex[:6]}"
    cached_profile = {"team_number": number, "cached_stats": {"qual_avg": 42}}
    await real_redis.set(_team_cache_key(number), json.dumps(cached_profile))

    spy = mocker.patch.object(scraper, "_fetch_from_tm", new_callable=mocker.AsyncMock)

    result = await scraper.fetch_team(number, "vex_tm.division_1")

    assert result == cached_profile
    spy.assert_not_called()

    await real_redis.delete(_team_cache_key(number))


async def test_fetch_team_cache_miss_fetches_and_caches(
    scraper, real_redis, session_factory, cleanup_team, mocker
):
    number = f"T{uuid.uuid4().hex[:6]}"
    cleanup_team(number)
    await real_redis.delete(_team_cache_key(number))

    tm_data = {
        "team_row": {"organization": "Test Org", "location": "Testville"},
        "event": {"name": "Test Event"},
        "ranking": {"rank": 1},
        "matches": [
            {"matchNum": 1, "round": "QUAL", "redTeams": [number], "blueTeams": ["9999B"], "redScore": 50, "blueScore": 30},
            {"matchNum": 2, "round": "QUAL", "redTeams": ["9999B"], "blueTeams": [number], "redScore": 20, "blueScore": 70},
        ],
        "skills": [],
    }
    mocker.patch.object(scraper, "_fetch_from_tm", new_callable=mocker.AsyncMock, return_value=tm_data)
    mocker.patch.object(
        scraper, "_fetch_from_robot_events", new_callable=mocker.AsyncMock,
        return_value={"bio": "A great team", "robot_name": "Robo"},
    )
    publish_spy = mocker.patch.object(scraper, "_publish_team_profile_updated", new_callable=mocker.AsyncMock)

    result = await scraper.fetch_team(number, "vex_tm.division_1")

    assert result["team_number"] == number
    assert result["bio"] == "A great team"
    assert result["robot_name"] == "Robo"
    # scores: match1 red=50 (team is red), match2 blue=70 (team is blue) -> avg 60, high 70, low 50
    assert result["cached_stats"]["qual_avg"] == 60.0
    assert result["cached_stats"]["qual_high"] == 70
    assert result["cached_stats"]["qual_low"] == 50
    # Regression test (code-review finding): organisation/location were
    # computed from the TM team row but silently discarded before persisting
    # (TeamProfile has no dedicated columns for them) — must now be kept in
    # cached_stats rather than dropped.
    assert result["cached_stats"]["organisation"] == "Test Org"
    assert result["cached_stats"]["location"] == "Testville"
    publish_spy.assert_awaited_once()

    cached_raw = await real_redis.get(_team_cache_key(number))
    assert cached_raw is not None
    assert json.loads(cached_raw)["team_number"] == number

    async with session_factory() as session:
        row = await session.get(TeamProfile, number)
        assert row is not None
        assert row.bio == "A great team"
        assert row.robot_name == "Robo"
        assert row.cached_stats["organisation"] == "Test Org"
        assert row.cached_stats["location"] == "Testville"
        assert row.cached_stats["qual_avg"] == 60.0


async def test_fetch_team_upsert_updates_existing_row(
    scraper, real_redis, session_factory, cleanup_team, mocker
):
    number = f"T{uuid.uuid4().hex[:6]}"
    cleanup_team(number)
    await real_redis.delete(_team_cache_key(number))

    async with session_factory() as session:
        session.add(TeamProfile(team_number=number, pit_location="Bay 12", bio="old bio"))
        await session.commit()

    mocker.patch.object(
        scraper, "_fetch_from_tm", new_callable=mocker.AsyncMock,
        return_value={"team_row": {}, "event": {}, "ranking": None, "matches": [], "skills": []},
    )
    mocker.patch.object(
        scraper, "_fetch_from_robot_events", new_callable=mocker.AsyncMock,
        return_value={"bio": "new bio", "robot_name": "NewBot"},
    )
    mocker.patch.object(scraper, "_publish_team_profile_updated", new_callable=mocker.AsyncMock)

    await scraper.fetch_team(number, "vex_tm.division_1")

    async with session_factory() as session:
        row = await session.get(TeamProfile, number)
        assert row.bio == "new bio"
        assert row.robot_name == "NewBot"
        # pit_location must be untouched by the scraper's upsert (only edited via routers/teams.py PUT).
        assert row.pit_location == "Bay 12"


async def test_extract_team_matches_excludes_skills_and_unscored(scraper):
    number = "1234A"
    matches = [
        {"matchNum": 1, "round": "QUAL", "redTeams": [number], "blueTeams": ["9999B"], "redScore": 40, "blueScore": 20},
        # Unscored future match — excluded.
        {"matchNum": 2, "round": "QUAL", "redTeams": [number], "blueTeams": ["9999B"], "redScore": None, "blueScore": None},
        # Not involving this team — excluded.
        {"matchNum": 3, "round": "QUAL", "redTeams": ["5555C"], "blueTeams": ["6666D"], "redScore": 10, "blueScore": 10},
    ]
    result = scraper._extract_team_matches(matches, number)
    assert len(result) == 1
    assert result[0]["matchNum"] == 1


async def test_auto_create_if_missing_only_creates_when_absent(
    scraper, real_redis, session_factory, cleanup_team, mocker
):
    number = f"T{uuid.uuid4().hex[:6]}"
    cleanup_team(number)

    fetch_spy = mocker.patch.object(scraper, "fetch_team", new_callable=mocker.AsyncMock)
    await scraper._auto_create_if_missing(number, "vex_tm.division_1")
    fetch_spy.assert_awaited_once_with(number, "vex_tm.division_1")

    # Now that a row exists, a second call must NOT re-fetch (plan §5.11:
    # "If not, it creates one automatically" — only on absence).
    async with session_factory() as session:
        session.add(TeamProfile(team_number=number))
        await session.commit()

    fetch_spy.reset_mock()
    await scraper._auto_create_if_missing(number, "vex_tm.division_1")
    fetch_spy.assert_not_called()


async def test_teams_discovered_creates_only_missing_teams(
    scraper, session_factory, cleanup_team, mocker
):
    """The schedule poll publishes every team it sees; profiles must be
    created up front for the ones that don't exist yet, and skipped for the
    ones that do."""
    existing = f"T{uuid.uuid4().hex[:6]}"
    missing = f"T{uuid.uuid4().hex[:6]}"
    cleanup_team(existing)
    cleanup_team(missing)
    async with session_factory() as session:
        session.add(TeamProfile(team_number=existing))
        await session.commit()

    fetch_spy = mocker.patch.object(scraper, "fetch_team", new_callable=mocker.AsyncMock)
    await scraper._handle_teams_discovered(
        json.dumps({"entity_id": "vex_tm.division_1", "teams": [existing, missing]})
    )
    await scraper._backfill_task

    fetch_spy.assert_awaited_once_with(missing, "vex_tm.division_1")


async def test_teams_discovered_ignores_malformed_message(scraper, mocker):
    spy = mocker.patch.object(scraper, "fetch_team", new_callable=mocker.AsyncMock)
    await scraper._handle_teams_discovered("not json")
    assert scraper._backfill_task is None
    spy.assert_not_called()


async def test_handle_raw_message_ignores_non_matching_event_types(scraper, mocker):
    spy = mocker.patch.object(scraper, "_auto_create_if_missing", new_callable=mocker.AsyncMock)
    event = EventBusMessage(
        entity_id="vex_tm.division_1", entity_tags=[], type="matchStarted",
        timestamp=time.time(), payload={},
    )
    await scraper._handle_raw_message(event.model_dump_json())
    spy.assert_not_called()


async def test_handle_raw_message_field_match_assigned_triggers_all_teams(scraper, mocker):
    spy = mocker.patch.object(scraper, "_auto_create_if_missing", new_callable=mocker.AsyncMock)
    event = EventBusMessage(
        entity_id="vex_tm.division_1", entity_tags=[], type="fieldMatchAssigned",
        timestamp=time.time(),
        payload={"redTeams": ["1234A", "5678B"], "blueTeams": ["9101C", "1121D"], "matchNum": 5, "divisionId": 1, "fieldID": 1, "round": "QUAL"},
    )
    await scraper._handle_raw_message(event.model_dump_json())
    assert spy.await_count == 4


async def test_robot_events_fetch_runs_rankings_awards_skills_concurrently_and_tolerates_partial_failure(
    scraper, session_factory, mocker
):
    """Regression test (code-review finding): rankings/awards/skills used to
    be fetched sequentially; now fetched concurrently via asyncio.gather.
    Also verifies one endpoint failing doesn't prevent the others' data from
    populating the result (return_exceptions=True isolation)."""
    async with session_factory() as session:
        setting = await session.get(SystemSetting, "robot_events_api")
        original_value = dict(setting.value) if setting else None
        setting.value = {"token": "fake-token"}
        await session.commit()

    def _resp(data):
        return _FakeResponse(data)

    async def fake_get(url, headers=None, params=None):
        if url == "/teams":
            return _resp(
                {"data": [{"id": 555, "description": "Bio!", "robot_name": "Bot", "program": {"id": 900, "name": "VRC"}}]}
            )
        if url == "/seasons":
            assert params == {"program[]": 900, "active": "true"}
            return _resp({"data": [{"id": 777, "name": "Current Season"}]})
        if url == "/teams/555/rankings":
            assert params == {"season[]": 777}
            raise RuntimeError("simulated rankings outage")
        if url == "/teams/555/awards":
            assert params == {"season[]": 777}
            return _resp({"data": [{"title": "Excellence Award"}]})
        if url == "/teams/555/skills":
            assert params == {"season[]": 777}
            return _resp({"data": [{"rank": 3, "event": {"id": 1, "name": "E", "code": "RE-VRC-1"}}]})
        raise AssertionError(f"unexpected URL {url}")

    mocker.patch.object(scraper._re_http, "get", side_effect=fake_get)

    try:
        result = await scraper._fetch_from_robot_events("1234A")
        # rankings failed -> empty list, not an unhandled exception.
        assert result["previous_rankings"] == []
        # awards/skills succeeded despite rankings failing concurrently.
        assert result["awards"] == [{"title": "Excellence Award"}]
        assert result["skills_rank"] == 3
    finally:
        await scraper._redis.delete("qecomp:re:season:900")
        if original_value is not None:
            async with session_factory() as session:
                setting = await session.get(SystemSetting, "robot_events_api")
                setting.value = original_value
                await session.commit()


async def test_robot_events_fetch_skipped_without_token(scraper, session_factory):
    async with session_factory() as session:
        setting = await session.get(SystemSetting, "robot_events_api")
        original_value = dict(setting.value) if setting else None
        if setting is not None:
            setting.value = {"token": ""}
            await session.commit()

    try:
        result = await scraper._fetch_from_robot_events("1234A")
        assert result == {}
    finally:
        if original_value is not None:
            async with session_factory() as session:
                setting = await session.get(SystemSetting, "robot_events_api")
                setting.value = original_value
                await session.commit()


async def test_robot_events_current_season_is_cached_not_refetched(scraper, session_factory, mocker):
    """Regression test: the current-season lookup must be Redis-cached, not
    re-issued on every team fetch, since the VEX Events API is strictly
    rate-limited. Fetching two different teams in the same program should
    only ever hit /seasons once."""
    async with session_factory() as session:
        setting = await session.get(SystemSetting, "robot_events_api")
        original_value = dict(setting.value) if setting else None
        setting.value = {"token": "fake-token"}
        await session.commit()

    def _resp(data):
        return _FakeResponse(data)

    seasons_call_count = 0

    async def fake_get(url, headers=None, params=None):
        nonlocal seasons_call_count
        if url == "/teams":
            number = params["number[]"]
            team_id = 555 if number == "1234A" else 556
            return _resp({"data": [{"id": team_id, "program": {"id": 900, "name": "VRC"}}]})
        if url == "/seasons":
            seasons_call_count += 1
            return _resp({"data": [{"id": 777, "name": "Current Season"}]})
        if url in ("/teams/555/rankings", "/teams/556/rankings"):
            assert params == {"season[]": 777}
            return _resp({"data": []})
        if url in ("/teams/555/awards", "/teams/556/awards"):
            return _resp({"data": []})
        if url in ("/teams/555/skills", "/teams/556/skills"):
            return _resp({"data": []})
        raise AssertionError(f"unexpected URL {url}")

    mocker.patch.object(scraper._re_http, "get", side_effect=fake_get)

    try:
        await scraper._fetch_from_robot_events("1234A")
        await scraper._fetch_from_robot_events("5678B")
        assert seasons_call_count == 1
    finally:
        await scraper._redis.delete("qecomp:re:season:900")
        if original_value is not None:
            async with session_factory() as session:
                setting = await session.get(SystemSetting, "robot_events_api")
                setting.value = original_value
                await session.commit()


async def test_robot_events_skips_rankings_awards_skills_when_season_unresolvable(
    scraper, session_factory, mocker
):
    """If the current season can't be resolved, skip rankings/awards/skills
    entirely rather than falling back to unscoped (multi-season) data — see
    the comment in `_fetch_from_robot_events`."""
    async with session_factory() as session:
        setting = await session.get(SystemSetting, "robot_events_api")
        original_value = dict(setting.value) if setting else None
        setting.value = {"token": "fake-token"}
        await session.commit()

    def _resp(data):
        return _FakeResponse(data)

    async def fake_get(url, headers=None, params=None):
        if url == "/teams":
            return _resp({"data": [{"id": 555, "program": {"id": 901, "name": "VRC"}}]})
        if url == "/seasons":
            return _resp({"data": []})  # no active season found
        raise AssertionError(f"unexpected URL {url} (rankings/awards/skills must not be called)")

    mocker.patch.object(scraper._re_http, "get", side_effect=fake_get)

    try:
        result = await scraper._fetch_from_robot_events("1234A")
        assert result["previous_rankings"] == []
        assert result["awards"] == []
        assert result["skills_rank"] is None
    finally:
        await scraper._redis.delete("qecomp:re:season:901")
        if original_value is not None:
            async with session_factory() as session:
                setting = await session.get(SystemSetting, "robot_events_api")
                setting.value = original_value
                await session.commit()


# ── Documented TM response shapes (docs/modules/VEX_API_DOCS.md) ─────────


def test_team_numbers_in_handles_documented_and_legacy_shapes():
    from backend.modules.scraper.scraper import _team_numbers_in

    assert _team_numbers_in({"alliance": {"teams": [{"number": "7925S"}]}}) == {"7925S"}  # rankings
    assert _team_numbers_in({"number": "1082C", "rank": 1}) == {"1082C"}  # skills
    assert _team_numbers_in({"team": {"number": "1A"}}) == {"1A"}
    assert _team_numbers_in({"team": "2B"}) == {"2B"}
    assert _team_numbers_in({"alliance": {"teams": [{"number": "3A"}, {"number": "3B"}]}}) == {"3A", "3B"}
    assert _team_numbers_in({}) == set() and _team_numbers_in(None) == set()


def test_team_location_prefers_location_then_joins_city_state_country():
    from backend.modules.scraper.scraper import _country_from_location, _team_location

    assert _team_location({"location": "London, United Kingdom", "city": "x"}) == "London, United Kingdom"
    assert _team_location({"city": "Redmond", "state": "Washington", "country": "United States"}) == "Redmond, Washington, United States"
    assert _team_location({"city": "London", "country": "United Kingdom"}) == "London, United Kingdom"
    assert _team_location({}) is None
    assert _country_from_location("London, United Kingdom") == "United Kingdom"
    assert _country_from_location("Nowhere") is None


async def test_fetch_from_tm_uses_div_id_and_documented_ranking_shape(scraper, mocker):
    mocker.patch.object(scraper, "_get_tm_base_url_and_token", new_callable=mocker.AsyncMock, return_value=("http://tm", "tok", None))
    responses = {
        "/api/teams": [{"number": "10D", "name": "Exothermic Dusk", "school": "Exothermic Robotics", "city": "Redmond", "country": "United States", "divId": 2}],
        "/api/event": {"name": "Event"},
        "/api/rankings/2/QUAL": [
            {"rank": 3, "alliance": {"teams": [{"number": "99X"}]}},
            {"rank": 1, "alliance": {"teams": [{"number": "10D"}]}, "wins": 4},
        ],
        "/api/matches/2": [],
        "/api/skills": [{"rank": 2, "number": "10D", "totalScore": 40}, {"rank": 1, "number": "11E", "totalScore": 50}],
    }

    async def fake_get(base_url, token, api_key, path, envelope_key=None):
        return responses[path]

    mocker.patch.object(scraper, "_tm_get", side_effect=fake_get)
    data = await scraper._fetch_from_tm("10D", "vex_tm.test")
    assert data["division_id"] == 2
    assert data["ranking"]["rank"] == 1 and data["ranking"]["wins"] == 4
    assert [s["number"] for s in data["skills"]] == ["10D"]


def test_merge_profile_maps_documented_team_fields():
    profile = Scraper._merge_profile(
        "10D",
        {
            "team_row": {"name": "Exothermic Dusk", "school": "Exothermic Robotics", "city": "Redmond", "state": "Washington", "country": "United States"},
            "division_id": 2,
            "ranking": {"rank": 1},
            "matches": [],
            "skills": [],
        },
        {},
    )
    stats = profile["cached_stats"]
    assert stats["team_name"] == "Exothermic Dusk" and stats["organisation"] == "Exothermic Robotics"
    assert stats["location"] == "Redmond, Washington, United States" and stats["country"] == "United States"
    assert stats["division_id"] == 2
