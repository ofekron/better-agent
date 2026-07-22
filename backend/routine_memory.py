from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import stat
from pathlib import Path
from typing import Any

import coordination
from json_store import write_json_durable
from paths import bc_home
import routine_lock

logger = logging.getLogger(__name__)

_TASK_ID_PATTERN = re.compile(r"^[0-9a-f]{12}$")
_MEMORY_SCHEMA_VERSION = 1
_MAX_CONTENT_BYTES = 256 * 1024
_MAX_FORMAT_CHARS = 128
_LOCK_TIMEOUT_SECONDS = 60
_LOCK_LEASE_SECONDS = 15 * 60


class RoutineMemoryError(Exception):
    pass


class RoutineMemoryAccessError(RoutineMemoryError):
    pass


class RoutineMemoryValidationError(RoutineMemoryError):
    pass


class RoutineMemoryBusyError(RoutineMemoryError):
    pass


def _validate_task_id(task_id: str) -> str:
    normalized = str(task_id or "").strip()
    if not _TASK_ID_PATTERN.fullmatch(normalized):
        raise RoutineMemoryAccessError("invalid routine memory identity")
    return normalized


def _memory_root() -> Path:
    return bc_home() / "routine-memory"


def _assert_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    mode = path.lstat().st_mode
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise RoutineMemoryAccessError(f"routine memory path is not a directory: {path}")


def _routine_dir(task_id: str) -> Path:
    root = _memory_root()
    _assert_directory(root)
    routine_dir = root / _validate_task_id(task_id)
    _assert_directory(routine_dir)
    return routine_dir


def _state_path(task_id: str) -> Path:
    return _routine_dir(task_id) / "state.json"


def _empty_state() -> dict[str, Any]:
    return {
        "schema_version": _MEMORY_SCHEMA_VERSION,
        "revision": 0,
        "content": "",
        "format": "text/plain",
    }


def _read_state(task_id: str) -> dict[str, Any]:
    path = _state_path(task_id)
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return _empty_state()
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise RoutineMemoryAccessError("routine memory state is not a regular file")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RoutineMemoryAccessError("routine memory state is unreadable") from exc
    if (
        not isinstance(raw, dict)
        or raw.get("schema_version") != _MEMORY_SCHEMA_VERSION
        or not isinstance(raw.get("revision"), int)
        or isinstance(raw.get("revision"), bool)
        or raw["revision"] < 0
        or not isinstance(raw.get("content"), str)
        or not isinstance(raw.get("format"), str)
        or len(raw["content"].encode("utf-8")) > _MAX_CONTENT_BYTES
        or not raw["format"].strip()
        or len(raw["format"]) > _MAX_FORMAT_CHARS
    ):
        raise RoutineMemoryAccessError("routine memory state has an unsupported shape")
    return raw


def _snapshot(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "revision": state["revision"],
        "content": state["content"],
        "format": state["format"],
    }


def _recover_tombstone(task_id: str) -> None:
    from stores import task_store

    path = _state_path(task_id)
    tombstone = path.with_name("state.deleting.json")
    if not tombstone.exists():
        return
    if path.exists():
        raise RoutineMemoryAccessError("routine memory deletion state is ambiguous")
    if task_store.get(task_id) is not None:
        os.replace(tombstone, path)
        return
    tombstone.unlink()
    try:
        path.parent.rmdir()
    except OSError:
        pass


def _resolve_routine_session(app_session_id: str, expected_task_id: str | None = None) -> str:
    from session_manager import manager as session_manager
    from stores import task_store

    session = session_manager.get_lite(str(app_session_id or "").strip())
    scope = (session or {}).get("storage_scope")
    if not isinstance(scope, dict) or scope.get("kind") != "routine" or scope.get("memory") is not True:
        raise RoutineMemoryAccessError("session is not a memory-enabled routine run")
    task_id = _validate_task_id(scope.get("routine_id"))
    if expected_task_id is not None and task_id != expected_task_id:
        raise RoutineMemoryAccessError("routine session identity changed")
    if task_store.get(task_id) is None:
        raise RoutineMemoryAccessError("routine no longer exists")
    return task_id


def _read_transaction(app_session_id: str, task_id: str) -> dict[str, Any]:
    fd = routine_lock.acquire("memory", task_id)
    try:
        _recover_tombstone(task_id)
        _resolve_routine_session(app_session_id, task_id)
        return _snapshot(_read_state(task_id))
    finally:
        routine_lock.release(fd)


def _validate_commit(expected_revision: int, content: str, memory_format: str) -> tuple[int, str, str]:
    if not isinstance(expected_revision, int) or isinstance(expected_revision, bool) or expected_revision < 0:
        raise RoutineMemoryValidationError("expected_revision must be a non-negative integer")
    if not isinstance(content, str):
        raise RoutineMemoryValidationError("content must be a string")
    if len(content.encode("utf-8")) > _MAX_CONTENT_BYTES:
        raise RoutineMemoryValidationError("routine memory content is too large")
    if not isinstance(memory_format, str):
        raise RoutineMemoryValidationError("format must be a string")
    memory_format = memory_format.strip()
    if not memory_format or len(memory_format) > _MAX_FORMAT_CHARS:
        raise RoutineMemoryValidationError("format is invalid")
    return expected_revision, content, memory_format


def _commit_transaction(
    app_session_id: str,
    task_id: str,
    expected_revision: int,
    content: str,
    memory_format: str,
) -> dict[str, Any]:
    fd = routine_lock.acquire("memory", task_id)
    try:
        _recover_tombstone(task_id)
        _resolve_routine_session(app_session_id, task_id)
        current = _read_state(task_id)
        if current["revision"] != expected_revision:
            return {
                "success": False,
                "error": "revision_conflict",
                "current": _snapshot(current),
            }
        next_revision = expected_revision + 1
        write_json_durable(_state_path(task_id), {
            "schema_version": _MEMORY_SCHEMA_VERSION,
            "revision": next_revision,
            "content": content,
            "format": memory_format,
        })
        return {"success": True, "revision": next_revision}
    finally:
        routine_lock.release(fd)


def _delete_transaction(task_id: str) -> dict | None:
    from stores import task_store

    fd = routine_lock.acquire("memory", task_id)
    try:
        _recover_tombstone(task_id)
        path = _state_path(task_id)
        tombstone = path.with_name("state.deleting.json")
        moved = path.exists()
        if moved:
            os.replace(path, tombstone)
        try:
            removed = task_store.delete(task_id)
        except BaseException:
            if moved:
                os.replace(tombstone, path)
            raise
        if removed is None:
            if moved:
                os.replace(tombstone, path)
            return None
        tombstone.unlink(missing_ok=True)
        try:
            path.parent.rmdir()
        except OSError:
            pass
        return removed
    finally:
        routine_lock.release(fd)


async def _acquire_distributed(task_id: str) -> dict[str, Any]:
    result = await coordination.lock_ops(
        key=f"routine_memory:{task_id}",
        timeout_seconds=_LOCK_TIMEOUT_SECONDS,
        lease_seconds=_LOCK_LEASE_SECONDS,
        owner={"source": "routine_memory"},
    )
    if result.get("success") is not True:
        raise RoutineMemoryBusyError(str(result.get("error") or "routine memory is busy"))
    return result


async def _release_distributed(task_id: str, acquired: dict[str, Any]) -> None:
    released = await coordination.lock_ops(
        key=f"routine_memory:{task_id}",
        release=True,
        holder_token=str(acquired.get("holder_token") or ""),
    )
    if released.get("success") is not True:
        logger.error("routine memory lock release failed for %s: %s", task_id, released.get("error"))


async def read(app_session_id: str) -> dict[str, Any]:
    task_id = await asyncio.to_thread(_resolve_routine_session, app_session_id)
    return await asyncio.to_thread(_read_transaction, app_session_id, task_id)


async def commit(
    app_session_id: str,
    *,
    expected_revision: int,
    content: str,
    memory_format: str,
) -> dict[str, Any]:
    expected_revision, content, memory_format = _validate_commit(
        expected_revision, content, memory_format,
    )
    task_id = await asyncio.to_thread(_resolve_routine_session, app_session_id)
    acquired = await _acquire_distributed(task_id)
    try:
        return await asyncio.to_thread(
            _commit_transaction,
            app_session_id,
            task_id,
            expected_revision,
            content,
            memory_format,
        )
    finally:
        await _release_distributed(task_id, acquired)


async def delete_task(task_id: str) -> dict | None:
    task_id = _validate_task_id(task_id)
    acquired = await _acquire_distributed(task_id)
    try:
        return await asyncio.to_thread(_delete_transaction, task_id)
    finally:
        await _release_distributed(task_id, acquired)
