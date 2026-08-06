"""Startup work reported into the background work registry.

INVARIANT: `on_startup` returns to uvicorn within milliseconds. Every step
that can take > ~50ms (migrations, recovery scans, replays) runs as a named
background task tracked here, so the frontend can render what is still
running without polling.

This module owns no state. It is the adapter that turns a startup step into
a `background_work` item, so startup work and every other kind of background
work share one registry, one event, one endpoint and one surface.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any, Awaitable, Callable

import background_work
from background_work import background_work_registry

logger = logging.getLogger(__name__)

_OWNER = "startup"

# Startup rows are the first thing a user sees on a cold boot, so they linger
# a beat longer after success than the registry default.
_RETENTION_MS = 2500


def _begin(task_id: str, label: str) -> str:
    """Register a running item. `label` is an i18n key owned by core, so it
    goes in the title slot; the raw key is the fallback the manager renders
    when a locale lacks the string."""
    return background_work_registry.report(
        owner_kind=background_work.OWNER_CORE,
        owner_id=_OWNER,
        local_id=task_id,
        label=label,
        title_key=label,
        retention_ms=_RETENTION_MS,
    )


async def run_task(
    task_id: str,
    label: str,
    fn: Callable[..., Any],
    *args: Any,
    in_thread: bool = True,
    **kwargs: Any,
) -> None:
    """Register, execute, and finish a task. `fn` may be sync or async — if
    sync and `in_thread=True` (default), it is offloaded via
    `asyncio.to_thread` so it cannot starve the event loop. Pass
    `in_thread=False` to await a sync `fn` inline (rare; only when the work is
    truly µs-fast or already threads internally).

    Returning gracefully on exception is the contract: a failed startup task
    must not crash the gather/wait_for that scheduled it. The error is
    recorded on the item and surfaced to the UI."""
    import asyncio

    item_id = _begin(task_id, label)
    try:
        if inspect.iscoroutinefunction(fn):
            result: Any = await fn(*args, **kwargs)  # type: ignore[func-returns-value]
        elif in_thread:
            result = await asyncio.to_thread(fn, *args, **kwargs)
        else:
            result = fn(*args, **kwargs)
        # Some startup helpers return a payload the caller chains on. This
        # wrapper ignores it — chaining lives in the composite variant below.
        _ = result
        background_work_registry.finish(item_id)
    except Exception as exc:
        logger.exception("startup task %s failed", task_id)
        background_work_registry.finish(
            item_id, status=background_work.STATUS_FAILED, error=str(exc),
        )


async def run_composite_task(
    task_id: str,
    label: str,
    body: Callable[[], Awaitable[None]],
) -> None:
    """Variant for tasks whose body needs multiple awaits (e.g. a sync scan
    followed by an async integration). `body` is a zero-arg async callable
    holding the full work; on exception the item is marked failed."""
    item_id = _begin(task_id, label)
    try:
        await body()
        background_work_registry.finish(item_id)
    except Exception as exc:
        logger.exception("startup task %s failed", task_id)
        background_work_registry.finish(
            item_id, status=background_work.STATUS_FAILED, error=str(exc),
        )
