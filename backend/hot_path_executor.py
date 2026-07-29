from __future__ import annotations

import asyncio
import contextvars
import time
from concurrent.futures import ThreadPoolExecutor

import perf


class HotPathExecutor:
    """A named thread pool for running blocking work off the event loop.

    Each instance owns its own threads, so a saturated pool can only
    delay its own callers. Every call records how long the work waited
    for a thread and how long it took, under the caller's metric name.
    The caller's contextvars are carried into the worker thread.
    """

    def __init__(self, name: str, max_workers: int) -> None:
        self._name = name
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix=name,
        )

    @property
    def name(self) -> str:
        return self._name

    async def run(self, metric: str, fn, /, *args, **kwargs):
        queued_at = time.perf_counter()
        ctx = contextvars.copy_context()

        def _call():
            perf.record(f"{metric}.queue_wait", (time.perf_counter() - queued_at) * 1000)
            return ctx.run(fn, *args, **kwargs)

        start = time.perf_counter()
        try:
            return await asyncio.get_running_loop().run_in_executor(
                self._executor,
                _call,
            )
        finally:
            perf.record(metric, (time.perf_counter() - start) * 1000)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)


# Separate pools so slow session-list work cannot starve session-detail
# requests, and neither can starve the general hot path.
hot_path = HotPathExecutor("hot-path", 8)
session_detail_path = HotPathExecutor("session-detail", 4)
session_list_path = HotPathExecutor("session-list", 4)

_ALL = (hot_path, session_detail_path, session_list_path)


def shutdown_all() -> None:
    for executor in _ALL:
        executor.shutdown()
