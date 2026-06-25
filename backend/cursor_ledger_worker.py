"""Single dedicated background thread that persists tailer cursor-advance
side-effects (backend_state.json writes, spawn_ledger records) completely
off the render-tree dispatch path.

Why this exists: every native provider's tailer used to submit a persist
job to a shared thread pool on EVERY dispatched line and `await` it before
reading the next line — so persistence latency (disk I/O, lock contention)
directly stalled dispatch to the render tree under real backend
concurrency, sometimes for tens of seconds, silently truncating turns
(see incident notes in git history for session e84dc9e3). Debouncing the
persist frequency helped but the tailer still awaited the executor future
on every debounce-triggered write.

This worker removes the coupling entirely: `note()` is an O(1),
lock-only, non-blocking call — the tailer's loop never waits on it, not
even briefly. A single background thread does all the actual I/O,
serialized, one write in flight at a time. If more `note()` calls arrive
for the same key while a write for that key is in flight, they simply
overwrite the pending callback — only the LATEST cursor value is ever
persisted, and the worker's own throughput becomes the natural coalescing
throttle (idle system → persists nearly every advance; busy system →
automatically batches, no manual tuning of count/interval thresholds).

`flush_now()` blocks the CALLING thread (not the event loop — callers
wrap it in `asyncio.to_thread`) until the latest known value for a key
has actually been written, for the one place that needs that guarantee:
after a turn's deterministic drain, so crash recovery sees the true final
cursor.
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class CursorLedgerWorker:
    def __init__(self, *, name: str = "cursor-ledger-worker") -> None:
        self._lock = threading.Lock()
        self._latest: dict[str, Callable[[], None]] = {}
        self._in_flight: set[str] = set()
        self._idle_events: dict[str, threading.Event] = {}
        self._queue: "queue.SimpleQueue[str]" = queue.SimpleQueue()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()

    def note(self, key: str, write_fn: Callable[[], None]) -> None:
        """Register `write_fn` as the latest pending persist for `key`.
        O(1): a dict write under a plain lock, never touches disk. Safe
        to call on every single dispatched line — callers do NOT need to
        debounce this themselves."""
        with self._lock:
            already_scheduled = key in self._latest or key in self._in_flight
            self._latest[key] = write_fn
        if not already_scheduled:
            self._queue.put(key)

    def flush_now(self, key: str, *, timeout: float = 5.0) -> bool:
        """Block until `key`'s latest pending write has been persisted
        (or there is none pending). Returns False on timeout — callers
        treat that the same as any other degraded-drain fallback."""
        with self._lock:
            if key not in self._latest and key not in self._in_flight:
                return True
            event = self._idle_events.get(key)
            if event is None:
                event = threading.Event()
                self._idle_events[key] = event
        return event.wait(timeout)

    def _process_key(self, key: str) -> None:
        with self._lock:
            write_fn = self._latest.pop(key, None)
            if write_fn is None:
                return
            self._in_flight.add(key)
        try:
            write_fn()
        except Exception:
            logger.exception("cursor_ledger_worker: write failed for key=%s", key)
        finally:
            with self._lock:
                self._in_flight.discard(key)
                # A note() may have landed while this write was in flight
                # (note() saw `key in self._in_flight` and skipped
                # re-queueing, trusting us to notice it here).
                requeue = key in self._latest
                event = None if requeue else self._idle_events.pop(key, None)
        if requeue:
            self._queue.put(key)
        elif event is not None:
            event.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                key = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            self._process_key(key)

    def stop(self) -> None:
        self._stop.set()


worker = CursorLedgerWorker()
