"""Regression tests for bounded-parallel recovered-run integration.

Run with:
    cd backend && python3 scripts/test_run_recovery_parallel_integration.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_BACKEND = _HERE.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import _test_home  # noqa: E402
_test_home.isolate("bc-test-recovery-parallel-")

import run_recovery  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


async def _test_independent_session_buckets_run_in_parallel() -> bool:
    original = run_recovery._integrate_recovered_session_group
    seen: list[list[str]] = []
    active = 0
    max_active = 0
    lock = asyncio.Lock()

    async def fake_group(_coordinator, descs, _summary):
        nonlocal active, max_active
        async with lock:
            active += 1
            max_active = max(max_active, active)
        seen.append([str(d["run_id"]) for d in descs])
        await asyncio.sleep(0.15)
        async with lock:
            active -= 1

    old_env = os.environ.get(run_recovery._RECOVERY_INTEGRATION_PARALLELISM_ENV)
    os.environ[run_recovery._RECOVERY_INTEGRATION_PARALLELISM_ENV] = "4"
    run_recovery._integrate_recovered_session_group = fake_group
    try:
        recovered = [
            {"run_id": f"run-{i}", "app_session_id": f"session-{i}"}
            for i in range(4)
        ]
        started = time.monotonic()
        await run_recovery.integrate_recovered_runs(None, recovered)
        elapsed = time.monotonic() - started
    finally:
        run_recovery._integrate_recovered_session_group = original
        if old_env is None:
            os.environ.pop(run_recovery._RECOVERY_INTEGRATION_PARALLELISM_ENV, None)
        else:
            os.environ[run_recovery._RECOVERY_INTEGRATION_PARALLELISM_ENV] = old_env

    if len(seen) != 4:
        print(f"{FAIL} expected 4 independent buckets, saw {seen!r}")
        return False
    if max_active < 2:
        print(f"{FAIL} expected cross-session parallelism, max_active={max_active}")
        return False
    if elapsed >= 0.45:
        print(f"{FAIL} expected bounded parallel runtime, elapsed={elapsed:.3f}s")
        return False
    print(f"{PASS} independent session buckets integrate in parallel")
    return True


async def _test_same_session_stays_in_one_serial_bucket() -> bool:
    original = run_recovery._integrate_recovered_session_group
    seen: list[list[str]] = []

    async def fake_group(_coordinator, descs, _summary):
        seen.append([str(d["run_id"]) for d in descs])

    run_recovery._integrate_recovered_session_group = fake_group
    try:
        recovered = [
            {"run_id": "older", "app_session_id": "same-session"},
            {"run_id": "newer", "app_session_id": "same-session"},
        ]
        await run_recovery.integrate_recovered_runs(None, recovered)
    finally:
        run_recovery._integrate_recovered_session_group = original

    if seen != [["older", "newer"]]:
        print(f"{FAIL} expected one ordered same-session bucket, saw {seen!r}")
        return False
    print(f"{PASS} same-session recovered runs stay serially bucketed")
    return True


async def _main() -> int:
    ok = True
    ok &= await _test_independent_session_buckets_run_in_parallel()
    ok &= await _test_same_session_stays_in_one_serial_bucket()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
