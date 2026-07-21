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
import os
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from paths import bc_home
from portable_lock import lock_ex, unlock
from runs_dir import atomic_write_json


_ASK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,200}$")


def is_valid_ask_id(ask_id: str) -> bool:
    return _ASK_ID_RE.fullmatch(str(ask_id or "").strip()) is not None


def _safe_id(ask_id: str) -> str:
    clean = str(ask_id or "").strip()
    if not is_valid_ask_id(clean):
        raise ValueError("invalid ask_id")
    return clean


def status_path(ask_id: str) -> Path:
    return bc_home() / "ask-status" / f"{_safe_id(ask_id)}.json"


@contextmanager
def _locked(ask_id: str) -> Iterator[None]:
    root = bc_home() / "ask-status"
    if root.is_symlink():
        raise ValueError("ask status root must not be a symlink")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not root.is_dir():
        raise ValueError("ask status root must be a directory")
    lock_path = root / f".{_safe_id(ask_id)}.lock"
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(lock_path, flags, 0o600)
    try:
        lock_ex(fd)
        path = status_path(ask_id)
        if path.is_symlink():
            raise ValueError("ask status path must not be a symlink")
        if path.exists() and not path.is_file():
            raise ValueError("ask status path must be a regular file")
        yield
    finally:
        unlock(fd)
        os.close(fd)


def _read_status_unlocked(ask_id: str) -> dict[str, Any] | None:
    path = status_path(ask_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _new_delivery(sender_session_id: str) -> dict[str, Any]:
    from event_ingester import event_ingester
    from session_manager import manager as session_manager

    root_id = session_manager._root_id_for(sender_session_id) or sender_session_id
    return {
        "state": "waiting",
        "caller_session_id": sender_session_id,
        "caller_root_id": root_id,
        "journal_after_seq": event_ingester.cursor(root_id),
        "caller_terminal": False,
    }


def _project_result(
    ask_id: str,
    current: dict[str, Any],
    result: dict[str, Any],
) -> None:
    result["ask_id"] = ask_id
    delivery = dict(current.get("delivery") or {})
    if not delivery:
        return
    if delivery.get("state") not in {"received", "inboxed"}:
        delivery["state"] = "pending"
    fallback_sender = (
        result.get("target_session_id")
        or result.get("worker_session_id")
        or current.get("target_session_id")
    )
    if fallback_sender:
        delivery["fallback_sender_session_id"] = fallback_sender
    assistant_content = str(result.get("assistant_content") or "").strip()
    delivery["fallback_message"] = assistant_content or json.dumps(
        result,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    current["delivery"] = delivery


def write_status(ask_id: str, **fields: Any) -> None:
    sender_session_id = str(fields.get("sender_session_id") or "").strip()
    new_delivery = _new_delivery(sender_session_id) if sender_session_id else None
    with _locked(ask_id):
        path = status_path(ask_id)
        current = _read_status_unlocked(ask_id) or {}
        if new_delivery is not None and not isinstance(current.get("delivery"), dict):
            current["delivery"] = new_delivery
        current.update(fields)
        result = current.get("result")
        if isinstance(result, dict):
            _project_result(ask_id, current, result)
        atomic_write_json(path, current)


def update_status(
    ask_id: str,
    update: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    with _locked(ask_id):
        path = status_path(ask_id)
        current = _read_status_unlocked(ask_id) or {}
        updated = update(current)
        if not isinstance(updated, dict):
            raise TypeError("ask status update must return a dict")
        atomic_write_json(path, updated)
        return updated


async def write_status_async(ask_id: str, **fields: Any) -> None:
    await asyncio.to_thread(write_status, ask_id, **fields)
    status = await asyncio.to_thread(read_status, ask_id)
    delivery = dict((status or {}).get("delivery") or {})
    if delivery.get("caller_terminal") and delivery.get("fallback_message"):
        import ask_delivery

        await ask_delivery.deliver_if_needed(ask_id)


def read_status(ask_id: str) -> dict[str, Any] | None:
    with _locked(ask_id):
        return _read_status_unlocked(ask_id)


def list_statuses() -> list[tuple[str, dict[str, Any]]]:
    root = bc_home() / "ask-status"
    if root.is_symlink():
        raise ValueError("ask status root must not be a symlink")
    if not root.is_dir():
        return []
    statuses: list[tuple[str, dict[str, Any]]] = []
    for path in root.glob("*.json"):
        if path.is_symlink() or not path.is_file():
            continue
        ask_id = path.stem
        status = read_status(ask_id)
        if status is not None:
            statuses.append((ask_id, status))
    return statuses


def delete_status(ask_id: str) -> None:
    with _locked(ask_id):
        try:
            status_path(ask_id).unlink()
        except FileNotFoundError:
            pass
