"""Queue-backed logging transport: install at boot, flush at shutdown."""
from __future__ import annotations

import logging
import logging.handlers
import queue

_LOG_QUEUE_LISTENERS: list[logging.handlers.QueueListener] = []


def install_queued_logging(target_logger: logging.Logger, *handlers: logging.Handler) -> None:
    """Route every record through an in-memory queue serviced by a
    dedicated listener thread, instead of letting the calling thread write
    to `handlers` directly. `logging.FileHandler`/`StreamHandler` writes
    synchronously under a lock shared by every thread using that handler —
    with 80+ background worker threads (ledger writers, event ingesters,
    extension backends, ...) all logging heavily, the asyncio event-loop
    thread can block on that lock for multiple seconds waiting its turn,
    making the backend briefly deaf to every client's request regardless
    of platform or network (observed once via the process's own
    faulthandler dump: the event-loop thread caught inside
    `logging.StreamHandler.flush()`'s `with self.lock:`; that specific
    sample has since rotated out of the dump file and can't be re-cited,
    but a slow/contended handler on the calling thread's path is a real
    risk independent of that one observation). Queuing makes every
    `logger.log()` call a fast, lock-free `put_nowait()`; the slow file
    I/O happens exclusively on the listener thread.

    Two accepted tradeoffs: the queue is unbounded, so a disk stall or
    full disk no longer blocks producers (the bug this fixes) but instead
    grows memory without limit until something else notices — this is
    the stdlib-canonical pattern, but it trades a latency failure for a
    potential memory failure. And records sitting in the queue when the
    process is hard-killed (not a graceful shutdown) are lost, unlike a
    synchronous handler that would have already written them."""
    log_queue: queue.SimpleQueue = queue.SimpleQueue()
    target_logger.handlers = []
    target_logger.addHandler(logging.handlers.QueueHandler(log_queue))
    listener = logging.handlers.QueueListener(log_queue, *handlers, respect_handler_level=True)
    listener.start()
    _LOG_QUEUE_LISTENERS.append(listener)


def stop_queued_logging() -> None:
    """Flush and join every queue listener's thread. Call once, late in
    shutdown, after every other shutdown step has finished logging."""
    for listener in _LOG_QUEUE_LISTENERS:
        listener.stop()
