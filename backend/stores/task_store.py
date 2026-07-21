from __future__ import annotations

import json
import logging
import threading
import uuid
import copy
from datetime import datetime
from pathlib import Path
from typing import Optional

from json_store import write_json
from paths import ba_home
from task_session_types import VALID as VALID_SESSION_TYPES

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

MAX_PER_PROJECT = 100
MAX_NAME_LEN = 200
MAX_PROMPT_LEN = 100_000
MAX_RECENT_RUNS = 10

_VALID_ORCH_MODES = ("team", "native")
_VALID_WORKER_POLICIES = ("ask", "approve", "deny")
_VALID_TRIGGER_KINDS = ("manual", "schedule", "script", "turn_end", "api")
_VALID_ASSESSMENT_KINDS = ("none", "script", "llm_judge")
_VALID_SCHEDULE_MODES = ("once", "recurring")
_MISPLACED_SCHEDULE_KEYS = ("mode", "fire_at", "interval_seconds")
_VALID_TURN_END_OUTCOMES = ("complete", "stopped")
MIN_TRIGGER_INTERVAL_SECONDS = 30
MAX_TRIGGER_INTERVAL_SECONDS = 60 * 60 * 24 * 365
MAX_SCRIPT_COMMANDS = 20
MAX_SCRIPT_ARGS = 64
MAX_SCRIPT_ARG_LEN = 4096
MAX_GOAL_LEN = 10_000
MAX_CRITERIA_LEN = 10_000
_VALID_VERDICTS = ("pending", "pass", "fail", "error", "skipped")

_lock = threading.RLock()
_data_cache: tuple[tuple[int, int], dict] | None = None


def _path() -> Path:
    return ba_home() / "tasks.json"


def _empty() -> dict:
    return {"version": SCHEMA_VERSION, "tasks": []}


def _fingerprint() -> tuple[int, int]:
    try:
        st = _path().stat()
    except OSError:
        return (0, 0)
    return (st.st_mtime_ns, st.st_size)


def _read() -> dict:
    global _data_cache
    path = _path()
    if not path.exists():
        return _empty()
    fingerprint = _fingerprint()
    cached = _data_cache
    if cached is not None and cached[0] == fingerprint:
        return copy.deepcopy(cached[1])
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.error(
            "task_store: failed to read %s (%s) - returning empty store. "
            "Delete the file to start fresh.", path, e,
        )
        return _empty()
    if not isinstance(raw, dict) or raw.get("version") != SCHEMA_VERSION:
        logger.error(
            "task_store: unexpected shape/version at %s (expected %s, got "
            "%r) - returning empty store. Delete the file to start fresh.",
            path, SCHEMA_VERSION,
            raw.get("version") if isinstance(raw, dict) else type(raw).__name__,
        )
        return _empty()
    raw.setdefault("tasks", [])
    if not isinstance(raw["tasks"], list):
        logger.error("task_store: 'tasks' is not a list - returning empty store")
        return _empty()
    for t in raw["tasks"]:
        _normalize_task(t)
    _data_cache = (fingerprint, copy.deepcopy(raw))
    return raw


def _write(data: dict) -> None:
    global _data_cache
    write_json(_path(), data)
    _data_cache = (_fingerprint(), copy.deepcopy(data))


def _clean_str(value, *, field: str, max_len: int, required: bool) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    value = value.strip()
    if required and not value:
        raise ValueError(f"{field} is required")
    if len(value) > max_len:
        raise ValueError(f"{field} exceeds {max_len} chars")
    return value


def _coerce_permission(value) -> Optional[dict]:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("permission must be an object or null")
    for k, v in value.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise ValueError("permission must map string axes to string choices")
    return dict(value)


def _coerce_capability_contexts(value) -> list[dict]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("capability_contexts must be a list")
    out: list[dict] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("capability_contexts items must be objects")
        out.append(dict(item))
    return out


def _coerce_script(value, *, label: str) -> Optional[dict]:
    """A script is {command: [str, ...], cwd?: str}. Command runs as an argv
    list (never a shell string) so untrusted task input cannot inject."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    command = value.get("command")
    if not isinstance(command, list) or not command:
        raise ValueError(f"{label}.command must be a non-empty list")
    if len(command) > MAX_SCRIPT_ARGS:
        raise ValueError(f"{label}.command has too many args")
    cmd_out: list[str] = []
    for arg in command:
        if not isinstance(arg, str) or not arg:
            raise ValueError(f"{label}.command args must be non-empty strings")
        if len(arg) > MAX_SCRIPT_ARG_LEN:
            raise ValueError(f"{label}.command arg exceeds {MAX_SCRIPT_ARG_LEN} chars")
        cmd_out.append(arg)
    cwd = value.get("cwd")
    if cwd is not None:
        if not isinstance(cwd, str):
            raise ValueError(f"{label}.cwd must be a string")
        cwd = cwd.strip() or None
    return {"command": cmd_out, "cwd": cwd}


def _coerce_script_list(value, *, label: str, max_items: int = MAX_SCRIPT_COMMANDS) -> list[dict]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    if len(value) > max_items:
        raise ValueError(f"{label} exceeds {max_items} entries")
    out: list[dict] = []
    for i, item in enumerate(value):
        script = _coerce_script(item, label=f"{label}[{i}]")
        if script is not None:
            out.append(script)
    return out


def _coerce_scripts(value) -> dict:
    if value is None:
        return {"pre": [], "post": []}
    if not isinstance(value, dict):
        raise ValueError("scripts must be an object")
    unknown = set(value) - {"pre", "post"}
    if unknown:
        raise ValueError(f"scripts has unknown keys: {sorted(unknown)}")
    return {
        "pre": _coerce_script_list(value.get("pre"), label="scripts.pre"),
        "post": _coerce_script_list(value.get("post"), label="scripts.post"),
    }


def _validate_interval(seconds) -> int:
    if isinstance(seconds, bool) or not isinstance(seconds, int):
        raise ValueError("interval must be an integer (seconds)")
    if seconds < MIN_TRIGGER_INTERVAL_SECONDS or seconds > MAX_TRIGGER_INTERVAL_SECONDS:
        raise ValueError(
            f"interval must be between {MIN_TRIGGER_INTERVAL_SECONDS} "
            f"and {MAX_TRIGGER_INTERVAL_SECONDS} seconds",
        )
    return seconds


def _coerce_trigger(value) -> dict:
    """Trigger = {kind, config}. manual needs no config. schedule fires at a
    time/interval. script polls a detector command and fires on exit 0.
    turn_end reacts to lifecycle terminal events. api is reserved."""
    if value is None:
        return {"kind": "manual", "config": {}}
    if not isinstance(value, dict):
        raise ValueError("trigger must be an object")
    kind = value.get("kind") or "manual"
    if kind not in _VALID_TRIGGER_KINDS:
        raise ValueError(f"trigger.kind must be one of {_VALID_TRIGGER_KINDS}")
    config = value.get("config") or {}
    if not isinstance(config, dict):
        raise ValueError("trigger.config must be an object")
    if kind == "schedule" and not config:
        misplaced = [k for k in _MISPLACED_SCHEDULE_KEYS if k in value]
        if misplaced:
            raise ValueError(
                "schedule fields must be nested under trigger.config, not on "
                f"trigger directly (found {misplaced} at the top level). "
                'Expected shape: {"kind": "schedule", "config": {"mode": '
                '"once", "fire_at": "<ISO-8601>"}} or {"kind": "schedule", '
                '"config": {"mode": "recurring", "interval_seconds": '
                f'{MIN_TRIGGER_INTERVAL_SECONDS}-{MAX_TRIGGER_INTERVAL_SECONDS}}}}}'
            )
    cfg: dict = {}
    if kind == "manual":
        if config:
            raise ValueError("manual trigger takes no config")
    elif kind == "schedule":
        mode = config.get("mode") or "once"
        if mode not in _VALID_SCHEDULE_MODES:
            raise ValueError(f"schedule.config.mode must be one of {_VALID_SCHEDULE_MODES}")
        cfg["mode"] = mode
        if mode == "once":
            fire_at = config.get("fire_at")
            if not isinstance(fire_at, str) or not fire_at.strip():
                raise ValueError(
                    'schedule.config.mode="once" requires schedule.config.fire_at '
                    '(ISO-8601 string), e.g. {"kind": "schedule", "config": '
                    '{"mode": "once", "fire_at": "2026-07-14T15:00:00Z"}}'
                )
            cfg["fire_at"] = fire_at.strip()
        else:
            interval = config.get("interval_seconds")
            if interval is None:
                raise ValueError(
                    'schedule.config.mode="recurring" requires schedule.config.'
                    "interval_seconds (integer, "
                    f"{MIN_TRIGGER_INTERVAL_SECONDS}-{MAX_TRIGGER_INTERVAL_SECONDS}), "
                    'e.g. {"kind": "schedule", "config": {"mode": "recurring", '
                    '"interval_seconds": 300}}'
                )
            cfg["interval_seconds"] = _validate_interval(interval)
            if config.get("fire_at"):
                cfg["fire_at"] = str(config["fire_at"]).strip()
    elif kind == "script":
        detector = _coerce_script(config.get("detector"), label="trigger.script.detector")
        if detector is None:
            raise ValueError("script trigger requires a detector command")
        cfg["detector"] = detector
        cfg["poll_interval_seconds"] = _validate_interval(
            config.get("poll_interval_seconds", 300),
        )
    elif kind == "turn_end":
        outcomes = config.get("outcomes", ["complete"])
        if not isinstance(outcomes, list) or not outcomes:
            raise ValueError("turn_end.outcomes must be a non-empty list")
        if any(outcome not in _VALID_TURN_END_OUTCOMES for outcome in outcomes):
            raise ValueError(
                f"turn_end.outcomes must contain only {_VALID_TURN_END_OUTCOMES}"
            )
        cfg["outcomes"] = list(dict.fromkeys(outcomes))

        reasons = config.get("reasons")
        if reasons is not None:
            if not isinstance(reasons, list) or not reasons:
                raise ValueError("turn_end.reasons must be a non-empty list")
            cleaned_reasons = []
            for reason in reasons:
                cleaned = _clean_str(
                    reason, field="turn_end.reason", max_len=100, required=True,
                )
                if cleaned not in cleaned_reasons:
                    cleaned_reasons.append(cleaned)
            cfg["reasons"] = cleaned_reasons

        provider_kind = config.get("provider_kind")
        if provider_kind is not None:
            cfg["provider_kind"] = _clean_str(
                provider_kind,
                field="turn_end.provider_kind",
                max_len=100,
                required=True,
            ).lower()
    elif kind == "api":
        # Reserved: fires when an external caller POSTs the task's fire
        # endpoint. No active config yet; validation accepts a note only.
        pass
    return {"kind": kind, "config": cfg}


def _coerce_assessment(value) -> dict:
    """Assessment = {kind, config}. none skips grading. script runs a command
    (exit 0 = pass, or stdout JSON {pass, reason}). llm_judge grades the run
    output against the goal + criteria."""
    if value is None:
        return {"kind": "none", "config": {}}
    if not isinstance(value, dict):
        raise ValueError("assessment must be an object")
    kind = value.get("kind") or "none"
    if kind not in _VALID_ASSESSMENT_KINDS:
        raise ValueError(f"assessment.kind must be one of {_VALID_ASSESSMENT_KINDS}")
    config = value.get("config") or {}
    if not isinstance(config, dict):
        raise ValueError("assessment.config must be an object")
    cfg: dict = {}
    if kind == "none":
        if config:
            raise ValueError("none assessment takes no config")
    elif kind == "script":
        script = _coerce_script(config, label="assessment.script")
        if script is None:
            raise ValueError("script assessment requires a command")
        cfg = script
    elif kind == "llm_judge":
        criteria = config.get("criteria")
        if not isinstance(criteria, str) or not criteria.strip():
            raise ValueError("llm_judge requires criteria text")
        if len(criteria) > MAX_CRITERIA_LEN:
            raise ValueError(f"criteria exceeds {MAX_CRITERIA_LEN} chars")
        cfg["criteria"] = criteria.strip()
        if config.get("model"):
            cfg["model"] = str(config["model"]).strip()[:200]
        if config.get("provider_id"):
            cfg["provider_id"] = str(config["provider_id"]).strip()[:200]
    return {"kind": kind, "config": cfg}


def _normalize_task(t: dict) -> dict:
    """Default new additive fields on legacy records. The store has no
    migration framework, so additive optional fields are filled on read."""
    if not isinstance(t, dict):
        return t
    t.setdefault("goal", "")
    t.setdefault("trigger", {"kind": "manual", "config": {}})
    t.setdefault("scripts", {"pre": [], "post": []})
    t.setdefault("assessment", {"kind": "none", "config": {}})
    t.setdefault("session_type", "normal")
    t.setdefault("stopped", False)
    if "spawned_session_ids" not in t:
        # Seed the ledger for legacy records from the run history we still
        # have; recent_runs is capped, so this is best-effort for old tasks.
        seed = [
            r.get("session_id")
            for r in (t.get("recent_runs") or [])
            if isinstance(r, dict) and r.get("session_id")
        ]
        if t.get("singleton_session_id"):
            seed.append(t["singleton_session_id"])
        t["spawned_session_ids"] = list(dict.fromkeys(seed))
        # recent_runs is capped, so a legacy task with more runs than the
        # seed has launches the ledger can never recover — surface that.
        # Singleton tasks reuse one session, so run_count > ledger size is
        # their normal shape, not evidence of lost sessions.
        t["spawned_ledger_partial"] = (
            not t.get("singleton")
            and int(t.get("run_count") or 0) > len(t["spawned_session_ids"])
        )
    t.setdefault("spawned_ledger_partial", False)
    runs = t.get("recent_runs")
    if isinstance(runs, list):
        for r in runs:
            if isinstance(r, dict):
                r.setdefault("verdict", "pending")
                r.setdefault("verdict_reason", "")
                r.setdefault("verdict_kind", "none")
                r.setdefault("queue_item_id", None)
    return t


def _validate_core(
    *,
    name: str,
    prompt: str,
    cwd: str,
    orchestration_mode: str,
    worker_creation_policy: str,
    session_type: str,
) -> None:
    if not name:
        raise ValueError("name is required")
    if not prompt:
        raise ValueError("prompt is required")
    if not cwd:
        raise ValueError("cwd is required")
    if orchestration_mode not in _VALID_ORCH_MODES:
        raise ValueError(
            f"orchestration_mode must be one of {_VALID_ORCH_MODES}")
    if worker_creation_policy not in _VALID_WORKER_POLICIES:
        raise ValueError(
            f"worker_creation_policy must be one of {_VALID_WORKER_POLICIES}")
    if session_type not in VALID_SESSION_TYPES:
        raise ValueError(f"session_type must be one of {VALID_SESSION_TYPES}")


def create(
    *,
    cwd: str,
    name: str,
    prompt: str,
    node_id: str = "primary",
    description: str = "",
    orchestration_mode: str = "native",
    worker_creation_policy: str = "approve",
    session_type: str = "normal",
    model: Optional[str] = None,
    provider_id: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
    runner: Optional[str] = None,
    permission: Optional[dict] = None,
    capability_contexts=None,
    singleton: bool = False,
    goal: str = "",
    trigger: Optional[dict] = None,
    scripts: Optional[dict] = None,
    assessment: Optional[dict] = None,
) -> dict:
    name = _clean_str(name, field="name", max_len=MAX_NAME_LEN, required=True)
    prompt = _clean_str(prompt, field="prompt", max_len=MAX_PROMPT_LEN, required=True)
    cwd = _clean_str(cwd, field="cwd", max_len=4096, required=True)
    node_id = _clean_str(node_id or "primary", field="node_id", max_len=200, required=True)
    description = _clean_str(description, field="description", max_len=MAX_NAME_LEN, required=False)
    orchestration_mode = (orchestration_mode or "native").strip()
    if orchestration_mode == "manager":
        orchestration_mode = "team"
    worker_creation_policy = (worker_creation_policy or "approve").strip()
    session_type = (session_type or "normal").strip()
    _validate_core(
        name=name, prompt=prompt, cwd=cwd,
        orchestration_mode=orchestration_mode,
        worker_creation_policy=worker_creation_policy,
        session_type=session_type,
    )
    permission = _coerce_permission(permission)
    capability_contexts = _coerce_capability_contexts(capability_contexts)
    model = _clean_str(model, field="model", max_len=200, required=False) or None
    provider_id = _clean_str(provider_id, field="provider_id", max_len=200, required=False) or None
    reasoning_effort = _clean_str(reasoning_effort, field="reasoning_effort", max_len=64, required=False) or None
    runner = _clean_str(runner, field="runner", max_len=64, required=False) or None
    goal = _clean_str(goal, field="goal", max_len=MAX_GOAL_LEN, required=False)
    trigger = _coerce_trigger(trigger)
    if trigger.get("kind") == "turn_end" and not singleton:
        raise ValueError("turn_end triggers require singleton=true")
    scripts = _coerce_scripts(scripts)
    assessment = _coerce_assessment(assessment)

    now = datetime.now().isoformat()
    record = {
        "id": uuid.uuid4().hex[:12],
        "cwd": cwd,
        "node_id": node_id,
        "name": name,
        "description": description,
        "prompt": prompt,
        "orchestration_mode": orchestration_mode,
        "worker_creation_policy": worker_creation_policy,
        "session_type": session_type,
        "model": model,
        "provider_id": provider_id,
        "reasoning_effort": reasoning_effort,
        "runner": runner,
        "permission": permission,
        "capability_contexts": capability_contexts,
        "singleton": bool(singleton),
        "goal": goal,
        "trigger": trigger,
        "scripts": scripts,
        "assessment": assessment,
        "stopped": False,
        "spawned_ledger_partial": False,
        "created_at": now,
        "updated_at": now,
        "last_run_at": None,
        "run_count": 0,
        "recent_runs": [],
        "spawned_session_ids": [],
        "singleton_session_id": None,
    }
    with _lock:
        data = _read()
        per_project = [
            t for t in data["tasks"]
            if t.get("cwd") == cwd and (t.get("node_id") or "primary") == node_id
        ]
        if len(per_project) >= MAX_PER_PROJECT:
            raise ValueError(
                f"project already has {MAX_PER_PROJECT} tasks")
        data["tasks"].append(record)
        _write(data)
    return dict(record)


def list_for_project(cwd: str, node_id: str = "primary") -> list[dict]:
    node_id = node_id or "primary"
    with _lock:
        data = _read()
    return [
        dict(t) for t in data["tasks"]
        if t.get("cwd") == cwd and (t.get("node_id") or "primary") == node_id
    ]


def get(task_id: str) -> Optional[dict]:
    with _lock:
        data = _read()
    for t in data["tasks"]:
        if t.get("id") == task_id:
            return dict(t)
    return None


_EDITABLE_FIELDS = (
    "name", "description", "prompt", "orchestration_mode",
    "worker_creation_policy", "session_type", "model", "provider_id", "reasoning_effort", "runner",
    "permission", "capability_contexts", "singleton", "stopped",
    "goal", "trigger", "scripts", "assessment",
)


def update(task_id: str, patch: dict) -> Optional[dict]:
    if not isinstance(patch, dict):
        raise ValueError("patch must be an object")
    with _lock:
        data = _read()
        for t in data["tasks"]:
            if t.get("id") != task_id:
                continue
            merged = dict(t)
            for key in _EDITABLE_FIELDS:
                if key in patch:
                    merged[key] = patch[key]
            name = _clean_str(merged.get("name"), field="name", max_len=MAX_NAME_LEN, required=True)
            prompt = _clean_str(merged.get("prompt"), field="prompt", max_len=MAX_PROMPT_LEN, required=True)
            description = _clean_str(merged.get("description"), field="description", max_len=MAX_NAME_LEN, required=False)
            orch = (merged.get("orchestration_mode") or "native").strip()
            if orch == "manager":
                orch = "team"
            policy = (merged.get("worker_creation_policy") or "approve").strip()
            session_type = (merged.get("session_type") or "normal").strip()
            _validate_core(
                name=name, prompt=prompt, cwd=t["cwd"],
                orchestration_mode=orch, worker_creation_policy=policy,
                session_type=session_type,
            )
            permission = _coerce_permission(merged.get("permission"))
            capability_contexts = _coerce_capability_contexts(merged.get("capability_contexts"))
            model = _clean_str(merged.get("model"), field="model", max_len=200, required=False) or None
            provider_id = _clean_str(merged.get("provider_id"), field="provider_id", max_len=200, required=False) or None
            reasoning_effort = _clean_str(merged.get("reasoning_effort"), field="reasoning_effort", max_len=64, required=False) or None
            runner = _clean_str(merged.get("runner"), field="runner", max_len=64, required=False) or None
            goal = _clean_str(merged.get("goal"), field="goal", max_len=MAX_GOAL_LEN, required=False) if "goal" in merged else t.get("goal", "")
            trigger = _coerce_trigger(merged.get("trigger")) if "trigger" in merged else t.get("trigger")
            scripts = _coerce_scripts(merged.get("scripts")) if "scripts" in merged else t.get("scripts")
            assessment = _coerce_assessment(merged.get("assessment")) if "assessment" in merged else t.get("assessment")

            t["name"] = name
            t["description"] = description
            t["prompt"] = prompt
            t["orchestration_mode"] = orch
            t["worker_creation_policy"] = policy
            t["session_type"] = session_type
            t["model"] = model
            t["provider_id"] = provider_id
            t["reasoning_effort"] = reasoning_effort
            t["runner"] = runner
            t["permission"] = permission
            t["capability_contexts"] = capability_contexts
            singleton = bool(merged.get("singleton"))
            if trigger.get("kind") == "turn_end" and not singleton:
                raise ValueError("turn_end triggers require singleton=true")
            t["singleton"] = singleton
            # update may only RESUME (stopped=false). Stopping goes through
            # the stop action, which also tears down what the task spawned;
            # a bare flag-flip here would fake a stop the UI can't trust.
            new_stopped = bool(merged.get("stopped"))
            if new_stopped and not t.get("stopped"):
                raise ValueError("use the stop action to stop a routine")
            t["stopped"] = new_stopped
            t["goal"] = goal
            t["trigger"] = trigger
            t["scripts"] = scripts
            t["assessment"] = assessment
            t["updated_at"] = datetime.now().isoformat()
            _write(data)
            return dict(t)
    return None


def delete(task_id: str) -> Optional[dict]:
    with _lock:
        data = _read()
        for i, t in enumerate(data["tasks"]):
            if t.get("id") == task_id:
                removed = data["tasks"].pop(i)
                _write(data)
                return dict(removed)
    return None


def record_run(
    task_id: str,
    session_id: str,
    *,
    queue_item_id: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Optional[dict]:
    now = now or datetime.now()
    ts = now.isoformat()
    with _lock:
        data = _read()
        for t in data["tasks"]:
            if t.get("id") != task_id:
                continue
            t["last_run_at"] = ts
            t["run_count"] = int(t.get("run_count") or 0) + 1
            runs = [r for r in (t.get("recent_runs") or []) if isinstance(r, dict)]
            runs = [r for r in runs if r.get("session_id") != session_id]
            runs.insert(0, {
                "session_id": session_id,
                "started_at": ts,
                "queue_item_id": queue_item_id,
                "verdict": "pending",
                "verdict_reason": "",
                "verdict_kind": (t.get("assessment") or {}).get("kind", "none"),
            })
            t["recent_runs"] = runs[:MAX_RECENT_RUNS]
            # Uncapped launch ledger: recent_runs is a capped display
            # projection; the ledger is what "stop" tears down. Pruned by
            # drop_session_references when a session is deleted.
            ledger = [s for s in (t.get("spawned_session_ids") or []) if s]
            if session_id not in ledger:
                ledger.append(session_id)
            t["spawned_session_ids"] = ledger
            if t.get("singleton"):
                t["singleton_session_id"] = session_id
            _write(data)
            return dict(t)
    return None


def claim_event_run(
    task_id: str,
    session_id: str,
    *,
    receipt_id: str,
    expected_trigger_config: dict,
    expected_task_updated_at: str,
    now: Optional[datetime] = None,
) -> tuple[str, Optional[dict]]:
    now = now or datetime.now()
    ts = now.isoformat()
    with _lock:
        data = _read()
        for t in data["tasks"]:
            if t.get("id") != task_id:
                continue
            if t.get("stopped"):
                return "stopped", dict(t)
            if str(t.get("updated_at") or "") != expected_task_updated_at:
                return "stale", dict(t)
            if not t.get("singleton"):
                return "invalid", dict(t)
            trigger = t.get("trigger") or {}
            if (
                trigger.get("kind") != "turn_end"
                or (trigger.get("config") or {}) != expected_trigger_config
            ):
                return "stale", dict(t)
            runs = [r for r in (t.get("recent_runs") or []) if isinstance(r, dict)]
            for run in runs:
                if run.get("event_receipt_id") != receipt_id:
                    continue
                state = run.get("event_admission_state")
                return ("duplicate" if state == "queued" else "admitted"), dict(run)
            runs = [r for r in runs if r.get("session_id") != session_id]
            run = {
                "session_id": session_id,
                "started_at": ts,
                "queue_item_id": None,
                "verdict": "pending",
                "verdict_reason": "",
                "verdict_kind": (t.get("assessment") or {}).get("kind", "none"),
                "event_receipt_id": receipt_id,
                "event_admission_state": "reserved",
            }
            runs.insert(0, run)
            t["recent_runs"] = runs[:MAX_RECENT_RUNS]
            t["last_run_at"] = ts
            t["run_count"] = int(t.get("run_count") or 0) + 1
            ledger = [s for s in (t.get("spawned_session_ids") or []) if s]
            if session_id not in ledger:
                ledger.append(session_id)
            t["spawned_session_ids"] = ledger
            t["singleton_session_id"] = session_id
            _write(data)
            return "admitted", dict(run)
    return "unknown", None


def confirm_event_run(
    task_id: str,
    receipt_id: str,
    queue_item_id: Optional[str],
) -> Optional[dict]:
    with _lock:
        data = _read()
        for t in data["tasks"]:
            if t.get("id") != task_id:
                continue
            for run in (t.get("recent_runs") or []):
                if not isinstance(run, dict) or run.get("event_receipt_id") != receipt_id:
                    continue
                run["queue_item_id"] = queue_item_id
                run["event_admission_state"] = "queued"
                _write(data)
                return dict(run)
            return None
    return None


def find_pending_run_for_session(session_id: str) -> Optional[tuple[str, dict]]:
    """Return (task_id, run_entry) for the most recent still-pending run of
    `session_id`, or None. Used by the post-turn assessor to find which task
    (if any) owns a completed turn."""
    with _lock:
        data = _read()
    for t in data["tasks"]:
        for r in (t.get("recent_runs") or []):
            if not isinstance(r, dict):
                continue
            if r.get("session_id") == session_id and r.get("verdict") == "pending":
                return t.get("id"), dict(r)
    return None


def set_run_verdict(
    task_id: str,
    session_id: str,
    *,
    verdict: str,
    reason: str = "",
    verdict_kind: Optional[str] = None,
) -> Optional[dict]:
    if verdict not in _VALID_VERDICTS:
        raise ValueError(f"verdict must be one of {_VALID_VERDICTS}")
    with _lock:
        data = _read()
        for t in data["tasks"]:
            if t.get("id") != task_id:
                continue
            for r in (t.get("recent_runs") or []):
                if isinstance(r, dict) and r.get("session_id") == session_id:
                    r["verdict"] = verdict
                    r["verdict_reason"] = (reason or "")[:2000]
                    if verdict_kind is not None:
                        r["verdict_kind"] = verdict_kind
                    _write(data)
                    return dict(t)
    return None


def set_stopped(task_id: str, stopped: bool) -> Optional[dict]:
    with _lock:
        data = _read()
        for t in data["tasks"]:
            if t.get("id") != task_id:
                continue
            t["stopped"] = bool(stopped)
            t["updated_at"] = datetime.now().isoformat()
            _write(data)
            return dict(t)
    return None


def clear_singleton_session(task_id: str) -> Optional[dict]:
    with _lock:
        data = _read()
        for t in data["tasks"]:
            if t.get("id") != task_id:
                continue
            t["singleton_session_id"] = None
            _write(data)
            return dict(t)
    return None


def drop_session_references(session_id: str) -> list[str]:
    changed: list[str] = []
    with _lock:
        data = _read()
        for t in data["tasks"]:
            dirty = False
            runs = [r for r in (t.get("recent_runs") or []) if isinstance(r, dict)]
            filtered = [r for r in runs if r.get("session_id") != session_id]
            if len(filtered) != len(runs):
                t["recent_runs"] = filtered
                dirty = True
            ledger = [s for s in (t.get("spawned_session_ids") or []) if s]
            pruned = [s for s in ledger if s != session_id]
            if len(pruned) != len(ledger):
                t["spawned_session_ids"] = pruned
                dirty = True
            if t.get("singleton_session_id") == session_id:
                t["singleton_session_id"] = None
                dirty = True
            if dirty:
                changed.append(t["id"])
        if changed:
            _write(data)
    return changed
