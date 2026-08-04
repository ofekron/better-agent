from __future__ import annotations

import asyncio
import logging
import threading

from event_bus import BusEvent, bus, register_event_schema

logger = logging.getLogger(__name__)

EVENT_TYPE = "provider.runtime_changed"
_CONFIG_CAUSE = "config"
_AUTH_CAUSE = "auth"

register_event_schema(EVENT_TYPE, 1)

_lock = threading.Lock()
_loop: asyncio.AbstractEventLoop | None = None
_pending_causes: set[str] = set()
_flush_scheduled = False
_flush_task: asyncio.Task[None] | None = None


def bind_loop() -> None:
    global _flush_scheduled, _flush_task, _loop
    loop = asyncio.get_running_loop()
    with _lock:
        if _loop is not None and _loop is not loop and not _loop.is_closed():
            raise RuntimeError("provider runtime fact loop is already bound")
        if _loop is not None and _loop is not loop:
            _flush_scheduled = False
            _flush_task = None
        _loop = loop
        _schedule_flush_locked()


def notify_config_committed() -> None:
    _notify(_CONFIG_CAUSE)


def notify_auth_committed() -> None:
    _notify(_AUTH_CAUSE)


def _notify(cause: str) -> None:
    with _lock:
        _pending_causes.add(cause)
        _schedule_flush_locked()


def _schedule_flush_locked() -> None:
    global _flush_scheduled, _loop
    if (
        not _pending_causes
        or _flush_scheduled
        or _loop is None
        or _loop.is_closed()
    ):
        return
    _flush_scheduled = True
    try:
        _loop.call_soon_threadsafe(_launch_flush)
    except RuntimeError:
        _loop = None
        _flush_scheduled = False


def _launch_flush() -> None:
    global _flush_scheduled, _flush_task
    loop = asyncio.get_running_loop()
    with _lock:
        if _loop is not loop:
            _flush_scheduled = False
            return
        if _flush_task is not None and not _flush_task.done():
            return
        _flush_task = loop.create_task(
            _flush(),
            name="provider-runtime-fact-flush",
        )


async def _flush() -> None:
    global _flush_scheduled, _flush_task
    await asyncio.sleep(0)
    with _lock:
        causes = sorted(_pending_causes)
        _pending_causes.clear()
    try:
        await bus.publish(BusEvent(
            type=EVENT_TYPE,
            root_id="",
            sid="",
            payload={"causes": causes},
            persist=False,
        ))
    except Exception:
        logger.exception("provider runtime fact delivery failed")
    finally:
        with _lock:
            _flush_task = None
            _flush_scheduled = False
            _schedule_flush_locked()


async def shutdown() -> None:
    global _loop, _flush_scheduled, _flush_task
    with _lock:
        _loop = None
        _pending_causes.clear()
        _flush_scheduled = False
        task = _flush_task
        _flush_task = None
    if task is not None and not task.done():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
