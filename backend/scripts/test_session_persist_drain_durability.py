"""Regression tests for pending-persist durability on write failure.

Covers:
  * _load_root pre-flush drain failure: the popped pending ref stays the
    authoritative in-memory root (the stale disk copy is NOT adopted) and
    the flush is re-queued for retry.
  * _tail_persist write/copy failure preserves the newest live ref without
    an autonomous retry storm.
  * Concurrent mutation survives a failed tail write.
  * Stale callbacks cannot alter a replacement claim after state reset.
  * flush_root_persist copy failure releases its claim and remains retryable.
  * flush_pending_persists attempts each root once, re-queues, and returns.
  * Deletion cancels failure requeue for an already-claimed tail write.
  * write_session_full(root, preserve_projection_fields=True) works end to
    end (locks the session_queue_projection.get_many overlay dependency).

Run with:
    cd backend && .venv/bin/python scripts/test_session_persist_drain_durability.py
"""
from __future__ import annotations

import os
import shutil
import sys
import threading

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


class _BlockedFailingWrite:
    def __init__(self) -> None:
        self.entered = threading.Event()
        self._release = threading.Event()
        self._original = session_store.write_session_full
        self.thread: threading.Thread | None = None

    def __enter__(self):
        def blocked_failure(*args, **kwargs):
            self.entered.set()
            self._release.wait()
            raise OSError("simulated blocked write failure")

        session_store.write_session_full = blocked_failure
        return self

    def start(self, target, *args) -> threading.Thread:
        if self.thread is not None:
            raise RuntimeError("blocked failing write already started")
        self.thread = threading.Thread(target=target, args=args)
        self.thread.start()
        assert self.entered.wait(timeout=2.0)
        return self.thread

    def release_and_join(self) -> bool:
        self._release.set()
        if self.thread is None:
            return True
        self.thread.join(timeout=2.0)
        return not self.thread.is_alive()

    def __exit__(self, exc_type, exc, traceback):
        try:
            stopped = self.release_and_join()
        finally:
            session_store.write_session_full = self._original
        if not stopped:
            message = "blocked failing write thread did not stop"
            if exc is not None:
                exc.add_note(message)
                return False
            raise AssertionError(message)
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


def test_blocked_failure_helper_preserves_body_error() -> bool:
    original = session_store.write_session_full
    blocked = _BlockedFailingWrite()
    try:
        with blocked:
            blocked.start(session_store.write_session_full)
            raise ValueError("scenario failure")
    except ValueError as error:
        preserved = str(error) == "scenario failure"
    else:
        preserved = False
    restored = session_store.write_session_full is original
    idempotent = blocked.release_and_join()
    ok = preserved and restored and idempotent
    print(f"{OK if ok else FAIL} blocked failure helper preserves body error "
          f"(preserved={preserved}, restored={restored}, "
          f"idempotent={idempotent})")
    return ok


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


def test_tail_failure_preserves_concurrent_mutation() -> bool:
    sid = _fresh_session("tail-concurrent")
    root = _queue_pending(sid, "tail-attempted")
    with _BlockedFailingWrite() as blocked:
        writer = blocked.start(sm._tail_persist, sid)
        with sm._lock_for_root(sid):
            root["name"] = "tail-concurrent-newest"
            sm._persist_root(sid, bump=False)
        blocked.release_and_join()

    pending = sm_mod._persist_pending.get(sid)
    preserved = pending is root and pending.get("name") == "tail-concurrent-newest"
    sm.flush_pending_persists()
    disk = session_store.get_root_tree(sid)
    durable = disk is not None and disk.get("name") == "tail-concurrent-newest"
    ok = not writer.is_alive() and preserved and durable
    print(f"{OK if ok else FAIL} failed tail preserves concurrent mutation "
          f"(preserved={preserved}, durable={durable})")
    return ok


def test_tail_copy_failure_requeues_without_retry() -> bool:
    sid = _fresh_session("tail-copy")
    root = _queue_pending(sid, "tail-copy-newer")
    coordinator = sm._persist_coordinator
    real_copy = sm._root_repository.copy_persistable_root
    calls = 0

    def failing_copy(_root):
        nonlocal calls
        calls += 1
        raise OSError("simulated persistable-copy failure")

    sm._root_repository.copy_persistable_root = failing_copy
    try:
        sm._tail_persist(sid)
    finally:
        sm._root_repository.copy_persistable_root = real_copy

    with coordinator.lock:
        requeued = coordinator.pending.get(sid) is root
        idle = sid not in coordinator.inflight
        no_retry = sid not in coordinator.deadlines
    sm.flush_pending_persists()
    disk = session_store.get_root_tree(sid)
    durable = disk is not None and disk.get("name") == "tail-copy-newer"
    ok = calls == 1 and requeued and idle and no_retry and durable
    print(f"{OK if ok else FAIL} copy failure re-queues without retry storm "
          f"(calls={calls}, requeued={requeued}, idle={idle}, no_retry={no_retry})")
    return ok


def test_stale_tail_cannot_mutate_new_claim_generation() -> bool:
    sid = _fresh_session("tail-stale")
    _queue_pending(sid, "tail-stale-old")
    coordinator = sm._persist_coordinator
    new_root = {"id": sid, "name": "tail-stale-new"}
    new_claim = object()
    with _BlockedFailingWrite() as blocked:
        writer = blocked.start(sm._tail_persist, sid)
        with coordinator.lock:
            coordinator.clear_unlocked()
            coordinator.pending[sid] = new_root
            coordinator.inflight.add(sid)
            coordinator.inflight_claims[sid] = new_claim
        blocked.release_and_join()

    with coordinator.lock:
        untouched = (
            coordinator.pending.get(sid) is new_root
            and sid in coordinator.inflight
            and coordinator.inflight_claims.get(sid) is new_claim
        )
        coordinator.pending.pop(sid, None)
        coordinator.inflight.discard(sid)
        coordinator.inflight_claims.pop(sid, None)
        coordinator.cancelled_claims.clear()
    ok = not writer.is_alive() and untouched
    print(f"{OK if ok else FAIL} stale tail cannot mutate new claim generation "
          f"(untouched={untouched})")
    return ok


def test_flush_root_copy_failure_requeues_and_recovers() -> bool:
    sid = _fresh_session("flush-root-copy")
    root = _queue_pending(sid, "flush-root-copy-newer")
    coordinator = sm._persist_coordinator
    real_copy = sm._root_repository.copy_persistable_root

    def failing_copy(_root):
        raise OSError("simulated flush-root copy failure")

    sm._root_repository.copy_persistable_root = failing_copy
    try:
        try:
            sm.flush_root_persist(sid)
            raised = False
        except OSError:
            raised = True
    finally:
        sm._root_repository.copy_persistable_root = real_copy

    with coordinator.lock:
        requeued = coordinator.pending.get(sid) is root
        cleared = (
            sid not in coordinator.inflight
            and sid not in coordinator.inflight_claims
            and not coordinator.cancelled_claims
        )
    sm.flush_root_persist(sid)
    disk = session_store.get_root_tree(sid)
    durable = disk is not None and disk.get("name") == "flush-root-copy-newer"
    ok = raised and requeued and cleared and durable
    print(f"{OK if ok else FAIL} flush_root copy failure re-queues and recovers "
          f"(raised={raised}, requeued={requeued}, cleared={cleared}, "
          f"durable={durable})")
    return ok


def test_flush_pending_failure_requeues_and_terminates() -> bool:
    first_sid = _fresh_session("flush-first")
    second_sid = _fresh_session("flush-second")
    first_root = _queue_pending(first_sid, "flush-first-newer")
    second_root = _queue_pending(second_sid, "flush-second-newer")
    with _FailingWrites(times=-1) as failures:
        sm.flush_pending_persists()
    attempted_once = failures.calls == 2
    requeued = (
        sm_mod._persist_pending.get(first_sid) is first_root
        and sm_mod._persist_pending.get(second_sid) is second_root
    )
    sm.flush_pending_persists()
    first_disk = session_store.get_root_tree(first_sid)
    second_disk = session_store.get_root_tree(second_sid)
    durable = (
        first_disk is not None
        and first_disk.get("name") == "flush-first-newer"
        and second_disk is not None
        and second_disk.get("name") == "flush-second-newer"
    )
    ok = attempted_once and requeued and durable
    print(f"{OK if ok else FAIL} flush_pending_persists failure re-queues + terminates "
          f"(attempted_once={attempted_once}, requeued={requeued}, durable={durable})")
    return ok


def test_delete_cancels_failed_tail_requeue() -> bool:
    sid = _fresh_session("delete-race")
    _queue_pending(sid, "delete-race-newer")
    with _BlockedFailingWrite() as blocked:
        writer = blocked.start(sm._tail_persist, sid)
        deleted = sm.delete(sid)
        blocked.release_and_join()

    with sm_mod._persist_state_lock:
        not_requeued = sid not in sm_mod._persist_pending
        cancellation_consumed = not sm._persist_coordinator.cancelled_claims
    absent = session_store.get_root_tree(sid) is None
    ok = (
        deleted
        and not writer.is_alive()
        and not_requeued
        and cancellation_consumed
        and absent
    )
    print(f"{OK if ok else FAIL} delete cancels failed tail requeue "
          f"(deleted={deleted}, not_requeued={not_requeued}, "
          f"cancellation_consumed={cancellation_consumed}, absent={absent})")
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
        test_blocked_failure_helper_preserves_body_error,
        test_load_root_drain_failure_keeps_pending,
        test_tail_persist_failure_requeues,
        test_tail_failure_preserves_concurrent_mutation,
        test_tail_copy_failure_requeues_without_retry,
        test_stale_tail_cannot_mutate_new_claim_generation,
        test_flush_root_copy_failure_requeues_and_recovers,
        test_flush_pending_failure_requeues_and_terminates,
        test_delete_cancels_failed_tail_requeue,
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
