"""Object-store layout for recordings and moments (issue #45).

    {matchType}/{matchNumber}/match.mp4
    {matchType}/{matchNumber}/segments/moment-{n}.mp4
"""
from __future__ import annotations

import re
from typing import Any

#: Rounds whose `match` number is unique on its own. Elimination rounds repeat
#: match numbers per instance (R16 2-1, 2-2...), so they use `{instance}-{match}`.
_FLAT_ROUNDS = {"QUAL", "PRACTICE", "SKILLS"}


def match_type_slug(round_: Any) -> str:
    slug = re.sub(r"[^A-Z0-9]", "", str(round_ or "").upper())
    return slug or "MATCH"


def match_number_slug(round_: Any, instance: Any, match: Any) -> str:
    match_s = re.sub(r"[^A-Za-z0-9]", "", str(match if match is not None else "")) or "0"
    if match_type_slug(round_) in _FLAT_ROUNDS:
        return match_s
    instance_s = re.sub(r"[^A-Za-z0-9]", "", str(instance if instance is not None else "")) or "1"
    return f"{instance_s}-{match_s}"


def recording_key(match_type: str, match_number: str) -> str:
    return f"{match_type}/{match_number}/match.mp4"


def moment_key(match_type: str, match_number: str, number: int) -> str:
    return f"{match_type}/{match_number}/segments/moment-{number}.mp4"
