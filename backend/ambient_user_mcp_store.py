from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable, TypeVar

from paths import bc_home


_SCHEMA_VERSION = 1
_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")
_LOCK = threading.RLock()
_T = TypeVar("_T")


def _path() -> Path:
    return bc_home() / "ambient_user_mcps.json"


def _load() -> dict[str, Any]:
    path = _path()
    if not path.exists():
        return {"version": _SCHEMA_VERSION, "records": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("version") != _SCHEMA_VERSION:
        raise ValueError("unsupported ambient user MCP store schema")
    if not isinstance(data.get("records"), dict):
        raise ValueError("ambient user MCP records must be an object")
    return data


def _save(data: dict[str, Any]) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(raw_tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def validate_record(record: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise ValueError("ambient user MCP record must be an object")
    record_id = str(record.get("id") or "").strip()
    name = str(record.get("name") or "").strip()
    if not _ID_RE.fullmatch(record_id):
        raise ValueError("ambient user MCP id is invalid")
    if not name or len(name) > 128:
        raise ValueError("ambient user MCP name is invalid")
    launcher = record.get("launcher")
    if not isinstance(launcher, dict):
        raise ValueError("ambient user MCP launcher must be an object")
    command = launcher.get("command")
    args = launcher.get("args", [])
    env = launcher.get("env", {})
    if not isinstance(command, str) or not command.strip():
        raise ValueError("ambient user MCP launcher command is required")
    if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
        raise ValueError("ambient user MCP launcher args must be a string list")
    if not isinstance(env, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in env.items()
    ):
        raise ValueError("ambient user MCP launcher env must be a string map")
    policy = record.get("policy", {})
    if not isinstance(policy, dict):
        raise ValueError("ambient user MCP policy must be an object")
    return {
        "id": record_id,
        "name": name,
        "launcher": {"command": command.strip(), "args": list(args), "env": dict(env)},
        "policy": dict(policy),
        "enabled": bool(record.get("enabled", True)),
    }


def list_records() -> list[dict[str, Any]]:
    with _LOCK:
        records = _load()["records"]
        return [validate_record(records[key]) for key in sorted(records)]


def put(record: dict[str, Any]) -> dict[str, Any]:
    clean = validate_record(record)
    with _LOCK:
        data = _load()
        data["records"][clean["id"]] = clean
        _save(data)
    return clean


def remove(record_id: str) -> bool:
    with _LOCK:
        data = _load()
        removed = data["records"].pop(record_id, None) is not None
        if removed:
            _save(data)
        return removed


def mutate_and_reconcile(
    mutation: Callable[[dict[str, dict[str, Any]]], _T],
    reconcile: Callable[[], Any],
) -> _T:
    with _LOCK:
        before = _load()
        after = json.loads(json.dumps(before))
        result = mutation(after["records"])
        _save(after)
        try:
            reconcile()
        except Exception:
            current = _load()
            if current != after:
                raise RuntimeError(
                    "ambient MCP reconciliation failed and store changed concurrently"
                )
            _save(before)
            raise
        return result
