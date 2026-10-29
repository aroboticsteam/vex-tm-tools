"""Unit tests for `backend.modules.automation.engine` (plan §14 `test_engine.py`):
trigger matching, Jinja2 sandboxed condition rendering, action resolution
(by entity_id and by tag, with `target_filter` narrowing + simultaneous
`asyncio.gather` fan-out), `delay` non-blocking semantics, `repeat`
semantics, `script` action chaining, and retry-once-then-continue error
handling.

These tests never touch real Postgres/Redis: `AutomationEngine`'s private
compiled-automation/-script state is populated directly, and
`backend.loader.get_instance` / `get_instances_by_tag` / `get_all_instances`
are monkeypatched to serve fake in-memory `Integration`-shaped objects —
same spirit as `backend/tests/test_loader_unit.py`.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import pytest

import backend.loader as loader_module
from backend.modules.automation.engine import (
    AutomationEngine,
    _CompiledAutomation,
    _CompiledScript,
    eval_bool,
    parse_hms,
    render_value,
    validate_automation_yaml,
)
from backend.schemas.events import EventBusMessage

pytestmark = pytest.mark.asyncio


@dataclass
class FakeIntegration:
    entity_id: str
    tags: list[str] = field(default_factory=list)
    state: dict[str, Any] = field(default_factory=dict)
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    fail_first_n: int = 0

    async def call_service(self, service: str, data: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((service, data))
        if len(self.calls) <= self.fail_first_n:
            raise RuntimeError(f"simulated failure #{len(self.calls)}")
        return {"ok": True}

    async def get_state(self) -> dict[str, Any]:
        return self.state


@pytest.fixture
def engine() -> AutomationEngine:
    return AutomationEngine(redis=object(), session_factory=object())


@pytest.fixture
def patch_loader(monkeypatch):
    """Registers fake integration instances and monkeypatches the loader's
    frozen public API (get_instance/get_instances_by_tag/get_all_instances)
    to serve them, without touching real Postgres/Redis."""
    registry: dict[str, FakeIntegration] = {}

    def get_instance(entity_id: str):
        return registry.get(entity_id)

    def get_instances_by_tag(tag: str):
        return [inst for inst in registry.values() if tag in inst.tags]

    def get_all_instances():
        return list(registry.values())

    monkeypatch.setattr(loader_module, "get_instance", get_instance)
    monkeypatch.setattr(loader_module, "get_instances_by_tag", get_instances_by_tag)
    monkeypatch.setattr(loader_module, "get_all_instances", get_all_instances)
    return registry


def _event(entity_id="vex_tm.division_1", event_type="matchStarted", tags=None, payload=None) -> EventBusMessage:
    return EventBusMessage(
        entity_id=entity_id,
        entity_tags=tags or [],
        type=event_type,
        timestamp=time.time(),
        payload=payload or {},
    )


# ── Trigger matching ──────────────────────────────────────────────────────


def test_trigger_matches_state_platform_entity_id_and_event_type(engine):
    spec = {"platform": "state", "entity_id": "vex_tm.division_1", "event_type": "matchStarted"}
    assert engine._trigger_matches(spec, _event()) is True
    assert engine._trigger_matches(spec, _event(event_type="matchStopped")) is False
    assert engine._trigger_matches(spec, _event(entity_id="vex_tm.division_2")) is False


def test_trigger_matches_state_platform_by_tag(engine):
    spec = {"platform": "state", "tag": "fs1", "event_type": "fieldActivated"}
    assert engine._trigger_matches(spec, _event(event_type="fieldActivated", tags=["fs1", "main"])) is True
    assert engine._trigger_matches(spec, _event(event_type="fieldActivated", tags=["fs2"])) is False


def test_trigger_matches_timer_milestone_tag_and_remaining(engine):
    spec = {"platform": "timer_milestone", "tag": "fieldCountdown", "remaining": "00:00:10"}
    matching_event = _event(
        entity_id="timer.fs1_field1",
        event_type="timer_milestone",
        tags=["fieldCountdown", "fs1"],
        payload={"remaining": 10, "elapsed": 5, "field_set_id": 1, "field_id": 1},
    )
    assert engine._trigger_matches(spec, matching_event) is True

    wrong_remaining = _event(
        entity_id="timer.fs1_field1",
        event_type="timer_milestone",
        tags=["fieldCountdown"],
        payload={"remaining": 3},
    )
    assert engine._trigger_matches(spec, wrong_remaining) is False

    wrong_type = _event(event_type="matchStarted", tags=["fieldCountdown"])
    assert engine._trigger_matches(spec, wrong_type) is False


def test_trigger_matches_optional_match_name_glob(engine):
    spec = {"platform": "state", "event_type": "matchStarted", "match_name": "Q*"}
    assert engine._trigger_matches(spec, _event(payload={"matchName": "Q45"})) is True
    assert engine._trigger_matches(spec, _event(payload={"matchName": "F1"})) is False
    assert engine._trigger_matches(spec, _event(payload={})) is False


def test_parse_hms():
    assert parse_hms("00:00:10") == 10
    assert parse_hms("00:01:00") == 60
    assert parse_hms("01:00:00") == 3600
    assert parse_hms(5) == 5


# ── Jinja2 sandboxed rendering ────────────────────────────────────────────


def test_render_value_full_expression_returns_native_type(engine):
    from backend.modules.automation.engine import _DotDict

    ctx = {"trigger": _DotDict({"payload": _DotDict({"fieldID": 3})})}
    assert render_value("{{ trigger.payload.fieldID }}", ctx) == 3
    assert isinstance(render_value("{{ trigger.payload.fieldID }}", ctx), int)


def test_render_value_mixed_string_renders_as_text(engine):
    ctx = {"x": 3}
    assert render_value("field_{{ x }}_active", ctx) == "field_3_active"


def test_render_value_non_string_passthrough():
    assert render_value(42, {}) == 42
    assert render_value(None, {}) is None
    assert render_value({"a": "{{ 1 + 1 }}"}, {}) == {"a": 2}


def test_eval_bool_condition_true_and_false():
    assert eval_bool("{{ 1 in [1, 2, 3] }}", {}) is True
    assert eval_bool("{{ 1 in [4, 5] }}", {}) is False


def test_jinja_sandbox_blocks_class_escape():
    """Appendix C.5: the Jinja2 environment must be a SandboxedEnvironment
    with no ability to reach unsafe internals like `''.__class__`."""
    from jinja2.exceptions import SecurityError

    with pytest.raises(SecurityError):
        render_value("{{ ''.__class__.__mro__[1].__subclasses__() }}", {})


def test_validate_automation_yaml_reports_syntax_errors():
    ok, errors = validate_automation_yaml(
        trigger_yaml="- platform: state\n  entity_id: vex_tm.division_1\n  event_type: matchStarted\n",
        condition_yaml="- \"{{ trigger.payload.fieldID in [1, 2, 3] }}\"\n",
        action_yaml="- service: atem.switch_input\n  target: atem.main_switcher\n  data:\n    input_index: \"{{ trigger.payload.fieldID }}\"\n",
    )
    assert ok is True
    assert errors == []

    ok, errors = validate_automation_yaml(
        trigger_yaml="- platform: state\n",
        condition_yaml=None,
        action_yaml="- service: atem.switch_input\n  data:\n    x: \"{{ not valid jinja ((\"\n",
    )
    assert ok is False
    assert any("Jinja2 syntax error" in e for e in errors)


# ── Target resolution (entity_id / tag / target_filter / gather) ─────────


async def test_resolve_targets_by_entity_id(engine, patch_loader):
    patch_loader["atem.main_switcher"] = FakeIntegration(entity_id="atem.main_switcher")
    targets = engine._resolve_targets({"target": "atem.main_switcher"}, {})
    assert [t.entity_id for t in targets] == ["atem.main_switcher"]


async def test_resolve_targets_by_tag_without_filter(engine, patch_loader):
    patch_loader["spotify.fs1_field1"] = FakeIntegration(entity_id="spotify.fs1_field1", tags=["fieldAudio", "fs1"])
    patch_loader["spotify.fs2_field1"] = FakeIntegration(entity_id="spotify.fs2_field1", tags=["fieldAudio", "fs2"])
    targets = engine._resolve_targets({"target_tag": "fieldAudio"}, {})
    assert {t.entity_id for t in targets} == {"spotify.fs1_field1", "spotify.fs2_field1"}


async def test_resolve_targets_by_tag_with_target_filter_intersect(engine, patch_loader):
    """Appendix B.6 / plan §3.6: `target_filter` narrows a tag-targeted set
    via `trigger.entity.tags | intersect(target.tags)` — only the entity
    sharing a field tag with the triggering entity should be selected."""
    patch_loader["spotify.fs1_field1"] = FakeIntegration(entity_id="spotify.fs1_field1", tags=["fieldAudio", "fs1"])
    patch_loader["spotify.fs2_field1"] = FakeIntegration(entity_id="spotify.fs2_field1", tags=["fieldAudio", "fs2"])

    from backend.modules.automation.engine import _DotDict

    context = {"trigger": _DotDict({"entity": _DotDict({"tags": ["fieldCountdown", "fs1"]})})}
    action = {
        "target_tag": "fieldAudio",
        "target_filter": "{{ trigger.entity.tags | intersect(target.tags) | length > 0 }}",
    }
    targets = engine._resolve_targets(action, context)
    assert [t.entity_id for t in targets] == ["spotify.fs1_field1"]


async def test_service_action_fires_tag_targets_simultaneously(engine, patch_loader):
    """Appendix B.6: "Ordering: Simultaneous (not sequential)" — two slow
    targets must both be mid-flight at the same time, not one after another."""

    class SlowIntegration(FakeIntegration):
        async def call_service(self, service, data):
            self.calls.append((service, data))
            await asyncio.sleep(0.2)
            return {"ok": True}

    a = SlowIntegration(entity_id="zeros.a", tags=["vfx"])
    b = SlowIntegration(entity_id="zeros.b", tags=["vfx"])
    patch_loader["zeros.a"] = a
    patch_loader["zeros.b"] = b

    action = {"service": "zeros.set_preset", "target_tag": "vfx", "data": {}}
    start = time.monotonic()
    ok, err = await engine._do_service_action(action, {})
    elapsed = time.monotonic() - start

    assert ok is True
    assert err is None
    # Sequential execution would take >= 0.4s; concurrent stays near 0.2s.
    assert elapsed < 0.35
    assert len(a.calls) == 1
    assert len(b.calls) == 1


async def test_service_action_no_targets_is_a_failure(engine, patch_loader):
    ok, err = await engine._do_service_action({"service": "atem.switch_input", "target": "atem.missing"}, {})
    assert ok is False
    assert "No targets resolved" in err


# ── Retry-once-then-continue error handling (Appendix A.7) ───────────────


async def test_action_retries_once_then_succeeds(engine, patch_loader):
    inst = FakeIntegration(entity_id="atem.main_switcher", fail_first_n=1)
    patch_loader["atem.main_switcher"] = inst
    action = {"service": "atem.switch_input", "target": "atem.main_switcher", "data": {"input_index": 1}}
    ok, err = await engine._do_service_action(action, {})
    assert ok is True
    assert err is None
    assert len(inst.calls) == 2  # original attempt + one retry


async def test_action_fails_after_retry_but_chain_continues(engine, patch_loader):
    failing = FakeIntegration(entity_id="atem.main_switcher", fail_first_n=99)
    succeeding = FakeIntegration(entity_id="zeros.lighting_board")
    patch_loader["atem.main_switcher"] = failing
    patch_loader["zeros.lighting_board"] = succeeding

    actions = [
        {"service": "atem.switch_input", "target": "atem.main_switcher", "data": {}},
        {"service": "zeros.set_preset", "target": "zeros.lighting_board", "data": {}},
    ]
    ok, error, failed_index, executed, action_results = await engine._execute_action_chain(actions, {})

    assert ok is False
    assert failed_index == 0
    assert error is not None
    assert executed == 2  # both actions ran despite the first one failing
    assert len(failing.calls) == 2  # one retry attempted
    assert len(succeeding.calls) == 1  # second action still executed
    assert action_results == [
        {"index": 0, "status": "failed", "error": action_results[0]["error"]},
        {"index": 1, "status": "success", "error": None},
    ]
    assert action_results[0]["error"] is not None


# ── Delay: never blocks the engine loop ──────────────────────────────────


async def test_delay_action_does_not_block_and_continuation_runs_later(engine, patch_loader):
    inst = FakeIntegration(entity_id="zeros.lighting_board")
    patch_loader["zeros.lighting_board"] = inst

    actions = [
        {"delay": 0.15},  # short delay, kept sub-second so the test stays fast
        {"service": "zeros.set_preset", "target": "zeros.lighting_board", "data": {}},
    ]

    start = time.monotonic()
    ok, error, failed_index, executed, action_results = await engine._execute_action_chain(actions, {})
    elapsed = time.monotonic() - start

    assert ok is True
    assert executed == 1  # only the delay action is "executed" synchronously
    assert elapsed < 0.1  # returned immediately — did not block on the 0.15s delay
    assert len(inst.calls) == 0  # continuation hasn't run yet

    await asyncio.sleep(0.3)
    assert len(inst.calls) == 1  # continuation ran in the background after the delay


async def test_second_event_processed_before_delay_resolves(engine, patch_loader):
    """Simulates the engine's event loop staying responsive: processing an
    automation containing a `delay` must not prevent a second, unrelated
    automation from being processed immediately afterward."""
    inst = FakeIntegration(entity_id="zeros.lighting_board")
    patch_loader["zeros.lighting_board"] = inst

    slow_automation = _CompiledAutomation(
        id=uuid.uuid4(),
        alias="slow",
        triggers=[{"platform": "state", "event_type": "matchStarted"}],
        conditions=[],
        actions=[{"delay": 5}, {"service": "zeros.set_preset", "target": "zeros.lighting_board", "data": {}}],
    )
    fast_inst = FakeIntegration(entity_id="atem.main_switcher")
    patch_loader["atem.main_switcher"] = fast_inst
    fast_automation = _CompiledAutomation(
        id=uuid.uuid4(),
        alias="fast",
        triggers=[{"platform": "state", "event_type": "matchStopped"}],
        conditions=[],
        actions=[{"service": "atem.switch_input", "target": "atem.main_switcher", "data": {}}],
    )
    engine._automations = [slow_automation, fast_automation]

    # Avoid touching Postgres for history logging in this pure-timing test.
    async def _noop_log(*args, **kwargs):
        return None

    engine._log_run = _noop_log
    engine._touch_last_triggered = _noop_log

    start = time.monotonic()
    await engine._process_event(_event(event_type="matchStarted"))
    await engine._process_event(_event(event_type="matchStopped"))
    elapsed = time.monotonic() - start

    assert elapsed < 1.0  # the 5s delay never blocked processing of the second event
    assert len(fast_inst.calls) == 1


# ── repeat action semantics ───────────────────────────────────────────────


async def test_repeat_count(engine, patch_loader):
    inst = FakeIntegration(entity_id="zeros.lighting_board")
    patch_loader["zeros.lighting_board"] = inst
    spec = {"count": 3, "sequence": [{"service": "zeros.set_preset", "target": "zeros.lighting_board", "data": {}}]}
    ok, err = await engine._do_repeat(spec, {})
    assert ok is True
    assert err is None
    assert len(inst.calls) == 3


async def test_repeat_while(engine, patch_loader):
    inst = FakeIntegration(entity_id="zeros.lighting_board")
    patch_loader["zeros.lighting_board"] = inst

    # `while` is a Jinja2 expression string; give it a variable to count down.
    context: dict[str, Any] = {"n": 3}

    async def _service_and_decrement(*a, **kw):
        context["n"] -= 1

    spec = {"while": "{{ n > 0 }}", "sequence": [{"service": "zeros.set_preset", "target": "zeros.lighting_board", "data": {}}]}

    # Patch the fake integration's call_service to decrement `n` each call
    # so the while-loop actually terminates.
    async def call_service(service, data):
        inst.calls.append((service, data))
        context["n"] -= 1
        return {"ok": True}

    inst.call_service = call_service  # type: ignore[method-assign]

    ok, err = await engine._do_repeat(spec, context)
    assert ok is True
    assert len(inst.calls) == 3
    assert context["n"] == 0


# ── script action ──────────────────────────────────────────────────────


async def test_script_action_calls_named_script_chain(engine, patch_loader):
    inst = FakeIntegration(entity_id="spotify.fs1_field1")
    patch_loader["spotify.fs1_field1"] = inst
    engine._scripts = {
        "field_spotify_play": _CompiledScript(
            name="field_spotify_play",
            actions=[
                {
                    "service": "spotify.play_playlist_track",
                    "target": "spotify.fs1_field1",
                    "data": {"tag_used": "{{ vars.field_tag }}"},
                }
            ],
        )
    }
    action = {"script": "field_spotify_play", "data": {"field_tag": "fs1"}}
    ok, err = await engine._do_script_action(action, {})
    assert ok is True
    assert err is None
    assert inst.calls == [("play_playlist_track", {"tag_used": "fs1"})]


async def test_script_action_unknown_script_fails(engine):
    ok, err = await engine._do_script_action({"script": "does_not_exist"}, {})
    assert ok is False
    assert "Unknown script" in err


# ── inline condition action stops the chain without being a "failure" ────


async def test_inline_condition_action_stops_chain(engine, patch_loader):
    inst = FakeIntegration(entity_id="zeros.lighting_board")
    patch_loader["zeros.lighting_board"] = inst
    actions = [
        {"condition": "{{ False }}"},
        {"service": "zeros.set_preset", "target": "zeros.lighting_board", "data": {}},
    ]
    ok, err, failed_index, executed, action_results = await engine._execute_action_chain(actions, {})
    assert ok is True
    assert err is None
    assert executed == 1  # chain stopped at the condition; second action never ran
    assert len(inst.calls) == 0


async def test_run_actions_executes_chain_and_reports(engine, patch_loader):
    wled = FakeIntegration(entity_id="wled.field1")
    patch_loader["wled.field1"] = wled
    actions = [
        {"service": "wled.set_power", "target": "wled.field1", "data": {"on": True}},
        {"service": "wled.set_preset", "target": "missing.entity", "data": {"preset": 1}},
    ]
    result = await engine.run_actions(actions, uuid.uuid4(), {"button": "Go"})
    assert wled.calls == [("set_power", {"on": True})]
    assert result["status"] == "failed"  # second action has no target
    assert result["failed_action_index"] == 1
    assert result["actions_executed"] == 2
    assert [r["status"] for r in result["action_results"]] == ["success", "failed"]


async def test_run_actions_renders_button_context(engine, patch_loader):
    osc = FakeIntegration(entity_id="osc.main")
    patch_loader["osc.main"] = osc
    actions = [{"service": "osc.send", "target": "osc.main", "data": {"address": "/x", "args": ["{{ trigger.payload.button }}"]}}]
    result = await engine.run_actions(actions, uuid.uuid4(), {"button": "Flash"})
    assert result["status"] == "success"
    assert osc.calls == [("send", {"address": "/x", "args": ["Flash"]})]
