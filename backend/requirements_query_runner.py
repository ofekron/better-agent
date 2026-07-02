"""Bounded executors for the requirements query endpoints.

The public ``/api/internal/get-requirements`` handler runs a long-lived fork
processor (``provisioning.run_sync``) that calls back into
``/api/internal/get-requirements/search`` via the ``get_requirements_internal``
MCP tool. Sharing one bounded pool between both endpoints self-deadlocks under
two or more concurrent public calls: every worker is consumed by a long-running
processor while each processor's ``/search`` callback queues behind them and
starves, surfacing as 120s tool-call timeouts.

The processor path (reentrant, long-running) and the search path (leaf, fast)
therefore run on SEPARATE pools. The invariant a fix must never violate: a task
running on the processor pool must never wait on a pool slot it already holds.
"""
from __future__ import annotations

import asyncio
import contextvars
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

import perf

REQUIREMENTS_PROCESSOR_EXECUTOR = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix="requirements-processor",
)
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
