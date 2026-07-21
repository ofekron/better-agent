from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SearchLimits:
    memory_mb: int
    cpu_seconds: int
    wall_seconds: int


SEARCH_LIMITS = {
    "unit_rg": SearchLimits(memory_mb=512, cpu_seconds=20, wall_seconds=30),
    "unit_fts": SearchLimits(memory_mb=768, cpu_seconds=25, wall_seconds=40),
    "unit_vector": SearchLimits(memory_mb=3072, cpu_seconds=75, wall_seconds=90),
    "index_sql": SearchLimits(memory_mb=768, cpu_seconds=35, wall_seconds=45),
}
_MAX_RESULT_BYTES = 64 * 1024 * 1024
_WORKER = Path(__file__).with_name("requirements_search_worker.py")


def _failure(action: str, resource_name: str, limit: str) -> dict[str, Any]:
    lane = action.replace("_", "-")
    message = (
        f"Requirements search lane '{lane}' exceeded its {resource_name} limit ({limit}). "
        "Retry with a finer search: narrow the cwd/project scope and use fewer, rarer terms "
        "or a more selective transcript SQL predicate."
    )
    return {
        "success": False,
        "error": message,
        "error_code": "requirements_search_resource_limit",
        "lane": lane,
        "resource": resource_name,
        "retryable": True,
        "retry_strategy": "narrow_query",
    }


def run_supervised_search(action: str, **kwargs: Any) -> dict[str, Any]:
    limits = SEARCH_LIMITS.get(action)
    if limits is None:
        raise ValueError(f"unsupported requirements search action: {action}")
    payload = json.dumps(
        {"action": action, "kwargs": kwargs},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    launch: dict[str, Any] = {
        "env": {
            **os.environ,
            "BETTER_AGENT_SEARCH_MEMORY_BYTES": str(limits.memory_mb * 1024 * 1024),
            "BETTER_AGENT_SEARCH_CPU_SECONDS": str(limits.cpu_seconds),
        }
    }
    if os.name == "posix":
        launch["start_new_session"] = True
    elif os.name == "nt":
        launch["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

    started = time.perf_counter()
    process = subprocess.Popen(
        [sys.executable, str(_WORKER)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **launch,
    )
    windows_job = _assign_windows_job(process, limits) if os.name == "nt" else None
    try:
        try:
            stdout, stderr = process.communicate(payload, timeout=limits.wall_seconds)
        except subprocess.TimeoutExpired:
            _terminate_process_tree(process, windows_job)
            process.communicate()
            return _failure(action, "wall-time", f"{limits.wall_seconds}s")
    finally:
        if windows_job is not None:
            import ctypes

            ctypes.windll.kernel32.CloseHandle(windows_job)
    elapsed_ms = (time.perf_counter() - started) * 1000
    logger.info(
        "requirements_search_lane action=%s pid=%s elapsed_ms=%.1f returncode=%s",
        action,
        process.pid,
        elapsed_ms,
        process.returncode,
    )
    if len(stdout) > _MAX_RESULT_BYTES:
        return _failure(action, "output", f"{_MAX_RESULT_BYTES // (1024 * 1024)}MB")
    if process.returncode != 0:
        stderr_text = stderr.decode("utf-8", "replace")
        if os.name == "nt":
            return _failure(
                action,
                "CPU-or-memory",
                f"{limits.cpu_seconds}s CPU / {limits.memory_mb}MB",
            )
        if process.returncode == -getattr(signal, "SIGXCPU", 24):
            return _failure(action, "CPU", f"{limits.cpu_seconds}s")
        if process.returncode == -signal.SIGKILL or "MemoryError" in stderr_text:
            return _failure(action, "memory", f"{limits.memory_mb}MB")
        detail = stderr_text[-2000:].strip()
        return {
            "success": False,
            "error": f"Requirements search lane '{action.replace('_', '-')}' failed: {detail or 'worker exited unexpectedly'}",
            "error_code": "requirements_search_worker_failed",
            "lane": action.replace("_", "-"),
            "retryable": False,
        }
    try:
        result = json.loads(stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {
            "success": False,
            "error": f"Requirements search lane returned an invalid result: {exc}",
            "error_code": "requirements_search_invalid_result",
            "lane": action.replace("_", "-"),
            "retryable": False,
        }
    if not isinstance(result, dict):
        raise TypeError("requirements search worker result must be an object")
    return result


def _terminate_process_tree(process: subprocess.Popen[bytes], windows_job: int | None) -> None:
    if process.poll() is not None:
        return
    if os.name == "posix":
        os.killpg(process.pid, signal.SIGKILL)
        return
    if windows_job is not None:
        import ctypes

        ctypes.windll.kernel32.TerminateJobObject(windows_job, 1)
    else:
        process.kill()


def _assign_windows_job(process: subprocess.Popen[bytes], limits: SearchLimits) -> int:
    import ctypes
    from ctypes import wintypes

    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
        )]

    class BasicLimits(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimits),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.windll.kernel32
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError()
    info = ExtendedLimits()
    info.BasicLimitInformation.LimitFlags = 0x00000100 | 0x00000002 | 0x00002000
    info.BasicLimitInformation.PerJobUserTimeLimit = limits.cpu_seconds * 10_000_000
    info.JobMemoryLimit = limits.memory_mb * 1024 * 1024
    if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
        kernel32.CloseHandle(job)
        raise ctypes.WinError()
    if not kernel32.AssignProcessToJobObject(job, wintypes.HANDLE(process._handle)):
        kernel32.CloseHandle(job)
        raise ctypes.WinError()
    return job
