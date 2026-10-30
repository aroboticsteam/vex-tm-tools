"""Per-team checklist (issue #43): inspection, 360 bot video, team video.

Pure helpers so the admin API and the public site agree on what counts as
done. A video counts when its processed clip is `DONE`; the 360 bot video also
counts the legacy processed `video_360` upload, matching the overlay fallback
in `routers/team_clips.py`.
"""
from __future__ import annotations

from typing import Any

from backend.models.team import TeamProfile
from backend.models.team_clip import TeamClip

INSPECTION_STATUSES = ("NOT_STARTED", "IN_PROGRESS", "PASSED")
TOTAL_ITEMS = 3


def checklist_for(profile: TeamProfile, clips: dict[str, TeamClip]) -> dict[str, Any]:
    bot_clip = clips.get("bot")
    team_clip = clips.get("team")
    bot_done = bool(
        (bot_clip is not None and bot_clip.status == "DONE")
        or (profile.video_360_s3_key and profile.video_processing_status == "DONE")
    )
    team_done = bool(team_clip is not None and team_clip.status == "DONE")
    inspection = profile.inspection_status or "NOT_STARTED"
    done = int(inspection == "PASSED") + int(bot_done) + int(team_done)
    return {
        "team_number": profile.team_number,
        "inspection": inspection,
        "bot_video": bot_done,
        "team_video": team_done,
        "done": done,
        "total": TOTAL_ITEMS,
        "complete": done == TOTAL_ITEMS,
    }
