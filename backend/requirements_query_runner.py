"""Bounded executors for the requirements query endpoints.

The public ``/api/internal/get-requirements`` handler runs a long-lived fork
processor (``provisioning.run_sync``) that calls back into
``/api/internal/get-requirements/search`` via processor-only evidence tools.
Sharing one bounded pool between both endpoints self-deadlocks under
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
import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping

import perf

logger = logging.getLogger(__name__)

PROCESSOR_ADMISSION_TIMEOUT_SECONDS = 30.0
# Longer than the requirements processor's run_sync budget (1320.5s). The public
# MCP client timeout stays higher, so processor completion/timeout owns the
# result instead of the public wrapper masking it first.
PROCESSOR_RESULT_TIMEOUT_SECONDS = 1350.0

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
_PROCESSOR_CAPACITY = 2
_REQUIREMENTS_PROCESSOR_ADMISSION = threading.BoundedSemaphore(_PROCESSOR_CAPACITY)
_ADMISSION_STATE_LOCK = threading.Lock()
_ADMISSION_WAITERS: dict[str, float] = {}
_ADMISSION_ACTIVE = 0
_QUERY_SCOPE_HMAC_KEY = secrets.token_bytes(32)


@dataclass(frozen=True)
class RequirementsQueryAttribution:
    request_id: str = "unknown"
    caller_extension: str = "unknown"
    session_id: str = "unknown"
    run_id: str = "unknown"
    action: str = "unknown"
    tool: str = "unknown"
    query_scope_hash: str = "unknown"
    extension_generation: str = "unknown"


_QUERY_ATTRIBUTION: contextvars.ContextVar[RequirementsQueryAttribution] = contextvars.ContextVar(
    "requirements_query_attribution",
    default=RequirementsQueryAttribution(),
)


def bind_requirements_attribution(
    *,
    request_id: str,
    caller_extension: str,
    action: str,
    payload: Mapping[str, Any],
    extension_generation: str = "unknown",
    session_id: str = "unknown",
    run_id: str = "unknown",
    tool: str = "unknown",
) -> contextvars.Token[RequirementsQueryAttribution]:
    query = payload.get("query")
    query_scope_hash = "unknown"
    if isinstance(query, str):
        scope = {
            "caller_extension": caller_extension,
            "query": query,
            "cwd": payload.get("cwd") if isinstance(payload.get("cwd"), str) else "",
            "cwds": payload.get("cwds") if isinstance(payload.get("cwds"), list) else [],
            "all_projects": payload.get("all_projects") is True,
        }
        encoded = json.dumps(scope, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        query_scope_hash = hmac.new(
            _QUERY_SCOPE_HMAC_KEY,
            encoded.encode("utf-8", "surrogatepass"),
            hashlib.sha256,
        ).hexdigest()[:16]
    return _QUERY_ATTRIBUTION.set(
        RequirementsQueryAttribution(
            request_id=request_id or "unknown",
            caller_extension=caller_extension or "unknown",
            session_id=session_id or "unknown",
            run_id=run_id or "unknown",
            action=action or "unknown",
            tool=tool or "unknown",
            query_scope_hash=query_scope_hash,
            extension_generation=extension_generation or "unknown",
        )
    )


def reset_requirements_attribution(token: contextvars.Token[RequirementsQueryAttribution]) -> None:
    _QUERY_ATTRIBUTION.reset(token)


def current_requirements_attribution() -> RequirementsQueryAttribution:
    return _QUERY_ATTRIBUTION.get()


class RequirementsQueryTimeout(TimeoutError):
    code = "requirements_timeout"


class RequirementsAdmissionTimeout(RequirementsQueryTimeout):
    code = "admission_timeout"


class RequirementsProviderTimeout(RequirementsQueryTimeout):
    code = "provider_timeout"


async def run_requirements_query(
    name: str,
    fn: Callable[..., Any],
    /,
    *,
    executor: ThreadPoolExecutor,
    requires_projection: bool = False,
    **kwargs: Any,
) -> Any:
    if requires_projection:
        readiness_started = time.perf_counter()
        from requirement_prewarm import ensure_requirements_projection_ready

        await ensure_requirements_projection_ready()
        perf.record(f"{name}.readiness_wait", (time.perf_counter() - readiness_started) * 1000)
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
    on_admitted: Callable[[], Awaitable[None]] | None = None,
    **kwargs: Any,
) -> Any:
    queued_at = time.perf_counter()
    attribution = _QUERY_ATTRIBUTION.get()
    waiter_id = attribution.request_id
    if waiter_id == "unknown":
        waiter_id = f"local-{id(asyncio.current_task())}"
    _register_admission_waiter(waiter_id, queued_at)
    _log_processor_lifecycle("admission", "queued", attribution, _admission_state())
    try:
        admitted = await _acquire_processor_admission(admission_timeout_seconds)
    except asyncio.CancelledError:
        state = _finish_admission_wait(waiter_id, admitted=False)
        _log_processor_lifecycle("cancellation", "admission_cancelled", attribution, state)
        raise
    if not admitted:
        state = _finish_admission_wait(waiter_id, admitted=False)
        perf.record(f"{name}.admission_timeout", admission_timeout_seconds * 1000)
        perf.record(f"{name}.queue_wait", (time.perf_counter() - queued_at) * 1000)
        _log_processor_lifecycle("admission_timeout", "admission_timeout", attribution, state)
        raise RequirementsAdmissionTimeout(
            "get-requirements processor admission timed out before a worker was available"
        )
    state = _finish_admission_wait(waiter_id, admitted=True)
    _log_processor_lifecycle("admitted", "running", attribution, state)
    if on_admitted is not None:
        try:
            await on_admitted()
        except BaseException:
            state = _release_processor_admission()
            _log_processor_lifecycle("completion", "admitted_callback_error", attribution, state)
            raise

    ctx = contextvars.copy_context()

    def _call() -> Any:
        perf.record(f"{name}.queue_wait", (time.perf_counter() - queued_at) * 1000)
        return ctx.run(fn, **kwargs)

    start = time.perf_counter()
    loop = asyncio.get_running_loop()
    try:
        future = loop.run_in_executor(executor, _call)
    except BaseException:
        state = _release_processor_admission()
        _log_processor_lifecycle("completion", "executor_submit_error", attribution, state)
        raise

    def _worker_done(done: asyncio.Future[Any]) -> None:
        state = _release_processor_admission()
        outcome = "cancelled" if done.cancelled() else "error" if done.exception() is not None else "success"
        _log_processor_lifecycle("completion", outcome, attribution, state)

    future.add_done_callback(_worker_done)
    try:
        return await asyncio.wait_for(
            asyncio.shield(future),
            timeout=max(0.0, result_timeout_seconds),
        )
    except asyncio.TimeoutError as exc:
        perf.record(f"{name}.result_timeout", result_timeout_seconds * 1000)
        _log_processor_lifecycle("request_outcome", "provider_timeout", attribution, _admission_state())
        raise RequirementsProviderTimeout(
            "get-requirements processor timed out before returning requirements"
        ) from exc
    except asyncio.CancelledError:
        _log_processor_lifecycle("cancellation", "caller_cancelled", attribution, _admission_state())
        raise
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


def _register_admission_waiter(waiter_id: str, queued_at: float) -> None:
    with _ADMISSION_STATE_LOCK:
        _ADMISSION_WAITERS[waiter_id] = queued_at


def _finish_admission_wait(waiter_id: str, *, admitted: bool) -> dict[str, float | int]:
    global _ADMISSION_ACTIVE
    with _ADMISSION_STATE_LOCK:
        _ADMISSION_WAITERS.pop(waiter_id, None)
        if admitted:
            _ADMISSION_ACTIVE += 1
        return _admission_state_locked()


def _release_processor_admission() -> dict[str, float | int]:
    global _ADMISSION_ACTIVE
    with _ADMISSION_STATE_LOCK:
        _ADMISSION_ACTIVE -= 1
        state = _admission_state_locked()
    _REQUIREMENTS_PROCESSOR_ADMISSION.release()
    return state


def _admission_state() -> dict[str, float | int]:
    with _ADMISSION_STATE_LOCK:
        return _admission_state_locked()


def _admission_state_locked() -> dict[str, float | int]:
    now = time.perf_counter()
    oldest = min(_ADMISSION_WAITERS.values(), default=now)
    return {
        "queue_depth": len(_ADMISSION_WAITERS),
        "active_permits": _ADMISSION_ACTIVE,
        "available_permits": _PROCESSOR_CAPACITY - _ADMISSION_ACTIVE,
        "oldest_queue_age_ms": max(0.0, (now - oldest) * 1000) if _ADMISSION_WAITERS else 0.0,
    }


def _log_processor_lifecycle(
    event: str,
    outcome: str,
    attribution: RequirementsQueryAttribution,
    state: Mapping[str, float | int],
) -> None:
    logger.info(
        "requirements_processor_lifecycle event=%s outcome=%s request_id=%s "
        "caller_extension=%s session_id=%s run_id=%s action=%s tool=%s "
        "query_scope_hash=%s extension_generation=%s queue_depth=%s "
        "active_permits=%s available_permits=%s oldest_queue_age_ms=%.3f",
        event,
        outcome,
        attribution.request_id,
        attribution.caller_extension,
        attribution.session_id,
        attribution.run_id,
        attribution.action,
        attribution.tool,
        attribution.query_scope_hash,
        attribution.extension_generation,
        state["queue_depth"],
        state["active_permits"],
        state["available_permits"],
        state["oldest_queue_age_ms"],
    )
