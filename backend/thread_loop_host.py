"""Reusable execution host: one thread, one loop, one bounded executor.

Subsystems that own long or blocking work need a home that is not the
main request loop. This is that home, factored out so every such
subsystem shares one implementation of thread lifecycle, cross-loop
submission, and cross-thread fact publishing.

A host is an execution host, not a policy owner. It knows how to run
work off the main loop and how to announce what happened; what the work
means belongs to the subsystem that subclasses it.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
from typing import Any, Awaitable, Callable, Optional

from event_bus import BusEvent, bus

logger = logging.getLogger(__name__)

_DEFAULT_EXECUTOR_WORKERS = 4


class ThreadLoopHost:
    """Owns a dedicated thread, its event loop, and its executor."""

    def __init__(
        self,
        *,
        name: str,
        executor_workers: int = _DEFAULT_EXECUTOR_WORKERS,
        io_thread_name_prefix: str = "",
    ) -> None:
        self._name = name
        self._io_thread_name_prefix = io_thread_name_prefix or f"{name}-io"
        self._executor_workers = executor_workers
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._executor: Optional[concurrent.futures.ThreadPoolExecutor] = None
        self._lock = threading.Lock()

    # -- lifecycle ------------------------------------------------------

    def start(self) -> None:
        """Bring up the host thread. Idempotent."""
        with self._lock:
            if self._thread is not None:
                return
            started = threading.Event()
            loop = asyncio.new_event_loop()
            executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=self._executor_workers,
                thread_name_prefix=self._io_thread_name_prefix,
            )
            loop.set_default_executor(executor)

            def _run() -> None:
                asyncio.set_event_loop(loop)
                loop.call_soon(started.set)
                try:
                    loop.run_forever()
                finally:
                    loop.close()

            thread = threading.Thread(target=_run, name=self._name, daemon=True)
            self._loop = loop
            self._executor = executor
            self._thread = thread
            thread.start()
        started.wait()

    def stop(self, *, timeout: float = 10.0) -> None:
        """Stop the host thread and release its executor. Idempotent.

        Safe to call from the main loop only via a worker thread: it
        joins its own thread and must not block the loop it runs on.
        """
        self.on_stopping()
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
            logger.warning("%s thread did not stop within %.1fs", self._name, timeout)
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)

    def on_stopping(self) -> None:
        """Hook for subclasses: release external wiring before teardown."""

    @property
    def running(self) -> bool:
        loop = self._loop
        return loop is not None and not loop.is_closed()

    @property
    def loop(self) -> Optional[asyncio.AbstractEventLoop]:
        return self._loop

    # -- submission -----------------------------------------------------

    async def run(self, factory: Callable[[], Awaitable[Any]]) -> Any:
        """Await a coroutine on the host loop from the caller's loop.

        Takes a factory rather than a coroutine so the coroutine object
        is created on the loop that will run it, and so nothing is left
        un-awaited if the host is down.

        The caller still awaits the result — this moves the WORK off the
        caller's loop, it does not make the caller fire-and-forget.

        Cancelling the caller does NOT abandon work already running: the
        cancellation is delivered only after the host-side coroutine
        settles, so shutdown cannot tear the host down underneath
        half-finished work.
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            # Fail closed onto the caller's loop rather than silently
            # skipping the work.
            logger.warning("%s not running; executing inline", self._name)
            return await factory()
        future = asyncio.run_coroutine_threadsafe(_invoke(factory), loop)
        awaitable = asyncio.wrap_future(future)
        try:
            return await asyncio.shield(awaitable)
        except asyncio.CancelledError:
            if not future.done():
                try:
                    await asyncio.shield(awaitable)
                except asyncio.CancelledError:
                    await awaitable
                except Exception:
                    logger.exception("%s work failed while cancelling", self._name)
            raise

    async def run_blocking(self, fn: Callable[..., Any], *args: Any) -> Any:
        """Await a blocking callable on this host's own executor.

        The sync counterpart of `run`: same guarantee that the work
        leaves the caller's loop, for subsystems whose work is plain
        blocking code rather than a coroutine. Using the host's executor
        rather than the default one keeps a slow subsystem from
        starving every other `to_thread` caller in the process.
        """
        async def _call() -> Any:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, lambda: fn(*args))

        return await self.run(_call)

    # -- facts ----------------------------------------------------------

    def publish_fact(
        self,
        event_type: str,
        *,
        sid: str = "",
        root_id: str = "",
        payload: Optional[dict] = None,
    ) -> None:
        """Announce a fact to main-loop subscribers.

        Callable from the host loop or any other thread. Never raises
        and never blocks: an announcement must not be able to fail the
        work it describes.
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
            logger.exception("%s failed to publish %s", self._name, event_type)


async def _invoke(factory: Callable[[], Awaitable[Any]]) -> Any:
    return await factory()
