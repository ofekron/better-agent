"""Locks the contract that `session_store.list_sessions()` (which
calls `session_manager.flush_pending_persists()`) does NOT block on
a per-root lock held by another thread (e.g. recovery's cold-load
hydration inside `_load_root`).

Pre-fix the log showed a 6971 ms `/api/sessions` GET after backend
restart while 12 in-flight runs were hydrating events.jsonl streams.
`flush_pending_persists` iterated `_persist_pending` and did
`with self._lock_for_root(rid)` — which waited behind every held
per-root lock.

Fix at `backend/session_manager.py:flush_pending_persists` switched
to `acquire(blocking=False)` and skips contended rids. Skipped
`_persist_pending[rid]` entries stay queued for the next caller.

This test:

  1. Seeds `_persist_pending[rid]` with a fake session dict.
  2. Acquires `_root_locks[rid]` from a sibling thread (simulates
     recovery's per-root lock held during hydration).
  3. Calls `session_store.list_sessions()` 5× and asserts every call
     returns within 500 ms (well under the 7-second pre-fix wait;
     the slack accommodates CI flakiness).
  4. Asserts `_persist_pending[rid]` still has the entry (proving
     the trylock skip-path fired, not the blocking acquire).
  5. Releases the sibling lock, calls flush again, asserts
     `_persist_pending[rid]` is gone (recovery-side cleanup works).

Run with:
    cd backend && .venv/bin/python scripts/test_list_sessions_non_blocking_when_lock_held.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
import time

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-flush-nonblock-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import session_manager as sm_module  # noqa: E402
import session_store  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _build_real_session() -> str:
    """Create a real session via session_manager so the `_root_locks`
    entry is created and `_summary_index` is consistent. Returns its
    rid (==sid for a root)."""
    sess = session_manager.create(
        name="t", model="sonnet", cwd="/tmp/flush-test",
        orchestration_mode="native", source="cli",
    )
    sid = sess["id"]
    # Flush whatever just got queued so we start from a known state.
    session_manager.flush_pending_persists()
    return sid


def _seed_pending(rid: str) -> dict:
    """Put a fake `_persist_pending` entry for `rid` so
    `flush_pending_persists` has something to drain."""
    sess = session_manager._roots.get(rid)
    assert sess is not None, "root must be cached for the test"
    with sm_module._persist_state_lock:
        sm_module._persist_pending[rid] = sess
    return sess


def _run() -> bool:
    results: list[tuple[str, bool, str]] = []

    sid = _build_real_session()
    rid = session_manager._root_id_for(sid)
    assert rid is not None
    lock = session_manager._root_locks.get(rid)
    assert lock is not None, "per-root lock should exist after create"

    # Hold the per-root lock from a sibling thread for 3 seconds.
    release_event = threading.Event()
    acquired_event = threading.Event()

    def _holder():
        with lock:
            acquired_event.set()
            release_event.wait(timeout=5.0)

    holder = threading.Thread(target=_holder, daemon=True)
    holder.start()
    assert acquired_event.wait(timeout=2.0), "holder thread couldn't acquire lock"

    # Now seed a pending entry; flush_pending_persists MUST skip it.
    _seed_pending(rid)
    assert rid in sm_module._persist_pending, "pending entry not seeded"

    # 5× list_sessions calls — each should return well under 500 ms.
    latencies_ms: list[float] = []
    for _ in range(5):
        t0 = time.perf_counter()
        out = session_store.list_sessions()
        latencies_ms.append((time.perf_counter() - t0) * 1000.0)
        assert isinstance(out, list)
    max_lat = max(latencies_ms)
    results.append(
        ("5× list_sessions p_max < 500 ms while per-root lock held",
         max_lat < 500.0,
         f"got max={max_lat:.1f}ms samples={[f'{x:.1f}' for x in latencies_ms]}"))

    # The pending entry must still be there — the skip-path ran.
    still_pending = rid in sm_module._persist_pending
    results.append(
        ("pending entry preserved when lock held (skip-path fired)",
         still_pending, "entry was drained somehow"))

    # Now release the holder, call flush again — entry should drain.
    release_event.set()
    holder.join(timeout=2.0)
    session_manager.flush_pending_persists()
    results.append(
        ("pending entry drained after lock released",
         rid not in sm_module._persist_pending,
         "entry survived flush after lock release"))

    # Also verify the trylock pattern doesn't accidentally hammer-loop
    # the contended rid — the skip-path should be O(1) per call, not
    # spin. The 5 × <500 ms above already establishes this.

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
