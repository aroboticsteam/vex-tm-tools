from backend.modules.comms.next_match import LATE_GRACE_SECONDS, find_next_match
from backend.modules.notifications.logic import parse_division

NOW = 1_700_000_000.0


def raw(num, at, state="UNPLAYED", red=("A1",), blue=("B1",), division=1):
    return {
        "matchInfo": {
            "timeScheduled": at,
            "state": state,
            "alliances": [{"teams": [{"number": t} for t in red]}, {"teams": [{"number": t} for t in blue]}],
            "matchTuple": {"session": 0, "division": division, "round": "QUAL", "instance": 1, "match": num},
        }
    }


def test_picks_earliest_unscored_across_divisions():
    d1 = parse_division(1, [raw(1, NOW - 600, "SCORED"), raw(2, NOW + 300)])
    d2 = parse_division(2, [raw(1, NOW + 120, division=2)])
    division, match = find_next_match({"1": d1, "2": d2}, NOW)
    assert division == "2" and match.match_num == 1


def test_returns_none_when_everything_scored_or_empty():
    assert find_next_match({}, NOW) is None
    assert find_next_match({"1": parse_division(1, [raw(1, NOW, "SCORED")])}, NOW) is None


def test_late_match_still_next_but_stale_ones_are_skipped():
    late = raw(1, NOW - 60)
    stale = raw(2, NOW - LATE_GRACE_SECONDS - 3600)
    upcoming = raw(3, NOW + 600)
    d = parse_division(1, [stale, late, upcoming])
    assert find_next_match({"1": d}, NOW)[1].match_num == 1
    d = parse_division(1, [stale, upcoming])
    assert find_next_match({"1": d}, NOW)[1].match_num == 3


def test_only_stale_matches_falls_back_to_earliest():
    d = parse_division(1, [raw(1, NOW - LATE_GRACE_SECONDS - 100), raw(2, NOW - LATE_GRACE_SECONDS - 50)])
    assert find_next_match({"1": d}, NOW)[1].match_num == 1


def test_untimed_matches_sort_last():
    d = parse_division(1, [raw(1, None), raw(2, NOW + 60)])
    assert find_next_match({"1": d}, NOW)[1].match_num == 2
