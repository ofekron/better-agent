"""Regression test: native_import must not share the process-wide default
`ThreadPoolExecutor` used by every bare `asyncio.to_thread(...)` call site
in the backend.

`native_import.count_native_sessions` does a full recursive `rglob` across
Claude+Codex+Gemini+pi native session directories, reading per-file headers
("can reach hundreds of MB across a full Claude+Codex history" per its own
docstring). It was dispatched via a bare
`asyncio.to_thread(native_import.count_native_sessions, ...)` from the
`/api/native-import/summary` route. While it ran, it could occupy shared-pool
worker slots long enough to delay any OTHER latency-sensitive
`asyncio.to_thread` call elsewhere in the backend that happened to land on
the same pool — the same shape of bug fixed for tailer cursor persistence
(see `test_tailer_cursor_ledger_worker.py`).

Fix: `native_import` now owns a small, dedicated `ThreadPoolExecutor`
(`native_import._SCAN_EXECUTOR`) and routes the scan through
`loop.run_in_executor(<dedicated>, ...)` instead of the shared default pool
— the same isolation pattern as `jsonl_tailer._FILE_POLL_EXECUTOR`,
`main._HOT_PATH_EXECUTOR`, and `extension_backend_loader._ROUNDTRIP_EXECUTOR`.

Two subtests:

  A. Saturating `native_import._SCAN_EXECUTOR` with slow work does not
     delay an unrelated default-pool `asyncio.to_thread` call.
  B. `native_import.count_native_sessions_async` actually executes its
     work on the dedicated scan executor's threads, not the default pool.

Run with:
    cd backend && .venv/bin/python scripts/test_native_import_scan_executor_isolation.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import threading
import time

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-native-import-scan-executor-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

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


async def test_a_native_import_scan_saturation_does_not_block_default_pool() -> bool:
    loop = asyncio.get_running_loop()
    workers = native_import._SCAN_EXECUTOR._max_workers
    futures = [
        loop.run_in_executor(native_import._SCAN_EXECUTOR, lambda: time.sleep(1.5))
        for _ in range(workers + 2)
    ]
    await asyncio.sleep(0.1)  # let the saturating work actually start
    elapsed = await _unrelated_default_pool_latency()
    ok = elapsed < 0.5
    print(
        f"{PASS if ok else FAIL} A: unrelated default-pool call took "
        f"{elapsed:.3f}s while {workers + 2} slow tasks saturated "
        f"native_import._SCAN_EXECUTOR ({workers} workers) (want < 0.5s)"
    )
    await asyncio.gather(*futures, return_exceptions=True)
    return ok


async def test_b_count_native_sessions_async_uses_dedicated_executor() -> bool:
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
        f"{PASS if ok else FAIL} B: count_native_sessions_async ran on "
        f"thread '{thread_name}' (want prefix 'native-import-scan')"
    )
    return ok


async def _run() -> int:
    results = [
        await test_a_native_import_scan_saturation_does_not_block_default_pool(),
        await test_b_count_native_sessions_async_uses_dedicated_executor(),
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
