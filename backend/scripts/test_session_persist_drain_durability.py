"""Regression tests for pending-persist durability on write failure.

Covers:
  * _load_root pre-flush drain failure: the popped pending ref stays the
    authoritative in-memory root (the stale disk copy is NOT adopted) and
    the flush is re-queued for retry.
  * _tail_persist write failure re-queues the pending ref.
  * flush_pending_persists write failure re-queues and terminates (no hang).
  * write_session_full(root, preserve_projection_fields=True) works end to
    end (locks the session_queue_projection.get_many overlay dependency).

Run with:
    cd backend && .venv/bin/python scripts/test_session_persist_drain_durability.py
"""
from __future__ import annotations

import os
import shutil
import sys

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-persist-drain-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import session_manager as sm_mod  # noqa: E402
import session_store  # noqa: E402
from session_manager import manager as sm  # noqa: E402

OK = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"


class _FailingWrites:
    """Monkeypatch session_store.write_session_full to raise `times` times."""

    def __init__(self, times: int) -> None:
        self.remaining = times
        self.calls = 0
        self._orig = session_store.write_session_full

    def __enter__(self):
        def failing(*args, **kwargs):
            self.calls += 1
            if self.remaining != 0:
                if self.remaining > 0:
                    self.remaining -= 1
                raise OSError("simulated write failure")
            return self._orig(*args, **kwargs)

        session_store.write_session_full = failing
        return self

    def __exit__(self, *exc):
        session_store.write_session_full = self._orig
        return False


def _fresh_session(name: str) -> str:
    sess = sm.create(
        name=name, model="sonnet", cwd="/tmp/test-drain",
        orchestration_mode="native", source="cli",
    )
    sm.flush_pending_persists()
    return sess["id"]


def _queue_pending(sid: str, new_name: str) -> dict:
    root = sm.get_ref(sid)
    root["name"] = new_name
    with sm_mod._persist_state_lock:
        sm_mod._persist_pending[sid] = root
    return root


def test_load_root_drain_failure_keeps_pending() -> bool:
    sid = _fresh_session("drain")
    root = _queue_pending(sid, "drain-newer")
    sm._roots.pop(sid, None)
    with _FailingWrites(times=1):
        loaded = sm._load_root(sid)
    kept_newest = loaded is not None and loaded.get("name") == "drain-newer"
    requeued = sm_mod._persist_pending.get(sid) is root
    sm.flush_pending_persists()
    disk = session_store.get_root_tree(sid)
    durable = disk is not None and disk.get("name") == "drain-newer"
    drained = sid not in sm_mod._persist_pending
    ok = kept_newest and requeued and durable and drained
    print(f"{OK if ok else FAIL} _load_root drain failure keeps pending "
          f"(kept_newest={kept_newest}, requeued={requeued}, durable={durable})")
    return ok


def test_tail_persist_failure_requeues() -> bool:
    sid = _fresh_session("tail")
    root = _queue_pending(sid, "tail-newer")
    with _FailingWrites(times=1):
        sm._tail_persist(sid)
    requeued = sm_mod._persist_pending.get(sid) is root
    sm.flush_pending_persists()
    disk = session_store.get_root_tree(sid)
    durable = disk is not None and disk.get("name") == "tail-newer"
    ok = requeued and durable
    print(f"{OK if ok else FAIL} _tail_persist failure re-queues pending "
          f"(requeued={requeued}, durable={durable})")
    return ok


def test_flush_pending_failure_requeues_and_terminates() -> bool:
    sid = _fresh_session("flush")
    root = _queue_pending(sid, "flush-newer")
    with _FailingWrites(times=-1):
        sm.flush_pending_persists()  # must return despite persistent failure
    requeued = sm_mod._persist_pending.get(sid) is root
    sm.flush_pending_persists()
    disk = session_store.get_root_tree(sid)
    durable = disk is not None and disk.get("name") == "flush-newer"
    ok = requeued and durable
    print(f"{OK if ok else FAIL} flush_pending_persists failure re-queues + terminates "
          f"(requeued={requeued}, durable={durable})")
    return ok


def test_write_full_with_projection_overlay() -> bool:
    sid = _fresh_session("overlay")
    root = session_store.get_root_tree(sid)
    session_store.write_session_full(root, preserve_projection_fields=True)
    disk = session_store.get_root_tree(sid)
    ok = disk is not None and disk.get("id") == sid
    print(f"{OK if ok else FAIL} write_session_full(preserve_projection_fields=True) works "
          f"(loaded={disk is not None})")
    return ok


def main_run() -> int:
    tests = [
        test_load_root_drain_failure_keeps_pending,
        test_tail_persist_failure_requeues,
        test_flush_pending_failure_requeues_and_terminates,
        test_write_full_with_projection_overlay,
    ]
    results = []
    for fn in tests:
        try:
            results.append(fn())
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"{FAIL} {fn.__name__} raised: {e}")
            results.append(False)
    n_pass = sum(1 for r in results if r)
    print(f"\n{n_pass}/{len(results)} persist-drain durability tests passed")
    return 0 if n_pass == len(results) else 1


if __name__ == "__main__":
    try:
        sys.exit(main_run())
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
