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
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

import perf

PROCESSOR_ADMISSION_TIMEOUT_SECONDS = 1.0
# Longer than the requirements processor's run_sync budget (720.5s). The public
# MCP client timeout stays higher, so processor completion/timeout owns the
# result instead of the public wrapper masking it first.
PROCESSOR_RESULT_TIMEOUT_SECONDS = 725.0

REQUIREMENTS_PROCESSOR_EXECUTOR = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix="requirements-processor",
)
# Wide enough for two processor forks each firing a full parallel round of
# index-SQL queries; each query holds its own readonly SQLite connection.
REQUIREMENTS_SEARCH_EXECUTOR = ThreadPoolExecutor(
    max_workers=16,
    thread_name_prefix="requirements-search",
)
_REQUIREMENTS_PROCESSOR_ADMISSION = threading.BoundedSemaphore(2)


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


async def run_requirements_processor_query(
    name: str,
    fn: Callable[..., Any],
    /,
    *,
    executor: ThreadPoolExecutor,
    admission_timeout_seconds: float = PROCESSOR_ADMISSION_TIMEOUT_SECONDS,
    result_timeout_seconds: float = PROCESSOR_RESULT_TIMEOUT_SECONDS,
    **kwargs: Any,
) -> Any:
    queued_at = time.perf_counter()
    if not await _acquire_processor_admission(admission_timeout_seconds):
        perf.record(f"{name}.admission_timeout", admission_timeout_seconds * 1000)
        perf.record(f"{name}.queue_wait", (time.perf_counter() - queued_at) * 1000)
        raise TimeoutError(
            "get-requirements processor admission timed out before a worker was available"
        )

    ctx = contextvars.copy_context()

    def _call() -> Any:
        perf.record(f"{name}.queue_wait", (time.perf_counter() - queued_at) * 1000)
        return ctx.run(fn, **kwargs)

    start = time.perf_counter()
    loop = asyncio.get_running_loop()
    try:
        future = loop.run_in_executor(executor, _call)
    except BaseException:
        _REQUIREMENTS_PROCESSOR_ADMISSION.release()
        raise
    future.add_done_callback(lambda _future: _REQUIREMENTS_PROCESSOR_ADMISSION.release())
    try:
        return await asyncio.wait_for(
            asyncio.shield(future),
            timeout=max(0.0, result_timeout_seconds),
        )
    except asyncio.TimeoutError as exc:
        perf.record(f"{name}.result_timeout", result_timeout_seconds * 1000)
        raise TimeoutError(
            "get-requirements processor timed out before returning requirements"
        ) from exc
    finally:
        perf.record(name, (time.perf_counter() - start) * 1000)


async def _acquire_processor_admission(timeout_seconds: float) -> bool:
    deadline = time.perf_counter() + max(0.0, timeout_seconds)
    while True:
        if _REQUIREMENTS_PROCESSOR_ADMISSION.acquire(blocking=False):
            return True
        if time.perf_counter() >= deadline:
            return False
        await asyncio.sleep(min(0.01, max(0.0, deadline - time.perf_counter())))
