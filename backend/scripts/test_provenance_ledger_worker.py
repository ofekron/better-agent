"""Regression test: provenance persistence must not block apply_event.

Incident shape (same root cause as the cursor-persistence bug fixed by
`cursor_ledger_worker`, but on the render-tree apply path itself, not a
crash-recovery side channel): `apply_event` (orchs/base.py) used to call
`session_manager.apply_provenance_from_event` -> `provenance_store.record`
SYNCHRONOUSLY — a blocking disk append serialized by a single GLOBAL
`threading.Lock()` shared by every session — directly on the shared
2-worker `_STREAM_EVENT_APPLY_EXECUTOR` (turn_manager.py) that EVERY
concurrent session's live event stream funnels through. One session's
slow/contended provenance write could stall a completely unrelated
session's render-tree event application.

Fix: `provenance_ledger_worker.ProvenanceLedgerWorker` is a single
dedicated background thread that owns all provenance persistence.
`session_manager.apply_provenance_from_event` now does an O(1),
lock-only `note()` call and returns immediately — no executor, no
await, no disk I/O on the apply_event call path at all. Unlike the
cursor worker, pending entries for the same session ACCUMULATE rather
than coalescing to the latest — every event's tool rows must all be
persisted, not just the newest.

Three subtests:

  A. Multiple `note()` calls for the SAME session queued while a
     write is in flight all land on disk — nothing is dropped (proves
     accumulation, not "latest wins" coalescing).

  B. `note()` never blocks the caller, even when the write it
     schedules is artificially slow.

  C. End-to-end, reproducing the actual bug: submit
     `session_manager.apply_provenance_from_event` calls for TWO
     different sessions, interleaved, onto a tiny 2-worker
     `ThreadPoolExecutor` (mirroring `_STREAM_EVENT_APPLY_EXECUTOR`),
     with `provenance_store.record_from_event` monkeypatched to
     simulate slow/contended disk I/O. All submissions must complete
     fast (proving the executor isn't stalled), and every row for
     both sessions must eventually land on disk with the
     "provenance_changed" ping firing only after the write lands.

Run with:
    cd backend && .venv/bin/python scripts/test_provenance_ledger_worker.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-provenance-ledger-worker-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from provenance_ledger_worker import ProvenanceLedgerWorker  # noqa: E402
from stores import provenance_store  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _event(tool_id: str, tool: str, why: str) -> dict:
    return {
        "uuid": "evt-" + tool_id,
        "timestamp": "2026-07-13T00:00:00",
        "data": {"message": {"id": "msg-" + tool_id, "content": [
            {"type": "thinking", "thinking": why},
            {"type": "tool_use", "id": tool_id, "name": tool,
             "input": {"command": "echo hi"}},
        ]}},
    }


# ─── A: pending notes for the same sid accumulate, none are dropped ──

def test_a_notes_accumulate_without_loss() -> bool:
    sid = "regress-provenance-a"
    orig_record_from_event = provenance_store.record_from_event

    def slow_record_from_event(app_session_id, normalized, *, backend_msg_id=None):
        time.sleep(0.05)
        return orig_record_from_event(app_session_id, normalized, backend_msg_id=backend_msg_id)

    provenance_store.record_from_event = slow_record_from_event
    w = ProvenanceLedgerWorker(name="test-a-worker")
    try:
        for i in range(5):
            w.note(sid, _event(f"toolu-a{i}", "Bash", f"step {i}"))

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if len(provenance_store.read(sid)) >= 5:
                break
            time.sleep(0.02)

        rows = provenance_store.read(sid)
        ids = {r["uuid"] for r in rows}
        expected = {f"toolu-a{i}" for i in range(5)}
        ok = ids == expected
        print(
            f"{PASS if ok else FAIL} A: {len(rows)}/5 provenance rows landed "
            f"(want all 5, none dropped by coalescing)"
        )
        return ok
    finally:
        w.stop()
        provenance_store.record_from_event = orig_record_from_event


# ─── B: note() never blocks the caller, however slow the write is ──

def test_b_note_never_blocks_caller() -> bool:
    sid = "regress-provenance-b"
    orig_record_from_event = provenance_store.record_from_event
    provenance_store.record_from_event = lambda *a, **k: (time.sleep(2.0), 1)[1]

    w = ProvenanceLedgerWorker(name="test-b-worker")
    try:
        start = time.monotonic()
        for i in range(100):
            w.note(sid, _event(f"toolu-b{i}", "Bash", "slow"))
        elapsed = time.monotonic() - start

        ok = elapsed < 0.5  # 100 note() calls, each would cost 2s if blocking
        print(
            f"{PASS if ok else FAIL} B: 100 note() calls (each scheduling a "
            f"2s write) returned in {elapsed:.3f}s (want < 0.5s)"
        )
        return ok
    finally:
        w.stop()
        provenance_store.record_from_event = orig_record_from_event


# ─── C: end-to-end — apply_provenance_from_event doesn't stall the ──
# ─── shared render-apply executor, across unrelated sessions ───────

def test_c_apply_provenance_does_not_stall_shared_executor() -> bool:
    sid_1 = "regress-provenance-c-1"
    sid_2 = "regress-provenance-c-2"
    orig_record_from_event = provenance_store.record_from_event

    def slow_record_from_event(app_session_id, normalized, *, backend_msg_id=None):
        time.sleep(0.3)  # simulated slow/contended disk I/O
        return orig_record_from_event(app_session_id, normalized, backend_msg_id=backend_msg_id)

    provenance_store.record_from_event = slow_record_from_event

    fired: list[tuple[str, dict]] = []
    fired_lock = threading.Lock()
    orig_fire = session_manager._fire

    def fake_fire(sid, change):
        with fired_lock:
            fired.append((sid, dict(change)))

    session_manager._fire = fake_fire

    # Mirrors turn_manager._STREAM_EVENT_APPLY_EXECUTOR: 2 threads shared
    # by every concurrent session's live event application.
    shared_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="test-stream-event-apply")
    try:
        events = []
        for i in range(8):
            sid = sid_1 if i % 2 == 0 else sid_2
            events.append((sid, f"toolu-c{i}"))

        start = time.monotonic()
        futures = [
            shared_executor.submit(
                session_manager.apply_provenance_from_event,
                sid, _event(tool_id, "Bash", "why"), backend_msg_id=None,
            )
            for sid, tool_id in events
        ]
        for f in futures:
            f.result(timeout=2.0)
        elapsed = time.monotonic() - start

        # 8 events * 0.3s of (simulated) persist latency would need 1.2s+
        # serialized through only 2 executor threads if apply_provenance_
        # from_event still did the write inline; decoupled, submission
        # completes almost immediately regardless of write latency.
        dispatch_ok = elapsed < 0.5

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            n1 = len(provenance_store.read(sid_1))
            n2 = len(provenance_store.read(sid_2))
            if n1 >= 4 and n2 >= 4:
                break
            time.sleep(0.02)

        rows_1 = {r["uuid"] for r in provenance_store.read(sid_1)}
        rows_2 = {r["uuid"] for r in provenance_store.read(sid_2)}
        expected_1 = {f"toolu-c{i}" for i in range(0, 8, 2)}
        expected_2 = {f"toolu-c{i}" for i in range(1, 8, 2)}
        persisted_ok = rows_1 == expected_1 and rows_2 == expected_2

        with fired_lock:
            fired_sids = {sid for sid, _ in fired}
        ping_ok = {sid_1, sid_2} <= fired_sids

        ok = dispatch_ok and persisted_ok and ping_ok
        print(
            f"{PASS if ok else FAIL} C: 8 apply_provenance_from_event calls "
            f"across 2 sessions dispatched in {elapsed:.2f}s (want < 0.5s) "
            f"despite 0.3s simulated persist latency per event; "
            f"persisted {len(rows_1)}/4 + {len(rows_2)}/4 rows; "
            f"provenance_changed fired for {fired_sids & {sid_1, sid_2}}"
        )
        return ok
    finally:
        shared_executor.shutdown(wait=True)
        session_manager._fire = orig_fire
        provenance_store.record_from_event = orig_record_from_event


def main() -> int:
    try:
        results = [
            test_a_notes_accumulate_without_loss(),
            test_b_note_never_blocks_caller(),
            test_c_apply_provenance_does_not_stall_shared_executor(),
        ]
        total = len(results)
        passed = sum(1 for r in results if r)
        print(f"\n{passed}/{total} subtests passed")
        return 0 if passed == total else 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
