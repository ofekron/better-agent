"""Regression test: a cold `_scan_max_seq()` full-file scan must not
block concurrent `_ingest_impl()` appends on the SAME root for the
scan's duration.

Before the fix, `_scan_max_seq` read+parsed the ENTIRE events.jsonl
file while holding `self._locks[root_id]` -- the SAME per-root lock
`_ingest_impl` needs for every hot-path append (`ingest()`). A slow or
cold full scan (e.g. a REST snapshot hitting a large session before
any local cache is warm) blocked live event ingestion for that same
root for the whole scan duration -- the exact bug shape already fixed
for the cursor-persistence path (see `cursor_ledger_worker.py`) and for
`spawn_ledger.all_sids()`.

This test forces a cold `max_seq_by_sid()` scan, injects an artificial
delay into the raw parse step (simulating a slow/large scan), and
asserts a concurrent `ingest()` call for the SAME root completes
quickly instead of waiting for the scan to finish. FAILS on the
pre-fix code (ingest blocks for the injected delay, since the scan
holds the per-root lock throughout); PASSES once the scan's expensive
parse runs with the per-root lock released.

Run with:
    cd backend && .venv/bin/python scripts/test_event_ingester_scan_lock_contention.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-scan-lock-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from event_ingester import EventIngester  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"

ROOT = "root-scan-lock-test"
SID = "sid-scan-lock-test"

_SCAN_DELAY_SECONDS = 1.0
_INGEST_BUDGET_SECONDS = 0.3
_INITIAL_EVENTS = 20


def _run() -> bool:
    results: list[tuple[str, bool, str]] = []
    ing = EventIngester()

    for i in range(_INITIAL_EVENTS):
        ing.ingest(
            ROOT, sid=SID, event_type="agent_message",
            data={"uuid": f"u-{i}", "type": "assistant",
                  "message": {"content": [{"type": "text", "text": f"m{i}"}]}},
            source="test", msg_id=f"msg-{i}",
        )

    # Force the next `max_seq_by_sid()` call to hit the cold-scan
    # fallback, mimicking a REST read that reaches this root before any
    # local cache has warmed (e.g. right after a backend restart).
    ing._max_seq_by_sid.pop(ROOT, None)
    ing._render_seq_by_sid.pop(ROOT, None)

    # Simulate a slow/large full-file scan by delaying the JSON parse of
    # the FIRST line the scan touches -- implementation-agnostic (works
    # whether the scan is one inline read loop or a separate parse
    # helper), and only fires once so the test runtime stays bounded.
    scan_entered = threading.Event()
    real_loads = json.loads
    triggered = {"done": False}

    def slow_loads(s, *a, **kw):
        if not triggered["done"] and '"u-0"' in s:
            triggered["done"] = True
            scan_entered.set()
            time.sleep(_SCAN_DELAY_SECONDS)
        return real_loads(s, *a, **kw)

    json.loads = slow_loads
    try:
        scan_result: dict = {}

        def do_scan() -> None:
            scan_result["value"] = ing.max_seq_by_sid(ROOT)

        scan_thread = threading.Thread(target=do_scan)
        scan_thread.start()
        entered = scan_entered.wait(timeout=2)
        results.append((
            "scan actually started (test setup sanity check)",
            entered,
            "scan thread never reached the parse step",
        ))

        ingest_started = time.perf_counter()
        seq = ing.ingest(
            ROOT, sid=SID, event_type="agent_message",
            data={"uuid": "u-concurrent", "type": "assistant",
                  "message": {"content": [{"type": "text", "text": "concurrent"}]}},
            source="test", msg_id="msg-concurrent",
        )
        ingest_elapsed = time.perf_counter() - ingest_started

        scan_thread.join(timeout=_SCAN_DELAY_SECONDS + 5)
        results.append((
            "concurrent ingest() for the same root does not wait for the scan",
            ingest_elapsed < _INGEST_BUDGET_SECONDS,
            f"ingest() took {ingest_elapsed:.3f}s (budget {_INGEST_BUDGET_SECONDS}s) "
            f"-- looks blocked behind the {_SCAN_DELAY_SECONDS}s scan",
        ))
        results.append((
            "ingest() during the scan actually wrote a new seq",
            seq == _INITIAL_EVENTS + 1,
            f"seq={seq}, expected {_INITIAL_EVENTS + 1}",
        ))
        results.append((
            "scan thread completed",
            not scan_thread.is_alive(),
            "scan thread is still running",
        ))
    finally:
        json.loads = real_loads

    # Correctness: whatever the scan (or the racing concurrent ingest)
    # installed, the view after everything settles must include the
    # concurrent event -- never silently regress/drop it.
    final_max = ing.max_seq_by_sid(ROOT)
    results.append((
        "post-race view is never stale (reflects all events, including the concurrent one)",
        final_max.get(SID) == _INITIAL_EVENTS + 1,
        f"max_seq_by_sid={final_max}",
    ))

    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, msg in results:
        tag = PASS if ok else FAIL
        print(f"  {tag} {name}{'' if ok else ' — ' + msg}")
    print(f"\n{passed}/{len(results)} checks passed")
    return passed == len(results)


def main() -> int:
    try:
        ok = _run()
        return 0 if ok else 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
