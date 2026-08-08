"""Regression test for the silent-truncation-under-load bug.

Incident (2026-07-13, session e84dc9e3): a native turn's answer was fully
written to `session_events.jsonl` but the render tree only ever captured
a 4-character prefix. Root cause: every native provider's cursor-advance
callback (`_on_cursor` / `_on_tailer_progress`) did TWO synchronous disk
operations — an atomic `backend_state.json` write and a
`spawn_ledger.record_discovered` append (global lock + open + write on a
35MB+ file) — on literally EVERY dispatched line, submitted to a single
process-wide `ThreadPoolExecutor` shared by every tailer in the backend.
The tailer's read loop `await`ed that callback before reading its next
line, so executor starvation under real concurrency stalled dispatch to
the render tree — silently, since `await_line_tailer_drained`'s fixed 5s
timeout then fired the turn "complete" with whatever partial content had
made it through.

Fix: `cursor_ledger_worker.CursorLedgerWorker` is a single dedicated
background thread that owns all cursor-advance persistence. Providers'
`on_cursor_advance` callbacks now do an O(1), lock-only `note()` call and
return immediately — no executor, no await, no I/O on the tailer's own
call path at all. The worker coalesces: if multiple `note()` calls land
for the same run while its previous write is still in flight, only the
LATEST one is ever actually persisted.

Four subtests:

  A. `CursorLedgerWorker` coalesces concurrent notes for the same key
     down to the latest value, and `flush_now` blocks until it lands.

  B. `spawn_ledger.record_discovered` called many times with the SAME
     sid appends to the on-disk ledger exactly once.

  C. The actual mechanism: `note()` never blocks the caller, even when
     the write it schedules is artificially slow — proving the tailer's
     read loop can never stall on persistence, regardless of executor
     contention (there IS no executor in the hot path anymore).

  D. End-to-end: tail a pre-written events.jsonl through
     `SessionEventsJsonlTailer` (the class Gemini/OpenAI providers reuse) with
     an `on_cursor_advance` whose persist side-effect is artificially
     slow. All lines dispatch to the render tree well within a bound
     that would be impossible if dispatch were still coupled to
     persistence latency.

Run with:
    cd backend && .venv/bin/python scripts/test_tailer_cursor_ledger_worker.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import time

import _test_home

import pytest

pytestmark = pytest.mark.anyio
_TMP_HOME = _test_home.isolate("bc-test-cursor-ledger-worker-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from pathlib import Path

import spawn_ledger  # noqa: E402
from cursor_ledger_worker import CursorLedgerWorker  # noqa: E402
from jsonl_tailer import SessionEventsJsonlTailer  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


# ─── A: CursorLedgerWorker coalesces and flush_now waits correctly ──

def test_a_worker_coalesces_and_flush_now_waits() -> None:
    w = CursorLedgerWorker(name="test-a-worker")
    try:
        results: list[int] = []
        started = __import__("threading").Event()

        def slow_write(val: int) -> None:
            started.set()
            time.sleep(0.2)
            results.append(val)

        w.note("k", lambda: slow_write(1))
        started.wait(1.0)  # first write is now in flight
        for i in range(2, 30):
            w.note("k", lambda i=i: slow_write(i))
        flushed = w.flush_now("k", timeout=3.0)

        idle_ok = w.flush_now("never-noted-key", timeout=1.0) is True

        print(
            f"A: worker executed {len(results)}/29 "
            f"notes (coalesced), first={results[0] if results else None}, "
            f"last={results[-1] if results else None}, flush_now={flushed}, "
            f"idle key flush_now={idle_ok}"
        )
        assert flushed is True, "flush_now should return True for a noted key"
        assert results[0] == 1, f"first write must be 1, got {results[0]!r}"
        assert results[-1] == 29, f"last write must be 29, got {results[-1]!r}"
        assert len(results) < 29, f"expected coalescing (<29 writes), got {len(results)}"
        assert idle_ok, "flush_now on an idle key should return True"
    finally:
        w.stop()


# ─── B: spawn_ledger dedup on repeated record_discovered ───────────

def test_b_record_discovered_writes_sid_once() -> None:
    sid = "regress-sid-cursor-ledger-worker"
    for _ in range(50):
        spawn_ledger.record_discovered(sid)

    p = spawn_ledger._path()
    lines = [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
    occurrences = sum(1 for ln in lines if ln.strip() == sid)

    print(f"B: sid appended {occurrences} time(s) after 50 record_discovered calls (want 1)")
    assert occurrences == 1, f"sid appended {occurrences} time(s), want 1"
    assert sid in spawn_ledger.all_sids(), "sid missing from all_sids()"


# ─── C: note() never blocks the caller, however slow the write is ──

def test_c_note_never_blocks_caller() -> None:
    w = CursorLedgerWorker(name="test-c-worker")
    try:
        def very_slow_write() -> None:
            time.sleep(2.0)

        start = time.monotonic()
        for i in range(100):
            w.note(f"run-{i}", very_slow_write)
        elapsed = time.monotonic() - start

        print(
            f"C: 100 note() calls (each scheduling a 2s write) returned in "
            f"{elapsed:.3f}s (want < 0.5s)"
        )
        assert elapsed < 0.5, f"100 note() calls took {elapsed:.3f}s, want < 0.5s"
    finally:
        w.stop()


# ─── D: dispatch stays decoupled from slow persistence end-to-end ──

async def test_d_dispatch_decoupled_from_slow_persist() -> None:
    events_path = Path(_TMP_HOME) / "cursor_ledger_worker_events.jsonl"
    with events_path.open("w", encoding="utf-8") as f:
        for i in range(20):
            f.write('{"type": "line", "i": %d}\n' % i)

    w = CursorLedgerWorker(name="test-d-worker")
    try:
        dispatched: list[dict] = []

        def _dispatch(event: dict) -> None:
            dispatched.append(event)

        def _on_cursor_advance(n: int) -> None:
            w.note("run-d", lambda: time.sleep(0.3))  # simulated slow disk I/O

        tailer = SessionEventsJsonlTailer(
            path=events_path,
            start_offset=0,
            dispatch=_dispatch,
            on_cursor_advance=_on_cursor_advance,
        )
        task = asyncio.create_task(tailer.run())
        start = time.monotonic()
        finished = True
        try:
            await asyncio.wait_for(_wait_for_count(dispatched, 20), timeout=1.0)
        except asyncio.TimeoutError:
            finished = False
        elapsed = time.monotonic() - start
        tailer.stop()
        try:
            await asyncio.wait_for(task, timeout=5.0)
        except (asyncio.TimeoutError, Exception):
            task.cancel()

        # 20 lines * 0.3s of (simulated) persist latency would need 6s if
        # dispatch were still coupled to it; decoupled, it finishes fast.
        print(
            f"D: dispatched {len(dispatched)}/20 lines in {elapsed:.2f}s "
            f"despite 0.3s simulated persist latency per line (want < 1.0s)"
        )
        assert finished, "dispatch did not finish within the 1.0s bound"
        assert elapsed < 1.0, f"dispatch took {elapsed:.2f}s, want < 1.0s"
        assert len(dispatched) == 20, f"dispatched {len(dispatched)}/20 lines"
    finally:
        w.stop()


async def _wait_for_count(lst: list, target: int) -> None:
    while len(lst) < target:
        await asyncio.sleep(0.01)


# ─── runner ───────────────────────────────────────────────────────

async def _run() -> int:
    sync_cases = [
        ("A", test_a_worker_coalesces_and_flush_now_waits),
        ("B", test_b_record_discovered_writes_sid_once),
        ("C", test_c_note_never_blocks_caller),
    ]
    passed = 0
    total = len(sync_cases) + 1
    for label, fn in sync_cases:
        try:
            fn()
            print(f"{PASS} {label}")
            passed += 1
        except AssertionError as exc:
            print(f"{FAIL} {label}: {exc}")
        except Exception:
            import traceback
            traceback.print_exc()
            print(f"{FAIL} {label} (unexpected error)")
    try:
        await test_d_dispatch_decoupled_from_slow_persist()
        print(f"{PASS} D")
        passed += 1
    except AssertionError as exc:
        print(f"{FAIL} D: {exc}")
    except Exception:
        import traceback
        traceback.print_exc()
        print(f"{FAIL} D (unexpected error)")
    print(f"\n{passed}/{total} subtests passed")
    return 0 if passed == total else 1


def main() -> int:
    try:
        return asyncio.run(_run())
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
