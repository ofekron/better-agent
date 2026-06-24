from __future__ import annotations

import logging
import queue
import threading
from typing import Any

logger = logging.getLogger(__name__)

_queue: queue.Queue[tuple[str, dict[str, Any]] | None] = queue.Queue()
_started = False
_lock = threading.Lock()


def note_event_written(root_id: str, entry: dict[str, Any]) -> None:
    _ensure_worker()
    _queue.put((root_id, entry))


def _ensure_worker() -> None:
    global _started
    if _started:
        return
    with _lock:
        if _started:
            return
        thread = threading.Thread(
            target=_worker_main,
            name="session-search-projection",
            daemon=True,
        )
        thread.start()
        _started = True


def _worker_main() -> None:
    while True:
        item = _queue.get()
        if item is None:
            _queue.task_done()
            return
        try:
            import session_search_index
            root_id, entry = item
            session_search_index.index_event(root_id, entry)
        except Exception:
            logger.debug("session search projection failed", exc_info=True)
        finally:
            _queue.task_done()


def drain_for_tests() -> None:
    _ensure_worker()
    _queue.join()
