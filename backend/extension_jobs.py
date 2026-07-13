"""Core-owned durable async job registry for extension work."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from json_store import write_json
from paths import bc_home

logger = logging.getLogger("uvicorn")

Runner = Callable[..., Awaitable[dict[str, Any]]]
JobKey = tuple[str, str, str]

RESULT_TTL_SECONDS = 1800.0
DISK_RETENTION_SECONDS = 24 * 3600.0
_DISK_SWEEP_INTERVAL_SECONDS = 300.0

_JOBS: dict[JobKey, asyncio.Task] = {}
_COMPLETED_AT: dict[JobKey, float] = {}
_LAST_DISK_SWEEP: dict[tuple[str, str], float] = {}
_RECORD_LOCK = threading.Lock()
_RESERVED_RECORD_KEYS = {
    "id",
    "owner",
    "operation",
    "payload",
    "status",
    "result",
    "error",
    "created_at",
    "updated_at",
    "completed_at",
    "resumed_at",
}
_RUNNING_RESPONSE_KEYS = (
    "created_at",
    "updated_at",
    "resumed_at",
    "phase",
    "message",
    "delegation_id",
)


def _transition_progress(record: dict[str, Any], phase: str, message: str, now: float) -> None:
    progress = record.get("progress")
    if not isinstance(progress, dict):
        progress = {"started_at": float(record.get("created_at") or now), "phases": []}
        record["progress"] = progress
    phases = progress.get("phases")
    if not isinstance(phases, list):
        phases = []
        progress["phases"] = phases
    current = phases[-1] if phases and isinstance(phases[-1], dict) else None
    if current and current.get("phase") == phase and "completed_at" not in current:
        current["message"] = message
        phase_started_at = float(current.get("started_at") or now)
    else:
        if current and "completed_at" not in current:
            started_at = float(current.get("started_at") or now)
            current.update(completed_at=now, duration_ms=max(0.0, (now - started_at) * 1000.0))
        phases.append({"phase": phase, "message": message, "started_at": now})
        phase_started_at = now
    progress.update(current_phase=phase, message=message, phase_started_at=phase_started_at)
    record.update(phase=phase, message=message)


def _finish_progress(record: dict[str, Any], now: float) -> None:
    progress = record.get("progress")
    if not isinstance(progress, dict):
        return
    phases = progress.get("phases")
    if isinstance(phases, list) and phases and isinstance(phases[-1], dict):
        current = phases[-1]
        if "completed_at" not in current:
            started_at = float(current.get("started_at") or now)
            current.update(completed_at=now, duration_ms=max(0.0, (now - started_at) * 1000.0))
    started_at = float(progress.get("started_at") or record.get("created_at") or now)
    progress.update(completed_at=now, total_elapsed_ms=max(0.0, (now - started_at) * 1000.0))


def _response_progress(record: dict[str, Any]) -> dict[str, Any] | None:
    progress = record.get("progress")
    if not isinstance(progress, dict):
        return None
    response = dict(progress)
    phases = progress.get("phases")
    response["phases"] = [dict(item) for item in phases if isinstance(item, dict)] if isinstance(phases, list) else []
    now = time.time()
    started_at = float(progress.get("started_at") or record.get("created_at") or now)
    response["total_elapsed_ms"] = (
        float(progress["total_elapsed_ms"])
        if "total_elapsed_ms" in progress
        else max(0.0, (now - started_at) * 1000.0)
    )
    if record.get("status") == "running" and response["phases"]:
        current = response["phases"][-1]
        if "completed_at" not in current:
            phase_started_at = float(current.get("started_at") or now)
            current["elapsed_ms"] = max(0.0, (now - phase_started_at) * 1000.0)
    return response


def _safe_id(value: str) -> str:
    raw = str(value or "")
    safe = "".join(ch for ch in raw if ch.isalnum() or ch in ("-", "_"))
    if safe != raw:
        raise ValueError("extension job ids may contain only alphanumerics, '-' and '_'")
    return safe


def _key(owner: str, operation: str, job_id: str) -> JobKey:
    safe_owner = _safe_id(owner)
    safe_operation = _safe_id(operation)
    safe_job_id = _safe_id(job_id)
    if not safe_owner or not safe_operation or not safe_job_id:
        raise ValueError("extension job owner, operation, and id must be filesystem-safe")
    return safe_owner, safe_operation, safe_job_id


def _jobs_dir(owner: str, operation: str) -> Path:
    safe_owner, safe_operation, _ = _key(owner, operation, "probe")
    return bc_home() / "extension_jobs" / safe_owner / safe_operation


def job_path(owner: str, operation: str, job_id: str) -> Path:
    safe_owner, safe_operation, safe_job_id = _key(owner, operation, job_id)
    return bc_home() / "extension_jobs" / safe_owner / safe_operation / f"{safe_job_id}.json"


def delegation_id(owner: str, operation: str, job_id: str, target: str) -> str:
    safe_owner, safe_operation, safe_job_id = _key(owner, operation, job_id)
    safe_target = _safe_id(target)
    if not safe_target:
        raise ValueError("extension job delegation target must be filesystem-safe")
    return f"{safe_owner}_{safe_operation}_{safe_target}_{safe_job_id[:64]}"


def read_record(owner: str, operation: str, job_id: str) -> dict[str, Any] | None:
    try:
        data = json.loads(job_path(owner, operation, job_id).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_record(owner: str, operation: str, job_id: str, record: dict[str, Any]) -> None:
    write_json(job_path(owner, operation, job_id), record)


def persist_complete(owner: str, operation: str, job_id: str, result: dict[str, Any]) -> dict[str, Any]:
    with _RECORD_LOCK:
        record = read_record(owner, operation, job_id) or {
            "id": job_id,
            "owner": owner,
            "operation": operation,
        }
        if record.get("status") in ("complete", "failed"):
            return response_from_record(record)
        now = time.time()
        _finish_progress(record, now)
        record.update(status="complete", result=result, completed_at=now)
        _write_record(owner, operation, job_id, record)
    return response_from_record(record)


def persist_running(owner: str, operation: str, job_id: str, **fields: Any) -> dict[str, Any]:
    reserved = _RESERVED_RECORD_KEYS.intersection(fields)
    if reserved:
        raise ValueError(f"extension job running update uses reserved keys: {sorted(reserved)}")
    with _RECORD_LOCK:
        record = read_record(owner, operation, job_id) or {
            "id": job_id,
            "owner": owner,
            "operation": operation,
            "status": "running",
            "created_at": time.time(),
        }
        if record.get("status") in ("complete", "failed"):
            return response_from_record(record)
        record.update(fields)
        record["status"] = "running"
        record["updated_at"] = time.time()
        _write_record(owner, operation, job_id, record)
    return response_from_record(record)


def persist_phase(
    owner: str,
    operation: str,
    job_id: str,
    phase: str,
    message: str,
    **fields: Any,
) -> dict[str, Any]:
    reserved = _RESERVED_RECORD_KEYS.intersection(fields)
    if reserved:
        raise ValueError(f"extension job phase update uses reserved keys: {sorted(reserved)}")
    with _RECORD_LOCK:
        record = read_record(owner, operation, job_id) or {
            "id": job_id,
            "owner": owner,
            "operation": operation,
            "status": "running",
            "created_at": time.time(),
        }
        if record.get("status") in ("complete", "failed"):
            return response_from_record(record)
        now = time.time()
        _transition_progress(record, phase, message, now)
        record.update(fields)
        record["status"] = "running"
        record["updated_at"] = now
        _write_record(owner, operation, job_id, record)
    return response_from_record(record)


def response_from_record(record: dict[str, Any]) -> dict[str, Any]:
    job_id = str(record.get("id") or "")
    status = str(record.get("status") or "")
    if status == "complete":
        response = {
            "success": True,
            "id": job_id,
            "status": "complete",
            "ready": True,
            "result": record.get("result"),
        }
        progress = _response_progress(record)
        if progress is not None:
            response["progress"] = progress
        return response
    if status == "failed":
        response = {
            "success": False,
            "id": job_id,
            "status": "failed",
            "ready": True,
            "error": str(record.get("error") or "job failed"),
        }
        progress = _response_progress(record)
        if progress is not None:
            response["progress"] = progress
        return response
    response = {"success": True, "id": job_id, "status": "running", "ready": False}
    for key in _RUNNING_RESPONSE_KEYS:
        if key in record:
            response[key] = record[key]
    progress = _response_progress(record)
    if progress is not None:
        response["progress"] = progress
    return response


def _persist_outcome(owner: str, operation: str, job_id: str, task: asyncio.Task) -> None:
    try:
        with _RECORD_LOCK:
            record = read_record(owner, operation, job_id) or {}
            if record.get("status") in ("complete", "failed"):
                return
            now = time.time()
            record["completed_at"] = now
            _finish_progress(record, now)
            if task.cancelled():
                record.update(status="failed", error="cancelled")
            elif task.exception() is not None:
                record.update(status="failed", error=str(task.exception()))
            else:
                record.update(status="complete", result=task.result())
            _write_record(owner, operation, job_id, record)
    except (OSError, TypeError, ValueError):
        logger.warning("extension_job_persist_failed owner=%s operation=%s id=%s", owner, operation, job_id)


def _register(owner: str, operation: str, job_id: str, payload: dict[str, Any], runner: Runner) -> asyncio.Task:
    key = _key(owner, operation, job_id)
    task = asyncio.get_running_loop().create_task(runner(payload, request_id=job_id))

    def _on_done(done: asyncio.Task) -> None:
        _COMPLETED_AT[key] = time.monotonic()
        _persist_outcome(owner, operation, job_id, done)

    task.add_done_callback(_on_done)
    _JOBS[key] = task
    return task


def fire(
    owner: str,
    operation: str,
    job_id: str,
    payload: dict[str, Any],
    runner: Runner,
    *,
    metadata: dict[str, Any] | None = None,
) -> asyncio.Task:
    record = {
        "id": job_id,
        "owner": owner,
        "operation": operation,
        "payload": payload,
        "status": "running",
        "created_at": time.time(),
    }
    if metadata:
        reserved = _RESERVED_RECORD_KEYS.intersection(metadata)
        if reserved:
            raise ValueError(f"extension job metadata uses reserved keys: {sorted(reserved)}")
        record.update(metadata)
    if isinstance(record.get("phase"), str) and isinstance(record.get("message"), str):
        _transition_progress(record, record["phase"], record["message"], float(record["created_at"]))
    with _RECORD_LOCK:
        _write_record(owner, operation, job_id, record)
    return _register(owner, operation, job_id, payload, runner)


def get_or_fire_idempotent(
    owner: str,
    operation: str,
    job_id: str,
    payload: dict[str, Any],
    runner: Runner,
    *,
    payload_digest: str,
    caller_extension: str,
    metadata: dict[str, Any] | None = None,
) -> asyncio.Task | dict[str, Any]:
    key = _key(owner, operation, job_id)
    with _RECORD_LOCK:
        record = read_record(owner, operation, job_id)
        if record is not None:
            if (
                record.get("payload_digest") != payload_digest
                or record.get("caller_extension") != caller_extension
            ):
                raise ValueError("idempotency key was already used with a different payload")
            task = _JOBS.get(key)
            return task if task is not None else response_from_record(record)
        record = {
            "id": job_id,
            "owner": owner,
            "operation": operation,
            "payload": payload,
            "payload_digest": payload_digest,
            "caller_extension": caller_extension,
            "status": "running",
            "created_at": time.time(),
        }
        if metadata:
            reserved = _RESERVED_RECORD_KEYS.intersection(metadata)
            if reserved:
                raise ValueError(f"extension job metadata uses reserved keys: {sorted(reserved)}")
            record.update(metadata)
        if isinstance(record.get("phase"), str) and isinstance(record.get("message"), str):
            _transition_progress(record, record["phase"], record["message"], float(record["created_at"]))
        _write_record(owner, operation, job_id, record)
        return _register(owner, operation, job_id, payload, runner)


def get_or_resume(owner: str, operation: str, job_id: str, runner: Runner) -> asyncio.Task | dict[str, Any] | None:
    key = _key(owner, operation, job_id)
    task = _JOBS.get(key)
    if task is not None:
        return task
    with _RECORD_LOCK:
        record = read_record(owner, operation, job_id)
        if record is None:
            return None
        status = record.get("status")
        if status in ("complete", "failed"):
            return response_from_record(record)
        payload = record.get("payload")
        if not isinstance(payload, dict):
            return None
        logger.info("extension_job_resume owner=%s operation=%s id=%s", owner, operation, job_id)
        now = time.time()
        record["resumed_at"] = now
        record["updated_at"] = now
        _transition_progress(record, "resuming", "Resuming job after backend restart", now)
        try:
            _write_record(owner, operation, job_id, record)
        except (OSError, TypeError, ValueError):
            logger.warning("extension_job_resume_persist_failed owner=%s operation=%s id=%s", owner, operation, job_id)
    return _register(owner, operation, job_id, payload, runner)


def get_active(owner: str, operation: str, job_id: str) -> asyncio.Task | None:
    return _JOBS.get(_key(owner, operation, job_id))


def has_active_jobs(owner: str | None = None, operation: str | None = None) -> bool:
    for key, task in _JOBS.items():
        if task.done():
            continue
        if owner is not None and key[0] != _safe_id(owner):
            continue
        if operation is not None and key[1] != _safe_id(operation):
            continue
        return True
    return False


def cleanup(owner: str | None = None, operation: str | None = None) -> None:
    cutoff = time.monotonic() - RESULT_TTL_SECONDS
    stale = [
        key
        for key, task in _JOBS.items()
        if task.done()
        and _COMPLETED_AT.get(key, 0.0) < cutoff
        and (owner is None or key[0] == _safe_id(owner))
        and (operation is None or key[1] == _safe_id(operation))
    ]
    for key in stale:
        _JOBS.pop(key, None)
        _COMPLETED_AT.pop(key, None)
    _sweep_disk(force=False, owner=owner, operation=operation)


def _sweep_disk(
    *,
    force: bool = False,
    owner: str | None = None,
    operation: str | None = None,
) -> None:
    scope = (_safe_id(owner) if owner else "", _safe_id(operation) if operation else "")
    now = time.monotonic()
    if not force and now - _LAST_DISK_SWEEP.get(scope, 0.0) < _DISK_SWEEP_INTERVAL_SECONDS:
        return
    _LAST_DISK_SWEEP[scope] = now
    wall_cutoff = time.time() - DISK_RETENTION_SECONDS
    roots: list[Path] = []
    try:
        if owner and operation:
            roots = [_jobs_dir(owner, operation)]
        else:
            base = bc_home() / "extension_jobs"
            roots = [path for path in base.glob("*/*") if path.is_dir()]
    except OSError:
        return
    for root in roots:
        try:
            entries = list(root.iterdir())
        except OSError:
            continue
        for path in entries:
            try:
                if path.stat().st_mtime < wall_cutoff:
                    path.unlink()
            except OSError:
                continue
