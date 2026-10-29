"""`NotificationService` end to end against real Postgres + fakeredis, with
the Web Push network call replaced by a recorder."""
from __future__ import annotations

import json
import time

import pytest
from sqlalchemy import select

from backend.models.notification import KIND_STAFF, KIND_TEAM, NotificationLog, PushSubscription, PushSubscriptionTeam
from backend.models.room import Room, RoomTeam
from backend.modules.notifications import delivery
from backend.modules.notifications.service import (
    ASSIGNED_HASH_KEY,
    AlertSettings,
    NotificationService,
)
from backend.tests.test_notifications.conftest import VALID_ENDPOINT

pytestmark = pytest.mark.asyncio

NOW = time.time()
SCHEDULE_KEY = "qecomp:tm:tm1:schedule"


def raw(num, red, blue, *, at, round_="QUAL", state="UNPLAYED", score=None):
    out = {
        "matchInfo": {
            "timeScheduled": at,
            "state": state,
            "alliances": [{"teams": [{"number": t} for t in red]}, {"teams": [{"number": t} for t in blue]}],
            "matchTuple": {"session": 0, "division": 1, "round": round_, "instance": 1, "match": num},
        }
    }
    if score:
        out["finalScore"] = list(score)
    return out


async def put_schedule(redis, *matches):
    await redis.set(SCHEDULE_KEY, json.dumps({"divisions": [{"id": 1, "name": "D", "matches": list(matches)}]}))


async def seed_room(factory, slug="r1", teams=("1234A",)):
    async with factory() as session:
        session.add(Room(slug=slug, name=slug.upper()))
        await session.flush()
        session.add_all(RoomTeam(room_slug=slug, team_number=t) for t in teams)
        await session.commit()


async def seed_sub(factory, endpoint, *, teams=(), room="r1", kind=KIND_TEAM, categories=None):
    async with factory() as session:
        sub = PushSubscription(
            endpoint=VALID_ENDPOINT + endpoint, p256dh="k", auth="a", kind=kind, room_slug=room if kind == KIND_TEAM else None,
            categories=categories,
        )
        session.add(sub)
        await session.flush()
        session.add_all(PushSubscriptionTeam(subscription_id=sub.id, team_number=t) for t in teams)
        await session.commit()
        return sub.id


async def listen_events(redis):
    listener = redis.pubsub()
    await listener.subscribe(delivery.EVENTS_CHANNEL)
    # The first read swallows the subscribe confirmation (returns None).
    await listener.get_message(ignore_subscribe_messages=True, timeout=0.25)
    return listener


async def room_notices(redis, since_listener):
    notices = []
    while True:
        msg = await since_listener.get_message(ignore_subscribe_messages=True, timeout=0.25)
        if msg is None:
            return notices
        data = json.loads(msg["data"])
        if data["type"] == "room_notice":
            notices.append(data["payload"])


@pytest.fixture
async def svc(session_factory, fake_redis, notif_settings):
    await notif_settings(
        {"team_alerts_enabled": True, "lead_matches_x": 2, "lead_minutes_y": 0, "scores_enabled": True,
         "scores_excluded_rounds": ["F"]}
    )
    listener = await listen_events(fake_redis)
    service = NotificationService(fake_redis, session_factory)
    service.listener = listener
    yield service
    await listener.aclose()


async def test_arrival_alert_goes_to_room_and_follower_once(svc, session_factory, fake_redis, pushes):
    await seed_room(session_factory, teams=("1234A",))
    await seed_sub(session_factory, "s1", teams=("1234A",))
    await seed_sub(session_factory, "s2", teams=("9999Z",))  # follows someone else
    await put_schedule(
        fake_redis,
        raw(1, ["1111A"], ["2222B"], at=NOW),
        raw(2, ["1234A", "5555C"], ["6666D", "7777E"], at=NOW + 600),
    )

    await svc.tick(NOW)

    notices = await room_notices(fake_redis, svc.listener)
    assert len(notices) == 1 and notices[0]["rooms"] == ["r1"]
    assert "Q2" in notices[0]["title"] and "1234A" in notices[0]["message"]
    assert [t.endpoint for t in pushes] == [VALID_ENDPOINT + "s1"]
    assert "1234A" in pushes[0].payload["body"] and "5555C" not in pushes[0].payload["body"]
    assert pushes[0].payload["url"] == "/room/r1"

    pushes.clear()
    await svc.tick(NOW + 1)
    assert pushes == [] and await room_notices(fake_redis, svc.listener) == []


async def test_assigned_match_moves_the_window(svc, session_factory, fake_redis, pushes):
    await seed_room(session_factory, teams=("3333C",))
    matches = [raw(n, [f"T{n}"], [f"U{n}"], at=NOW + n * 3600) for n in range(1, 6)]
    matches[4] = raw(5, ["3333C"], ["U5"], at=NOW + 5 * 3600)
    await put_schedule(fake_redis, *matches)

    await svc.tick(NOW)
    assert await room_notices(fake_redis, svc.listener) == []  # match 5 is 5 ahead, window is 2

    await svc.handle_message(
        delivery.EVENTS_CHANNEL,
        json.dumps({"type": "fieldMatchAssigned", "payload": {"match": {"division": 1, "session": 0, "round": "QUAL", "instance": 1, "match": 3}}}),
    )
    assert await fake_redis.hget(ASSIGNED_HASH_KEY, "1") == "1:0:QUAL:1:3"
    await svc.tick(NOW)
    notices = await room_notices(fake_redis, svc.listener)
    assert len(notices) == 1 and "Q5" in notices[0]["title"]


async def test_minutes_alert(svc, session_factory, fake_redis, pushes, notif_settings):
    await notif_settings({"team_alerts_enabled": True, "lead_matches_x": 0, "lead_minutes_y": 15, "scores_enabled": False})
    await seed_room(session_factory, teams=("1234A",))
    await put_schedule(fake_redis, raw(9, ["1234A"], ["2222B"], at=NOW + 10 * 60))
    await svc.tick(NOW)
    notices = await room_notices(fake_redis, svc.listener)
    assert len(notices) == 1 and "~10 min" in notices[0]["title"]


async def test_scores_baseline_then_new_score_and_finals_excluded(svc, session_factory, fake_redis, pushes, notif_settings):
    await notif_settings({"team_alerts_enabled": True, "lead_matches_x": 0, "lead_minutes_y": 0, "scores_enabled": True,
                          "scores_excluded_rounds": ["F"]})
    await seed_room(session_factory, teams=("1234A", "8888H"))
    await seed_sub(session_factory, "s1", teams=("1234A",))
    await put_schedule(fake_redis, raw(1, ["1234A"], ["2222B"], at=NOW, state="SCORED", score=(10, 5)))

    await svc.tick(NOW)  # baseline: pre-existing score must NOT be announced
    assert await room_notices(fake_redis, svc.listener) == [] and pushes == []

    await put_schedule(
        fake_redis,
        raw(1, ["1234A"], ["2222B"], at=NOW, state="SCORED", score=(10, 5)),
        raw(2, ["1234A"], ["3333C"], at=NOW + 60, state="SCORED", score=(45, 38)),
        raw(1, ["8888H"], ["9999I"], at=NOW + 120, round_="F", state="SCORED", score=(99, 1)),
    )
    await svc.tick(NOW + 5)

    notices = await room_notices(fake_redis, svc.listener)
    assert [n["title"] for n in notices] == ["Q2 final"]
    assert "Red 45 – Blue 38" in notices[0]["message"]
    assert len(pushes) == 1 and pushes[0].payload["title"] == "Q2 final (1234A)" and pushes[0].payload["kind"] == "score"

    pushes.clear()
    await svc.tick(NOW + 10)
    assert pushes == []


async def test_scores_disabled(svc, session_factory, fake_redis, pushes, notif_settings):
    await notif_settings({"team_alerts_enabled": True, "lead_matches_x": 0, "lead_minutes_y": 0, "scores_enabled": False})
    await seed_room(session_factory)
    await put_schedule(fake_redis, raw(1, ["1234A"], ["2222B"], at=NOW))
    await svc.tick(NOW)
    await put_schedule(fake_redis, raw(1, ["1234A"], ["2222B"], at=NOW, state="SCORED", score=(1, 2)))
    await svc.tick(NOW + 1)
    assert await room_notices(fake_redis, svc.listener) == []


async def test_master_switch_off_sends_nothing(svc, session_factory, fake_redis, pushes, notif_settings):
    await notif_settings({"team_alerts_enabled": False, "lead_matches_x": 5, "lead_minutes_y": 30})
    await seed_room(session_factory)
    await put_schedule(fake_redis, raw(1, ["1234A"], ["2222B"], at=NOW + 60))
    await svc.tick(NOW)
    assert await room_notices(fake_redis, svc.listener) == [] and pushes == []


async def test_dead_subscription_is_pruned(svc, session_factory, fake_redis, pushes):
    await seed_room(session_factory)
    await seed_sub(session_factory, "dead", teams=("1234A",))
    pushes.result[VALID_ENDPOINT + "dead"] = "gone"
    await put_schedule(fake_redis, raw(1, ["1234A"], ["2222B"], at=NOW + 60))
    await svc.tick(NOW)
    async with session_factory() as session:
        assert (await session.execute(select(PushSubscription))).first() is None


async def test_repeated_errors_eventually_drop_subscription(session_factory, pushes):
    await seed_room(session_factory)
    sub_id = await seed_sub(session_factory, "flaky", teams=("1234A",))
    pushes.result[VALID_ENDPOINT + "flaky"] = "error"
    target = delivery.PushTarget(sub_id, VALID_ENDPOINT + "flaky", "k", "a", {"title": "t"})
    for _ in range(delivery.MAX_FAILURES - 1):
        await delivery.send_push(session_factory, [target])
    async with session_factory() as session:
        assert (await session.get(PushSubscription, sub_id)).failure_count == delivery.MAX_FAILURES - 1
    await delivery.send_push(session_factory, [target])
    async with session_factory() as session:
        assert await session.get(PushSubscription, sub_id) is None


async def test_staff_alert_reaches_only_opted_in_staff(svc, session_factory, pushes):
    await seed_sub(session_factory, "all", kind=KIND_STAFF, categories=None)
    await seed_sub(session_factory, "sys", kind=KIND_STAFF, categories=["system"])
    await seed_sub(session_factory, "bc", kind=KIND_STAFF, categories=["broadcast"])
    await seed_room(session_factory)
    await seed_sub(session_factory, "team", teams=("1234A",))

    await svc.handle_message(delivery.STAFF_ALERTS_CHANNEL, json.dumps({"title": "Integration DEGRADED", "message": "atem down"}))

    assert sorted(t.endpoint.rsplit("/", 1)[1] for t in pushes) == ["all", "sys"]
    assert all(t.payload["kind"] == "staff" for t in pushes)


async def test_broadcast_targets_rooms_and_staff(session_factory, fake_redis, pushes):
    await seed_room(session_factory, "r1", ("1234A",))
    await seed_room(session_factory, "r2", ("5678B",))
    await seed_sub(session_factory, "in-r1", teams=("1234A",), room="r1")
    await seed_sub(session_factory, "in-r2", teams=("5678B",), room="r2")
    await seed_sub(session_factory, "staff-bc", kind=KIND_STAFF, categories=["broadcast"])
    listener = await listen_events(fake_redis)

    result = await delivery.broadcast(fake_redis, session_factory, "Lunch", "Back at 1pm", rooms=["r1"])
    assert result == {"rooms": 1, "pushed": 2}
    notices = await room_notices(fake_redis, listener)
    assert len(notices) == 1 and notices[0]["rooms"] == ["r1"] and notices[0]["modal"] is True
    assert sorted(t.endpoint.rsplit("/", 1)[1] for t in pushes) == ["in-r1", "staff-bc"]

    pushes.clear()
    result = await delivery.broadcast(fake_redis, session_factory, "All", "Everyone", rooms=None, push=False)
    assert result == {"rooms": 2, "pushed": 0} and pushes == []
    await listener.aclose()


async def test_push_is_noop_without_vapid(session_factory, monkeypatch):
    from backend.core.settings import settings

    monkeypatch.setattr(settings, "VAPID_PRIVATE_KEY", "")
    target = delivery.PushTarget(None, VALID_ENDPOINT + "x", "k", "a", {})
    assert await delivery.send_push(session_factory, [target]) == 0


async def test_claim_is_atomic_dedupe(svc):
    assert await svc._claim("some:key") is True
    assert await svc._claim("some:key") is False


async def test_alert_settings_defaults_and_clamping():
    assert AlertSettings.from_value(None) == AlertSettings()
    s = AlertSettings.from_value({"lead_matches_x": 9999, "lead_minutes_y": -5, "scores_excluded_rounds": ["sf", "f"]})
    assert s.lead_matches == 20 and s.lead_minutes == 0 and s.scores_excluded_rounds == {"SF", "F"}
    assert AlertSettings.from_value({"lead_matches_x": "abc"}).lead_matches == 3


async def test_start_stop_runs_event_and_tick_loops(session_factory, fake_redis, notif_settings, pushes):
    """The leader-only lifecycle: `start()` spawns the tick + bus loops (which
    pick up a field assignment and a staff alert), `stop()` ends them."""
    import asyncio

    await notif_settings({"team_alerts_enabled": True, "lead_matches_x": 2, "lead_minutes_y": 0})
    await seed_sub(session_factory, "ops", kind=KIND_STAFF)
    await seed_room(session_factory)
    await put_schedule(fake_redis, raw(1, ["1234A"], ["2222B"], at=NOW))

    service = NotificationService(fake_redis, session_factory)
    await service.start()
    try:
        await asyncio.sleep(0.2)  # let the bus loop subscribe
        await fake_redis.publish(
            delivery.EVENTS_CHANNEL,
            json.dumps({"type": "fieldMatchAssigned", "payload": {"match": {"division": 1, "session": 0, "round": "QUAL", "instance": 1, "match": 1}}}),
        )
        await fake_redis.publish(delivery.STAFF_ALERTS_CHANNEL, json.dumps({"title": "Leader failover", "message": "pod-2 took over"}))
        await fake_redis.publish(delivery.STAFF_ALERTS_CHANNEL, "not json")  # ignored, must not kill the loop
        for _ in range(40):
            if service._assigned.get("1") and pushes:
                break
            await asyncio.sleep(0.05)
        assert service._assigned == {"1": "1:0:QUAL:1:1"}
        assert [t.payload["title"] for t in pushes] == ["Leader failover"]
        assert len(service._tasks) == 2 and not any(t.done() for t in service._tasks)
    finally:
        await service.stop()
    assert service._tasks == []


async def test_assignment_survives_failover_via_redis(session_factory, fake_redis):
    await fake_redis.hset(ASSIGNED_HASH_KEY, "2", "2:0:QUAL:1:9")
    service = NotificationService(fake_redis, session_factory)
    await service._load_assigned()
    assert service._assigned == {"2": "2:0:QUAL:1:9"}


async def test_unparseable_cached_schedule_is_ignored(svc, fake_redis):
    await fake_redis.set(SCHEDULE_KEY, "{not json")
    assert await svc._load_schedules() == {}
    await svc.tick(NOW)  # must not raise


# ── late warnings, agenda announcements, category opt-out (issue #22) ─


async def test_late_warning_sent_once_to_room_and_follower(svc, session_factory, fake_redis, pushes, notif_settings):
    await notif_settings({"team_alerts_enabled": True, "lead_matches_x": 0, "lead_minutes_y": 0, "scores_enabled": False,
                          "late_minutes": 5, "agenda_announcements_enabled": False})
    await seed_room(session_factory, teams=("1234A",))
    await seed_sub(session_factory, "s1", teams=("1234A",))
    await put_schedule(
        fake_redis,
        raw(1, ["1111A"], ["2222B"], at=NOW - 1200),
        raw(2, ["1234A"], ["3333C"], at=NOW - 600),  # 10 min overdue, field hasn't reached it
    )
    await svc.handle_message(
        delivery.EVENTS_CHANNEL,
        json.dumps({"type": "fieldMatchAssigned", "payload": {"match": {"division": 1, "session": 0, "round": "QUAL", "instance": 1, "match": 1}}}),
    )

    await svc.tick(NOW)
    notices = await room_notices(fake_redis, svc.listener)
    assert [n["title"] for n in notices] == ["Q2 is running late"] and notices[0]["source"] == "late_alert"
    assert len(pushes) == 1 and pushes[0].payload["kind"] == "late" and "10 min" in pushes[0].payload["body"]

    pushes.clear()
    await svc.tick(NOW + 1)
    assert pushes == [] and await room_notices(fake_redis, svc.listener) == []


async def test_late_warning_off_and_not_sent_once_on_field(svc, session_factory, fake_redis, notif_settings):
    await notif_settings({"team_alerts_enabled": True, "lead_matches_x": 0, "lead_minutes_y": 0, "scores_enabled": False,
                          "late_minutes": 0})
    await seed_room(session_factory, teams=("1234A",))
    await put_schedule(fake_redis, raw(2, ["1234A"], ["3333C"], at=NOW - 600))
    await svc.tick(NOW)
    assert await room_notices(fake_redis, svc.listener) == []

    await notif_settings({"team_alerts_enabled": True, "lead_matches_x": 0, "lead_minutes_y": 0, "scores_enabled": False,
                          "late_minutes": 5})
    await svc.handle_message(
        delivery.EVENTS_CHANNEL,
        json.dumps({"type": "fieldMatchAssigned", "payload": {"match": {"division": 1, "session": 0, "round": "QUAL", "instance": 1, "match": 2}}}),
    )
    await svc.tick(NOW)
    assert await room_notices(fake_redis, svc.listener) == []  # already on the field


async def test_category_opt_out_skips_push(svc, session_factory, fake_redis, pushes, notif_settings):
    await notif_settings({"team_alerts_enabled": True, "lead_matches_x": 0, "lead_minutes_y": 0, "scores_enabled": False,
                          "late_minutes": 5, "agenda_announcements_enabled": False})
    await seed_room(session_factory, teams=("1234A",))
    await seed_sub(session_factory, "wants-late", teams=("1234A",), categories=["late"])
    await seed_sub(session_factory, "no-late", teams=("1234A",), categories=["score"])
    await seed_sub(session_factory, "all", teams=("1234A",), categories=None)
    await put_schedule(fake_redis, raw(2, ["1234A"], ["3333C"], at=NOW - 600))
    await svc.tick(NOW)
    assert sorted(t.endpoint for t in pushes) == sorted([VALID_ENDPOINT + "wants-late", VALID_ENDPOINT + "all"])


@pytest.fixture
async def agenda_rows(session_factory):
    from sqlalchemy import delete

    from backend.models.comms import AgendaItem

    async def _wipe():
        async with session_factory() as session:
            await session.execute(delete(AgendaItem))
            await session.commit()

    await _wipe()

    async def _add(start, title, **kw):
        async with session_factory() as session:
            session.add(AgendaItem(scope="day", start_time=start, title=title, **kw))
            await session.commit()

    yield _add
    await _wipe()


async def test_agenda_announcement_all_rooms_once_in_event_timezone(
    svc, session_factory, fake_redis, pushes, notif_settings, agenda_rows
):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    zone = ZoneInfo("Europe/London")
    now = datetime(2026, 10, 29, 12, 55, tzinfo=zone).timestamp()
    await notif_settings({"team_alerts_enabled": False, "agenda_announcements_enabled": True,
                          "agenda_lead_minutes": 10, "timezone": "Europe/London"})
    await seed_room(session_factory, slug="r1")
    await seed_room(session_factory, slug="r2", teams=("9999Z",))
    await seed_sub(session_factory, "t1", teams=("1234A",))
    await seed_sub(session_factory, "opted-out", teams=("1234A",), categories=["score"])
    await agenda_rows("13:00", "Lunch", details="Cafeteria")
    await agenda_rows("15:00", "Finals", announce_lead_minutes=0)

    await svc.tick(now)
    notices = await room_notices(fake_redis, svc.listener)
    assert len(notices) == 1 and notices[0]["rooms"] == ["r1", "r2"] and notices[0]["source"] == "announcement"
    assert notices[0]["title"] == "Coming up: Lunch" and "Cafeteria" in notices[0]["message"]
    assert [t.endpoint for t in pushes] == [VALID_ENDPOINT + "t1"] and pushes[0].payload["kind"] == "announcement"

    pushes.clear()
    await svc.tick(now + 30)
    assert pushes == [] and await room_notices(fake_redis, svc.listener) == []


async def test_agenda_disabled_and_bad_timezone(svc, session_factory, fake_redis, notif_settings, agenda_rows):
    from datetime import datetime, timezone

    await seed_room(session_factory)
    await agenda_rows("12:00", "Lunch")
    utc_noon_minus_5 = datetime(2026, 10, 29, 11, 55, tzinfo=timezone.utc).timestamp()
    await notif_settings({"team_alerts_enabled": False, "agenda_announcements_enabled": False})
    await svc.tick(utc_noon_minus_5)
    assert await room_notices(fake_redis, svc.listener) == []

    await notif_settings({"team_alerts_enabled": False, "agenda_announcements_enabled": True, "timezone": "Not/AZone"})
    await svc.tick(utc_noon_minus_5)  # falls back to UTC rather than crashing
    assert len(await room_notices(fake_redis, svc.listener)) == 1


async def test_alert_settings_parse_new_fields():
    s = AlertSettings.from_value({"late_minutes": 999, "agenda_lead_minutes": "x", "timezone": ""})
    assert s.late_minutes == 60 and s.agenda_lead_minutes == 10 and s.timezone == "UTC"
    assert AlertSettings.from_value(None).late_minutes == 5
