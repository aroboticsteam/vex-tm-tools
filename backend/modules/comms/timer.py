"""Station timer state transitions (issue #27, stagetimer-style cues).

State is a plain dict (see `TimerState`): it never stores a "remaining"
value, only when the current run began and what ran before it, so every
client can compute the same display from the shared server clock.
"""
from __future__ import annotations

import uuid
from typing import Any


def start(*, mode: str, duration: int | None, label: str, warn: int, critical: int, now: float) -> dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "mode": mode,
        "label": label,
        "duration": duration if mode == "countdown" else None,
        "warn": warn,
        "critical": critical,
        "elapsed_before": 0.0,
        "running_since": now,
    }


def elapsed(state: dict[str, Any], now: float) -> float:
    running = state.get("running_since")
    return float(state.get("elapsed_before", 0.0)) + (max(0.0, now - running) if running is not None else 0.0)


def remaining(state: dict[str, Any], now: float) -> float | None:
    """Seconds left on a countdown (negative once overtime); None for count-up."""
    if state.get("mode") != "countdown" or state.get("duration") is None:
        return None
    return float(state["duration"]) - elapsed(state, now)


def pause(state: dict[str, Any], now: float) -> dict[str, Any]:
    if state.get("running_since") is None:
        return state
    return {**state, "elapsed_before": elapsed(state, now), "running_since": None}


def resume(state: dict[str, Any], now: float) -> dict[str, Any]:
    if state.get("running_since") is not None:
        return state
    return {**state, "running_since": now}


def adjust(state: dict[str, Any], seconds: int) -> dict[str, Any]:
    """Adds time to (or removes it from) a countdown; count-ups have no
    target to move so they're returned unchanged."""
    if state.get("mode") != "countdown" or state.get("duration") is None:
        return state
    return {**state, "duration": max(1, int(state["duration"]) + seconds)}


def colour(state: dict[str, Any], now: float) -> str:
    """The cue colour a station shows: green / amber / red / overtime."""
    left = remaining(state, now)
    if left is None:
        return "neutral"
    if left < 0:
        return "overtime"
    if left <= state.get("critical", 30):
        return "red"
    if left <= state.get("warn", 120):
        return "amber"
    return "green"
