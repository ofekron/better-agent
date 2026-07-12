from __future__ import annotations

import asyncio
import threading
import time

import perf

_lock = threading.Lock()
_interactive_requests = 0
_quiet = asyncio.Event()
_quiet.set()

MAX_INTERACTIVE_DEFER_SECONDS = 0.250


def interactive_request_started() -> None:
    global _interactive_requests
    with _lock:
        _interactive_requests += 1
        _quiet.clear()
        perf.record_count("startup.recovery.interactive.active", _interactive_requests)


def interactive_request_finished() -> None:
    global _interactive_requests
    with _lock:
        _interactive_requests = max(0, _interactive_requests - 1)
        if _interactive_requests == 0:
            _quiet.set()


def interactive_request_count() -> int:
    with _lock:
        return _interactive_requests


async def admit_recovery_quantum() -> None:
    if interactive_request_count() == 0:
        return
    started = time.monotonic()
    try:
        await asyncio.wait_for(_quiet.wait(), timeout=MAX_INTERACTIVE_DEFER_SECONDS)
        perf.record_count("startup.recovery.quantum.preempted", 1)
    except TimeoutError:
        perf.record_count("startup.recovery.quantum.starvation_escape", 1)
    finally:
        perf.record(
            "startup.recovery.quantum.admission_wait",
            (time.monotonic() - started) * 1000.0,
        )


def reset_for_tests() -> None:
    global _interactive_requests
    with _lock:
        _interactive_requests = 0
        _quiet.set()
