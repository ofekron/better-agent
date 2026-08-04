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

State isolation: a throwaway BETTER_AGENT_HOME via `_test_home` before any
backend import.
"""
from __future__ import annotations

import logging
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import _test_home

_TEST_HOME = _test_home.TestHome.acquire("ba-clw-unit-")

import cursor_ledger_worker as clw  # noqa: E402


def setup_function(_function) -> None:
    _test_home.engage(_TEST_HOME.path)


def _stop(worker: clw.CursorLedgerWorker) -> None:
    """Stop the worker and join its thread so the loop-exit branch is
    captured and no thread is left dangling."""
    worker.stop()
    worker._thread.join(timeout=3.0)
    assert not worker._thread.is_alive(), "worker thread did not exit"


class _ObservedIdleEvents(dict[str, threading.Event]):
    def __init__(self) -> None:
        super().__init__()
        self.created = threading.Event()
        self.reused = threading.Event()

    def get(self, key, default=None):
        event = super().get(key, default)
        if event is not None:
            self.reused.set()
        return event

    def __setitem__(self, key, value) -> None:
        super().__setitem__(key, value)
        self.created.set()


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
    proceed = threading.Event()
    try:
        started = threading.Event()

        def bad_write() -> None:
            started.set()
            proceed.wait(3.0)
            raise RuntimeError("persist boom")

        caplog.set_level(logging.ERROR, logger="cursor_ledger_worker")
        w.note("k", bad_write)
        assert started.wait(2.0), "write never started"
        # Releasing the write lets flush_now observe the exception path's
        # finally cleanup rather than relying on a duration guess.
        proceed.set()
        assert w.flush_now("k", timeout=3.0) is True
        assert "k" not in w._in_flight
        assert any("write failed" in rec.message for rec in caplog.records)
    finally:
        proceed.set()
        _stop(w)


# ---------------------------------------------------------------------------
# flush_now: existing idle_event reuse, timeout, idle key
# ---------------------------------------------------------------------------


def test_flush_now_reuses_existing_idle_event():
    """A second concurrent flush_now for an in-flight key must find the idle
    event already created (the `event is None` False branch), not create a
    second one."""
    w = clw.CursorLedgerWorker(name="unit-reuse")
    proceed = threading.Event()
    threads: list[threading.Thread] = []
    try:
        started = threading.Event()
        observed = _ObservedIdleEvents()
        w._idle_events = observed

        def slow() -> None:
            started.set()
            proceed.wait(3.0)

        w.note("k", slow)
        assert started.wait(2.0)
        results: list[bool] = []

        def flusher():
            results.append(w.flush_now("k", timeout=5.0))

        threads = [
            threading.Thread(target=flusher, name="cursor-flush-first"),
            threading.Thread(target=flusher, name="cursor-flush-second"),
        ]
        threads[0].start()
        assert observed.created.wait(2.0), "first flusher did not create an idle event"
        threads[1].start()
        assert observed.reused.wait(2.0), "second flusher did not reuse the idle event"
        assert len(w._idle_events) == 1  # reused, not duplicated
        proceed.set()
        for thread in threads:
            thread.join(5.0)
            assert not thread.is_alive(), f"{thread.name} did not exit"
        assert results == [True, True]
    finally:
        proceed.set()
        for thread in threads:
            thread.join(5.0)
        _stop(w)


def test_flush_now_returns_false_on_timeout():
    w = clw.CursorLedgerWorker(name="unit-timeout")
    proceed = threading.Event()
    try:
        started = threading.Event()

        def slow() -> None:
            started.set()
            proceed.wait(10.0)

        w.note("k", slow)
        assert started.wait(2.0)
        assert w.flush_now("k", timeout=0.2) is False
        proceed.set()
        assert w.flush_now("k", timeout=3.0) is True  # eventually lands
    finally:
        proceed.set()
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
    proceed = threading.Event()
    try:
        started = threading.Event()
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
        proceed.set()
        _stop(w)


def test_worker_loop_exits_on_stop():
    """stop() must break the worker loop and permit a clean join."""
    w = clw.CursorLedgerWorker(name="unit-exit")
    done = threading.Event()
    w.note("k", lambda: done.set())
    assert done.wait(2.0)
    _stop(w)  # asserts the thread is dead


def teardown_module() -> None:
    _TEST_HOME.release()
