from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from event_bus import BusEvent, bus

logger = logging.getLogger(__name__)

BackgroundHookHandler = Callable[[BusEvent], Awaitable[None]]


def bind_background_event_hook(
    *,
    name: str,
    pattern: str,
    handler: BackgroundHookHandler,
    priority: int = 250,
) -> None:
    bus.unsubscribe(name)

    async def _dispatch(event: BusEvent) -> None:
        task = asyncio.create_task(
            handler(event),
            name=f"agent-hook-{name}",
        )
        task.add_done_callback(lambda done: _log_hook_failure(name, done))

    bus.subscribe(pattern, _dispatch, priority=priority, name=name)


def _log_hook_failure(name: str, task: asyncio.Task) -> None:
    try:
        task.result()
    except asyncio.CancelledError:
        return
    except Exception:
        logger.exception("agent hook %s failed", name)
