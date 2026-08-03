"""Unit coverage for ordered_root_dispatcher.py — serialized per-root dispatch.

OrderedRootDispatcher fans items out across one single-worker executor per
shard, so all items for a given ``root_id`` run serially in submit order, while
a bounded semaphore caps the in-flight backlog and an error-drain serializes an
optional ``on_error`` callback. It is a real concurrency component (used by the
session-projection off-loop), so the tests pin every branch deterministically
with event handshakes rather than sleeps.

The defensive re-check ``if self._on_error is not None`` inside ``_drain_errors``
is unreachable through the public contract (errors are only enqueued after the
same check in ``_schedule_error``), so one test puts the dispatcher into that
defended-against state directly to prove the guard tolerates it instead of
crashing.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time

import pytest

from ordered_root_dispatcher import OrderedRootDispatcher

log = logging.getLogger("test.ordered_root_dispatcher")


def _wait_until(predicate, *, timeout=5.0, interval=0.01) -> bool:
    """Poll a real condition until it holds or the deadline elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class _Gate:
    """Handler that signals on start and blocks until released."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.proceed = threading.Event()

    def __call__(self, item) -> None:
        self.started.set()
        self.proceed.wait(timeout=10)


# ── __init__ capacity default ───────────────────────────────────


def test_default_capacity_is_pool_size_times_64():
    d = OrderedRootDispatcher(
        lambda item: None,
        pool_size=2,
        thread_name_prefix="cap",
        logger=log,
    )
    for _ in range(128):
        assert d._capacity.acquire(blocking=False)
    assert d._capacity.acquire(blocking=False) is False
    d.shutdown(wait=True)


# ── submit: happy path + ordering ───────────────────────────────


def test_same_root_processed_in_submit_order():
    seen: list[int] = []
    lock = threading.Lock()

    def handler(item: int) -> None:
        with lock:
            seen.append(item)

    d = OrderedRootDispatcher(
        handler, pool_size=4, thread_name_prefix="ord", logger=log,
    )
    futures = [d.submit("root-a", i) for i in range(20)]
    for future in futures:
        assert future.result(timeout=5) is None
    assert seen == list(range(20))
    d.shutdown(wait=True)


# ── submit: backlog-full reject ─────────────────────────────────


def test_submit_rejects_when_backlog_full_then_restores_on_completion():
    gate = _Gate()
    d = OrderedRootDispatcher(
        gate, pool_size=1, thread_name_prefix="full", logger=log, max_pending=2,
    )
    f1 = d.submit("r", 1)  # acquires capacity (1/2), runs, blocks -> worker busy
    assert gate.started.wait(timeout=5)
    f2 = d.submit("r", 2)  # acquires capacity (2/2), queued behind busy worker
    f3 = d.submit("r", 3)  # capacity exhausted -> rejected immediately
    exc = f3.exception(timeout=5)
    assert isinstance(exc, RuntimeError)
    assert "backlog is full" in str(exc)

    gate.proceed.set()
    assert f1.result(timeout=5) is None
    assert f2.result(timeout=5) is None
    d.shutdown(wait=True)


# ── _handle_completion: cancelled queued future ─────────────────


def test_cancelled_queued_future_releases_capacity():
    gate = _Gate()
    d = OrderedRootDispatcher(
        gate, pool_size=1, thread_name_prefix="cancel", logger=log, max_pending=2,
    )
    f1 = d.submit("r", 1)
    assert gate.started.wait(timeout=5)  # worker busy with f1 -> f2 is queued
    f2 = d.submit("r", 2)
    assert f2.cancel() is True  # queued, not started -> cancel succeeds

    # done_callback ran with cancelled()==True -> capacity released -> new submit ok
    f3 = d.submit("r", 3)
    gate.proceed.set()
    assert f1.result(timeout=5) is None
    assert f3.result(timeout=5) is None
    d.shutdown(wait=True)


# ── _handle_completion: handler exception -> on_error ───────────


def test_handler_exception_invokes_on_error():
    errors: list[tuple] = []

    def handler(item: int) -> None:
        raise ValueError(f"boom-{item}")

    def on_error(root_id, item, exc) -> None:
        errors.append((root_id, item, exc))

    d = OrderedRootDispatcher(
        handler, pool_size=2, thread_name_prefix="exc", logger=log, on_error=on_error,
    )
    future = d.submit("r", 7)
    with pytest.raises(ValueError):
        future.result(timeout=5)
    assert _wait_until(lambda: len(errors) >= 1, timeout=5)
    root_id, item, exc = errors[0]
    assert (root_id, item) == ("r", 7)
    assert isinstance(exc, ValueError)
    d.shutdown(wait=True)


def test_handler_exception_without_on_error_is_swallowed():
    def handler(item: int) -> None:
        raise ValueError("boom")

    d = OrderedRootDispatcher(
        handler, pool_size=1, thread_name_prefix="noerr", logger=log,  # on_error=None
    )
    future = d.submit("r", 1)
    with pytest.raises(ValueError):
        future.result(timeout=5)
    d.shutdown(wait=True)  # no error drain scheduled -> clean shutdown


# ── _drain_errors: on_error raising is caught ───────────────────


def test_on_error_raising_is_caught_and_logged(caplog):
    def handler(item: int) -> None:
        raise ValueError("boom")

    def on_error(root_id, item, exc) -> None:
        raise RuntimeError("cb broke")

    caplog.set_level(logging.ERROR, logger="test.ordered_root_dispatcher")
    d = OrderedRootDispatcher(
        handler, pool_size=1, thread_name_prefix="badcb", logger=log, on_error=on_error,
    )
    future = d.submit("r", 1)
    with pytest.raises(ValueError):
        future.result(timeout=5)
    assert _wait_until(
        lambda: any("error handler failed" in r.message for r in caplog.records),
        timeout=5,
    )
    d.shutdown(wait=True)  # drain survived the callback exception


# ── _schedule_error: errors_closed after shutdown ───────────────


def test_reject_after_shutdown_does_not_drain_error():
    drained: list[tuple] = []

    def on_error(root_id, item, exc) -> None:
        drained.append((root_id, item))

    d = OrderedRootDispatcher(
        lambda item: None, pool_size=1, thread_name_prefix="cls", logger=log,
        on_error=on_error,
    )
    d.shutdown(wait=True)  # closes executors AND marks errors_closed
    future = d.submit("r", 1)  # closed -> rejected
    exc = future.exception(timeout=5)
    assert isinstance(exc, RuntimeError)
    assert "closed" in str(exc)
    assert _wait_until(lambda: True, timeout=0.5)
    assert drained == []  # errors_closed -> _schedule_error returned before enqueue


# ── _schedule_error: coalesce while drain in-flight ─────────────


def test_error_scheduled_while_drain_in_flight_is_coalesced():
    cb_started = threading.Event()
    cb_proceed = threading.Event()

    def on_error(root_id, item, exc) -> None:
        cb_started.set()
        cb_proceed.wait(timeout=5)

    d = OrderedRootDispatcher(
        lambda item: None, pool_size=1, thread_name_prefix="coal", logger=log,
        on_error=on_error,
    )
    d._schedule_error("r1", 1, RuntimeError("a"))  # schedules drain -> on_error blocks
    assert cb_started.wait(timeout=5)
    assert d._error_drain_scheduled is True

    d._schedule_error("r2", 2, RuntimeError("b"))  # drain already running -> coalesce
    assert "r2" in d._pending_errors
    assert d._error_drain_scheduled is True  # not re-submitted

    cb_proceed.set()  # drain resumes -> its loop picks up r2
    d.shutdown(wait=True)
    assert d._pending_errors == {}


# ── _drain_errors: defensive on_error-None guard ────────────────


def test_drain_skips_call_when_on_error_cleared():
    d = OrderedRootDispatcher(
        lambda item: None, pool_size=1, thread_name_prefix="guard", logger=log,
        on_error=lambda *args: None,
    )
    d._pending_errors["r"] = (1, RuntimeError("x"))
    d._on_error = None  # defended-against state: queued error but no callback
    d._drain_errors()  # must skip the call, pop the error, and unschedule
    assert d._pending_errors == {}
    assert d._error_drain_scheduled is False
    d.shutdown(wait=True)


# ── barrier ──────────────────────────────────────────────────────


def test_barrier_runs_after_prior_work_on_same_root():
    order: list[str] = []
    started = threading.Event()
    proceed = threading.Event()

    def handler(item: int) -> None:
        started.set()
        proceed.wait(timeout=5)
        order.append("work")

    d = OrderedRootDispatcher(
        handler, pool_size=1, thread_name_prefix="bar", logger=log,
    )
    d.submit("r", 1)
    assert started.wait(timeout=5)

    async def _go() -> None:
        barrier_task = asyncio.ensure_future(d.barrier("r"))
        await asyncio.sleep(0)  # let barrier enqueue its noop on the busy executor
        proceed.set()  # release the worker; noop runs after the handler
        await barrier_task

    asyncio.run(_go())
    assert order == ["work"]
    d.shutdown(wait=True)


# ── shutdown idempotency ────────────────────────────────────────


def test_shutdown_is_idempotent_and_rejects_after_close():
    d = OrderedRootDispatcher(
        lambda item: None, pool_size=3, thread_name_prefix="shut", logger=log,
    )
    d.shutdown(wait=True)
    d.shutdown(wait=True)  # second call is a no-op
    exc = d.submit("r", 1).exception(timeout=5)  # closed -> reject
    assert isinstance(exc, RuntimeError)
    assert "closed" in str(exc)


# ── _executor selection ─────────────────────────────────────────


def test_executor_selection_is_stable_per_root():
    d = OrderedRootDispatcher(
        lambda item: None, pool_size=4, thread_name_prefix="ex", logger=log,
    )
    assert d._executor("root") is d._executor("root")
    assert all(d._executor(f"r{i}") in d._executors for i in range(10))
    d.shutdown(wait=True)
