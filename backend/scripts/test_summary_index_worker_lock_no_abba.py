"""Deterministic regression for the cross-thread ABBA deadlock between
`_summary_index_lock` and `worker_store._lock_for(cwd)`.

  - Order A (the bug): `_ensure_summary_index` held `_summary_index_lock`
    while calling `_build_summary_for_root` -> `worker_store.list_workers`
    -> `_lock_for(cwd)`.
  - Order B: `worker_store._write` (under `_lock_for(cwd)`) ->
    `_refresh_summaries_for_cwd_from` -> `_summary_index_lock`.

Two threads on the same cwd in opposite order = deadlock. The fix runs
the build under `_summary_build_lock` (not `_summary_index_lock`), so
order A no longer exists.

The interleave is FORCED with Events (no sleeps): a worker thread holds
`_lock_for(cwd)` and parks before taking `_summary_index_lock`; the build
thread is signalled (via a `_build_summary_for_root` monkeypatch) the
moment it enters the build; then the worker is released to grab
`_summary_index_lock`. Pre-fix the build holds `_summary_index_lock` at
that point -> guaranteed deadlock; post-fix it holds only
`_summary_build_lock` -> no cycle.

Run with:
    cd backend && .venv/bin/python scripts/test_summary_index_worker_lock_no_abba.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-abba-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import session_store  # noqa: E402
from paths import ba_home  # noqa: E402
from stores import worker_store  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"

CWD = "/tmp/test-abba"


def _write_v8_session(sid: str) -> None:
    d = ba_home() / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    rec = {
        "_schema_version": 8,
        "id": sid,
        "name": sid,
        "model": "sonnet",
        "cwd": CWD,
        "orchestration_mode": "native",
        "kind": "user",
        "parent_session_id": None,
        "forks": [],
        "messages": [
            {"id": "m1", "role": "assistant", "content": "x", "events": [], "seq": 1},
        ],
        "next_seq": 2,
        "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00",
        "source": "cli",
    }
    with open(d / f"{sid}.json", "w") as f:
        json.dump(rec, f)


def _run() -> bool:
    # v8 session, no .summary.json -> Pass-2 -> _build_summary_for_root ->
    # list_workers(CWD) -> _lock_for(CWD). Schema 8 so it is NOT dirty:
    # isolates the ABBA from the self-deadlock path.
    _write_v8_session("abba-session")

    x_in_build = threading.Event()    # build reached _build_summary_for_root
    go = threading.Event()            # release worker to grab _summary_index_lock
    w_holds_lockfor = threading.Event()

    _orig = session_store._build_summary_for_root

    def _patched(root):
        x_in_build.set()
        return _orig(root)

    session_store._build_summary_for_root = _patched

    def worker():
        # Order B: _lock_for(cwd) THEN _summary_index_lock.
        with worker_store._lock_for(CWD):
            w_holds_lockfor.set()
            go.wait(timeout=15)
            with session_store._summary_index_lock:
                pass

    def builder():
        session_store.list_sessions()

    wt = threading.Thread(target=worker, daemon=True)
    xt = threading.Thread(target=builder, daemon=True)

    try:
        wt.start()
        assert w_holds_lockfor.wait(timeout=5), "worker never acquired _lock_for"
        xt.start()
        assert x_in_build.wait(timeout=5), \
            "builder never reached _build_summary_for_root"
        # Close the cycle: pre-fix the builder holds _summary_index_lock now.
        go.set()
        xt.join(timeout=10)
        wt.join(timeout=10)
        no_deadlock = (not xt.is_alive()) and (not wt.is_alive())
    finally:
        session_store._build_summary_for_root = _orig

    results = [(
        "_ensure_summary_index vs worker _write: no ABBA deadlock",
        no_deadlock,
        f"builder_alive={xt.is_alive()} worker_alive={wt.is_alive()}",
    )]
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, msg in results:
        print(f"  {PASS if ok else FAIL} {name}{'' if ok else ' — ' + msg}")
    print(f"\n{passed}/{len(results)} checks passed")
    return passed == len(results)


def main() -> int:
    try:
        return 0 if _run() else 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
