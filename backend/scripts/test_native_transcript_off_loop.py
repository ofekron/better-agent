"""Regression tests: native transcript blocking reads must never run on the
asyncio event-loop thread.

`wait_fresh`, `ensure_fresh_for_read`, `match_paths`, `search_rows`, and
`run_readonly_sql` block on time.sleep / SQLite. Every production caller
offloads them to an executor; these tests lock the fail-closed guard that
makes an accidental on-loop call an immediate RuntimeError instead of a
silent event-loop stall:

  * each guarded function raises RuntimeError when called from a coroutine
    running on the event loop.
  * the same calls succeed when offloaded via asyncio.to_thread.
  * plain sync contexts (no running loop) are unaffected.

Run:
    cd backend && .venv/bin/python scripts/test_native_transcript_off_loop.py
"""
from __future__ import annotations

import asyncio
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-native-off-loop-")

import native_transcript_index as idx  # noqa: E402

_CALLS = {
    "wait_fresh": lambda: idx.wait_fresh(timeout=0.01),
    "ensure_fresh_for_read": lambda: idx.ensure_fresh_for_read(timeout=0.01),
    "match_paths": lambda: idx.match_paths(["needle"], set()),
    "search_rows": lambda: idx.search_rows(["needle"]),
    "run_readonly_sql": lambda: idx.run_readonly_sql("SELECT 1"),
}


def test_on_loop_calls_raise() -> None:
    async def run() -> None:
        for name, call in _CALLS.items():
            try:
                call()
            except RuntimeError as exc:
                assert name in str(exc), f"{name}: unexpected message {exc!r}"
            else:
                raise AssertionError(f"{name} did not raise on the event loop")

    asyncio.run(run())
    print("ok: all guarded functions raise on the event-loop thread")


def test_to_thread_offload_passes() -> None:
    async def run() -> None:
        for name, call in _CALLS.items():
            result = await asyncio.to_thread(call)
            if name == "run_readonly_sql":
                assert result.get("error") == "index_not_built", result

    asyncio.run(run())
    print("ok: asyncio.to_thread offload passes the guard")


def test_plain_sync_context_unaffected() -> None:
    for name, call in _CALLS.items():
        try:
            call()
        except RuntimeError as exc:
            raise AssertionError(f"{name} raised without a running loop: {exc}")
    print("ok: sync contexts without a loop are unaffected")


def main() -> None:
    test_on_loop_calls_raise()
    test_to_thread_offload_passes()
    test_plain_sync_context_unaffected()
    print("ALL PASS")


if __name__ == "__main__":
    main()
