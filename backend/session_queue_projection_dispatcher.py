from __future__ import annotations

import copy
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Any, Iterable, Iterator, Optional

from session_queue_projection_runtime import ProjectionRuntime, StorageContext


Record = dict[str, Any]


class ProjectionDispatcher:
    def __init__(
        self,
        context: StorageContext,
        projection_runtime: ProjectionRuntime,
    ) -> None:
        self.context = context
        self.runtime = projection_runtime
        self._cv = threading.Condition(threading.Lock())
        self._state = "open"
        self._active_producers = 0
        self._producer_threads: dict[int, int] = {}
        self._pending: dict[str, Record] = {}
        self._inflight: dict[str, Record] = {}
        self._failure: Optional[BaseException] = None
        self._executor: Optional[ThreadPoolExecutor] = None
        self._worker_scheduled = False

    @contextmanager
    def producer(self) -> Iterator[None]:
        thread_id = threading.get_ident()
        with self._cv:
            if self._state != "open":
                raise RuntimeError("queue projection dispatcher is closing")
            self._active_producers += 1
            self._producer_threads[thread_id] = (
                self._producer_threads.get(thread_id, 0) + 1
            )
        try:
            yield
        finally:
            with self._cv:
                depth = self._producer_threads[thread_id] - 1
                if depth:
                    self._producer_threads[thread_id] = depth
                else:
                    self._producer_threads.pop(thread_id)
                self._active_producers -= 1
                self._cv.notify_all()

    def submit(self, record: Record) -> None:
        sid = record.get("id")
        if not isinstance(sid, str) or not sid:
            return
        with self._cv:
            if (
                self._state != "open"
                and threading.get_ident() not in self._producer_threads
            ):
                raise RuntimeError("queue projection dispatcher is closing")
            self._pending[sid] = copy.deepcopy(record)
            self._start_worker_locked()
            self._cv.notify_all()

    def overlay(self, session_ids: Iterable[str]) -> dict[str, Record]:
        with self._cv:
            selected = {
                sid: self._pending.get(sid, self._inflight.get(sid))
                for sid in session_ids
                if sid in self._inflight or sid in self._pending
            }
        return copy.deepcopy(selected)

    def drain(self, timeout: Optional[float] = None) -> None:
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cv:
            while self._pending or self._inflight or self._worker_scheduled:
                if self._failure is not None:
                    self._failure = None
                    self._start_worker_locked()
                    self._cv.notify_all()
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise TimeoutError(
                        "queue projection submissions did not drain"
                    )
                self._cv.wait(remaining)
            if self._failure is not None:
                raise RuntimeError(
                    "queue projection submission failed"
                ) from self._failure

    def close(self, timeout: Optional[float] = None) -> None:
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cv:
            self._state = "closing"
            while self._active_producers:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise TimeoutError(
                        "queue projection producers did not quiesce"
                    )
                self._cv.wait(remaining)
        remaining = None if deadline is None else max(
            0.0,
            deadline - time.monotonic(),
        )
        self.drain(remaining)
        with self._cv:
            executor = self._executor
            self._executor = None
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=False)
        with self._cv:
            self._state = "closed"

    def _start_worker_locked(self) -> None:
        if self._worker_scheduled or self._failure is not None:
            return
        if self._executor is None:
            self._executor = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="queue-projection-submit",
            )
        self._worker_scheduled = True
        try:
            self._executor.submit(self._drain_worker)
        except BaseException:
            self._worker_scheduled = False
            raise

    def _drain_worker(self) -> None:
        while True:
            with self._cv:
                if not self._pending:
                    self._worker_scheduled = False
                    self._cv.notify_all()
                    return
                sid, record = self._pending.popitem()
                self._inflight[sid] = record
            try:
                self.runtime.upsert(record, durable=False)
            except BaseException as exc:
                with self._cv:
                    current = self._pending.get(sid)
                    if current is None:
                        self._pending[sid] = record
                    self._inflight.pop(sid, None)
                    self._failure = exc
                    self._worker_scheduled = False
                    self._cv.notify_all()
                return
            with self._cv:
                if self._inflight.get(sid) == record:
                    self._inflight.pop(sid, None)
                self._cv.notify_all()


class DispatcherRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._dispatcher: Optional[ProjectionDispatcher] = None

    def acquire(
        self,
        context: StorageContext,
        projection_runtime: ProjectionRuntime,
    ) -> ProjectionDispatcher:
        with self._lock:
            dispatcher = self._dispatcher
            if dispatcher is None:
                dispatcher = ProjectionDispatcher(context, projection_runtime)
                self._dispatcher = dispatcher
                return dispatcher
            if not dispatcher.context.equivalent_to(context):
                raise RuntimeError(
                    "queue projection dispatcher context mismatch"
                )
            return dispatcher

    def close(
        self,
        context: StorageContext,
        *,
        timeout: Optional[float] = None,
    ) -> None:
        with self._lock:
            dispatcher = self._dispatcher
            if dispatcher is None:
                return
            if not dispatcher.context.equivalent_to(context):
                raise RuntimeError(
                    "queue projection dispatcher context mismatch"
                )
        dispatcher.close(timeout)

    def release(self, context: StorageContext) -> None:
        with self._lock:
            dispatcher = self._dispatcher
            if (
                dispatcher is not None
                and dispatcher.context.equivalent_to(context)
            ):
                self._dispatcher = None


registry = DispatcherRegistry()
