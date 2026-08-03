from __future__ import annotations

import os
import queue
import sys
import threading
import types
from typing import Callable
from unittest.mock import patch

import pytest

import _test_home

_test_home.isolate("bc-test-session-search-projection-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import session_search_index  # noqa: E402
import session_search_projection as ssp  # noqa: E402


# Track every worker thread the module starts by intercepting Thread.start().
# The module constructs its worker via `threading.Thread(...)` at runtime, so
# rebinding the module-global `threading` attribute captures each start.
class _TrackingThread(threading.Thread):
    started: list["_TrackingThread"] = []

    def start(self) -> None:
        type(self).started.append(self)
        super().start()


def _install_tracking() -> tuple[list["_TrackingThread"], Callable[[], None]]:
    _TrackingThread.started = []
    fake_threading = types.SimpleNamespace(
        Thread=_TrackingThread,
        Lock=threading.Lock,
    )
    patcher = patch.object(ssp, "threading", fake_threading)
    patcher.start()
    return _TrackingThread.started, patcher.stop


def _reset_state() -> None:
    ssp._started = False
    ssp._queue = queue.Queue()


@pytest.fixture(autouse=True)
def threads():
    _reset_state()
    tracked, stop_tracking = _install_tracking()
    yield tracked
    # Tell any live worker to drain-and-exit, then deterministically join it.
    ssp._queue.put(None)
    for t in tracked:
        if t.is_alive():
            t.join(timeout=5)
    assert not any(t.is_alive() for t in tracked), "projection worker did not stop"
    stop_tracking()
    _reset_state()


# --- note_event_written: enqueues to a background worker that indexes ---

def test_note_event_written_indexes_via_worker(threads) -> None:
    indexed: list[tuple[str, dict]] = []
    with patch.object(session_search_index, "index_event", side_effect=lambda r, e: indexed.append((r, e))):
        ssp.note_event_written("root-1", {"kind": "x"})
        ssp.note_event_written("root-1", {"kind": "y"})  # second call hits _started fast path
        ssp._queue.join()  # blocks until task_done for every enqueued item
    assert indexed == [("root-1", {"kind": "x"}), ("root-1", {"kind": "y"})]
    assert len(threads) == 1


# --- _ensure_worker: double-checked locking, single thread ---

def test_ensure_worker_starts_at_most_one_thread(threads) -> None:
    ssp._ensure_worker()
    ssp._ensure_worker()
    assert len(threads) == 1


def test_ensure_worker_double_check_after_lock_starts_no_thread(threads) -> None:
    # Simulate the race loser: the outer check sees _started False, but another
    # caller has flipped _started True by the time the lock is held. The
    # double-check must return without starting a second thread.
    class _RaceLock:
        def __enter__(self):
            ssp._started = True
            return self

        def __exit__(self, *exc):
            return False

    ssp._started = False
    with patch.object(ssp, "_lock", _RaceLock()):
        ssp._ensure_worker()
    assert threads == []


# --- _worker_main: exception isolation ---

def test_worker_swallows_index_exception_and_continues(threads) -> None:
    calls: list[int] = []

    def flaky(_root_id: str, entry: dict) -> None:
        calls.append(entry["n"])
        if entry["n"] == 1:
            raise RuntimeError("boom")

    with patch.object(session_search_index, "index_event", side_effect=flaky):
        ssp.note_event_written("root", {"n": 1})  # raises inside the worker
        ssp.note_event_written("root", {"n": 2})  # must still be processed
        ssp._queue.join()
    assert calls == [1, 2]


# --- _worker_main: None sentinel stops the worker ---

def test_worker_stops_on_sentinel(threads) -> None:
    with patch.object(session_search_index, "index_event", lambda r, e: None):
        ssp.note_event_written("root", {"k": 1})
        ssp._queue.join()
    assert len(threads) == 1
    worker = threads[0]
    ssp._queue.put(None)
    worker.join(timeout=5)
    assert not worker.is_alive()
