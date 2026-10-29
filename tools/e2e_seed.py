"""Seeds (or removes) the fixture data the Playwright suite expects.

    python tools/e2e_seed.py            # insert E2E teams + a cached TM schedule
    python tools/e2e_seed.py --cleanup  # remove them again

Reads POSTGRES_DSN and REDIS_URL from the environment, like the backend.
Two-team alliances make division 1 an IQ division; four-team ones make
division 2 VRC, so both leaderboard programmes exist.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time

import redis.asyncio as redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

SCHEDULE_KEY = "qecomp:tm:e2e:schedule"

TEAMS = {
    "E2E1A": {
        "team_name": "Gearsquad E2E", "organisation": "QE School", "location": "London, United Kingdom",
        "country": "United Kingdom", "division_id": 1, "event": {"name": "E2E Battle"},
        "ranking": {"rank": 1, "wins": 4, "losses": 0, "ties": 0, "avgPoints": 41.5},
        "skills": [{"rank": 2, "totalScore": 88, "driverHighScore": 40, "progHighScore": 48}],
        "awards": [{"title": "Excellence"}], "skills_rank": 12,
    },
    "E2E2B": {
        "team_name": "Beta E2E", "division_id": 1, "country": "United Kingdom",
        "ranking": {"rank": 2, "wins": 3, "losses": 1, "ties": 0, "avgPoints": 35.0},
        "skills": [{"rank": 1, "totalScore": 95, "driverHighScore": 45, "progHighScore": 50}], "skills_rank": 5,
    },
    "E2E3C": {
        "team_name": "VexFive E2E", "division_id": 2, "ranking": {"rank": 1, "wins": 3, "losses": 0, "ties": 0, "avgPoints": 120.0},
        "skills": [{"rank": 1, "totalScore": 210}], "awards": [{"title": "Design"}, {"title": "Judges"}],
    },
    "E2E4D": {"team_name": "Delta E2E", "division_id": 2, "ranking": {"rank": 2}},
}


def _match(num: int, red: list[str], blue: list[str], division: int, now: float, offset: float, scored: bool) -> dict:
    out = {
        "matchInfo": {
            "timeScheduled": now + offset,
            "state": "SCORED" if scored else "UNPLAYED",
            "alliances": [{"teams": [{"number": t} for t in red]}, {"teams": [{"number": t} for t in blue]}],
            "matchTuple": {"session": 0, "division": division, "round": "QUAL", "instance": 1, "match": num},
        }
    }
    if scored:
        out["finalScore"] = [30, 20]
    return out


async def main(cleanup: bool) -> None:
    engine = create_async_engine(os.environ["POSTGRES_DSN"])
    client = redis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0"), decode_responses=True)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM team_profiles WHERE team_number LIKE 'E2E%'"))
            if not cleanup:
                for number, stats in TEAMS.items():
                    await conn.execute(
                        text("INSERT INTO team_profiles (team_number, cached_stats) VALUES (:n, CAST(:s AS jsonb))"),
                        {"n": number, "s": json.dumps(stats)},
                    )
        if cleanup:
            await client.delete(SCHEDULE_KEY)
        else:
            now = time.time()
            schedule = {
                "divisions": [
                    {"id": 1, "matches": [
                        _match(1, ["E2E1A"], ["E2E2B"], 1, now, -600, True),
                        _match(2, ["E2E2B"], ["E2E1A"], 1, now, 600, False),
                    ]},
                    {"id": 2, "matches": [_match(1, ["E2E3C", "X1"], ["E2E4D", "X2"], 2, now, -300, True)]},
                ]
            }
            await client.set(SCHEDULE_KEY, json.dumps(schedule), ex=6 * 3600)
        print("cleaned up E2E fixtures" if cleanup else "seeded E2E fixtures")
    finally:
        await client.aclose()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main("--cleanup" in sys.argv))
