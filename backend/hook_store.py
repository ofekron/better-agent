from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from json_store import write_json
from paths import ba_home

SCHEMA_VERSION = 1
DEFAULT_TIMEOUT_SECONDS = 10.0
MAX_TIMEOUT_SECONDS = 300.0
MAX_OUTPUT_BYTES = 64_000


class HookConfigError(ValueError):
    pass


def _store_path() -> Path:
    return ba_home() / "hooks" / "hooks.json"


def list_hooks() -> list[dict[str, Any]]:
    state = _load_state()
    return list(state["hooks"])


def replace_hooks(raw_hooks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    hooks = [_normalize_hook(raw, index=i) for i, raw in enumerate(raw_hooks)]
    _assert_unique_ids(hooks)
    state = {"schema_version": SCHEMA_VERSION, "hooks": hooks}
    write_json(_store_path(), state)
    return hooks


def upsert_hook(raw_hook: dict[str, Any]) -> dict[str, Any]:
    hook = _normalize_hook(raw_hook, index=0)
    state = _load_state()
    hooks = [h for h in state["hooks"] if h["id"] != hook["id"]]
    hooks.append(hook)
    _assert_unique_ids(hooks)
    write_json(_store_path(), {"schema_version": SCHEMA_VERSION, "hooks": hooks})
    return hook


def delete_hook(hook_id: str) -> bool:
    if not isinstance(hook_id, str) or not hook_id.strip():
        raise HookConfigError("hook id must be a non-empty string")
    state = _load_state()
    hooks = [h for h in state["hooks"] if h["id"] != hook_id]
    if len(hooks) == len(state["hooks"]):
        return False
    write_json(_store_path(), {"schema_version": SCHEMA_VERSION, "hooks": hooks})
    return True


def _load_state() -> dict[str, Any]:
    path = _store_path()
    if not path.exists():
        return {"schema_version": SCHEMA_VERSION, "hooks": []}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise HookConfigError(f"failed to parse hooks config: {exc}") from exc
    if not isinstance(state, dict):
        raise HookConfigError("hooks config must be an object")
    if state.get("schema_version") != SCHEMA_VERSION:
        raise HookConfigError(
            f"unsupported hooks schema_version={state.get('schema_version')!r}; "
            f"expected {SCHEMA_VERSION}"
        )
    hooks = state.get("hooks")
    if not isinstance(hooks, list):
        raise HookConfigError("hooks must be a list")
    normalized = [_normalize_hook(raw, index=i) for i, raw in enumerate(hooks)]
    _assert_unique_ids(normalized)
    return {"schema_version": SCHEMA_VERSION, "hooks": normalized}


def _normalize_hook(raw: dict[str, Any], *, index: int) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise HookConfigError(f"hook[{index}] must be an object")
    hook_id = _normalize_id(raw.get("id"))
    name = _normalize_name(raw.get("name"), hook_id)
    pattern = _normalize_pattern(raw.get("pattern"))
    command = _normalize_command(raw.get("command"))
    cwd = _normalize_cwd(raw.get("cwd"))
    timeout_seconds = _normalize_timeout(raw.get("timeout_seconds"))
    env = _normalize_env(raw.get("env"))
    return {
        "id": hook_id,
        "name": name,
        "enabled": bool(raw.get("enabled", True)),
        "pattern": pattern,
        "command": command,
        "cwd": cwd,
        "timeout_seconds": timeout_seconds,
        "env": env,
    }


def _normalize_id(value: Any) -> str:
    if value is None:
        return str(uuid.uuid4())
    if not isinstance(value, str) or not value.strip():
        raise HookConfigError("hook id must be a non-empty string")
    return value.strip()


def _normalize_name(value: Any, hook_id: str) -> str:
    if value is None:
        return hook_id
    if not isinstance(value, str) or not value.strip():
        raise HookConfigError("hook name must be a non-empty string")
    return value.strip()


def _normalize_pattern(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HookConfigError("hook pattern must be a non-empty string")
    return value.strip()


def _normalize_command(value: Any) -> list[str]:
    if not isinstance(value, list) or not value:
        raise HookConfigError("hook command must be a non-empty argv list")
    command: list[str] = []
    for part in value:
        if not isinstance(part, str) or not part:
            raise HookConfigError("hook command entries must be non-empty strings")
        if "\x00" in part:
            raise HookConfigError("hook command entries cannot contain NUL bytes")
        command.append(part)
    return command


def _normalize_cwd(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not value.strip():
        raise HookConfigError("hook cwd must be a non-empty string when supplied")
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        raise HookConfigError("hook cwd must be absolute")
    return str(candidate)


def _normalize_timeout(value: Any) -> float:
    if value is None:
        return DEFAULT_TIMEOUT_SECONDS
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise HookConfigError("hook timeout_seconds must be a number")
    timeout = float(value)
    if timeout <= 0 or timeout > MAX_TIMEOUT_SECONDS:
        raise HookConfigError(
            f"hook timeout_seconds must be > 0 and <= {MAX_TIMEOUT_SECONDS:g}"
        )
    return timeout


def _normalize_env(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise HookConfigError("hook env must be an object")
    env: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise HookConfigError("hook env keys must be non-empty strings")
        if "=" in key or "\x00" in key:
            raise HookConfigError("hook env keys cannot contain '=' or NUL bytes")
        if not isinstance(item, str):
            raise HookConfigError("hook env values must be strings")
        if "\x00" in item:
            raise HookConfigError("hook env values cannot contain NUL bytes")
        env[key] = item
    return env


def _assert_unique_ids(hooks: list[dict[str, Any]]) -> None:
    seen: set[str] = set()
    for hook in hooks:
        hook_id = hook["id"]
        if hook_id in seen:
            raise HookConfigError(f"duplicate hook id {hook_id!r}")
        seen.add(hook_id)
