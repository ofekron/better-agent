"""Dedicated coverage for thread_loop_host.py.

The host is a concurrency primitive (one thread, one loop, one bounded
executor), so these tests drive real event loops and real threads rather
than mocking asyncio. Isolation is handled by conftest's import-time
test-home engagement; this module touches no Better Agent state on disk.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
import time

import pytest

import thread_loop_host
from thread_loop_host import ThreadLoopHost, _invoke


# --------------------------------------------------------------------------
# module-level helper
# --------------------------------------------------------------------------


def test_invoke_awaits_factory():
    async def factory():
        return 7

    assert asyncio.run(_invoke(factory)) == 7


# --------------------------------------------------------------------------
# __init__
# --------------------------------------------------------------------------


def test_init_defaults():
    h = ThreadLoopHost(name="alpha")
    assert h._name == "alpha"
    assert h._io_thread_name_prefix == "alpha-io"
    assert h._executor_workers == 4
    assert h._loop is None
    assert h._thread is None
    assert h._executor is None
    assert h.running is False
    assert h.loop is None


def test_init_explicit_prefix_and_workers():
    h = ThreadLoopHost(name="alpha", executor_workers=2, io_thread_name_prefix="x-io")
    assert h._io_thread_name_prefix == "x-io"
    assert h._executor_workers == 2


# --------------------------------------------------------------------------
# start / stop lifecycle
# --------------------------------------------------------------------------


def test_start_creates_thread_loop_executor():
    h = ThreadLoopHost(name="t1")
    h.start()
    try:
        assert h.running is True
        assert h.loop is h._loop
        assert h._thread is not None
        assert h._thread.name == "t1"
        assert h._thread.daemon is True
        assert isinstance(h._executor, concurrent.futures.ThreadPoolExecutor)
    finally:
        h.stop()


def test_start_is_idempotent():
    h = ThreadLoopHost(name="t2")
    h.start()
    first_thread = h._thread
    first_loop = h._loop
    h.start()  # already started -> no-op
    assert h._thread is first_thread
    assert h._loop is first_loop
    h.stop()


def test_on_stopping_hook_invoked_before_teardown():
    calls: list[str] = []

    class Sub(ThreadLoopHost):
        def on_stopping(self) -> None:
            calls.append("stopping")

    h = Sub(name="t3")
    h.start()
    h.stop()
    assert calls == ["stopping"]


def test_stop_closes_loop_and_thread_exits():
    h = ThreadLoopHost(name="t4")
    h.start()
    loop = h.loop
    thread = h._thread
    h.stop()
    assert loop.is_closed()  # _run finally closed the loop
    assert thread.is_alive() is False
    assert h.running is False  # _loop nulled
    assert h.loop is None


def test_stop_without_start_is_noop():
    h = ThreadLoopHost(name="t5")
    h.stop()  # loop None -> early return, no error
    assert h.running is False


def test_stop_swallows_runtime_error_on_closed_loop():
    """call_soon_threadsafe on an already-closed loop raises RuntimeError;
    stop() must swallow it and finish teardown."""
    h = ThreadLoopHost(name="t6")
    h.start()
    loop = h.loop
    # Stop+close the loop externally so the thread exits, leaving a stale
    # closed-loop reference on the host.
    loop.call_soon_threadsafe(loop.stop)
    h._thread.join(timeout=5)
    assert loop.is_closed()
    h.stop()  # must not raise
    assert h.running is False


def test_stop_skips_executor_shutdown_when_none():
    h = ThreadLoopHost(name="t7")
    h.start()
    loop = h.loop
    loop.call_soon_threadsafe(loop.stop)
    h._thread.join(timeout=5)
    h._executor = None  # executor already released elsewhere
    h.stop()  # `if executor is not None` False -> no shutdown, no AttributeError
    assert h.running is False


def test_stop_warns_when_thread_does_not_stop_in_time(caplog):
    """A loop hogged by a non-yielding coroutine cannot process the stop
    callback, so join() times out -> warning. Event-gated, not sleep-based."""
    h = ThreadLoopHost(name="t8")
    h.start()
    thread = h._thread
    gate = threading.Event()
    entered = threading.Event()

    async def hog():
        entered.set()
        while not gate.is_set():
            pass  # never yields -> loop cannot drain the stop callback

    asyncio.run_coroutine_threadsafe(hog(), h.loop)
    assert entered.wait(2)

    with caplog.at_level(logging.WARNING, logger="thread_loop_host"):
        h.stop(timeout=0.05)
    assert any("did not stop within" in r.message for r in caplog.records)

    # Releasing the hog lets the loop finally drain the queued stop callback,
    # run_forever returns, and the daemon thread exits cleanly.
    gate.set()
    thread.join(timeout=5)
    assert not thread.is_alive()


# --------------------------------------------------------------------------
# running / loop properties
# --------------------------------------------------------------------------


def test_running_false_when_loop_closed_but_present():
    h = ThreadLoopHost(name="t9")
    h.start()
    loop = h.loop
    loop.call_soon_threadsafe(loop.stop)
    h._thread.join(timeout=5)
    assert loop.is_closed()
    assert h._loop is loop  # present but closed
    assert h.running is False  # `and not loop.is_closed()` short-circuits
    h.stop()


# --------------------------------------------------------------------------
# run() submission
# --------------------------------------------------------------------------


def test_run_inline_when_loop_none():
    h = ThreadLoopHost(name="r1")  # not started

    async def factory():
        return "inline"

    async def caller():
        return await h.run(factory)

    assert asyncio.run(caller()) == "inline"


def test_run_inline_when_loop_closed():
    h = ThreadLoopHost(name="r2")
    h.start()
    loop = h.loop
    loop.call_soon_threadsafe(loop.stop)
    h._thread.join(timeout=5)
    assert loop.is_closed()

    async def factory():
        return "inline-closed"

    async def caller():
        return await h.run(factory)

    assert asyncio.run(caller()) == "inline-closed"
    h.stop()


def test_run_executes_on_host_loop():
    h = ThreadLoopHost(name="r3")
    h.start()
    seen: dict[str, int] = {}

    async def factory():
        seen["loop"] = id(asyncio.get_running_loop())
        return "hosted"

    async def caller():
        return await h.run(factory)

    try:
        assert asyncio.run(caller()) == "hosted"
        assert seen["loop"] == id(h.loop)  # ran on the host loop, not caller's
    finally:
        h.stop()


async def _await_cross_thread_event(event: threading.Event, timeout: float = 2.0) -> bool:
    """Poll a threading.Event from inside a running loop without blocking it.

    Blocking on ``event.wait()`` inside an async caller freezes the loop and
    starves the very task we are waiting on, so we yield between checks.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if event.is_set():
            return True
        await asyncio.sleep(0.01)
    return event.is_set()


def test_run_propagates_host_exception():
    """When the host coroutine raises and the caller is NOT concurrently
    cancelling, the exception propagates straight through `run` (the shield
    re-raises it; the CancelledError handler does not apply)."""
    h = ThreadLoopHost(name="r4")
    h.start()

    async def factory():
        raise ValueError("boom")

    async def caller():
        await h.run(factory)

    try:
        with pytest.raises(ValueError, match="boom"):
            asyncio.run(caller())
    finally:
        h.stop()


def test_run_cancel_waits_for_inflight_host_work():
    """Cancel while work is in flight: shield awaits host completion, then
    CancelledError re-raises. Host work must not be abandoned."""
    h = ThreadLoopHost(name="r5")
    h.start()
    gate = threading.Event()
    entered = threading.Event()
    completed: list[bool] = []

    async def factory():
        entered.set()
        gate.wait(5)
        completed.append(True)
        return "done"

    async def caller():
        task = asyncio.ensure_future(h.run(factory))
        assert await _await_cross_thread_event(entered)  # factory blocked on gate
        # Release the gate from a background thread so the shield await is
        # genuinely pending when cancellation arrives.
        releaser = threading.Thread(target=lambda: (time.sleep(0.05), gate.set()))
        releaser.start()
        await asyncio.sleep(0.02)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return "cancelled"
        finally:
            releaser.join(2)
        return "completed"

    try:
        assert asyncio.run(caller()) == "cancelled"
        assert completed == [True]
    finally:
        h.stop()


def test_run_cancel_logs_when_host_work_fails(caplog):
    """Cancel while in flight, and the shielded host work then raises: the
    exception is logged (never re-raised past the cancellation) and
    CancelledError re-raises."""
    h = ThreadLoopHost(name="r6")
    h.start()
    gate = threading.Event()
    entered = threading.Event()

    async def factory():
        entered.set()
        gate.wait(5)
        raise ValueError("host-failed")

    async def caller():
        task = asyncio.ensure_future(h.run(factory))
        assert await _await_cross_thread_event(entered)
        releaser = threading.Thread(target=lambda: (time.sleep(0.05), gate.set()))
        releaser.start()
        await asyncio.sleep(0.02)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return "cancelled"
        finally:
            releaser.join(2)
        return "completed"

    try:
        with caplog.at_level(logging.ERROR, logger="thread_loop_host"):
            assert asyncio.run(caller()) == "cancelled"
        assert any("work failed while cancelling" in r.message for r in caplog.records)
    finally:
        h.stop()


# --------------------------------------------------------------------------
# run_blocking()
# --------------------------------------------------------------------------


def test_run_blocking_executes_callable_with_args():
    h = ThreadLoopHost(name="rb1")
    h.start()

    def blocking(a, b):
        return a * b

    async def caller():
        return await h.run_blocking(blocking, 3, 4)

    try:
        assert asyncio.run(caller()) == 12
    finally:
        h.stop()


def test_run_blocking_inline_when_loop_none():
    h = ThreadLoopHost(name="rb2")  # not started

    def blocking():
        return "inline-blk"

    async def caller():
        return await h.run_blocking(blocking)

    assert asyncio.run(caller()) == "inline-blk"


# --------------------------------------------------------------------------
# publish_fact()
# --------------------------------------------------------------------------


def test_publish_fact_builds_event_with_sid_as_root(monkeypatch):
    captured: dict[str, object] = {}

    def fake_publish(event):
        captured["event"] = event

    monkeypatch.setattr(thread_loop_host.bus, "publish_threadsafe", fake_publish)
    h = ThreadLoopHost(name="p1")
    h.publish_fact("my.fact", sid="s1", payload={"k": "v"})

    ev = captured["event"]
    assert ev.type == "my.fact"
    assert ev.sid == "s1"
    assert ev.root_id == "s1"  # root_id defaults to sid
    assert ev.payload == {"k": "v"}
    assert ev.persist is False


def test_publish_fact_root_id_overrides_sid(monkeypatch):
    captured: list[object] = []
    monkeypatch.setattr(
        thread_loop_host.bus, "publish_threadsafe", lambda e: captured.append(e)
    )
    h = ThreadLoopHost(name="p2")
    h.publish_fact("f", sid="s", root_id="r", payload=None)
    assert captured[0].root_id == "r"
    assert captured[0].payload == {}  # dict(payload or {})


def test_publish_fact_swallows_publish_exceptions(monkeypatch, caplog):
    def boom(event):
        raise RuntimeError("bus-down")

    monkeypatch.setattr(thread_loop_host.bus, "publish_threadsafe", boom)
    h = ThreadLoopHost(name="p3")
    with caplog.at_level(logging.ERROR, logger="thread_loop_host"):
        h.publish_fact("f", sid="s")  # must not raise
    assert any("failed to publish" in r.message for r in caplog.records)
