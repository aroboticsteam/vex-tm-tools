"""Team checklist logic (issue #43)."""
from __future__ import annotations

from backend.models.team import TeamProfile
from backend.models.team_clip import TeamClip
from backend.modules.site.checklist import INSPECTION_STATUSES, checklist_for


def _profile(**kw) -> TeamProfile:
    base = dict(team_number="1234A", inspection_status="NOT_STARTED", video_processing_status="NONE", video_360_s3_key=None)
    base.update(kw)
    return TeamProfile(**base)


def _clip(kind: str, status: str) -> TeamClip:
    return TeamClip(team_number="1234A", kind=kind, status=status)


def test_nothing_done():
    result = checklist_for(_profile(), {})
    assert result == {
        "team_number": "1234A",
        "inspection": "NOT_STARTED",
        "bot_video": False,
        "team_video": False,
        "done": 0,
        "total": 3,
        "complete": False,
    }


def test_all_done_is_complete():
    result = checklist_for(_profile(inspection_status="PASSED"), {"bot": _clip("bot", "DONE"), "team": _clip("team", "DONE")})
    assert result["done"] == 3 and result["complete"] is True


def test_in_progress_inspection_does_not_count():
    assert checklist_for(_profile(inspection_status="IN_PROGRESS"), {})["done"] == 0


def test_only_done_clips_count():
    clips = {"bot": _clip("bot", "PROCESSING"), "team": _clip("team", "FAILED")}
    result = checklist_for(_profile(), clips)
    assert (result["bot_video"], result["team_video"]) == (False, False)


def test_legacy_360_upload_counts_for_bot_video_only():
    legacy = _profile(video_360_s3_key="k.webm", video_processing_status="DONE")
    result = checklist_for(legacy, {})
    assert result["bot_video"] is True and result["team_video"] is False
    # an unfinished legacy upload does not count
    assert checklist_for(_profile(video_360_s3_key="k.webm", video_processing_status="PROCESSING"), {})["bot_video"] is False


def test_missing_inspection_value_defaults_to_not_started():
    assert checklist_for(_profile(inspection_status=None), {})["inspection"] == "NOT_STARTED"


def test_statuses_constant():
    assert INSPECTION_STATUSES == ("NOT_STARTED", "IN_PROGRESS", "PASSED")
