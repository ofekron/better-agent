"""Regression test for the silent-truncation-under-load bug.

Incident (2026-07-13, session e84dc9e3): a native turn's answer was fully
written to `session_events.jsonl` but the render tree only ever captured
a 4-character prefix. Root cause: every native provider's cursor-advance
callback (`_on_cursor` / `_on_tailer_progress`) did TWO synchronous disk
operations — an atomic `backend_state.json` write and a
`spawn_ledger.record_discovered` append (global lock + open + write on a
35MB+ file) — on literally EVERY dispatched line, submitted to a single
process-wide `ThreadPoolExecutor(max_workers=2)` (`jsonl_tailer.py`'s
`_CURSOR_EXECUTOR`, shared by every tailer in the backend). The tailer's
read loop `await`s that callback before reading its next line, so
executor starvation under real concurrency stalled dispatch to the
render tree — silently, since `await_line_tailer_drained`'s fixed 5s
timeout then fired the turn "complete" with whatever partial content had
made it through.

Three subtests:

  A. `CursorPersistGate` batches persistence decisions by count
     (deterministic — a huge `min_interval` removes any time-based
     flakiness) and always signals immediate persistence on rewind.

  B. `spawn_ledger.record_discovered` called many times with the SAME
     sid appends to the on-disk ledger exactly once — locks the fix that
     stops an ever-running turn from re-appending its own sid on every
     persisted cursor flush.

  C. The actual mechanism: tail a pre-written events.jsonl through
     `GeminiJsonlTailer` (the class Gemini/OpenAI providers reuse) with
     an `on_cursor_advance` callback whose "persist" side-effect is
     artificially slow (simulating real disk I/O + lock contention),
     against a deliberately saturated single-worker executor. Gated
     (debounced) persistence dispatches every line to the render tree
     well within a bounded time; UNGATED (persist-every-line, i.e. the
     pre-fix behavior) provably blows that bound — proving the coupling
     between slow cursor persistence and stalled render-tree dispatch is
     real, and that the fix breaks it.

Run with:
    cd backend && .venv/bin/python scripts/test_tailer_cursor_persist_debounce.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-cursor-debounce-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from pathlib import Path

import jsonl_tailer  # noqa: E402
import spawn_ledger  # noqa: E402
from jsonl_tailer import CursorPersistGate, GeminiJsonlTailer  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


# ─── A: CursorPersistGate batching ─────────────────────────────────

def test_a_gate_batches_by_count_and_flushes_on_rewind() -> bool:
    gate = CursorPersistGate(start=0, min_advance=8, min_interval=1e9)
    decisions = []
    for n in range(1, 21):
        should_persist = gate.advance(n)
        decisions.append((n, should_persist))
        if should_persist:
            gate.mark_persisted(gate.pending)

    persisted_at = [n for n, should in decisions if should]
    ok_batching = persisted_at == [8, 16]

    # Rewind (source truncated/reset) must persist immediately regardless
    # of how little the cursor has advanced.
    should_persist_rewind = gate.advance(3)
    ok_rewind = should_persist_rewind is True and gate.pending == 3

    ok = ok_batching and ok_rewind
    print(
        f"{PASS if ok else FAIL} A: gate persisted at {persisted_at} "
        f"(want [8, 16]); rewind persist={should_persist_rewind}"
    )
    return ok


# ─── B: spawn_ledger dedup on repeated record_discovered ───────────

def test_b_record_discovered_writes_sid_once() -> bool:
    sid = "regress-sid-cursor-debounce"
    for _ in range(50):
        spawn_ledger.record_discovered(sid)

    p = spawn_ledger._path()
    lines = [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
    occurrences = sum(1 for ln in lines if ln.strip() == sid)

    ok = occurrences == 1 and sid in spawn_ledger.all_sids()
    print(
        f"{PASS if ok else FAIL} B: sid appended {occurrences} time(s) "
        f"after 50 record_discovered calls (want 1)"
    )
    return ok


# ─── C: dispatch stays decoupled from slow persistence ─────────────

async def _run_tailer_and_time_dispatch(
    events_path: Path, *, on_cursor_advance, bound_seconds: float,
) -> tuple[bool, int, float]:
    """Run a GeminiJsonlTailer over `events_path` and return
    (finished_within_bound, dispatched_count, elapsed)."""
    dispatched: list[dict] = []

    def _dispatch(event: dict) -> None:
        dispatched.append(event)

    tailer = GeminiJsonlTailer(
        path=events_path,
        start_offset=0,
        dispatch=_dispatch,
        on_cursor_advance=on_cursor_advance,
    )
    task = asyncio.create_task(tailer.run())
    start = time.monotonic()
    finished = True
    try:
        await asyncio.wait_for(
            _wait_for_count(dispatched, 20), timeout=bound_seconds,
        )
    except asyncio.TimeoutError:
        finished = False
    elapsed = time.monotonic() - start
    tailer.stop()
    try:
        await asyncio.wait_for(task, timeout=5.0)
    except (asyncio.TimeoutError, Exception):
        task.cancel()
    return finished, len(dispatched), elapsed


async def _wait_for_count(lst: list, target: int) -> None:
    while len(lst) < target:
        await asyncio.sleep(0.01)


async def test_c_slow_persist_stalls_dispatch_without_gating() -> bool:
    events_path = Path(_TMP_HOME) / "cursor_debounce_events.jsonl"
    with events_path.open("w", encoding="utf-8") as f:
        for i in range(20):
            f.write('{"type": "line", "i": %d}\n' % i)

    original_executor = jsonl_tailer._CURSOR_EXECUTOR
    # Deliberately saturated: one worker, so any blocking callback fully
    # serializes against every other pending cursor-advance job — this is
    # what "hundreds of concurrent tailers on a fixed small pool" looks
    # like in miniature, made deterministic instead of relying on real
    # concurrent load.
    jsonl_tailer._CURSOR_EXECUTOR = ThreadPoolExecutor(max_workers=1)
    try:
        # Gated: persistence debounced, so the slow side-effect fires
        # only twice across 20 lines (see test A's math for min_advance=8).
        gate = CursorPersistGate(start=0, min_advance=8, min_interval=1e9)

        def _on_cursor_gated(n: int) -> None:
            if gate.advance(n):
                time.sleep(0.15)  # simulated disk write + lock contention
                gate.mark_persisted(gate.pending)

        gated_ok, gated_count, gated_elapsed = await _run_tailer_and_time_dispatch(
            events_path, on_cursor_advance=_on_cursor_gated, bound_seconds=1.5,
        )

        # Ungated control: the pre-fix behavior — persist on EVERY line.
        def _on_cursor_ungated(n: int) -> None:
            time.sleep(0.15)

        ungated_ok, ungated_count, ungated_elapsed = await _run_tailer_and_time_dispatch(
            events_path, on_cursor_advance=_on_cursor_ungated, bound_seconds=1.5,
        )
    finally:
        jsonl_tailer._CURSOR_EXECUTOR.shutdown(wait=False)
        jsonl_tailer._CURSOR_EXECUTOR = original_executor

    # Gated must finish comfortably inside the bound (2 slow calls ~0.3s
    # total); ungated must NOT (20 slow calls, serialized, ~3s minimum on
    # a 1-worker pool) — this is the actual regression: before the fix,
    # per-line synchronous persistence stalls dispatch to the render tree.
    ok = gated_ok and not ungated_ok
    print(
        f"{PASS if ok else FAIL} C: gated dispatch finished="
        f"{gated_ok} in {gated_elapsed:.2f}s ({gated_count}/20 lines); "
        f"ungated dispatch finished={ungated_ok} in {ungated_elapsed:.2f}s "
        f"({ungated_count}/20 lines) — want gated=True, ungated=False"
    )
    return ok


# ─── runner ───────────────────────────────────────────────────────

async def _run() -> int:
    results = [
        test_a_gate_batches_by_count_and_flushes_on_rewind(),
        test_b_record_discovered_writes_sid_once(),
        await test_c_slow_persist_stalls_dispatch_without_gating(),
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
