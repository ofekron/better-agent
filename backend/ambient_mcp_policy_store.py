from __future__ import annotations

import copy
import json
import os
import re
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TypeVar

from paths import bc_home


_SCHEMA_VERSION = 1
_ID_SEGMENT_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")
_LOCK = threading.RLock()
_T = TypeVar("_T")


def _path() -> Path:
    return bc_home() / "ambient_mcp_policy.json"


def validate_capability_id(capability_id: str) -> str:
    if not isinstance(capability_id, str) or len(capability_id) > 255:
        raise ValueError("ambient MCP capability id is invalid")
    segments = capability_id.split(":")
    if len(segments) < 2 or not all(_ID_SEGMENT_RE.fullmatch(item) for item in segments):
        raise ValueError("ambient MCP capability id is invalid")
    return capability_id


def _default() -> dict[str, Any]:
    return {
        "version": _SCHEMA_VERSION,
        "share_all_eligible": True,
        "excluded_ids": [],
        "generation": 0,
        "updated_at": None,
    }


def _validate(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict) or data.get("version") != _SCHEMA_VERSION:
        raise ValueError("unsupported ambient MCP policy schema")
    if not isinstance(data.get("share_all_eligible"), bool):
        raise ValueError("ambient MCP share_all_eligible must be a boolean")
    excluded = data.get("excluded_ids")
    if not isinstance(excluded, list):
        raise ValueError("ambient MCP excluded_ids must be valid capability ids")
    try:
        excluded = [validate_capability_id(item) for item in excluded]
    except ValueError as exc:
        raise ValueError("ambient MCP excluded_ids must be valid capability ids") from exc
    if len(excluded) != len(set(excluded)):
        raise ValueError("ambient MCP excluded_ids must be unique")
    generation = data.get("generation")
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
        raise ValueError("ambient MCP policy generation must be a non-negative integer")
    updated_at = data.get("updated_at")
    if updated_at is not None and not isinstance(updated_at, str):
        raise ValueError("ambient MCP policy updated_at must be a string or null")
    return {
        "version": _SCHEMA_VERSION,
        "share_all_eligible": data["share_all_eligible"],
        "excluded_ids": sorted(excluded),
        "generation": generation,
        "updated_at": updated_at,
    }


def _load() -> dict[str, Any]:
    path = _path()
    if not path.exists():
        return _default()
    return _validate(json.loads(path.read_text(encoding="utf-8")))


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


def get() -> dict[str, Any]:
    with _LOCK:
        return copy.deepcopy(_load())


def public(policy: dict[str, Any] | None = None) -> dict[str, Any]:
    value = policy if policy is not None else get()
    return {
        key: copy.deepcopy(value[key])
        for key in ("share_all_eligible", "excluded_ids", "generation", "updated_at")
    }


def is_exposed(capability_id: str, *, available: bool = True) -> bool:
    if not available:
        return False
    policy = get()
    return policy["share_all_eligible"] and capability_id not in set(policy["excluded_ids"])


def mutate_and_reconcile(
    mutation: Callable[[dict[str, Any]], _T],
    reconcile: Callable[[], Any],
) -> _T:
    with _LOCK:
        before = _load()
        after = copy.deepcopy(before)
        result = mutation(after)
        after["generation"] = before["generation"] + 1
        after["updated_at"] = datetime.now(timezone.utc).isoformat()
        after = _validate(after)
        _save(after)
        try:
            reconcile()
        except Exception:
            if _load() != after:
                raise RuntimeError(
                    "ambient MCP reconciliation failed and policy changed concurrently"
                )
            _save(before)
            raise
        return result
