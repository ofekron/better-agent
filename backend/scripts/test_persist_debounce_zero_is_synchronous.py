"""Regression for debounced persist test teardown races."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-debounce0-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import session_store  # noqa: E402
import session_manager as sm_mod  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _create_then_mutate() -> None:
    """Create a root, then drive TWO back-to-back `_run`-based mutations.
    The first is the leading edge (arms the window); the second lands
    inside it — so with a real debounce it queues a deferred Timer."""
    sess = session_manager.create(
        name="t", model="sonnet", cwd="/tmp/debounce0",
        orchestration_mode="native", source="cli",
    )
    sid = sess["id"]
    for i in range(2):
        session_manager.append_user_msg(sid, {
            "id": f"u{i}", "role": "user", "content": "hi", "events": [],
            "timestamp": "2026-06-05T00:00:00", "isStreaming": False,
        })


def _reset() -> None:
    session_store._fork_index.clear()
    session_store._index_loaded = False
    session_store._summary_index.clear()
    session_store._summary_index_loaded = False
    session_manager._roots.clear()
    session_manager._node_root_id.clear()
    session_manager._root_locks.clear()
    session_manager._batches.clear()
    with sm_mod._persist_state_lock:
        for t in sm_mod._persist_timer.values():
            t.cancel()
        sm_mod._persist_timer.clear()
        sm_mod._persist_pending.clear()
        sm_mod._persist_last_at.clear()
        sm_mod._persist_inflight.clear()


def _wait_for_persist_drain(timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with sm_mod._persist_state_lock:
            drained = (
                not sm_mod._persist_timer
                and not sm_mod._persist_pending
                and not sm_mod._persist_inflight
            )
        if drained:
            return True
        time.sleep(0.005)
    with sm_mod._persist_state_lock:
        return (
            not sm_mod._persist_timer
            and not sm_mod._persist_pending
            and not sm_mod._persist_inflight
        )


def _run() -> bool:
    results: list[tuple[str, bool, str]] = []
    original = sm_mod.PERSIST_DEBOUNCE_S

    try:
        # ── A. real debounce coalesces behind one pending/in-flight writer
        sm_mod.PERSIST_DEBOUNCE_S = 0.050
        _reset()
        _create_then_mutate()
        with sm_mod._persist_state_lock:
            queued = bool(
                sm_mod._persist_timer
                or sm_mod._persist_pending
                or sm_mod._persist_inflight
            )
        results.append((
            "real debounce queues async persist work",
            queued,
            "no timer/pending/inflight persist work observed",
        ))
        drained_real = _wait_for_persist_drain()
        results.append((
            "real debounce drains queued persist work",
            drained_real,
            "persist work did not drain",
        ))

        # ── B. debounce 0 still writes outside the root lock, but drains
        # promptly and leaves no daemon work for test teardown.
        sm_mod.PERSIST_DEBOUNCE_S = 0.0
        _reset()
        writes = {"n": 0}
        real_write = session_store.write_session_full

        def _counting_write(*a, **k):
            writes["n"] += 1
            return real_write(*a, **k)

        session_store.write_session_full = _counting_write
        try:
            _create_then_mutate()
        finally:
            session_store.write_session_full = real_write

        drained_zero = _wait_for_persist_drain()
        results.append((
            "debounce=0 drains async persist work before teardown",
            drained_zero,
            f"timer={list(sm_mod._persist_timer)} pending={list(sm_mod._persist_pending)} "
            f"inflight={list(sm_mod._persist_inflight)}",
        ))
        results.append((
            "debounce=0 writes every mutation burst",
            writes["n"] >= 2,
            f"only {writes['n']} writes (expected >=2)",
        ))
    finally:
        sm_mod.PERSIST_DEBOUNCE_S = original
        _reset()

    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        tag = PASS if ok else FAIL
        print(f"  {tag} {name}{'' if ok else ' — ' + detail}")
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
