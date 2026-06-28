from __future__ import annotations

import json
import logging
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from json_store import write_json
from paths import ba_home

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

MAX_PER_PROJECT = 100
MAX_NAME_LEN = 200
MAX_PROMPT_LEN = 100_000
MAX_RECENT_RUNS = 10

_VALID_ORCH_MODES = ("team", "native")
_VALID_WORKER_POLICIES = ("ask", "approve", "deny")

_lock = threading.RLock()


def _path() -> Path:
    return ba_home() / "tasks.json"


def _empty() -> dict:
    return {"version": SCHEMA_VERSION, "tasks": []}


def _read() -> dict:
    path = _path()
    if not path.exists():
        return _empty()
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
    return raw


def _write(data: dict) -> None:
    write_json(_path(), data)


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


def _validate_core(
    *,
    name: str,
    prompt: str,
    cwd: str,
    orchestration_mode: str,
    worker_creation_policy: str,
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


def create(
    *,
    cwd: str,
    name: str,
    prompt: str,
    node_id: str = "primary",
    description: str = "",
    orchestration_mode: str = "native",
    worker_creation_policy: str = "approve",
    model: Optional[str] = None,
    provider_id: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
    permission: Optional[dict] = None,
    capability_contexts=None,
    singleton: bool = False,
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
    _validate_core(
        name=name, prompt=prompt, cwd=cwd,
        orchestration_mode=orchestration_mode,
        worker_creation_policy=worker_creation_policy,
    )
    permission = _coerce_permission(permission)
    capability_contexts = _coerce_capability_contexts(capability_contexts)
    model = _clean_str(model, field="model", max_len=200, required=False) or None
    provider_id = _clean_str(provider_id, field="provider_id", max_len=200, required=False) or None
    reasoning_effort = _clean_str(reasoning_effort, field="reasoning_effort", max_len=64, required=False) or None

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
        "model": model,
        "provider_id": provider_id,
        "reasoning_effort": reasoning_effort,
        "permission": permission,
        "capability_contexts": capability_contexts,
        "singleton": bool(singleton),
        "created_at": now,
        "updated_at": now,
        "last_run_at": None,
        "run_count": 0,
        "recent_runs": [],
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
    "worker_creation_policy", "model", "provider_id", "reasoning_effort",
    "permission", "capability_contexts", "singleton",
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
            _validate_core(
                name=name, prompt=prompt, cwd=t["cwd"],
                orchestration_mode=orch, worker_creation_policy=policy,
            )
            permission = _coerce_permission(merged.get("permission"))
            capability_contexts = _coerce_capability_contexts(merged.get("capability_contexts"))
            model = _clean_str(merged.get("model"), field="model", max_len=200, required=False) or None
            provider_id = _clean_str(merged.get("provider_id"), field="provider_id", max_len=200, required=False) or None
            reasoning_effort = _clean_str(merged.get("reasoning_effort"), field="reasoning_effort", max_len=64, required=False) or None

            t["name"] = name
            t["description"] = description
            t["prompt"] = prompt
            t["orchestration_mode"] = orch
            t["worker_creation_policy"] = policy
            t["model"] = model
            t["provider_id"] = provider_id
            t["reasoning_effort"] = reasoning_effort
            t["permission"] = permission
            t["capability_contexts"] = capability_contexts
            t["singleton"] = bool(merged.get("singleton"))
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


def record_run(task_id: str, session_id: str, *, now: Optional[datetime] = None) -> Optional[dict]:
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
            runs.insert(0, {"session_id": session_id, "started_at": ts})
            t["recent_runs"] = runs[:MAX_RECENT_RUNS]
            if t.get("singleton"):
                t["singleton_session_id"] = session_id
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
            if t.get("singleton_session_id") == session_id:
                t["singleton_session_id"] = None
                dirty = True
            if dirty:
                changed.append(t["id"])
        if changed:
            _write(data)
    return changed
