from __future__ import annotations

import shutil
import threading
import time

import _test_home

_TEST_HOME = _test_home.isolate("ba-test-persist-lifecycle-")

import session_manager as sm  # noqa: E402


def _clear_root(coordinator, root_id: str) -> None:
    with coordinator.changed:
        coordinator.pending.pop(root_id, None)
        coordinator.cancel_deadline_unlocked(root_id)
        coordinator.inflight.discard(root_id)
        coordinator.inflight_claims.pop(root_id, None)
        coordinator.accepted_roots.discard(root_id)
        coordinator.changed.notify_all()


def _close_for_cleanup(coordinator) -> None:
    with coordinator.changed:
        coordinator.clear_unlocked()
    coordinator.close(lambda: None, timeout=2.0)
    assert coordinator.state == "closed"
    assert coordinator._thread is None or not coordinator._thread.is_alive()


def _test_never_started_and_atomic_rejection() -> None:
    coordinator = sm._SessionPersistCoordinator(lambda _root_id: None)
    drains = 0

    def drain() -> None:
        nonlocal drains
        drains += 1

    coordinator.close(drain, timeout=0.2)
    assert drains == 1
    assert coordinator.state == "closed"
    assert coordinator._thread is None
    with coordinator.changed:
        before = (
            dict(coordinator.pending),
            dict(coordinator.deadlines),
            list(coordinator.deadline_heap),
            set(coordinator.accepted_roots),
            coordinator.scheduler_started,
        )
        try:
            coordinator.enqueue_unlocked(
                "rejected", {"id": "rejected"}, now=time.monotonic(), debounce=0,
            )
            raise AssertionError("closed coordinator accepted work")
        except RuntimeError:
            pass
        after = (
            dict(coordinator.pending),
            dict(coordinator.deadlines),
            list(coordinator.deadline_heap),
            set(coordinator.accepted_roots),
            coordinator.scheduler_started,
        )
    assert after == before


def _test_accepted_follow_up_drains_during_close() -> None:
    entered = threading.Event()
    release = threading.Event()
    calls: list[str] = []
    coordinator = None

    def flush(root_id: str) -> None:
        with coordinator.changed:
            claim = coordinator.claim_pending_unlocked(root_id)
        assert claim is not None
        calls.append(root_id)
        if len(calls) == 1:
            entered.set()
            release.wait(timeout=2.0)
        with coordinator.changed:
            coordinator.finish_claim_unlocked(
                claim, failed=False, reschedule_concurrent=True,
            )

    coordinator = sm._SessionPersistCoordinator(flush)
    with coordinator.changed:
        coordinator.enqueue_unlocked(
            "root", {"id": "root", "revision": 1},
            now=time.monotonic(), debounce=0,
        )
    try:
        assert entered.wait(timeout=2.0)
        with coordinator.changed:
            coordinator.enqueue_unlocked(
                "root", {"id": "root", "revision": 2},
                now=time.monotonic(), debounce=0,
            )
        coordinator.request_close()
        with coordinator.changed:
            try:
                coordinator.enqueue_unlocked(
                    "late", {"id": "late"}, now=time.monotonic(), debounce=0,
                )
                raise AssertionError("closing coordinator accepted new work")
            except RuntimeError:
                pass
        release.set()
        coordinator.close(lambda: None, timeout=2.0)
        assert calls == ["root", "root"]
        assert coordinator.state == "closed"
        assert coordinator._thread is not None
        assert not coordinator._thread.is_alive()
    finally:
        release.set()
        _close_for_cleanup(coordinator)


def _test_drain_failure_and_retry() -> None:
    coordinator = sm._SessionPersistCoordinator(lambda _root_id: None)
    with coordinator.changed:
        coordinator.accepted_roots.add("root")
        coordinator.pending["root"] = {"id": "root"}

    def fail() -> None:
        raise OSError("drain failed")

    try:
        coordinator.close(fail, timeout=0.2)
        raise AssertionError("failed drain reported success")
    except OSError:
        pass
    assert coordinator.state == "closing"
    assert "root" in coordinator.pending
    coordinator.close(lambda: _clear_root(coordinator, "root"), timeout=0.2)
    assert coordinator.state == "closed"


def _test_serialized_drain_and_waiter_timeout() -> None:
    coordinator = sm._SessionPersistCoordinator(lambda _root_id: None)
    with coordinator.changed:
        coordinator.accepted_roots.add("root")
        coordinator.pending["root"] = {"id": "root"}
    entered = threading.Event()
    release = threading.Event()
    calls = 0
    outcomes: list[str] = []

    def drain() -> None:
        nonlocal calls
        calls += 1
        entered.set()
        release.wait(timeout=2.0)
        _clear_root(coordinator, "root")

    def owner() -> None:
        coordinator.close(drain, timeout=2.0)
        outcomes.append("owner")

    def waiter() -> None:
        try:
            coordinator.close(drain, timeout=0.05)
        except TimeoutError:
            outcomes.append("timeout")

    owner_thread = threading.Thread(target=owner)
    waiter_thread = threading.Thread(target=waiter)
    owner_thread.start()
    try:
        assert entered.wait(timeout=2.0)
        waiter_thread.start()
        waiter_thread.join(timeout=1.0)
        assert not waiter_thread.is_alive()
        release.set()
        owner_thread.join(timeout=2.0)
        assert not owner_thread.is_alive()
        coordinator.close(drain, timeout=0.2)
        assert calls == 1
        assert sorted(outcomes) == ["owner", "timeout"]
    finally:
        release.set()
        owner_thread.join(timeout=2.0)
        if waiter_thread.ident is not None:
            waiter_thread.join(timeout=2.0)
        assert not owner_thread.is_alive()
        assert not waiter_thread.is_alive()
        _close_for_cleanup(coordinator)


def _test_waiter_retries_failed_drain() -> None:
    coordinator = sm._SessionPersistCoordinator(lambda _root_id: None)
    with coordinator.changed:
        coordinator.accepted_roots.add("root")
        coordinator.pending["root"] = {"id": "root"}
    entered = threading.Event()
    release = threading.Event()
    outcomes: list[str] = []

    def fail_drain() -> None:
        entered.set()
        release.wait(timeout=2.0)
        raise OSError("drain failed")

    def recover_drain() -> None:
        _clear_root(coordinator, "root")
        outcomes.append("recovered")

    def owner() -> None:
        try:
            coordinator.close(fail_drain, timeout=2.0)
        except OSError:
            outcomes.append("failed")

    def waiter() -> None:
        coordinator.close(recover_drain, timeout=2.0)
        outcomes.append("closed")

    owner_thread = threading.Thread(target=owner)
    waiter_thread = threading.Thread(target=waiter)
    owner_thread.start()
    try:
        assert entered.wait(timeout=2.0)
        waiter_thread.start()
        release.set()
        owner_thread.join(timeout=2.0)
        waiter_thread.join(timeout=2.0)
        assert not owner_thread.is_alive()
        assert not waiter_thread.is_alive()
        assert sorted(outcomes) == ["closed", "failed", "recovered"]
    finally:
        release.set()
        owner_thread.join(timeout=2.0)
        if waiter_thread.ident is not None:
            waiter_thread.join(timeout=2.0)
        assert not owner_thread.is_alive()
        assert not waiter_thread.is_alive()
        _close_for_cleanup(coordinator)


def _test_start_failure_is_recoverable() -> None:
    coordinator = sm._SessionPersistCoordinator(lambda _root_id: None)
    original_start = threading.Thread.start

    def fail_start(_thread) -> None:
        raise RuntimeError("start failed")

    threading.Thread.start = fail_start
    try:
        with coordinator.changed:
            try:
                coordinator.enqueue_unlocked(
                    "root", {"id": "root"}, now=time.monotonic(), debounce=0,
                )
                raise AssertionError("thread start failure was hidden")
            except RuntimeError as error:
                assert str(error) == "start failed"
    finally:
        threading.Thread.start = original_start
    assert coordinator.state == "accepting"
    assert not coordinator.scheduler_started
    assert coordinator._thread is None
    _clear_root(coordinator, "root")
    coordinator.close(lambda: None, timeout=0.2)


def _test_request_close_cannot_skip_owed_drain() -> None:
    flushed = threading.Event()
    coordinator = None

    def flush(root_id: str) -> None:
        with coordinator.changed:
            claim = coordinator.claim_pending_unlocked(root_id)
            assert claim is not None
            coordinator.finish_claim_unlocked(
                claim, failed=False, reschedule_concurrent=False,
            )
        flushed.set()

    coordinator = sm._SessionPersistCoordinator(flush)
    drains = 0

    def drain() -> None:
        nonlocal drains
        drains += 1

    try:
        with coordinator.changed:
            coordinator.enqueue_unlocked(
                "root", {"id": "root"}, now=time.monotonic(), debounce=0,
            )
        assert flushed.wait(timeout=2.0)
        coordinator.request_close()
        with coordinator.changed:
            coordinator.changed.wait(timeout=0.05)
            assert coordinator.state == "closing"
        coordinator.close(drain, timeout=2.0)
        coordinator.close(drain, timeout=0.2)
        assert drains == 1
    finally:
        _close_for_cleanup(coordinator)


def _test_single_thread_and_callback_close() -> None:
    callback_closed = threading.Event()
    self_close_rejected = threading.Event()
    coordinator = None

    def flush(root_id: str) -> None:
        with coordinator.changed:
            claim = coordinator.claim_pending_unlocked(root_id)
        assert claim is not None
        coordinator.request_close()
        try:
            coordinator.close(lambda: None, timeout=0.1)
        except RuntimeError:
            self_close_rejected.set()
        with coordinator.changed:
            coordinator.finish_claim_unlocked(
                claim, failed=False, reschedule_concurrent=False,
            )
        callback_closed.set()

    coordinator = sm._SessionPersistCoordinator(flush)
    threads = []
    with coordinator.changed:
        now = time.monotonic()
        coordinator.last_at.update(first=now, second=now)

    def enqueue(root_id: str) -> None:
        with coordinator.changed:
            coordinator.enqueue_unlocked(
                root_id, {"id": root_id}, now=time.monotonic(), debounce=10,
            )

    try:
        for root_id in ("first", "second"):
            thread = threading.Thread(target=enqueue, args=(root_id,))
            threads.append(thread)
            thread.start()
        for thread in threads:
            thread.join(timeout=1.0)
            assert not thread.is_alive()
        scheduler = coordinator._thread
        assert scheduler is not None and scheduler.is_alive()

        with coordinator.changed:
            coordinator._arm_accepted_deadline_unlocked("first", 0)
        assert callback_closed.wait(timeout=2.0)
        assert self_close_rejected.is_set()
        _clear_root(coordinator, "second")
        coordinator.close(lambda: None, timeout=2.0)
        assert coordinator._thread is scheduler
        assert not scheduler.is_alive()
    finally:
        for thread in threads:
            thread.join(timeout=2.0)
            assert not thread.is_alive()
        _close_for_cleanup(coordinator)


def main() -> int:
    try:
        _test_never_started_and_atomic_rejection()
        _test_accepted_follow_up_drains_during_close()
        _test_drain_failure_and_retry()
        _test_serialized_drain_and_waiter_timeout()
        _test_waiter_retries_failed_drain()
        _test_start_failure_is_recoverable()
        _test_request_close_cannot_skip_owed_drain()
        _test_single_thread_and_callback_close()
        print("PASS session persist coordinator lifecycle")
        return 0
    finally:
        shutil.rmtree(_TEST_HOME, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
