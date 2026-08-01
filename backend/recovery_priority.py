from __future__ import annotations

import asyncio
import threading
import time

import perf

_lock = threading.Lock()
_interactive_requests = 0
_quiet = asyncio.Event()
_quiet.set()
_quiet_loop: object = None

MAX_INTERACTIVE_DEFER_SECONDS = 0.250


def _rebind_quiet_to_running_loop() -> None:
    """Recreate the admission Event when the running event loop changes.

    asyncio.Event binds to the loop of its first wait() caller. Production
    runs one loop for the process, but test harnesses (and any rare loop
    recreation) create fresh loops per scenario, which would otherwise leave
    the Event bound to a dead loop. set/clear are loop-safe; only wait()
    binds, so rebind here at the sole wait() caller, preserving the current
    set state so admission semantics are unchanged.
    """
    global _quiet, _quiet_loop
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    with _lock:
        if _quiet_loop is loop:
            return
        was_set = _quiet.is_set()
        _quiet = asyncio.Event()
        if was_set:
            _quiet.set()
        _quiet_loop = loop


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
    _rebind_quiet_to_running_loop()
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
