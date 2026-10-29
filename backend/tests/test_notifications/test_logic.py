"""Pure scheduling logic for team notifications (no I/O)."""
from __future__ import annotations

from backend.modules.notifications import logic
from backend.modules.notifications.logic import (
    ArrivalAlert,
    arrival_text,
    current_index,
    due_arrival_alerts,
    match_label,
    newly_scored,
    parse_division,
    parse_schedule,
    score_text,
)

NOW = 1_700_000_000.0


def raw_match(num, red, blue, *, round_="QUAL", at=None, state="UNPLAYED", score=None, instance=1):
    out = {
        "matchInfo": {
            "timeScheduled": at,
            "state": state,
            "alliances": [
                {"teams": [{"number": t} for t in red]},
                {"teams": [{"number": t} for t in blue]},
            ],
            "matchTuple": {"session": 0, "division": 1, "round": round_, "instance": instance, "match": num},
        }
    }
    if score is not None:
        out["finalScore"] = list(score)
    return out


def sched(*matches):
    return parse_division(1, list(matches))


def test_match_label_formats():
    assert match_label("QUAL", 1, 12) == "Q12"
    assert match_label("PRACTICE", 1, 3) == "P3"
    assert match_label("SF", 2, 1) == "SF 2-1"


def test_parse_division_orders_by_time_and_splits_alliances():
    matches = sched(
        raw_match(2, ["B1"], ["B2"], at=NOW + 600),
        raw_match(1, ["A1", "A2"], ["A3", "A4"], at=NOW),
    )
    assert [m.match_num for m in matches] == [1, 2]
    assert [m.idx for m in matches] == [0, 1]
    assert matches[0].red == ("A1", "A2") and matches[0].blue == ("A3", "A4")
    assert matches[0].label == "Q1"
    assert matches[0].key == "1:0:QUAL:1:1"


def test_parse_division_keeps_tm_order_without_times_and_skips_malformed():
    matches = sched(raw_match(5, ["A"], ["B"]), {"matchInfo": {}}, raw_match(4, ["C"], ["D"]))
    assert [m.match_num for m in matches] == [5, 4]


def test_parse_schedule_keys_by_division():
    parsed = parse_schedule({"divisions": [{"id": 3, "matches": [raw_match(1, ["A"], ["B"])]}]})
    assert list(parsed) == ["3"]


def test_final_score_parsed_only_when_present():
    matches = sched(raw_match(1, ["A"], ["B"], state="SCORED", score=(17, 11)), raw_match(2, ["C"], ["D"]))
    assert matches[0].final_score == (17, 11) and matches[0].scored
    assert matches[1].final_score is None and not matches[1].scored


def test_current_index_uses_latest_scored_or_assigned():
    matches = sched(
        raw_match(1, ["A"], ["B"], at=1, state="SCORED", score=(1, 0)),
        raw_match(2, ["C"], ["D"], at=2),
        raw_match(3, ["E"], ["F"], at=3),
    )
    assert current_index(matches, None) == 0
    assert current_index(matches, matches[2].key) == 2
    assert current_index(sched(raw_match(1, ["A"], ["B"])), None) == -1


def test_arrival_alerts_by_matches_ahead():
    matches = sched(*[raw_match(n, [f"T{n}"], [f"U{n}"], at=NOW + n * 3600) for n in range(1, 6)])
    alerts = due_arrival_alerts(matches, current_idx=0, now=NOW, lead_matches=2, lead_minutes=0)
    assert [(a.kind, a.match.match_num, a.matches_ahead) for a in alerts] == [("matches", 2, 1), ("matches", 3, 2)]


def test_arrival_alerts_by_minutes_window():
    matches = sched(
        raw_match(1, ["A"], ["B"], at=NOW + 20 * 60),  # outside 15 min window
        raw_match(2, ["C"], ["D"], at=NOW + 10 * 60),  # inside
        raw_match(3, ["E"], ["F"], at=NOW - 60),  # slightly late, still due
        raw_match(4, ["G"], ["H"], at=NOW - 3600),  # long gone: no alert
    )
    alerts = due_arrival_alerts(matches, current_idx=-1, now=NOW, lead_matches=0, lead_minutes=15)
    assert sorted((a.match.match_num, a.minutes_away) for a in alerts) == [(2, 10), (3, 0)]


def test_both_kinds_can_fire_for_one_match_and_have_distinct_dedupe_keys():
    matches = sched(raw_match(1, ["A"], ["B"], at=NOW + 5 * 60))
    alerts = due_arrival_alerts(matches, -1, NOW, 3, 15)
    assert {a.kind for a in alerts} == {"matches", "minutes"}
    assert len({a.dedupe_key for a in alerts}) == 2


def test_no_alert_for_scored_or_already_played_matches():
    matches = sched(
        raw_match(1, ["A"], ["B"], at=NOW, state="SCORED", score=(1, 2)),
        raw_match(2, ["C"], ["D"], at=NOW + 60),
    )
    alerts = due_arrival_alerts(matches, current_idx=1, now=NOW, lead_matches=5, lead_minutes=30)
    assert alerts == []


def test_newly_scored_requires_score():
    matches = sched(
        raw_match(1, ["A"], ["B"], state="SCORED", score=(1, 2)),
        raw_match(2, ["C"], ["D"], state="SCORED"),
    )
    assert [m.match_num for m in newly_scored(matches)] == [1]


def test_arrival_text_variants():
    m = sched(raw_match(12, ["1234A"], ["5678B"], at=NOW))[0]
    title, body = arrival_text(ArrivalAlert("matches", m, matches_ahead=1))
    assert title == "Q12 coming up" and "up next" in body
    _, body = arrival_text(ArrivalAlert("matches", m, matches_ahead=3), ["1234A"])
    assert body.startswith("1234A is 3 matches away")
    title, body = arrival_text(ArrivalAlert("minutes", m, minutes_away=1))
    assert "~1 min" in title and "1 minute." in body
    title, _ = arrival_text(ArrivalAlert("minutes", m, minutes_away=0))
    assert "due now" in title


def test_score_text_room_vs_personal():
    m = sched(raw_match(3, ["A1", "A2"], ["B1", "B2"], state="SCORED", score=(45, 38)))[0]
    title, body = score_text(m)
    assert title == "Q3 final" and "Red 45 – Blue 38" in body and "A1, A2" in body
    title, body = score_text(m, ["B2"])
    assert title == "Q3 final (B2)" and body == "Red 45 – Blue 38"


def test_match_key_from_tuple_matches_parse():
    tup = {"division": 1, "session": 0, "round": "QUAL", "instance": 1, "match": 7}
    m = sched(raw_match(7, ["A"], ["B"]))[0]
    assert logic.match_key_from_tuple(tup) == m.key
