"""Disk-backed status for in-flight `ask` team-message calls.

Mirrors `delegation_status_store`: lets a runner's `ask` tool re-attach to
the target turn it started after a backend restart, instead of re-queueing a
duplicate prompt. Keyed by a stable client-side `ask_id`; one JSON file per
in-flight ask under `<ba_home>/ask-status/`.

A record holds the correlation ids needed to re-attach (`lifecycle_msg_id`,
`target_session_id`, `sender_session_id`) and, once the target turn resolves,
the `result` payload the runner's `recover` path returns without re-POSTing.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from paths import ba_home
from runs_dir import atomic_write_json


def _safe_id(ask_id: str) -> str:
    return "".join(ch for ch in ask_id if ch.isalnum() or ch in ("-", "_"))


def status_path(ask_id: str) -> Path:
    return ba_home() / "ask-status" / f"{_safe_id(ask_id)}.json"


def write_status(ask_id: str, **fields: Any) -> None:
    path = status_path(ask_id)
    current = read_status(ask_id) or {}
    current.update(fields)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, current)


async def write_status_async(ask_id: str, **fields: Any) -> None:
    await asyncio.to_thread(write_status, ask_id, **fields)


def read_status(ask_id: str) -> dict[str, Any] | None:
    path = status_path(ask_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def delete_status(ask_id: str) -> None:
    try:
        status_path(ask_id).unlink()
    except FileNotFoundError:
        pass
