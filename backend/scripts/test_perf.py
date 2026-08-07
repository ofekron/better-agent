"""Dedicated unit tests owning ``perf``'s full public surface.

The sibling ``test_perf_lagged_queue.py`` and ``test_perf_rollup_off_loop.py``
own two specific regressions (queue stamp unwrapping; flush off-loaded off
the event loop). This module owns the rest of the primitive: stat and count
accumulation with their max-update branches, the ``timed``/``timed_fn``
primitives (sync + async), queue-gauge registration, ``stamp_enq``/
``record_lag``, every ``flush()`` branch (queue-depth line, failing-gauge
skip, count rollup line, empty no-op return), one live ``_rollup_loop``
iteration (proving it survives a flush failure and honours cancellation),
and ``start_rollup_task`` idempotency.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest  # noqa: E402

import perf  # noqa: E402


def _stat(name: str) -> dict:
    with perf._lock:
        return dict(perf._stats.get(name, {}))


def _count(name: str) -> dict:
    with perf._lock:
        return dict(perf._counts.get(name, {}))


def _cancel_rollup_task() -> None:
    task = perf._rollup_task
    perf._rollup_task = None
    if task is not None and not task.done():
        with contextlib.suppress(BaseException):
            task.cancel()


@pytest.fixture(autouse=True)
def _reset_perf_state():
    """perf accumulates into module globals; reset them between tests so one
    test's samples can't be read by another, and never leave a rollup task
    running where another test (or the rest of the suite) could observe it."""
    _cancel_rollup_task()
    with perf._lock:
        perf._stats.clear()
        perf._counts.clear()
        perf._queue_gauges.clear()
    yield
    _cancel_rollup_task()
    with perf._lock:
        perf._stats.clear()
        perf._counts.clear()
        perf._queue_gauges.clear()


@contextlib.contextmanager
def _capturing_perf_logs():
    """Capture rendered ``perf.logger`` records at INFO so flush() output can
    be asserted. flush() logs at INFO; the logger's effective level is usually
    inherited, so force INFO for the capture window and restore after."""
    captured: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record.getMessage())

    handler = _Capture()
    prior_level = perf.logger.level
    perf.logger.setLevel(logging.INFO)
    perf.logger.addHandler(handler)
    try:
        yield captured
    finally:
        perf.logger.removeHandler(handler)
        perf.logger.setLevel(prior_level)


def test_record_accumulates_samples_and_tracks_max():
    # Ascending then smaller: exercises both directions of the max branch.
    perf.record("op", 1.0)
    perf.record("op", 5.0)
    perf.record("op", 3.0)

    s = _stat("op")
    assert s["n"] == 3
    assert s["ms_sum"] == 9.0
    assert s["ms_max"] == 5.0

    # Distinct names accumulate independently.
    perf.record("other", 9.0)
    assert _stat("other")["n"] == 1
    assert _stat("op")["n"] == 3


def test_record_count_accumulates_and_tracks_max():
    perf.record_count("ev", 5)
    perf.record_count("ev", 12)  # 12 > 5 -> max becomes 12
    perf.record_count("ev", 3)   # 3 < 12 -> max unchanged

    c = _count("ev")
    assert c["n"] == 3
    assert c["total"] == 20
    assert c["max"] == 12

    # Default value is 1.
    perf.record_count("d")
    d = _count("d")
    assert d == {"n": 1, "total": 1, "max": 1}


def test_timed_context_records_elapsed():
    with perf.timed("ctx"):
        pass
    s = _stat("ctx")
    assert s["n"] == 1
    assert s["ms_sum"] >= 0.0
    assert s["ms_max"] >= 0.0


def test_timed_fn_wraps_sync_function():
    @perf.timed_fn("syncfn")
    def double(value: int) -> int:
        return value * 2

    assert double(21) == 42
    assert _stat("syncfn")["n"] == 1


def test_timed_fn_wraps_async_function():
    @perf.timed_fn("asyncfn")
    async def inc(value: int) -> int:
        return value + 1

    async def drive() -> int:
        return await inc(41)

    assert asyncio.run(drive()) == 42
    assert _stat("asyncfn")["n"] == 1


def test_queue_gauge_register_and_unregister():
    perf.register_queue("q", lambda: 1)
    assert "q" in perf._queue_gauges
    perf.unregister_queue("q")
    assert "q" not in perf._queue_gauges
    # Unregistering an absent gauge is a no-op (pop default).
    perf.unregister_queue("absent")


def test_stamp_enq_pairs_with_record_lag():
    enq = perf.stamp_enq()
    perf.record_lag("rq", enq)
    assert _stat("queue.lag.rq")["n"] == 1


def test_flush_is_noop_when_nothing_recorded():
    with _capturing_perf_logs() as logs:
        perf.flush()
    assert logs == []


def test_flush_emits_queue_depth_and_skips_failing_gauge():
    perf.record("op", 2.0)
    perf.register_queue("good", lambda: 7)
    perf.register_queue("broken", lambda: 1 / 0)

    with _capturing_perf_logs() as logs:
        perf.flush()

    assert logs, "flush emitted no rollup line"
    body = logs[0]
    assert "q.good depth=7" in body
    assert "q.broken" not in body
    assert "op n=1" in body


def test_flush_emits_count_rollup_lines():
    perf.record_count("ev", 10)
    perf.record_count("ev", 3)

    with _capturing_perf_logs() as logs:
        perf.flush()

    assert logs
    assert "ev samples=2 count_total=13 count_max=10" in logs[0]


def test_rollup_loop_flushes_and_tolerates_flush_failure(monkeypatch):
    async def drive() -> None:
        perf.record("keep", 1.0)
        calls = {"n": 0}
        real_flush = perf.flush

        def flaky() -> None:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
            real_flush()

        monkeypatch.setattr(perf, "ROLLUP_SECS", 0.01)
        monkeypatch.setattr(perf, "flush", flaky)

        task = asyncio.create_task(perf._rollup_loop())
        try:
            # The first flush raises -> the loop must log and keep going; a
            # later successful flush clears the recorded stat. Wait for that.
            deadline = 100
            while "keep" in perf._stats and deadline > 0:
                await asyncio.sleep(0.01)
                deadline -= 1
            with perf._lock:
                still_present = "keep" in perf._stats
            assert not still_present, "rollup loop never flushed after a failed flush"
            assert calls["n"] >= 2

            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        finally:
            perf._rollup_task = None

    asyncio.run(drive())


def test_start_rollup_task_is_idempotent(monkeypatch):
    async def drive() -> None:
        monkeypatch.setattr(perf, "ROLLUP_SECS", 0.01)
        perf._rollup_task = None

        perf.start_rollup_task()
        first = perf._rollup_task
        assert first is not None
        assert not first.done()

        # Already running -> early return, same task object.
        perf.start_rollup_task()
        assert perf._rollup_task is first

        first.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await first
        perf._rollup_task = None

    asyncio.run(drive())
