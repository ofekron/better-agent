"""Regression test: native_import / lag_incident_queue must not share the
process-wide default `ThreadPoolExecutor` used by every bare
`asyncio.to_thread(...)` call site in the backend.

Both subsystems are slow/unbounded consumers of that shared pool:

  - `native_import.count_native_sessions` does a full recursive `rglob`
    across Claude+Codex+Gemini+pi native session directories, reading
    per-file headers ("can reach hundreds of MB across a full Claude+Codex
    history" per its own docstring). It was dispatched via a bare
    `asyncio.to_thread(native_import.count_native_sessions, ...)` from the
    `/api/native-import/summary` route.

  - `lag_incident_queue`'s background spool-processing/poll loop
    (`_drain_outcome` / `_run`) makes ~20 `asyncio.to_thread(...)` calls per
    dispatch cycle for ongoing, unbounded-duration file I/O.

While either ran, it could occupy shared-pool worker slots long enough to
delay any OTHER latency-sensitive `asyncio.to_thread` call elsewhere in the
backend that happened to land on the same pool — the same shape of bug
fixed for tailer cursor persistence (see
`test_tailer_cursor_ledger_worker.py`) and per-turn dispatch.

Fix: each subsystem now owns a small, dedicated `ThreadPoolExecutor`
(`native_import._SCAN_EXECUTOR`, `lag_incident_queue._SPOOL_IO_EXECUTOR`)
and routes its blocking calls through `loop.run_in_executor(<dedicated>,
...)` instead of the shared default pool — the same isolation pattern as
`jsonl_tailer._FILE_POLL_EXECUTOR`, `main._HOT_PATH_EXECUTOR`, and
`extension_backend_loader._ROUNDTRIP_EXECUTOR`.

Four subtests:

  A. Saturating `native_import._SCAN_EXECUTOR` with slow work does not
     delay an unrelated default-pool `asyncio.to_thread` call.
  B. Saturating `lag_incident_queue._SPOOL_IO_EXECUTOR` with slow work
     does not delay an unrelated default-pool `asyncio.to_thread` call.
  C. `native_import.count_native_sessions_async` actually executes its
     work on the dedicated scan executor's threads, not the default pool.
  D. `lag_incident_queue._to_thread` actually executes its work on the
     dedicated spool-I/O executor's threads, not the default pool.

Run with:
    cd backend && .venv/bin/python scripts/test_native_import_lag_queue_executor_isolation.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import threading
import time

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-native-import-lag-queue-executor-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import lag_incident_queue  # noqa: E402
import native_import  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


async def _unrelated_default_pool_latency() -> float:
    """Time a trivial `asyncio.to_thread` call on the SHARED default pool.
    If a dedicated executor leaks work back onto the default pool, this call
    queues behind it and the measured latency spikes."""
    start = time.monotonic()
    await asyncio.to_thread(lambda: None)
    return time.monotonic() - start


async def _saturate_executor(executor) -> tuple[list, threading.Event]:
    """Fill `executor` with Event-released blockers (max_workers + 2 of them)
    and wait until every worker slot is actually occupied. Returns the
    futures and the release Event the caller sets once its assertion ran."""
    loop = asyncio.get_running_loop()
    workers = executor._max_workers
    release = threading.Event()
    started = [threading.Event() for _ in range(workers + 2)]

    def _blocker(started_evt: threading.Event) -> None:
        started_evt.set()
        release.wait(timeout=10.0)

    futures = [loop.run_in_executor(executor, _blocker, evt) for evt in started]
    deadline = time.monotonic() + 5.0
    while sum(1 for evt in started if evt.is_set()) < workers:
        if time.monotonic() >= deadline:
            release.set()
            await asyncio.gather(*futures, return_exceptions=True)
            raise AssertionError("saturating blockers never occupied all worker slots")
        await asyncio.sleep(0.005)
    return futures, release


async def test_a_native_import_scan_saturation_does_not_block_default_pool() -> bool:
    workers = native_import._SCAN_EXECUTOR._max_workers
    futures, release = await _saturate_executor(native_import._SCAN_EXECUTOR)
    elapsed = await _unrelated_default_pool_latency()
    release.set()
    ok = elapsed < 0.5
    print(
        f"{PASS if ok else FAIL} A: unrelated default-pool call took "
        f"{elapsed:.3f}s while {workers + 2} blocked tasks saturated "
        f"native_import._SCAN_EXECUTOR ({workers} workers) (want < 0.5s)"
    )
    await asyncio.gather(*futures, return_exceptions=True)
    return ok


async def test_b_lag_incident_spool_io_saturation_does_not_block_default_pool() -> bool:
    workers = lag_incident_queue._SPOOL_IO_EXECUTOR._max_workers
    futures, release = await _saturate_executor(lag_incident_queue._SPOOL_IO_EXECUTOR)
    elapsed = await _unrelated_default_pool_latency()
    release.set()
    ok = elapsed < 0.5
    print(
        f"{PASS if ok else FAIL} B: unrelated default-pool call took "
        f"{elapsed:.3f}s while {workers + 2} blocked tasks saturated "
        f"lag_incident_queue._SPOOL_IO_EXECUTOR ({workers} workers) (want < 0.5s)"
    )
    await asyncio.gather(*futures, return_exceptions=True)
    return ok


async def test_c_count_native_sessions_async_uses_dedicated_executor() -> bool:
    captured: dict[str, str] = {}
    original = native_import.count_native_sessions

    def fake(provider_ids=None, project_paths=None):
        captured["thread"] = threading.current_thread().name
        return {"total": 0, "imported": 0, "pending": 0, "by_provider": {}}

    native_import.count_native_sessions = fake
    try:
        result = await native_import.count_native_sessions_async(None, None)
    finally:
        native_import.count_native_sessions = original

    thread_name = captured.get("thread", "")
    ok = thread_name.startswith("native-import-scan") and result.get("total") == 0
    print(
        f"{PASS if ok else FAIL} C: count_native_sessions_async ran on "
        f"thread '{thread_name}' (want prefix 'native-import-scan')"
    )
    return ok


async def test_d_lag_incident_to_thread_uses_dedicated_executor() -> bool:
    captured: dict[str, str] = {}

    def fn() -> int:
        captured["thread"] = threading.current_thread().name
        return 42

    result = await lag_incident_queue._to_thread(fn)
    thread_name = captured.get("thread", "")
    ok = thread_name.startswith("lag-incident-io") and result == 42
    print(
        f"{PASS if ok else FAIL} D: lag_incident_queue._to_thread ran on "
        f"thread '{thread_name}' (want prefix 'lag-incident-io')"
    )
    return ok


async def _run() -> int:
    results = [
        await test_a_native_import_scan_saturation_does_not_block_default_pool(),
        await test_b_lag_incident_spool_io_saturation_does_not_block_default_pool(),
        await test_c_count_native_sessions_async_uses_dedicated_executor(),
        await test_d_lag_incident_to_thread_uses_dedicated_executor(),
    ]
    total = len(results)
    passed = sum(1 for r in results if r)
    print(f"\n{passed}/{total} subtests passed")
    return 0 if passed == total else 1


def main() -> int:
    try:
        return asyncio.run(_run())
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
