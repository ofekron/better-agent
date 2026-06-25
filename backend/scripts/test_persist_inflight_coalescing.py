from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-persist-coalesce-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import session_manager as sm_mod  # noqa: E402
import session_store  # noqa: E402
from session_manager import manager  # noqa: E402


PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _reset() -> None:
    session_store._fork_index.clear()
    session_store._root_forks.clear()
    session_store._root_index_signatures.clear()
    session_store._index_loaded = False
    session_store._summary_index.clear()
    session_store._summary_index_loaded = False
    manager._roots.clear()
    manager._node_root_id.clear()
    manager._root_locks.clear()
    manager._batches.clear()
    with sm_mod._persist_state_lock:
        sm_mod._persist_deadlines.clear()
        sm_mod._persist_deadline_heap.clear()
        sm_mod._persist_pending.clear()
        sm_mod._persist_last_at.clear()
        sm_mod._persist_inflight.clear()
        sm_mod._persist_state_changed.notify_all()


def _wait_until(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def main() -> int:
    original_debounce = sm_mod.PERSIST_DEBOUNCE_S
    original_write = session_store.write_session_full
    _reset()
    started = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    stats = {"writes": 0, "active": 0, "max_active": 0}

    def slow_write(*args, **kwargs):
        with lock:
            stats["writes"] += 1
            stats["active"] += 1
            stats["max_active"] = max(stats["max_active"], stats["active"])
            write_number = stats["writes"]
        if write_number == 1:
            started.set()
            release.wait(timeout=2.0)
        try:
            return original_write(*args, **kwargs)
        finally:
            with lock:
                stats["active"] -= 1

    try:
        sm_mod.PERSIST_DEBOUNCE_S = 0.050
        sess = manager.create(
            id="persist-coalesce-root",
            name="persist coalesce",
            model="sonnet",
            cwd="/tmp/persist-coalesce",
            orchestration_mode="native",
            source="cli",
        )
        sid = sess["id"]
        session_store.write_session_full = slow_write
        manager.append_user_msg(sid, {
            "id": "u0",
            "role": "user",
            "content": "first",
            "timestamp": "2026-06-24T00:00:00",
            "events": [],
        })
        if not started.wait(timeout=2.0):
            print(f"{FAIL} first persist did not start")
            return 1
        for i in range(1, 31):
            manager.append_user_msg(sid, {
                "id": f"u{i}",
                "role": "user",
                "content": f"msg {i}",
                "timestamp": "2026-06-24T00:00:00",
                "events": [],
            })
        with sm_mod._persist_state_lock:
            pending_during_write = sid in sm_mod._persist_pending
            inflight_during_write = sid in sm_mod._persist_inflight
            deadlines_during_write = len(sm_mod._persist_deadlines)
        release.set()
        flushed = _wait_until(lambda: stats["writes"] >= 2, timeout=2.0)
        drained = _wait_until(
            lambda: not sm_mod._persist_pending and not sm_mod._persist_inflight,
            timeout=2.0,
        )
        ok = (
            pending_during_write
            and inflight_during_write
            and deadlines_during_write == 0
            and flushed
            and drained
            and stats["max_active"] == 1
            and stats["writes"] <= 3
        )
        detail = (
            f"pending={pending_during_write} inflight={inflight_during_write} "
            f"deadlines={deadlines_during_write} flushed={flushed} drained={drained} "
            f"writes={stats['writes']} max_active={stats['max_active']}"
        )
        print(f"{PASS if ok else FAIL} persist coalesces while write is in-flight — {detail}")
        return 0 if ok else 1
    finally:
        release.set()
        session_store.write_session_full = original_write
        sm_mod.PERSIST_DEBOUNCE_S = original_debounce
        _reset()
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
