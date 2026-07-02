from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _test_home

_test_home.isolate("bc-test-extension-lifecycle-hook-cancel-")

import extension_backend_loader  # noqa: E402
import extension_store  # noqa: E402
from event_bus import BusEvent, bus  # noqa: E402
from event_bus_subscribers import (  # noqa: E402
    _log_hook_task_exception,
    bind_post_turn_hooks,
    bind_pre_turn_hooks,
)


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"PASS {message}")


async def _cancel_hook_task(
    bind,
    event_type: str,
    hook_attr: str,
) -> None:
    original_hooks = getattr(extension_store, hook_attr)
    original_invoke = extension_backend_loader.invoke_extension_backend
    original_create_task = asyncio.create_task
    loop = asyncio.get_running_loop()
    original_handler = loop.get_exception_handler()
    created: list[asyncio.Task] = []
    loop_errors: list[dict] = []
    started = asyncio.Event()

    async def blocked_invoke(*args, **kwargs):
        started.set()
        await asyncio.Future()

    def capture_create_task(coro, *, name=None, context=None):
        task = original_create_task(coro, name=name, context=context)
        created.append(task)
        return task

    def exception_handler(_loop, context):
        loop_errors.append(context)

    setattr(extension_store, hook_attr, lambda: [("cancel-ext", "/hook")])
    extension_backend_loader.invoke_extension_backend = blocked_invoke
    asyncio.create_task = capture_create_task
    loop.set_exception_handler(exception_handler)
    try:
        bind()
        await bus.publish(BusEvent(type=event_type, root_id="r", sid="session1234", payload={}))
        await asyncio.wait_for(started.wait(), timeout=1)
        hook_tasks = [task for task in created if "cancel-ext" in task.get_name()]
        _check(len(hook_tasks) == 1, f"{event_type} hook task captured")
        hook_tasks[0].cancel()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        _check(not loop_errors, f"{event_type} hook cancellation does not hit loop exception handler")
    finally:
        setattr(extension_store, hook_attr, original_hooks)
        extension_backend_loader.invoke_extension_backend = original_invoke
        asyncio.create_task = original_create_task
        loop.set_exception_handler(original_handler)
        bus.unsubscribe("extension_post_turn_hooks")
        bus.unsubscribe("extension_pre_turn_hooks")


async def _real_hook_failure_still_logs() -> None:
    original_hooks = extension_store.post_turn_hooks
    original_invoke = extension_backend_loader.invoke_extension_backend
    messages: list[str] = []

    class _Handler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            messages.append(record.getMessage())

    async def failing_invoke(*args, **kwargs):
        raise RuntimeError("boom")

    handler = _Handler()
    logger = logging.getLogger("event_bus_subscribers")
    logger.addHandler(handler)
    extension_store.post_turn_hooks = lambda: [("fail-ext", "/hook")]
    extension_backend_loader.invoke_extension_backend = failing_invoke
    try:
        bind_post_turn_hooks()
        await bus.publish(BusEvent(type="lifecycle.turn_complete", root_id="r", sid="session1234", payload={}))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        _check(
            any("post-turn hook fail-ext failed" in message for message in messages),
            "real post-turn hook failure is still logged",
        )
    finally:
        extension_store.post_turn_hooks = original_hooks
        extension_backend_loader.invoke_extension_backend = original_invoke
        logger.removeHandler(handler)
        bus.unsubscribe("extension_post_turn_hooks")


async def _task_level_failure_logs_traceback() -> None:
    records: list[logging.LogRecord] = []

    class _Handler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    async def failing_task():
        raise RuntimeError("task boom")

    handler = _Handler()
    logger = logging.getLogger("event_bus_subscribers")
    logger.addHandler(handler)
    try:
        task = asyncio.create_task(failing_task())
        await asyncio.sleep(0)
        _log_hook_task_exception(task, "test")
        matching = [record for record in records if "test hook task raised" in record.getMessage()]
        _check(len(matching) == 1, "task-level hook failure is logged once")
        exc_info = matching[0].exc_info
        _check(
            bool(exc_info) and exc_info[0] is RuntimeError and str(exc_info[1]) == "task boom",
            "task-level hook failure log keeps exception info",
        )
    finally:
        logger.removeHandler(handler)


async def _run() -> None:
    await _cancel_hook_task(bind_post_turn_hooks, "lifecycle.turn_complete", "post_turn_hooks")
    await _cancel_hook_task(bind_pre_turn_hooks, "lifecycle.turn_start", "pre_turn_hooks")
    await _real_hook_failure_still_logs()
    await _task_level_failure_logs_traceback()


if __name__ == "__main__":
    asyncio.run(_run())
