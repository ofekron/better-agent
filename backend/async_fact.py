"""A one-shot fact that any event loop can await.

`asyncio.Event` binds to the first loop that awaits it, which makes it
the wrong primitive for something several loops care about. Backend
readiness signals are exactly that: startup recovery finishes on one
loop, while waiters may sit on the main loop, on a provisioning worker's
private loop, or on the recovery loop.

Working around that with a polled boolean costs latency and wakeups;
blocking a worker thread per waiter costs executor threads. `AsyncFact`
does neither: it keeps the truth in a plain flag under a lock, and gives
each awaiting loop its own `asyncio.Event`, woken through
`call_soon_threadsafe` when the fact is set.

Set-once, stays set. `set()` is safe from any thread; `wait()` is safe
from any loop.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Optional

logger = logging.getLogger(__name__)


class AsyncFact:
    def __init__(self, name: str = "") -> None:
        self._name = name
        self._set = False
        self._lock = threading.Lock()
        self._waiters: dict[int, tuple[asyncio.AbstractEventLoop, asyncio.Event]] = {}

    def is_set(self) -> bool:
        return self._set

    def set(self) -> None:
        """Mark the fact true and wake every waiting loop. Idempotent."""
        with self._lock:
            if self._set:
                return
            self._set = True
            waiters = list(self._waiters.values())
            self._waiters.clear()
        for loop, event in waiters:
            _wake(loop, event)

    def clear(self) -> None:
        """Return to the un-set state. For reuse across runs, not for
        racing a waiter — anything already awaiting stays woken."""
        with self._lock:
            self._set = False

    async def wait(self, timeout: Optional[float] = None) -> bool:
        """Await the fact from whichever loop is calling.

        Returns True if the fact is set, False on timeout.
        """
        if self._set:
            return True
        loop = asyncio.get_running_loop()
        event = asyncio.Event()
        key = id(event)
        with self._lock:
            if self._set:
                return True
            self._waiters[key] = (loop, event)
        try:
            if timeout is None:
                await event.wait()
                return True
            try:
                await asyncio.wait_for(event.wait(), timeout=timeout)
                return True
            except asyncio.TimeoutError:
                return False
        finally:
            with self._lock:
                self._waiters.pop(key, None)

    def waiter_count(self) -> int:
        with self._lock:
            return len(self._waiters)


def _wake(loop: asyncio.AbstractEventLoop, event: asyncio.Event) -> None:
    if loop.is_closed():
        return
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is loop:
        event.set()
        return
    try:
        loop.call_soon_threadsafe(event.set)
    except RuntimeError:
        # Loop stopped between the closed check and the wake.
        logger.debug("async_fact: waiter loop stopped before wake")
