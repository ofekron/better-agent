"""Single dedicated background thread that persists provenance rows
(the tool + WHY log behind the Details/Changes panel) completely off
the render-tree `apply_event` dispatch path.

Why this exists: `apply_event` (orchs/base.py) used to call
`provenance_store.record_from_event` SYNCHRONOUSLY — a blocking disk
append (open + write) serialized by a single GLOBAL `threading.Lock()`
shared by every session — directly from `turn_manager.save_ws_callback`,
which awaits `loop.run_in_executor(_STREAM_EVENT_APPLY_EXECUTOR, ...)`
on a process-wide 2-worker executor shared by EVERY concurrent
session's live event stream. One session's slow/contended provenance
write could stall a COMPLETELY UNRELATED session's render-tree event
application. This is the render-apply-path sibling of the bug
`cursor_ledger_worker` fixed for tailer cursor persistence — same
shape, worse blast radius, because it sits on the correctness-critical
live apply path itself rather than a crash-recovery side channel.

This worker removes the coupling: `note()` is an O(1), lock-only,
non-blocking call that appends the event to a per-session pending
list and returns immediately — the hot path never touches disk or
waits on I/O. A single background thread drains pending events and
calls the UNCHANGED, still-synchronous
`provenance_store.record_from_event` for each one, in submission
order, one session at a time. It is the ONLY thread that ever touches
`provenance_store`'s on-disk state in production, which is what lets
`provenance_store` drop its own global lock (see that module) without
introducing a race.

Unlike `cursor_ledger_worker`, pending entries ACCUMULATE rather than
being replaced by the latest: a cursor advance is monotonic (only the
newest value matters), but every provenance event carries distinct
tool rows that must all be persisted — coalescing to "latest wins"
would silently drop rows from any event that arrived while a prior
one for the same session was still queued.

The "provenance_changed" Details-panel ping is deferred to fire only
after a batch's rows actually land on disk (passed in as
`on_written`), keeping that side effect off the hot path too.
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class ProvenanceLedgerWorker:
    def __init__(self, *, name: str = "provenance-ledger-worker") -> None:
        self._lock = threading.Lock()
        self._pending: dict[str, list[tuple[dict, Optional[str]]]] = {}
        self._in_flight: set[str] = set()
        self._on_written: dict[str, Callable[[str], None]] = {}
        self._queue: "queue.SimpleQueue[str]" = queue.SimpleQueue()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()

    def note(
        self,
        app_session_id: str,
        normalized: dict,
        *,
        backend_msg_id: Optional[str] = None,
        on_written: Optional[Callable[[str], None]] = None,
    ) -> None:
        """Queue one event's provenance rows for `app_session_id` to be
        extracted + appended off this call path. O(1): an in-memory
        list append under a plain lock, never touches disk. Safe to
        call on every dispatched event — never blocks, however slow
        or contended the actual write is."""
        with self._lock:
            already_scheduled = (
                app_session_id in self._pending or app_session_id in self._in_flight
            )
            self._pending.setdefault(app_session_id, []).append(
                (normalized, backend_msg_id)
            )
            if on_written is not None:
                self._on_written[app_session_id] = on_written
        if not already_scheduled:
            self._queue.put(app_session_id)

    def _process_key(self, app_session_id: str) -> None:
        from stores import provenance_store

        with self._lock:
            batch = self._pending.pop(app_session_id, [])
            on_written = self._on_written.pop(app_session_id, None)
            if not batch:
                return
            self._in_flight.add(app_session_id)
        try:
            written_total = 0
            for normalized, backend_msg_id in batch:
                try:
                    written_total += provenance_store.record_from_event(
                        app_session_id, normalized, backend_msg_id=backend_msg_id,
                    )
                except Exception:
                    logger.debug(
                        "provenance record failed sid=%s", app_session_id,
                        exc_info=True,
                    )
            if written_total and on_written is not None:
                try:
                    on_written(app_session_id)
                except Exception:
                    logger.exception(
                        "provenance on_written callback failed sid=%s",
                        app_session_id,
                    )
        finally:
            with self._lock:
                self._in_flight.discard(app_session_id)
                # A note() may have landed while this batch was in
                # flight (note() saw `app_session_id in self._in_flight`
                # and skipped re-queueing, trusting us to notice it
                # here). Requeueing preserves submission order: only
                # this one worker thread ever pops/appends a given
                # session's pending list.
                requeue = app_session_id in self._pending
        if requeue:
            self._queue.put(app_session_id)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                key = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            self._process_key(key)

    def stop(self) -> None:
        self._stop.set()


worker = ProvenanceLedgerWorker()
