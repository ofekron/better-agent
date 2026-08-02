#!/usr/bin/env python3
"""Dedicated unit coverage for backend/cursor_ledger_worker.py.

The single background thread that owns tailer cursor-advance persistence.
The regression test (test_tailer_cursor_ledger_worker.py) covers the
coalescing/non-blocking happy paths but leaves several branches cold (~90%):
the `_process_key` None-guard, the write-exception path, `flush_now`'s
existing-event reuse + timeout, and the worker-loop exit (its thread is
never joined before the coverage report, so the loop-exit branch isn't
captured). This file drives each directly with real assertions and joins
every worker thread it starts.

State isolation: a throwaway BETTER_AGENT_HOME via `paths.engage_test_home`
before any backend import (project rule).
"""
from __future__ import annotations

import logging
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

_TEST_HOME = tempfile.mkdtemp(prefix="ba-clw-unit-")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import paths  # noqa: E402

paths.engage_test_home(_TEST_HOME)

import cursor_ledger_worker as clw  # noqa: E402


def _stop(worker: clw.CursorLedgerWorker) -> None:
    """Stop the worker and join its thread so the loop-exit branch is
    captured and no thread is left dangling."""
    worker.stop()
    worker._thread.join(timeout=3.0)
    assert not worker._thread.is_alive(), "worker thread did not exit"


# ---------------------------------------------------------------------------
# _process_key None-guard
# ---------------------------------------------------------------------------


def test_process_key_noop_when_nothing_pending():
    """If a key is dequeued whose latest was already consumed, `_process_key`
    must return without touching in_flight."""
    w = clw.CursorLedgerWorker(name="unit-noop")
    try:
        w._process_key("never-noted")  # _latest.pop -> None -> early return
        assert "never-noted" not in w._in_flight
        assert w._latest == {}
    finally:
        _stop(w)


# ---------------------------------------------------------------------------
# write-exception is swallowed + logged, in_flight still cleared
# ---------------------------------------------------------------------------


def test_write_exception_is_caught_and_logged(caplog):
    w = clw.CursorLedgerWorker(name="unit-exc")
    try:
        started = threading.Event()

        def bad_write() -> None:
            started.set()
            time.sleep(0.15)
            raise RuntimeError("persist boom")

        caplog.set_level(logging.ERROR, logger="cursor_ledger_worker")
        w.note("k", bad_write)
        assert started.wait(2.0), "write never started"
        # flush_now blocks until the worker's finally clears in_flight and sets
        # the idle event — proving the exception path still completes cleanup.
        assert w.flush_now("k", timeout=3.0) is True
        assert "k" not in w._in_flight
        assert any("write failed" in rec.message for rec in caplog.records)
    finally:
        _stop(w)


# ---------------------------------------------------------------------------
# flush_now: existing idle_event reuse, timeout, idle key
# ---------------------------------------------------------------------------


def test_flush_now_reuses_existing_idle_event():
    """A second concurrent flush_now for an in-flight key must find the idle
    event already created (the `event is None` False branch), not create a
    second one."""
    w = clw.CursorLedgerWorker(name="unit-reuse")
    try:
        started = threading.Event()
        proceed = threading.Event()

        def slow() -> None:
            started.set()
            proceed.wait(3.0)

        w.note("k", slow)
        assert started.wait(2.0)
        results: list[bool] = []

        def flusher():
            results.append(w.flush_now("k", timeout=5.0))

        t1 = threading.Thread(target=flusher)
        t2 = threading.Thread(target=flusher)
        t1.start()
        t2.start()
        time.sleep(0.2)  # both flushers parked; only one created the event
        assert len(w._idle_events) == 1  # reused, not duplicated
        proceed.set()
        t1.join(5.0)
        t2.join(5.0)
        assert results == [True, True]
    finally:
        _stop(w)


def test_flush_now_returns_false_on_timeout():
    w = clw.CursorLedgerWorker(name="unit-timeout")
    try:
        started = threading.Event()
        proceed = threading.Event()

        def slow() -> None:
            started.set()
            proceed.wait(10.0)

        w.note("k", slow)
        assert started.wait(2.0)
        assert w.flush_now("k", timeout=0.2) is False
        proceed.set()
        assert w.flush_now("k", timeout=3.0) is True  # eventually lands
    finally:
        _stop(w)


def test_flush_now_idle_key_returns_true_immediately():
    w = clw.CursorLedgerWorker(name="unit-idle")
    try:
        assert w.flush_now("absent-key", timeout=1.0) is True
    finally:
        _stop(w)


# ---------------------------------------------------------------------------
# note coalescing + requeue, and worker loop exit
# ---------------------------------------------------------------------------


def test_note_coalesces_in_flight_key_without_requeue():
    """A note() for a key whose write is already in_flight must NOT re-queue
    (already_scheduled True); the worker picks it up via the requeue path in
    its finally block."""
    w = clw.CursorLedgerWorker(name="unit-coalesce")
    try:
        started = threading.Event()
        proceed = threading.Event()
        written: list[str] = []

        def slow(v: str) -> None:
            if v == "first":
                started.set()
                proceed.wait(3.0)
            written.append(v)

        w.note("k", lambda: slow("first"))
        assert started.wait(2.0)  # first write in flight
        # These land while in_flight -> coalesced, not re-queued by note().
        for i in range(5):
            w.note("k", lambda i=i: slow(f"v{i}"))
        proceed.set()
        assert w.flush_now("k", timeout=5.0) is True
        assert written[0] == "first"
        # Only the latest coalesced value is persisted (last write wins).
        assert written[-1] == "v4"
        assert len(written) <= 3  # coalescing dropped the middle ones
    finally:
        _stop(w)


def test_worker_loop_exits_on_stop():
    """stop() must break the `while not self._stop.is_set()` loop; joining the
    thread (in _stop) captures the loop-exit branch and proves clean exit."""
    w = clw.CursorLedgerWorker(name="unit-exit")
    done = threading.Event()
    w.note("k", lambda: done.set())
    assert done.wait(2.0)
    # Idle past the queue.get timeout so the queue.Empty branch fires too.
    time.sleep(0.6)
    _stop(w)  # asserts the thread is dead
