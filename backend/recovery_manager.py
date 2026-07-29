"""Execution host for crash recovery.

Recovery is startup-time work that competes with request handlers for
both the main event loop and asyncio's default executor. This module
gives it a home of its own: one dedicated thread running one event
loop, backed by a bounded executor, so a long run-directory scan or
replay cannot starve a user's first request.

The manager is an execution host, not a policy owner. It knows how to
run work off the main loop and how to announce what happened; it does
not decide what recovery means. Phases move onto it one at a time, and
only after each phase is shown to be free of main-loop affinity —
`recovery_schedule` owns bucket ordering, `run_recovery` still owns the
integration pipeline.

Announcements go out as facts on the event bus
(`recovery.started` / `recovery.session_ready` / `recovery.finished` /
`recovery.failed`), published cross-thread so main-loop subscribers
receive them on the main loop. Facts describe what happened; the
subscriber decides what to do about it.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
from typing import Any, Awaitable, Callable, Optional

from event_bus import BusEvent, bus

logger = logging.getLogger(__name__)

RECOVERY_STARTED = "recovery.started"
RECOVERY_SESSION_READY = "recovery.session_ready"
RECOVERY_FINISHED = "recovery.finished"
RECOVERY_FAILED = "recovery.failed"

# Blocking recovery IO (run-dir scans, replay reads, marker writes) runs
# here instead of asyncio's default executor, so it cannot exhaust the
# thread pool that live request handlers share.
_DEFAULT_EXECUTOR_WORKERS = 4


class RecoveryManager:
    """Owns the recovery thread, its loop, and its executor."""

    def __init__(self, *, executor_workers: int = _DEFAULT_EXECUTOR_WORKERS) -> None:
        self._executor_workers = executor_workers
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._executor: Optional[concurrent.futures.ThreadPoolExecutor] = None
        self._lock = threading.Lock()

    # -- lifecycle ------------------------------------------------------

    def start(self) -> None:
        """Bring up the recovery thread. Idempotent."""
        with self._lock:
            if self._thread is not None:
                return
            started = threading.Event()
            loop = asyncio.new_event_loop()
            executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=self._executor_workers,
                thread_name_prefix="recovery-io",
            )
            loop.set_default_executor(executor)

            def _run() -> None:
                asyncio.set_event_loop(loop)
                loop.call_soon(started.set)
                try:
                    loop.run_forever()
                finally:
                    loop.close()

            thread = threading.Thread(
                target=_run, name="recovery-manager", daemon=True,
            )
            self._loop = loop
            self._executor = executor
            self._thread = thread
            thread.start()
        started.wait()

    def stop(self, *, timeout: float = 10.0) -> None:
        """Stop the recovery thread and release its executor. Idempotent.

        Safe to call from the main loop: it never awaits recovery work,
        it asks the loop to stop and joins the thread.
        """
        with self._lock:
            loop, thread, executor = self._loop, self._thread, self._executor
            self._loop = self._thread = self._executor = None
        if loop is None or thread is None:
            return
        try:
            loop.call_soon_threadsafe(loop.stop)
        except RuntimeError:
            pass
        thread.join(timeout=timeout)
        if thread.is_alive():
            logger.warning("recovery manager thread did not stop within %.1fs", timeout)
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)

    @property
    def running(self) -> bool:
        loop = self._loop
        return loop is not None and not loop.is_closed()

    @property
    def loop(self) -> Optional[asyncio.AbstractEventLoop]:
        return self._loop

    # -- submission -----------------------------------------------------

    async def run(self, factory: Callable[[], Awaitable[Any]]) -> Any:
        """Await a coroutine on the recovery loop from the main loop.

        Takes a factory rather than a coroutine so the coroutine object
        is created on the loop that will run it, and so nothing is left
        un-awaited if the manager is down.

        The caller still awaits the result — this moves the WORK off the
        main loop, it does not make the caller fire-and-forget. That is
        the point: startup can keep its existing sequencing while the
        expensive part stops competing with request handlers.
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            # Fail closed onto the caller's loop rather than silently
            # skipping recovery work.
            logger.warning("recovery manager not running; executing inline")
            return await factory()
        future = asyncio.run_coroutine_threadsafe(_invoke(factory), loop)
        return await asyncio.wrap_future(future)

    # -- facts ----------------------------------------------------------

    def publish_fact(
        self,
        event_type: str,
        *,
        sid: str = "",
        root_id: str = "",
        payload: Optional[dict] = None,
    ) -> None:
        """Announce a recovery fact to main-loop subscribers.

        Callable from the recovery loop or any other thread. Never
        raises and never blocks the caller: an announcement must not be
        able to fail recovery itself.
        """
        event = BusEvent(
            type=event_type,
            root_id=root_id or sid,
            sid=sid,
            payload=dict(payload or {}),
            persist=False,
        )
        try:
            bus.publish_threadsafe(event)
        except Exception:
            logger.exception("recovery manager failed to publish %s", event_type)


async def _invoke(factory: Callable[[], Awaitable[Any]]) -> Any:
    return await factory()


manager = RecoveryManager()
