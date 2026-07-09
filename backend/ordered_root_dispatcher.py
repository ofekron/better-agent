from __future__ import annotations

import asyncio
import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Callable, Generic, TypeVar


T = TypeVar("T")


class OrderedRootDispatcher(Generic[T]):
    def __init__(
        self,
        handler: Callable[[T], None],
        *,
        pool_size: int,
        thread_name_prefix: str,
        logger: logging.Logger,
        on_error: Callable[[str, T, BaseException], None] | None = None,
        max_pending: int | None = None,
    ) -> None:
        self._handler = handler
        self._logger = logger
        self._on_error = on_error
        self._lock = threading.RLock()
        self._closed = False
        self._errors_closed = False
        self._capacity = threading.BoundedSemaphore(
            max_pending if max_pending is not None else pool_size * 64,
        )
        self._pending_errors: dict[str, tuple[T, BaseException]] = {}
        self._error_drain_scheduled = False
        self._error_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix=f"{thread_name_prefix}-errors",
        )
        self._executors = tuple(
            ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix=f"{thread_name_prefix}-{index}",
            )
            for index in range(pool_size)
        )

    def submit(self, root_id: str, item: T) -> Future[None]:
        with self._lock:
            if self._closed:
                return self._reject(
                    root_id,
                    item,
                    RuntimeError("ordered root dispatcher is closed"),
                )
            if not self._capacity.acquire(blocking=False):
                return self._reject(
                    root_id,
                    item,
                    RuntimeError("ordered root dispatcher backlog is full"),
                )
            future = self._executor(root_id).submit(self._handler, item)
        future.add_done_callback(
            lambda completed: self._handle_completion(root_id, item, completed),
        )
        return future

    async def barrier(self, root_id: str) -> None:
        future = self._executor(root_id).submit(lambda: None)
        await asyncio.wrap_future(future)

    def shutdown(self, *, wait: bool = True) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        for executor in self._executors:
            executor.shutdown(wait=wait)
        with self._lock:
            self._errors_closed = True
        self._error_executor.shutdown(wait=wait)

    def _executor(self, root_id: str) -> ThreadPoolExecutor:
        return self._executors[hash(root_id) % len(self._executors)]

    def _handle_completion(
        self,
        root_id: str,
        item: T,
        future: Future[None],
    ) -> None:
        if future.cancelled():
            self._capacity.release()
            return
        exc = future.exception()
        self._capacity.release()
        if exc is None:
            return
        self._logger.error(
            "ordered root dispatch failed",
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        self._schedule_error(root_id, item, exc)

    def _reject(self, root_id: str, item: T, exc: BaseException) -> Future[None]:
        future: Future[None] = Future()
        future.set_exception(exc)
        self._logger.error("ordered root dispatch rejected root=%s: %s", root_id, exc)
        self._schedule_error(root_id, item, exc)
        return future

    def _schedule_error(self, root_id: str, item: T, exc: BaseException) -> None:
        if self._on_error is None:
            return
        with self._lock:
            if self._errors_closed:
                return
            self._pending_errors[root_id] = (item, exc)
            if self._error_drain_scheduled:
                return
            self._error_drain_scheduled = True
            self._error_executor.submit(self._drain_errors)

    def _drain_errors(self) -> None:
        while True:
            with self._lock:
                if not self._pending_errors:
                    self._error_drain_scheduled = False
                    return
                root_id, (item, exc) = self._pending_errors.popitem()
            try:
                if self._on_error is not None:
                    self._on_error(root_id, item, exc)
            except Exception:
                self._logger.exception(
                    "ordered root dispatch error handler failed root=%s",
                    root_id,
                )
