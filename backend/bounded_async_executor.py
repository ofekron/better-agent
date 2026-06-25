from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from typing import Any, Callable

import perf


class AdmissionOverloaded(RuntimeError):
    pass


class BoundedAsyncExecutor:
    def __init__(
        self,
        *,
        name: str,
        max_workers: int,
        capacity: int,
        timeout_seconds: float,
    ) -> None:
        self._name = name
        self._capacity = capacity
        self._timeout_seconds = timeout_seconds
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix=name,
        )
        self._lock = threading.Lock()
        self._in_use = 0
        self._closed = False
        self._waiters: deque[tuple[asyncio.AbstractEventLoop, asyncio.Future[None]]] = deque()
        perf.register_queue(name, self.depth)

    def depth(self) -> int:
        with self._lock:
            return self._in_use

    async def _acquire(self) -> None:
        loop = asyncio.get_running_loop()
        with self._lock:
            if self._closed:
                raise RuntimeError(f"{self._name} executor is closed")
            if self._in_use < self._capacity:
                self._in_use += 1
                perf.record_count(f"{self._name}.admitted_depth", self._in_use)
                return
            waiter = loop.create_future()
            entry = (loop, waiter)
            self._waiters.append(entry)
        started = time.perf_counter()
        try:
            await asyncio.wait_for(asyncio.shield(waiter), self._timeout_seconds)
        except asyncio.CancelledError:
            granted = False
            with self._lock:
                try:
                    self._waiters.remove(entry)
                except ValueError:
                    granted = True
            if granted:
                self._release()
            raise
        except TimeoutError as exc:
            granted = False
            with self._lock:
                try:
                    self._waiters.remove(entry)
                except ValueError:
                    granted = True
            if granted:
                self._release()
            perf.record_count(f"{self._name}.rejected")
            perf.record(
                f"{self._name}.admission_wait",
                (time.perf_counter() - started) * 1000.0,
            )
            raise AdmissionOverloaded(f"{self._name} capacity is full") from exc

    def _release(self) -> None:
        while True:
            with self._lock:
                while self._waiters:
                    loop, waiter = self._waiters.popleft()
                    if waiter.done() or loop.is_closed():
                        continue
                    break
                else:
                    self._in_use -= 1
                    return
            try:
                def grant() -> None:
                    if waiter.done():
                        self._release()
                        return
                    waiter.set_result(None)

                loop.call_soon_threadsafe(grant)
                return
            except RuntimeError:
                continue

    async def run(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        await self._acquire()
        queued_at = time.perf_counter()
        context = copy_context()

        def call() -> Any:
            perf.record(
                f"{self._name}.queue_wait",
                (time.perf_counter() - queued_at) * 1000.0,
            )
            started = time.perf_counter()
            try:
                return context.run(fn, *args, **kwargs)
            finally:
                perf.record(
                    f"{self._name}.run",
                    (time.perf_counter() - started) * 1000.0,
                )

        loop = asyncio.get_running_loop()
        try:
            future = loop.run_in_executor(self._executor, call)
        except BaseException:
            self._release()
            raise
        future.add_done_callback(lambda _future: self._release())
        return await asyncio.shield(future)

    async def shutdown(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            waiters = list(self._waiters)
            self._waiters.clear()
        for loop, waiter in waiters:
            if not waiter.done() and not loop.is_closed():
                loop.call_soon_threadsafe(
                    waiter.set_exception,
                    RuntimeError(f"{self._name} executor is closed"),
                )
        await asyncio.to_thread(self._executor.shutdown, wait=True, cancel_futures=False)
        perf.unregister_queue(self._name)
