"""Public site extras (issue #28): leaderboards, season highlights, match
recordings, team logo/website, uploaded site images, bus tracker."""
from __future__ import annotations

import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import fakeredis
import pytest
from fakeredis import aioredis as fake_aioredis
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from backend.models.match_recording import MatchRecording
from backend.models.settings import SystemSetting
from backend.models.team import TeamProfile
from backend.modules.site import leaderboards as lb
from backend.tests.test_public_site.test_public_site import PREFIX, raw, seed, session_factory  # noqa: F401  (fixtures)

pytestmark = pytest.mark.asyncio

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 16


def team(number, **stats):
    return SimpleNamespace(team_number=number, cached_stats=stats or None)


# ── pure leaderboard logic ───────────────────────────────────────────


def test_compute_leaderboards_panels_and_programme_split():
    profiles = [
        team("A", division_id=1, team_name="Alpha", ranking={"rank": 1, "wins": 5}, skills=[{"rank": 4, "totalScore": 90}], awards=[{"title": "Excellence"}, {"title": "Design"}]),
        team("B", division_id=1, ranking={"rank": 2}, skills=[{"rank": 1, "totalScore": 120, "driverHighScore": 50, "progHighScore": 70}]),
        team("C", division_id=1, ranking={"rank": 3}),  # no skills -> not in overall
        team("D", division_id=2, ranking={"rank": 1}, skills=[{"rank": 2}], awards=[{"name": "Judges"}]),
        team("E"),  # no division: appears in both programmes
    ]
    groups = lb.compute_leaderboards(profiles, {"1": "iq", "2": "vrc"})
    assert [g["programme"] for g in groups] == ["iq", "vrc"] and [g["label"] for g in groups] == ["VIQRC", "V5RC"]
    iq, vrc = groups

    assert [r["team"] for r in iq["tournament"]] == ["A", "B", "C"]
    assert [r["team"] for r in iq["skills"]] == ["B", "A"] and iq["skills"][0]["total"] == 120 and iq["skills"][0]["driver"] == 50
    overall = {r["team"]: r for r in iq["overall"]}
    assert overall["A"]["score"] == 5 and overall["B"]["score"] == 3 and "C" not in overall
    assert [r["team"] for r in iq["overall"]] == ["B", "A"] and [r["rank"] for r in iq["overall"]] == [1, 2]
    assert [(r["team"], r["count"], r["latest"]) for r in iq["awards"]] == [("A", 2, "Design")]
    assert [r["team"] for r in vrc["tournament"]] == ["D"] and vrc["awards"][0]["latest"] == "Judges"
    assert "A" not in {r["team"] for r in vrc["overall"] + vrc["skills"] + vrc["tournament"]}


def test_compute_leaderboards_single_group_without_programmes_and_limit():
    profiles = [team(f"T{i}", ranking={"rank": i}) for i in range(1, 40)]
    (group,) = lb.compute_leaderboards(profiles, {}, limit=5)
    assert group["programme"] is None and group["label"] == "All teams"
    assert [r["rank"] for r in group["tournament"]] == [1, 2, 3, 4, 5]
    assert len(lb.compute_leaderboards(profiles, {}, limit=9999)[0]["tournament"]) == 39  # capped at MAX_LIMIT (50)
    assert lb.compute_leaderboards(profiles, {}, limit=0)[0]["tournament"][0]["rank"] == 1  # clamped to >= 1


def test_season_highlights():
    profiles = [
        team("A", country="United Kingdom", skills_rank=40, skills=[{"rank": 3, "totalScore": 77}], awards=[{}, {}]),
        team("B", country="England", skills_rank=15, skills=[{"rank": 1, "totalScore": 99}]),
        team("C", country="France", skills_rank=2, awards=[{}]),
        team("D"),
    ]
    h = lb.season_highlights(profiles, ["iq", "vrc"])
    assert h["total_awards"] == 3
    assert h["best_world_skills"] == {"team": "C", "rank": 2}
    assert h["best_uk_skills"] == {"team": "B", "rank": 15, "uk_teams_ranked": 2}
    assert h["top_combined_skills"] == {"team": "B", "total": 99}
    assert [p["label"] for p in h["active_programmes"]] == ["VIQRC", "V5RC"]
    empty = lb.season_highlights([], [])
    assert empty["best_world_skills"] is None and empty["best_uk_skills"] is None and empty["top_combined_skills"] is None


# ── API ──────────────────────────────────────────────────────────────


@pytest.fixture
async def api(session_factory, monkeypatch):  # noqa: F811
    from backend.core.db import get_db
    from backend.core.dependencies import ALL_PERMISSIONS, CurrentPrincipal, get_current_principal, get_redis
    from backend.main import create_app
    from backend.modules.media import s3

    redis = fake_aioredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)
    app = create_app()

    async def _db():
        async with session_factory() as session:
            yield session

    state = {"principal": CurrentPrincipal(subject="tester", permissions={ALL_PERMISSIONS})}
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_redis] = lambda: redis
    app.dependency_overrides[get_current_principal] = lambda: state["principal"]
    uploads = AsyncMock()
    deletes = AsyncMock()
    monkeypatch.setattr(s3, "upload_multipart", uploads)
    monkeypatch.setattr(s3, "delete_object", deletes)
    monkeypatch.setattr(s3, "generate_presigned_url", AsyncMock(side_effect=lambda key, expires_in=900: f"https://s3.test/{key}"))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        c.redis, c.factory, c.state, c.uploads, c.deletes = redis, session_factory, state, uploads, deletes
        yield c
    async with session_factory() as session:
        await session.execute(delete(MatchRecording))
        await session.commit()
    await redis.aclose()


async def set_site(client, value):
    async with client.factory() as session:
        row = await session.get(SystemSetting, "site")
        if row is None:
            session.add(SystemSetting(key="site", value=value))
        else:
            row.value = value
        await session.commit()


async def test_leaderboards_endpoint_uses_schedule_programmes_and_config_timing(api):
    await seed(api)
    body = (await api.get("/api/v1/public/site/leaderboards")).json()
    assert body["panel_seconds"] == 60 and body["programme_seconds"] == 240
    (group,) = body["groups"]  # two-team alliances => IQ
    assert group["programme"] == "iq" and group["label"] == "VIQRC"
    assert [r["team"] for r in group["tournament"]] == [f"{PREFIX}1A", f"{PREFIX}2B"]
    assert group["awards"][0]["team"] == f"{PREFIX}1A" and group["awards"][0]["latest"] == "Excellence"
    assert [r["team"] for r in group["overall"]] == [f"{PREFIX}1A"]  # only 1A has both ranks

    await set_site(api, {"leaderboard": {"panel_seconds": 2, "programme_seconds": 99999}})
    timing = (await api.get("/api/v1/public/site/leaderboards?limit=1")).json()
    assert timing["panel_seconds"] == 5 and timing["programme_seconds"] == 3600  # clamped
    assert len((await api.get("/api/v1/public/site/leaderboards?limit=1")).json()["groups"][0]["tournament"]) == 1


async def test_overview_dashboard_next_competition_bus_tracker_and_images(api):
    await seed(api)
    key = "a" * 32 + ".png"
    await set_site(api, {
        "title": "QE Battle",
        "next_competition": {"name": "Nationals", "date": "2999-01-02", "location": "Birmingham", "url": "javascript:alert(1)",
                             "registration_url": "https://reg.example.com/x", "description": "Big one", "teams": [f"{PREFIX}1A"]},
        "sponsors": [{"name": "Kingston", "url": "https://k.example.com", "logo_key": key}, {"name": "Bad", "logo_key": "../../etc/passwd", "logo_url": "ftp://x"}],
        "map": {"image_key": key, "rooms": [{"name": "M9", "x": 140, "y": -5}, {"name": "", "x": 1, "y": 1}, {"name": "NoPos"}]},
        "bus_tracker": {"title": "Shuttle", "embed_url": "https://bus.example.com/embed", "link_url": "javascript:x", "note": "Every 15 min",
                        "buses": [{"name": "Bus 1", "status": "At school", "eta": "10:15"}, {"status": "no name"}]},
    })
    o = (await api.get("/api/v1/public/site/overview")).json()
    nc = o["next_competition"]
    assert nc["name"] == "Nationals" and nc["days_until"] > 1000 and nc["url"] is None  # unsafe scheme dropped
    assert nc["registration_url"] == "https://reg.example.com/x" and nc["location"] == "Birmingham"
    assert o["sponsors"][0] == {"name": "Kingston", "url": "https://k.example.com", "logo_url": f"https://s3.test/site/images/{key}"}
    assert o["sponsors"][1]["logo_url"] is None  # path-traversal key and non-http url both refused
    assert o["map"]["image_url"] == f"https://s3.test/site/images/{key}"
    assert o["map"]["rooms"] == [{"name": "M9", "x": 100, "y": 0, "description": None, "teams": []}]  # clamped; junk rooms dropped
    bt = o["bus_tracker"]
    assert bt["embed_url"] == "https://bus.example.com/embed" and bt["link_url"] is None and bt["buses"] == [{"name": "Bus 1", "status": "At school", "eta": "10:15"}]
    h = o["highlights"]
    assert h["total_awards"] == 1 and h["best_world_skills"] == {"team": f"{PREFIX}1A", "rank": 12}
    assert h["best_uk_skills"]["team"] == f"{PREFIX}1A" and h["top_combined_skills"]["total"] == 40
    assert h["active_programmes"] == [{"programme": "iq", "label": "VIQRC"}]

    await set_site(api, {"bus_tracker": {"title": "empty"}})
    assert (await api.get("/api/v1/public/site/overview")).json()["bus_tracker"] is None


async def test_site_image_upload_validates_content_and_size(api):
    ok = await api.post("/api/v1/site/images", files={"file": ("map.png", PNG, "image/png")})
    assert ok.status_code == 201
    body = ok.json()
    assert body["key"].endswith(".png") and len(body["key"]) == 36 and body["url"].startswith("https://s3.test/site/images/")
    assert api.uploads.await_args.args[1] == f"site/images/{body['key']}"
    assert (await api.post("/api/v1/site/images", files={"file": ("a.jpg", JPEG, "image/jpeg")})).json()["key"].endswith(".jpg")
    assert (await api.post("/api/v1/site/images", files={"file": ("a.webp", WEBP, "image/webp")})).json()["key"].endswith(".webp")

    calls = api.uploads.await_count
    # The name and type are ignored: content decides.
    assert (await api.post("/api/v1/site/images", files={"file": ("evil.png", b"<svg onload=alert(1)>", "image/png")})).status_code == 400
    assert (await api.post("/api/v1/site/images", files={"file": ("big.png", PNG + b"\x00" * (4 * 1024 * 1024), "image/png")})).status_code == 413
    assert api.uploads.await_count == calls

    from backend.core.dependencies import CurrentPrincipal

    api.state["principal"] = CurrentPrincipal(subject="viewer", permissions={"settings:read"})
    assert (await api.post("/api/v1/site/images", files={"file": ("m.png", PNG, "image/png")})).status_code == 403


async def test_team_logo_and_website(api):
    await seed(api)
    n = f"{PREFIX}1A"
    r = await api.put(f"/api/v1/teams/{n}", json={"website_url": "https://gearsquad.example.com"})
    assert r.status_code == 200 and r.json()["website_url"] == "https://gearsquad.example.com"
    assert (await api.put(f"/api/v1/teams/{n}", json={"website_url": "javascript:alert(1)"})).status_code == 422
    assert (await api.put(f"/api/v1/teams/{n}", json={"website_url": "not a url"})).status_code == 422

    up = await api.post(f"/api/v1/teams/{n}/logo", files={"file": ("l.png", PNG, "image/png")})
    assert up.status_code == 200 and up.json()["logo_s3_key"] == f"teams/{n}/logo.png"
    replaced = await api.post(f"/api/v1/teams/{n}/logo", files={"file": ("l.jpg", JPEG, "image/jpeg")})
    assert replaced.json()["logo_s3_key"] == f"teams/{n}/logo.jpg"
    api.deletes.assert_awaited_with(f"teams/{n}/logo.png")  # old extension cleaned up
    assert (await api.post(f"/api/v1/teams/{n}/logo", files={"file": ("l.png", b"nope", "image/png")})).status_code == 400
    assert (await api.post("/api/v1/teams/NOPE/logo", files={"file": ("l.png", PNG, "image/png")})).status_code == 404

    public = (await api.get(f"/api/v1/public/site/teams/{n}")).json()
    assert public["website_url"] == "https://gearsquad.example.com" and public["logo_url"] == f"https://s3.test/teams/{n}/logo.jpg"

    cleared = await api.put(f"/api/v1/teams/{n}", json={"website_url": ""})
    assert cleared.json()["website_url"] is None
    gone = await api.delete(f"/api/v1/teams/{n}/logo")
    assert gone.status_code == 200 and gone.json()["logo_s3_key"] is None
    public = (await api.get(f"/api/v1/public/site/teams/{n}")).json()
    assert public["logo_url"] is None and public["website_url"] is None
    assert (await api.get("/api/v1/public/site/teams/PS3C")).json()["logo_url"] is None


async def test_match_recordings_explicit_links_template_and_import(api):
    await seed(api)
    q1 = "1:0:QUAL:1:1"

    resp = await api.put("/api/v1/match-recordings", json={"division": "1", "label": "q1", "url": "https://youtu.be/abc?t=95", "title": "Q1"})
    assert resp.status_code == 200 and resp.json()["match_key"] == q1 and resp.json()["label"] == "Q1"
    assert (await api.put("/api/v1/match-recordings", json={"division": "1", "label": "Q99", "url": "https://x.example"})).status_code == 404
    assert (await api.put("/api/v1/match-recordings", json={"division": "1", "label": "Q1", "url": "javascript:x"})).status_code == 422
    listed = (await api.get("/api/v1/match-recordings")).json()
    assert [(r["match_key"], r["label"], r["url"]) for r in listed] == [(q1, "Q1", "https://youtu.be/abc?t=95")]

    event = (await api.get("/api/v1/public/site/event")).json()
    by_label = {m["label"]: m for m in event["matches"]}
    assert by_label["Q1"]["recording_url"] == "https://youtu.be/abc?t=95" and by_label["Q2"]["recording_url"] is None
    team_page = (await api.get(f"/api/v1/public/site/teams/{PREFIX}1A")).json()
    assert {m["label"]: m["recording_url"] for m in team_page["schedule"]}["Q1"] == "https://youtu.be/abc?t=95"

    # An explicit link beats the template; the template only applies to scored matches.
    await set_site(api, {"recording_url_template": "https://rec.example.com/{division}/{label}.mp4"})
    by_label = {m["label"]: m for m in (await api.get("/api/v1/public/site/event")).json()["matches"]}
    assert by_label["Q1"]["recording_url"] == "https://youtu.be/abc?t=95"
    assert by_label["Q2"]["recording_url"] is None  # unscored: nothing to watch yet
    async with api.factory() as session:
        await session.execute(delete(MatchRecording))
        await session.commit()
    by_label = {m["label"]: m for m in (await api.get("/api/v1/public/site/event")).json()["matches"]}
    assert by_label["Q1"]["recording_url"] == "https://rec.example.com/1/Q1.mp4"
    await set_site(api, {"recording_url_template": "javascript:{label}"})
    assert {m["label"]: m for m in (await api.get("/api/v1/public/site/event")).json()["matches"]}["Q1"]["recording_url"] is None

    ok = (await api.post("/api/v1/match-recordings/import", json={"text": "# links\n1 Q1 https://v.example.com/1 First match\n1 Q2 https://v.example.com/2\n"})).json()
    assert ok["saved"] == 2 and ok["errors"] == []
    titles = {r["label"]: r["title"] for r in (await api.get("/api/v1/match-recordings")).json()}
    assert titles == {"Q1": "First match", "Q2": None}
    bad = (await api.post("/api/v1/match-recordings/import", json={"text": "1 Q1 https://ok.example.com\nnonsense\n2 Q7 https://x.example.com"})).json()
    assert bad["saved"] == 0 and [e["line"] for e in bad["errors"]] == [2, 3]
    assert (await api.post("/api/v1/match-recordings/import", json={"text": "# nothing\n"})).json()["saved"] == 0

    assert (await api.delete(f"/api/v1/match-recordings/{q1}")).status_code == 204
    assert (await api.delete(f"/api/v1/match-recordings/{q1}")).status_code == 404


async def test_recordings_need_settings_permissions(api):
    from backend.core.dependencies import CurrentPrincipal

    api.state["principal"] = CurrentPrincipal(subject="viewer", permissions={"settings:read"})
    assert (await api.get("/api/v1/match-recordings")).status_code == 200
    assert (await api.put("/api/v1/match-recordings", json={"division": "1", "label": "Q1", "url": "https://x.example"})).status_code == 403
    assert (await api.post("/api/v1/match-recordings/import", json={"text": "1 Q1 https://x.example"})).status_code == 403
    assert (await api.delete("/api/v1/match-recordings/x")).status_code == 403
    api.state["principal"] = CurrentPrincipal(subject="nobody", permissions=set())
    assert (await api.get("/api/v1/match-recordings")).status_code == 403
