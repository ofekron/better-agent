"""Durable registry for async get-requirements lookup jobs.

The backend restarts routinely (auto-restart-on-idle picks up dev code), so
in-flight lookups must survive process death. Each job is persisted as a JSON
record under ``bc_home()/requirement_analysis/async_jobs/<id>.json`` the moment
it is fired; completion overwrites the record with the result. A poll that
misses the in-memory task falls back to disk: finished records are served from
the persisted result, and a record still marked running with no live task (a
restart orphan) is resumed by re-running its persisted payload under the same
id — pollers never see "unknown id" for a job this process or a predecessor
accepted.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from json_store import write_json
from paths import bc_home

logger = logging.getLogger("uvicorn")

Runner = Callable[..., Awaitable[dict[str, Any]]]

RESULT_TTL_SECONDS = 1800.0
DISK_RETENTION_SECONDS = 24 * 3600.0
_DISK_SWEEP_INTERVAL_SECONDS = 300.0

_JOBS: dict[str, asyncio.Task] = {}
_COMPLETED_AT: dict[str, float] = {}
_LAST_DISK_SWEEP = 0.0


def _safe_id(request_id: str) -> str:
    return "".join(ch for ch in request_id if ch.isalnum() or ch in ("-", "_"))


def _jobs_dir() -> Path:
    return bc_home() / "requirement_analysis" / "async_jobs"


def job_path(request_id: str) -> Path:
    safe = _safe_id(request_id)
    if not safe:
        raise ValueError("request id has no filesystem-safe characters")
    return _jobs_dir() / f"{safe}.json"


def _read_record(request_id: str) -> dict[str, Any] | None:
    try:
        data = json.loads(job_path(request_id).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_record(request_id: str, record: dict[str, Any]) -> None:
    write_json(job_path(request_id), record)


def _persist_outcome(request_id: str, task: asyncio.Task) -> None:
    record = _read_record(request_id) or {}
    record["completed_at"] = time.time()
    if task.cancelled():
        record.update(status="failed", error="cancelled")
    elif task.exception() is not None:
        record.update(status="failed", error=str(task.exception()))
    else:
        record.update(status="complete", result=task.result())
    try:
        _write_record(request_id, record)
    except (OSError, TypeError, ValueError):
        logger.warning("requirements_async_persist_failed request_id=%s", request_id)


def _register(request_id: str, payload: dict[str, Any], runner: Runner) -> asyncio.Task:
    task = asyncio.get_running_loop().create_task(runner(payload, request_id=request_id))

    def _on_done(done: asyncio.Task) -> None:
        _COMPLETED_AT[request_id] = time.monotonic()
        _persist_outcome(request_id, done)

    task.add_done_callback(_on_done)
    _JOBS[request_id] = task
    return task


def fire(request_id: str, payload: dict[str, Any], runner: Runner) -> asyncio.Task:
    _write_record(
        request_id,
        {"id": request_id, "payload": payload, "status": "running", "created_at": time.time()},
    )
    return _register(request_id, payload, runner)


def get_or_resume(request_id: str, runner: Runner) -> asyncio.Task | dict[str, Any] | None:
    """Return the live task, a finished-record response, or None for unknown ids.

    Fully synchronous: the miss -> disk read -> re-register sequence must not
    yield to the event loop, or two concurrent pollers could double-resume.
    """
    task = _JOBS.get(request_id)
    if task is not None:
        return task
    record = _read_record(request_id)
    if record is None:
        return None
    status = record.get("status")
    if status == "complete":
        return {
            "success": True,
            "id": request_id,
            "status": "complete",
            "ready": True,
            "result": record.get("result"),
        }
    if status == "failed":
        return {
            "success": False,
            "id": request_id,
            "status": "failed",
            "ready": True,
            "error": str(record.get("error") or "job failed"),
        }
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None
    logger.info("requirements_async_resume request_id=%s", request_id)
    # Refresh the record (and its mtime) so the retention sweep cannot delete
    # a job that just came back to life.
    record["resumed_at"] = time.time()
    try:
        _write_record(request_id, record)
    except (OSError, TypeError, ValueError):
        logger.warning("requirements_async_resume_persist_failed request_id=%s", request_id)
    return _register(request_id, payload, runner)


def has_active_jobs() -> bool:
    return any(not task.done() for task in _JOBS.values())


def cleanup() -> None:
    cutoff = time.monotonic() - RESULT_TTL_SECONDS
    stale = [
        request_id
        for request_id, task in _JOBS.items()
        if task.done() and _COMPLETED_AT.get(request_id, 0.0) < cutoff
    ]
    for request_id in stale:
        _JOBS.pop(request_id, None)
        _COMPLETED_AT.pop(request_id, None)
    _sweep_disk()


def _sweep_disk(*, force: bool = False) -> None:
    global _LAST_DISK_SWEEP
    now = time.monotonic()
    if not force and now - _LAST_DISK_SWEEP < _DISK_SWEEP_INTERVAL_SECONDS:
        return
    _LAST_DISK_SWEEP = now
    wall_cutoff = time.time() - DISK_RETENTION_SECONDS
    try:
        entries = list(_jobs_dir().iterdir())
    except OSError:
        return
    for path in entries:
        try:
            if path.stat().st_mtime < wall_cutoff:
                path.unlink()
        except OSError:
            continue
