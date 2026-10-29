"""YAML + Jinja2 automation & scripting engine (plan §5.8 / §10 / §C.2).

Owns: loading `automations`/`scripts` rows from Postgres, YAML parsing,
Jinja2 template evaluation (via a `SandboxedEnvironment` — no filesystem
access, no `os`/`subprocess`, per plan §C.5), action dispatch, script
execution, non-blocking `delay` handling, and automation execution history
logging (Appendix A.7, via the new `automation_runs` table — see
`backend/models/automation.py`'s `AutomationRun` and the accompanying
Alembic migration).

**Frozen public API** (plan §C.2 / §10) — do not change these signatures:

    class AutomationEngine:
        async def start(self) -> None
        async def stop(self) -> None
        async def reload(self) -> None
        async def trigger(self, automation_id: UUID, context: dict = {}) -> dict

Per plan §C.2/§C.5, this module:
  * May import from `backend/core/` (DB, Redis), `backend/models/`
    (read-only automation/script/timer rows), and `backend/loader`
    (`get_instance`/`get_instances_by_tag` — never integration classes
    directly).
  * Must NOT import from `routers/`, `schemas/`, integration modules,
    `modules/timer/`, `modules/scraper/`, `modules/media/`.
  * Resolves integration instances exclusively via
    `backend.loader.get_instance(entity_id)` /
    `backend.loader.get_instances_by_tag(tag)` — it never imports an
    integration class. This is how new integrations can be added without
    touching the engine (plan §C.5 rule 4).

Known Wave 1/2a gap this module is aware of but does not attempt to fix
(flagged by the task brief): `backend.loader.get_instance_status()` can be
briefly stale for self-reporting-DEGRADED integrations like OBS. The engine
never reads integration *status* (only `get_state()` via `states()`/
`is_state()` in Jinja templates and `call_service()` for actions), so this
gap does not affect the engine's own correctness — noted here only so a
future wave doesn't have to rediscover it.
"""
from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import logging
import ipaddress
import re
import time
import uuid
from urllib.parse import urlparse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import yaml
from jinja2 import TemplateError
from jinja2.sandbox import SandboxedEnvironment
from sqlalchemy import func, select, update

from backend import loader
from backend.core.ntfy import send_ntfy_notification
from backend.core.redis import redis_client as _default_redis_client
from backend.core.db import async_session_factory as _default_session_factory
from backend.models.automation import Automation, AutomationRun, Script
from backend.models.timer import TimerInstance
from backend.schemas.events import EventBusMessage

logger = logging.getLogger(__name__)

EVENTS_CHANNEL = "qecomp:events"
CONFIG_CHANGE_CHANNEL = "qecomp:config_change"

# Config-change resource types that should trigger a hot reload of the
# engine's compiled automations/scripts (plan §3.2-style hot reload, applied
# to automations — see engine docstring + plan §5.8 point 6).
_RELOAD_RESOURCE_TYPES = {"automation", "automation_folder", "script", "timer_instance"}

# Safety cap on `repeat: {while: ...}` loops so a buggy condition can never
# spin the engine forever.
_MAX_REPEAT_WHILE_ITERATIONS = 1000

_FULL_EXPR_RE = re.compile(r"^\s*\{\{(.*)\}\}\s*$", re.DOTALL)


# ── Jinja2 context helpers ───────────────────────────────────────────────


class _DotDict(dict):
    """Thin dict subclass giving attribute access to keys (recursively),
    e.g. `trigger.payload.fieldID` and `trigger.entity.tags` (plan §5.8's
    Jinja2 context: "`trigger` — the raw event (`.entity_id`, `.payload`,
    `.entity.tags`, `.entity.field`)"). Plain `dict` access (`trigger['x']`)
    keeps working too."""

    def __getattr__(self, item: str) -> Any:
        try:
            value = self[item]
        except KeyError as exc:
            raise AttributeError(item) from exc
        return _wrap(value)


def _wrap(value: Any) -> Any:
    if isinstance(value, dict):
        return _DotDict(value)
    if isinstance(value, list):
        return [_wrap(v) for v in value]
    return value


def _intersect_filter(a: Any, b: Any) -> list[Any]:
    """Jinja2 filter used by tag-intersection targeting (plan §3.6/§6/§B.6):
    `{{ trigger.entity.tags | intersect(target.tags) | length > 0 }}`."""
    a = a or []
    b = b or []
    return [x for x in a if x in b]


def _build_jinja_env() -> SandboxedEnvironment:
    """A `SandboxedEnvironment` per plan §C.5: "No filesystem access, no
    `os`, no `subprocess` in templates." `SandboxedEnvironment` blocks
    attribute access to unsafe/internal attributes (e.g. `''.__class__`)
    at render time regardless of what's placed in the context."""
    env = SandboxedEnvironment(autoescape=False)
    env.filters["intersect"] = _intersect_filter
    return env


_JINJA_ENV = _build_jinja_env()


def parse_hms(value: Any) -> int | float:
    """Parses a `"HH:MM:SS"` duration string (plan §5.8/§7's `delay`/
    `remaining` fields) into total seconds. A bare `int`/`float` is passed
    through as-is (not truncated) so a `delay:` action can use a sub-second
    number directly."""
    if isinstance(value, (int, float)):
        return value
    parts = [int(p) for p in str(value).strip().split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    hours, minutes, seconds = parts[-3:]
    return hours * 3600 + minutes * 60 + seconds


def render_value(value: Any, context: dict[str, Any]) -> Any:
    """Renders a single YAML-parsed value against `context`.

    A string that is *entirely* one `{{ ... }}` expression (ignoring
    surrounding whitespace) is evaluated via `Environment.compile_expression`
    so it returns its native Python type (int/bool/list/...) rather than a
    stringified render — this is what lets `input_index: "{{ trigger.payload.fieldID }}"`
    produce an `int` and `target_filter: "{{ ... | length > 0 }}"` produce a
    `bool`. A string containing template markup mixed with other text is
    rendered as a plain string (Home-Assistant-style). Anything else
    (dict/list/non-string scalars) is walked recursively / returned as-is.
    """
    if isinstance(value, str):
        match = _FULL_EXPR_RE.match(value)
        if match:
            compiled = _JINJA_ENV.compile_expression(match.group(1).strip(), undefined_to_none=False)
            return compiled(**context)
        if "{{" in value or "{%" in value:
            return _JINJA_ENV.from_string(value).render(**context)
        return value
    if isinstance(value, dict):
        return {k: render_value(v, context) for k, v in value.items()}
    if isinstance(value, list):
        return [render_value(v, context) for v in value]
    return value


def eval_bool(expr: Any, context: dict[str, Any]) -> bool:
    result = render_value(expr, context)
    if isinstance(result, bool):
        return result
    if isinstance(result, str):
        return result.strip().lower() in ("true", "1", "yes")
    return bool(result)


def validate_automation_yaml(
    trigger_yaml: str, condition_yaml: str | None, action_yaml: str
) -> tuple[bool, list[str]]:
    """Appendix A.7's "Validate" button backend: checks YAML syntax and
    Jinja2 expression syntax without executing any services. Thin routers
    (`backend/routers/automations.py`) delegate here rather than embedding
    this logic themselves."""
    errors: list[str] = []

    def _load_list(name: str, text: str | None) -> list[Any]:
        if not text:
            return []
        try:
            parsed = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            errors.append(f"{name}: invalid YAML — {exc}")
            return []
        if parsed is None:
            return []
        return parsed if isinstance(parsed, list) else [parsed]

    triggers = _load_list("trigger", trigger_yaml)
    conditions = _load_list("condition", condition_yaml)
    actions = _load_list("action", action_yaml)

    def _check_templates(value: Any, path: str) -> None:
        if isinstance(value, str) and ("{{" in value or "{%" in value):
            try:
                _JINJA_ENV.parse(value)
            except TemplateError as exc:
                errors.append(f"{path}: Jinja2 syntax error — {exc}")
        elif isinstance(value, dict):
            for k, v in value.items():
                _check_templates(v, f"{path}.{k}")
        elif isinstance(value, list):
            for i, v in enumerate(value):
                _check_templates(v, f"{path}[{i}]")

    for i, cond in enumerate(conditions):
        _check_templates(cond, f"condition[{i}]")
    for i, action in enumerate(actions):
        _check_templates(action, f"action[{i}]")

    if not triggers:
        errors.append("trigger: at least one trigger is required")
    if not actions:
        errors.append("action: at least one action is required")

    return (len(errors) == 0), errors


def validate_script_yaml(action_yaml: str) -> tuple[bool, list[str]]:
    """Script equivalent of `validate_automation_yaml`'s checks: YAML +
    Jinja2 syntax only, with no trigger requirement (scripts have no
    trigger_yaml — they're only ever invoked via a `script: <name>` action
    entry in an automation)."""
    return validate_automation_yaml(trigger_yaml="- platform: manual", condition_yaml=None, action_yaml=action_yaml)


# ── Compiled automation/script representations ──────────────────────────


@dataclass
class _CompiledAutomation:
    id: uuid.UUID
    alias: str
    triggers: list[dict[str, Any]]
    conditions: list[Any]
    actions: list[dict[str, Any]]


@dataclass
class _CompiledScript:
    name: str
    actions: list[dict[str, Any]] = field(default_factory=list)


class AutomationEngine:
    """See module docstring. Public API is frozen per plan §C.2/§10."""

    def __init__(self, redis: Any = None, session_factory: Any = None) -> None:
        self._redis = redis if redis is not None else _default_redis_client
        self._session_factory = session_factory if session_factory is not None else _default_session_factory

        self._automations: list[_CompiledAutomation] = []
        self._scripts: dict[str, _CompiledScript] = {}
        self._timer_field_by_entity: dict[str, int] = {}

        self._events_task: asyncio.Task | None = None
        self._config_task: asyncio.Task | None = None
        self._shutdown = asyncio.Event()

    # ── public API ────────────────────────────────────────────────────

    async def start(self) -> None:
        """Loads automations/scripts from Postgres and subscribes to
        `qecomp:events` (plan §5.8 point 1)."""
        await self.reload()
        self._shutdown = asyncio.Event()
        self._events_task = asyncio.create_task(self._events_listener())
        self._config_task = asyncio.create_task(self._config_change_listener())

    async def stop(self) -> None:
        self._shutdown.set()
        for task_attr in ("_events_task", "_config_task"):
            task = getattr(self, task_attr)
            if task is not None:
                task.cancel()
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await task
                setattr(self, task_attr, None)

    async def reload(self) -> None:
        """Re-reads all enabled automations + all scripts + timer instances
        (for `trigger.entity.field` resolution, plan §3.3) from Postgres."""
        async with self._session_factory() as session:
            auto_rows = list(
                (
                    await session.execute(select(Automation).where(Automation.enabled.is_(True)))
                ).scalars().all()
            )
            script_rows = list((await session.execute(select(Script))).scalars().all())
            timer_rows = list((await session.execute(select(TimerInstance))).scalars().all())

        compiled_automations: list[_CompiledAutomation] = []
        for row in auto_rows:
            parsed = self._parse_automation_yaml(row)
            if parsed is None:
                continue
            triggers, conditions, actions = parsed
            compiled_automations.append(
                _CompiledAutomation(id=row.id, alias=row.alias, triggers=triggers, conditions=conditions, actions=actions)
            )
        self._automations = compiled_automations

        compiled_scripts: dict[str, _CompiledScript] = {}
        for row in script_rows:
            try:
                actions = yaml.safe_load(row.action_yaml) or []
                if not isinstance(actions, list):
                    actions = [actions]
            except yaml.YAMLError:
                logger.exception("Failed to parse action_yaml for script '%s'; skipping", row.name)
                continue
            compiled_scripts[row.name] = _CompiledScript(name=row.name, actions=actions)
        self._scripts = compiled_scripts

        self._timer_field_by_entity = {row.entity_id: row.field_id for row in timer_rows}

    async def trigger(self, automation_id: uuid.UUID, context: dict = {}) -> dict:  # noqa: B006 — frozen signature
        """Manually fires one automation's action chain immediately,
        regardless of its trigger/condition (the "Test Run" button, plan
        §12, and `POST /api/v1/automations/{id}/trigger`). Always re-reads
        the automation fresh from Postgres so a Test Run right after an edit
        (before the next `reload()`) uses the latest saved YAML."""
        automation = await self._load_single_automation(automation_id)
        if automation is None:
            raise ValueError(f"Automation {automation_id} not found")

        extra = dict(context) if context else {}
        state_cache = await self._build_state_cache()
        eval_context = self._build_manual_context(automation_id, extra, state_cache)

        ok, error, failed_index, executed, action_results = await self._execute_action_chain(
            automation.actions, eval_context
        )
        status = "success" if ok else "failed"
        await self._log_run(automation.id, extra, status, failed_index, error, action_results)
        await self._touch_last_triggered(automation.id)
        return {
            "automation_id": automation.id,
            "status": status,
            "failed_action_index": failed_index,
            "error": error,
            "actions_executed": executed,
            "action_results": action_results,
        }

    async def run_actions(self, actions: list[dict[str, Any]], source_id: uuid.UUID, context: dict | None = None) -> dict:
        """Runs an ad-hoc action chain (e.g. a button-page button press)
        immediately, using the same context/globals as a manual automation
        trigger. Not logged to `automation_runs`; the caller reports the
        outcome."""
        extra = dict(context) if context else {}
        eval_context = self._build_manual_context(source_id, extra, await self._build_state_cache())
        ok, error, failed_index, executed, action_results = await self._execute_action_chain(actions, eval_context)
        return {
            "status": "success" if ok else "failed",
            "failed_action_index": failed_index,
            "error": error,
            "actions_executed": executed,
            "action_results": action_results,
        }

    # ── YAML parsing ─────────────────────────────────────────────────────

    def _parse_automation_yaml(
        self, row: Automation
    ) -> tuple[list[dict[str, Any]], list[Any], list[dict[str, Any]]] | None:
        try:
            triggers = yaml.safe_load(row.trigger_yaml) or []
            conditions = yaml.safe_load(row.condition_yaml) if row.condition_yaml else []
            actions = yaml.safe_load(row.action_yaml) or []
        except yaml.YAMLError:
            logger.exception("Failed to parse YAML for automation %s (%s); skipping", row.id, row.alias)
            return None
        if not isinstance(triggers, list):
            triggers = [triggers]
        if conditions and not isinstance(conditions, list):
            conditions = [conditions]
        if not isinstance(actions, list):
            actions = [actions]
        return triggers, conditions or [], actions

    async def _load_single_automation(self, automation_id: uuid.UUID) -> _CompiledAutomation | None:
        async with self._session_factory() as session:
            row = (
                await session.execute(select(Automation).where(Automation.id == automation_id))
            ).scalar_one_or_none()
        if row is None:
            return None
        parsed = self._parse_automation_yaml(row)
        if parsed is None:
            raise ValueError(f"Automation {automation_id} has invalid YAML")
        triggers, conditions, actions = parsed
        return _CompiledAutomation(id=row.id, alias=row.alias, triggers=triggers, conditions=conditions, actions=actions)

    async def _load_script_from_db(self, name: str) -> _CompiledScript | None:
        try:
            async with self._session_factory() as session:
                row = (await session.execute(select(Script).where(Script.name == name))).scalar_one_or_none()
        except Exception:
            logger.exception("Failed to look up script '%s' in Postgres", name)
            return None
        if row is None:
            return None
        try:
            actions = yaml.safe_load(row.action_yaml) or []
            if not isinstance(actions, list):
                actions = [actions]
        except yaml.YAMLError:
            logger.exception("Failed to parse action_yaml for script '%s'", name)
            return None
        compiled = _CompiledScript(name=name, actions=actions)
        self._scripts[name] = compiled  # opportunistically warm the cache
        return compiled

    # ── trigger matching (plan §5.8 / §3.6 / §7) ────────────────────────

    def _trigger_matches_any(self, triggers: list[dict[str, Any]], event: EventBusMessage) -> bool:
        return any(self._trigger_matches(t, event) for t in triggers)

    def _trigger_matches(self, spec: dict[str, Any], event: EventBusMessage) -> bool:
        platform = spec.get("platform")
        if platform == "state":
            if "entity_id" in spec and spec["entity_id"] != event.entity_id:
                return False
            if "event_type" in spec and spec["event_type"] != event.type:
                return False
            if "tag" in spec and spec["tag"] not in event.entity_tags:
                return False
            return self._match_name_ok(spec, event)
        if platform == "timer_milestone":
            if event.type != "timer_milestone":
                return False
            if "entity_id" in spec and spec["entity_id"] != event.entity_id:
                return False
            if "tag" in spec and spec["tag"] not in event.entity_tags:
                return False
            if "remaining" in spec:
                wanted = parse_hms(spec["remaining"])
                actual = event.payload.get("remaining")
                if actual is None or int(actual) != wanted:
                    return False
            return True
        return False

    def _match_name_ok(self, spec: dict[str, Any], event: EventBusMessage) -> bool:
        """Optional fnmatch-style match-name pattern filter (ported concept
        from legacy `models/actions.py`'s `ActionMapping.get_actions()`,
        e.g. `match_name: "Q*"`), applied against `payload.matchName` when
        the trigger spec declares it. A no-op for triggers that don't use
        this field."""
        pattern = spec.get("match_name")
        if not pattern:
            return True
        match_name = event.payload.get("matchName")
        if match_name is None:
            return False
        return fnmatch.fnmatch(str(match_name), str(pattern))

    # ── Jinja2 context building ──────────────────────────────────────────

    async def _build_state_cache(self) -> dict[str, dict[str, Any]]:
        """Prefetches `get_state()` for every currently-loaded integration
        instance (plan §9: "Must NEVER raise") so `states()`/`is_state()`
        inside Jinja templates are plain synchronous lookups rather than
        needing to `await` mid-render."""
        instances = loader.get_all_instances()
        if not instances:
            return {}

        async def _one(inst: Any) -> tuple[str, dict[str, Any]]:
            try:
                return inst.entity_id, (await inst.get_state() or {})
            except Exception:
                logger.exception("get_state() raised for '%s' (should never happen)", inst.entity_id)
                return inst.entity_id, {}

        pairs = await asyncio.gather(*(_one(i) for i in instances))
        return dict(pairs)

    def _entity_field(self, entity_id: str | None) -> int | None:
        if not entity_id:
            return None
        return self._timer_field_by_entity.get(entity_id)

    def _context_globals(self, state_cache: dict[str, dict[str, Any]]) -> dict[str, Any]:
        def states_fn(entity_id: str, attr: str | None = None) -> Any:
            state = state_cache.get(entity_id, {}) or {}
            return state if attr is None else state.get(attr)

        def is_state_fn(entity_id: str, value: Any) -> bool:
            state = state_cache.get(entity_id, {}) or {}
            current = state.get("state", state)
            return current == value

        def entities_with_tag_fn(tag: str) -> list[str]:
            return [inst.entity_id for inst in loader.get_instances_by_tag(tag)]

        return {
            "states": states_fn,
            "is_state": is_state_fn,
            "entities_with_tag": entities_with_tag_fn,
            "now": lambda: datetime.now(timezone.utc),
        }

    def _build_event_context(
        self, event: EventBusMessage, state_cache: dict[str, dict[str, Any]]
    ) -> dict[str, Any]:
        trigger_obj = _DotDict(
            {
                "entity_id": event.entity_id,
                "type": event.type,
                "payload": _wrap(event.payload),
                "entity": _DotDict({"tags": list(event.entity_tags), "field": self._entity_field(event.entity_id)}),
            }
        )
        context: dict[str, Any] = {"trigger": trigger_obj}
        context.update(self._context_globals(state_cache))
        return context

    def _build_manual_context(
        self, automation_id: uuid.UUID, extra: dict[str, Any], state_cache: dict[str, dict[str, Any]]
    ) -> dict[str, Any]:
        """Context for `trigger()` (manual "Test Run"): `context` becomes
        `trigger.payload`, with a synthetic `entity_id`/`type` so templates
        referencing `trigger.entity_id` don't blow up during a test run."""
        trigger_obj = _DotDict(
            {
                "entity_id": f"automation.{automation_id}",
                "type": "manual_trigger",
                "payload": _wrap(extra),
                "entity": _DotDict({"tags": [], "field": None}),
            }
        )
        context: dict[str, Any] = {"trigger": trigger_obj}
        context.update(self._context_globals(state_cache))
        return context

    # ── condition evaluation ─────────────────────────────────────────────

    def _conditions_pass(self, conditions: list[Any], context: dict[str, Any]) -> bool:
        for cond in conditions:
            try:
                if not eval_bool(cond, context):
                    return False
            except Exception:
                logger.exception("Condition %r raised while evaluating; treating as not-met", cond)
                return False
        return True

    # ── action execution (plan §10 / Appendix A.7 / B.6) ─────────────────

    async def _execute_action_chain(
        self, actions: list[dict[str, Any]], context: dict[str, Any]
    ) -> tuple[bool, str | None, int | None, int, list[dict[str, Any]]]:
        """Runs `actions` in order.

        Per Appendix A.7: a failing action is retried once (inside the
        per-action helpers below), then execution *continues* to the next
        action regardless of outcome — every action still runs, and every
        one's outcome is recorded in the returned `action_results` list (not
        just the first failure) so a chain with several failing actions
        doesn't hide all but one of them. The two exceptions: `delay` hands
        the remainder of the chain to a background `asyncio.create_task()`
        and returns immediately (plan §5.8 point 3 — never block the engine
        loop), and an inline `condition` action that evaluates false
        deliberately halts the chain (not a failure).

        Returns `(ok, error, failed_action_index, actions_executed, action_results)`,
        where `error`/`failed_action_index` describe only the *first*
        failure (kept for backwards compatibility) and `action_results` is
        `[{"index": int, "status": "success" | "failed", "error": str | None}, ...]`
        for every action actually attempted.
        """
        first_failure_idx: int | None = None
        first_failure_err: str | None = None
        executed = 0
        action_results: list[dict[str, Any]] = []
        idx = 0
        while idx < len(actions):
            action = actions[idx]
            try:
                if "delay" in action:
                    delay_s = parse_hms(action["delay"])
                    remaining = actions[idx + 1 :]
                    asyncio.create_task(self._run_delayed_continuation(delay_s, remaining, context))
                    executed += 1
                    action_results.append({"index": idx, "status": "success", "error": None})
                    break
                if "condition" in action:
                    executed += 1
                    if not eval_bool(action["condition"], context):
                        action_results.append({"index": idx, "status": "success", "error": None})
                        break
                    action_results.append({"index": idx, "status": "success", "error": None})
                    idx += 1
                    continue
                if "repeat" in action:
                    ok, err = await self._do_repeat(action["repeat"], context)
                elif "script" in action:
                    ok, err = await self._do_script_action(action, context)
                elif "service" in action:
                    ok, err = await self._do_service_action(action, context)
                elif "spotlight" in action:
                    ok, err = await self._do_spotlight_action(action, context)
                elif "http" in action:
                    ok, err = await self._do_http_action(action, context)
                else:
                    ok, err = False, f"Unknown action type at index {idx}: {action!r}"
                executed += 1
                action_results.append({"index": idx, "status": "success" if ok else "failed", "error": err})
                if not ok and first_failure_idx is None:
                    first_failure_idx = idx
                    first_failure_err = err
            except Exception as exc:  # never let one bad action kill the whole chain/engine
                logger.exception("Unhandled error executing action %d", idx)
                executed += 1
                action_results.append({"index": idx, "status": "failed", "error": str(exc)})
                if first_failure_idx is None:
                    first_failure_idx = idx
                    first_failure_err = str(exc)
            idx += 1

        return (first_failure_idx is None), first_failure_err, first_failure_idx, executed, action_results

    async def _run_delayed_continuation(
        self, delay_s: float, remaining_actions: list[dict[str, Any]], context: dict[str, Any]
    ) -> None:
        """Child task spawned by a `delay:` action (plan §5.8 point 3): the
        engine's event-processing loop is never blocked waiting for this."""
        try:
            await asyncio.sleep(delay_s)
            await self._execute_action_chain(remaining_actions, context)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Error running delayed action continuation")

    async def _do_repeat(self, spec: dict[str, Any], context: dict[str, Any]) -> tuple[bool, str | None]:
        sequence = spec.get("sequence", []) or []
        first_err: str | None = None

        if "count" in spec:
            count = int(render_value(spec["count"], context))
            for _ in range(max(0, count)):
                ok, err, _, _, _ = await self._execute_action_chain(sequence, context)
                if not ok and first_err is None:
                    first_err = err
            return first_err is None, first_err

        if "while" in spec:
            iterations = 0
            while iterations < _MAX_REPEAT_WHILE_ITERATIONS and eval_bool(spec["while"], context):
                ok, err, _, _, _ = await self._execute_action_chain(sequence, context)
                if not ok and first_err is None:
                    first_err = err
                iterations += 1
            return first_err is None, first_err

        return False, "repeat action requires 'count' or 'while'"

    async def _do_script_action(self, action: dict[str, Any], context: dict[str, Any]) -> tuple[bool, str | None]:
        name = render_value(action["script"], context)
        script = self._scripts.get(name)
        if script is None:
            # `trigger()` (manual Test Run) always loads the *automation*
            # fresh from Postgres rather than relying on `reload()` having
            # run first (see its docstring) — a script it calls deserves
            # the same freshness guarantee, so fall back to a direct DB
            # lookup instead of only trusting the `reload()`-populated
            # cache, which may be empty/stale.
            script = await self._load_script_from_db(str(name))
        if script is None:
            return False, f"Unknown script '{name}'"

        extra_data = render_value(action.get("data", {}), context)
        sub_context = dict(context)
        sub_context["vars"] = _wrap(extra_data)

        ok, err, _, _, _ = await self._execute_action_chain(script.actions, sub_context)
        return ok, err

    async def _do_spotlight_action(self, action: dict[str, Any], context: dict[str, Any]) -> tuple[bool, str | None]:
        """`spotlight: {team: "1234A", overlay: "overlay-main"}` — shows one
        team on the finals overlay (`team: null`/empty returns to the match
        view). Omitting `overlay` targets every overlay instance."""
        from backend.models.overlay import OverlayInstance

        spec = render_value(action["spotlight"], context)
        if not isinstance(spec, dict):
            return False, "spotlight action requires a mapping with 'team' (and optionally 'overlay')"
        team = spec.get("team")
        team = str(team).strip().upper() if team not in (None, "", "none", "None") else None
        if team is not None and not re.fullmatch(r"[A-Z0-9]{1,20}", team):
            return False, f"Invalid team number {team!r}"
        wanted = spec.get("overlay")
        async with self._session_factory() as session:
            stmt = select(OverlayInstance)
            if wanted:
                stmt = stmt.where(OverlayInstance.entity_id == str(wanted))
            rows = list((await session.execute(stmt)).scalars())
        if not rows:
            return False, f"No overlay found{f' for {wanted!r}' if wanted else ''}"
        for row in rows:
            event = EventBusMessage(
                entity_id=row.entity_id,
                entity_tags=list(row.tags or []),
                type="overlay_show_team",
                timestamp=time.time(),
                payload={"team": team},
            )
            await self._redis.publish("qecomp:events", event.model_dump_json())
        return True, None

    async def _do_http_action(self, action: dict[str, Any], context: dict[str, Any]) -> tuple[bool, str | None]:
        """`http: {url, method: POST, json: {...}, headers: {...}, timeout: 5}`
        — fires a webhook. Only http(s) to a non-link-local host."""
        import httpx

        spec = render_value(action["http"], context)
        if isinstance(spec, str):
            spec = {"url": spec}
        if not isinstance(spec, dict) or not spec.get("url"):
            return False, "http action requires a 'url'"
        url = str(spec["url"])
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return False, "http action only supports http(s) URLs"
        try:
            if ipaddress.ip_address(parsed.hostname).is_link_local:
                return False, "http action may not target link-local addresses"
        except ValueError:
            pass  # a hostname, not an IP literal
        method = str(spec.get("method", "POST")).upper()
        if method not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
            return False, f"Unsupported http method {method!r}"
        try:
            timeout = max(0.5, min(float(spec.get("timeout", 5)), 15.0))
        except (TypeError, ValueError):
            timeout = 5.0
        headers = spec.get("headers") if isinstance(spec.get("headers"), dict) else None
        body = spec.get("json")
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
                resp = await client.request(method, url, json=body if body is not None else None, headers=headers)
        except httpx.HTTPError as exc:
            return False, f"HTTP request failed: {exc}"
        if resp.status_code >= 400:
            return False, f"HTTP {resp.status_code} from {parsed.hostname}"
        return True, None

    async def _do_service_action(self, action: dict[str, Any], context: dict[str, Any]) -> tuple[bool, str | None]:
        service_full = render_value(action["service"], context)
        _, _, service_name = str(service_full).partition(".")
        service_name = service_name or str(service_full)
        data = render_value(action.get("data", {}), context)
        if not isinstance(data, dict):
            data = {}

        targets = self._resolve_targets(action, context)
        if not targets:
            return False, f"No targets resolved for service '{service_full}'"

        async def _call_one(inst: Any) -> str | None:
            try:
                await self._call_service_with_retry(inst, service_name, data)
                return None
            except Exception as exc:
                return f"{inst.entity_id}: {exc}"

        # Per Appendix B.6: tag-intersection targeting fires on ALL matching
        # entities simultaneously — `asyncio.gather`, not sequentially. A
        # single `target:` entity goes through the same path (a 1-item
        # gather), so there is exactly one code path for both cases.
        results = await asyncio.gather(*(_call_one(t) for t in targets))
        errors = [r for r in results if r]
        if errors:
            return False, "; ".join(errors)
        return True, None

    async def _call_service_with_retry(self, instance: Any, service: str, data: dict[str, Any]) -> dict[str, Any]:
        """Appendix A.7: "retry once (immediately)" on failure."""
        try:
            return await instance.call_service(service, data)
        except Exception:
            return await instance.call_service(service, data)

    def _resolve_targets(self, action: dict[str, Any], context: dict[str, Any]) -> list[Any]:
        if "target" in action:
            entity_id = render_value(action["target"], context)
            instance = loader.get_instance(str(entity_id))
            return [instance] if instance is not None else []

        if "target_tag" in action:
            tag = render_value(action["target_tag"], context)
            candidates = loader.get_instances_by_tag(str(tag))
            target_filter = action.get("target_filter")
            if not target_filter:
                return candidates

            filtered = []
            for candidate in candidates:
                candidate_context = dict(context)
                candidate_context["target"] = _DotDict(
                    {
                        "entity_id": candidate.entity_id,
                        "tags": list(getattr(candidate, "tags", [])),
                        "field": self._entity_field(candidate.entity_id),
                    }
                )
                try:
                    if eval_bool(target_filter, candidate_context):
                        filtered.append(candidate)
                except Exception:
                    logger.exception("target_filter %r raised for candidate '%s'", target_filter, candidate.entity_id)
            return filtered

        return []

    # ── event bus subscription ───────────────────────────────────────────

    async def _process_event(self, event: EventBusMessage) -> None:
        matching = [a for a in self._automations if self._trigger_matches_any(a.triggers, event)]
        if not matching:
            return

        state_cache = await self._build_state_cache()
        for automation in matching:
            context = self._build_event_context(event, state_cache)
            if not self._conditions_pass(automation.conditions, context):
                continue

            ok, error, failed_index, executed, action_results = await self._execute_action_chain(
                automation.actions, context
            )
            status = "success" if ok else "failed"
            try:
                await self._log_run(automation.id, event.model_dump(), status, failed_index, error, action_results)
                await self._touch_last_triggered(automation.id)
                if status == "failed":
                    # Appendix A.8: notify once the retry (Appendix A.7) is
                    # exhausted and the failure is on the record.
                    await send_ntfy_notification(
                        "Automation action failed",
                        f"Automation '{automation.alias}' failed: {error}",
                        priority="high",
                    )
            except Exception:
                logger.exception("Failed to record execution history for automation '%s'", automation.alias)

    async def _events_listener(self) -> None:
        pubsub = self._redis.pubsub()
        try:
            await pubsub.subscribe(EVENTS_CHANNEL)
            while not self._shutdown.is_set():
                try:
                    message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Error reading from %s pub/sub", EVENTS_CHANNEL)
                    await asyncio.sleep(1)
                    continue
                if message is None:
                    continue
                try:
                    event = EventBusMessage.model_validate_json(message["data"])
                except Exception:
                    logger.exception("Malformed EventBusMessage on %s; skipping", EVENTS_CHANNEL)
                    continue
                try:
                    await self._process_event(event)
                except Exception:
                    logger.exception("Unhandled error processing event %r", event)
        finally:
            with contextlib.suppress(Exception):
                await pubsub.unsubscribe(EVENTS_CHANNEL)
                await pubsub.aclose()

    async def _config_change_listener(self) -> None:
        """Subscribes to `qecomp:config_change` for hot-reload (plan §5.8
        point 6), mirroring `backend.loader`'s own hot-reload subscription
        pattern for consistency (per the task brief)."""
        pubsub = self._redis.pubsub()
        try:
            await pubsub.subscribe(CONFIG_CHANGE_CHANNEL)
            while not self._shutdown.is_set():
                try:
                    message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Error reading from %s pub/sub", CONFIG_CHANGE_CHANNEL)
                    await asyncio.sleep(1)
                    continue
                if message is None:
                    continue
                try:
                    event = EventBusMessage.model_validate_json(message["data"])
                except Exception:
                    continue
                if event.payload.get("resource_type") in _RELOAD_RESOURCE_TYPES:
                    try:
                        await self.reload()
                    except Exception:
                        logger.exception("reload() failed while handling config_change")
        finally:
            with contextlib.suppress(Exception):
                await pubsub.unsubscribe(CONFIG_CHANGE_CHANNEL)
                await pubsub.aclose()

    # ── execution history (Appendix A.7) ─────────────────────────────────

    async def _log_run(
        self,
        automation_id: uuid.UUID,
        trigger_event: dict[str, Any],
        status: str,
        failed_action_index: int | None,
        error: str | None,
        action_results: list[dict[str, Any]] | None = None,
    ) -> None:
        async with self._session_factory() as session:
            session.add(
                AutomationRun(
                    automation_id=automation_id,
                    trigger_event=trigger_event,
                    status=status,
                    failed_action_index=failed_action_index,
                    error=error,
                    action_results=action_results,
                )
            )
            await session.commit()

    async def _touch_last_triggered(self, automation_id: uuid.UUID) -> None:
        async with self._session_factory() as session:
            await session.execute(
                update(Automation).where(Automation.id == automation_id).values(last_triggered_at=func.now())
            )
            await session.commit()
