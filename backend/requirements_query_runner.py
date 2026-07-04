"""Bounded executor for requirements query endpoints."""
from __future__ import annotations

import asyncio
import contextvars
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

import perf

REQUIREMENTS_SEARCH_EXECUTOR = ThreadPoolExecutor(
    max_workers=4,
    thread_name_prefix="requirements-search",
)


async def run_requirements_query(
    name: str,
    fn: Callable[..., Any],
    /,
    *,
    executor: ThreadPoolExecutor,
    **kwargs: Any,
) -> Any:
    queued_at = time.perf_counter()
    ctx = contextvars.copy_context()

    def _call() -> Any:
        perf.record(f"{name}.queue_wait", (time.perf_counter() - queued_at) * 1000)
        return ctx.run(fn, **kwargs)

    start = time.perf_counter()
    try:
        return await asyncio.get_running_loop().run_in_executor(executor, _call)
    finally:
        perf.record(name, (time.perf_counter() - start) * 1000)
