from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

import _test_home
_test_home.isolate("bc-test-global-ws-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from orchestrator import Coordinator  # noqa: E402
from global_events import extension_event  # noqa: E402
from ws_serialization import (  # noqa: E402
    reopen_ws_json_executor,
    shutdown_ws_json_executor,
)

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


async def test_global_broadcast_reaches_unsubscribed_ws() -> bool:
    coordinator = Coordinator()
    received: list[dict] = []

    async def callback(event: dict) -> None:
        received.append(event)

    coordinator.register_global_ws(callback)
    await coordinator.broadcast_global("projects_changed", {})
    deadline = time.monotonic() + 1.0
    while not received and time.monotonic() < deadline:
        await asyncio.sleep(0.01)

    ok = received == [{"type": "projects_changed", "data": {}}]
    print(f"{PASS if ok else FAIL} global broadcast reaches unsubscribed WS")
    return ok


async def test_global_broadcast_dedupes_session_subscribed_ws() -> bool:
    coordinator = Coordinator()
    received: list[dict] = []

    async def callback(event: dict) -> None:
        received.append(event)

    coordinator.register_global_ws(callback)
    coordinator.register_ws("sid-1", callback)
    await coordinator.broadcast_global("projects_changed", {})
    deadline = time.monotonic() + 1.0
    while not received and time.monotonic() < deadline:
        await asyncio.sleep(0.01)

    ok = len(received) == 1 and received[0]["type"] == "projects_changed"
    print(f"{PASS if ok else FAIL} global broadcast dedupes subscribed WS")
    return ok


async def test_global_broadcast_shares_serialization_task() -> bool:
    coordinator = Coordinator()
    task_ids: list[int] = []

    async def callback_a(event: dict) -> None:
        task = getattr(event, "_bc_serialized_json_task", None)
        task_ids.append(id(task) if task is not None else 0)

    async def callback_b(event: dict) -> None:
        task = getattr(event, "_bc_serialized_json_task", None)
        task_ids.append(id(task) if task is not None else 0)

    coordinator.register_global_ws(callback_a)
    coordinator.register_global_ws(callback_b)
    await coordinator.broadcast_global("projects_changed", {})
    deadline = time.monotonic() + 1.0
    while len(task_ids) < 2 and time.monotonic() < deadline:
        await asyncio.sleep(0.01)

    ok = len(task_ids) == 2 and task_ids[0] == task_ids[1] and task_ids[0] != 0
    print(f"{PASS if ok else FAIL} global broadcast shares serialization task")
    return ok


async def test_invalid_global_event_rejects_before_task_creation() -> bool:
    coordinator = Coordinator()
    before = set(asyncio.all_tasks())
    try:
        coordinator.schedule_global("not_registered", {})
    except ValueError:
        await asyncio.sleep(0)
        ok = set(asyncio.all_tasks()) == before
    else:
        ok = False
    try:
        coordinator.schedule_global("projects_changed", {"bad": float("nan")})
    except ValueError:
        pass
    else:
        ok = False
    try:
        coordinator.schedule_global("projects_changed", {"bad": object()})
    except ValueError:
        pass
    else:
        ok = False
    print(f"{PASS if ok else FAIL} invalid event rejects synchronously")
    return ok


async def test_global_broadcast_drain_owns_delivery() -> bool:
    coordinator = Coordinator()
    delivered = asyncio.Event()

    async def callback(_event: dict) -> None:
        await asyncio.sleep(0.01)
        delivered.set()

    coordinator.register_global_ws(callback)
    coordinator.schedule_global("projects_changed", {})
    await coordinator.drain_global_broadcasts()
    ok = delivered.is_set() and not coordinator._global_broadcast_tasks
    print(f"{PASS if ok else FAIL} global broadcast drain owns delivery")
    return ok


async def test_extension_event_validation() -> bool:
    coordinator = Coordinator()
    event_type, payload = extension_event("reviews.ext", "marker_changed", {"n": 1})
    coordinator.prepare_global_event(event_type, payload)
    try:
        extension_event("../escape", "marker_changed", {})
    except ValueError:
        ok = True
    else:
        ok = False
    try:
        extension_event("reviews.ext", "marker_changed", {"bad": float("inf")})
    except ValueError:
        pass
    else:
        ok = False
    print(f"{PASS if ok else FAIL} extension envelope validation")
    return ok


async def test_owned_task_exception_is_retrieved() -> bool:
    coordinator = Coordinator()
    loop = asyncio.get_running_loop()
    unhandled: list[dict] = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: unhandled.append(context))

    async def callback(_event: dict) -> None:
        return None

    async def fail_serialization(_event: dict) -> str:
        raise RuntimeError("serialization failed")

    coordinator.register_global_ws(callback)
    try:
        with patch("orchestrator.dumps_ws_json", fail_serialization):
            coordinator.schedule_global("projects_changed", {})
            await coordinator.drain_global_broadcasts()
            await asyncio.sleep(0)
        ok = not unhandled and not coordinator._global_broadcast_tasks
    finally:
        loop.set_exception_handler(previous_handler)
    print(f"{PASS if ok else FAIL} owned task exception is retrieved")
    return ok


async def test_cross_thread_schedule_is_drained() -> bool:
    coordinator = Coordinator()
    delivered = asyncio.Event()

    async def callback(_event: dict) -> None:
        delivered.set()

    coordinator.register_global_ws(callback)
    loop = asyncio.get_running_loop()
    thread = threading.Thread(
        target=lambda: coordinator.schedule_global(
            "projects_changed", {}, loop=loop,
        )
    )
    thread.start()
    thread.join()
    await coordinator.drain_global_broadcasts()
    ok = delivered.is_set()
    print(f"{PASS if ok else FAIL} cross-thread schedule is drained")
    return ok


async def test_drain_closes_validation_submission_race() -> bool:
    coordinator = Coordinator()
    loop = asyncio.get_running_loop()
    validation_entered = threading.Event()
    release_validation = threading.Event()
    errors: list[BaseException] = []
    unhandled: list[dict] = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
    original_prepare = coordinator.prepare_global_event

    def paused_prepare(event_type: str, data: dict):
        event = original_prepare(event_type, data)
        validation_entered.set()
        release_validation.wait(timeout=2)
        return event

    coordinator.prepare_global_event = paused_prepare  # type: ignore[method-assign]

    def submit() -> None:
        try:
            coordinator.schedule_global("projects_changed", {}, loop=loop)
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=submit)
    try:
        with patch("orchestrator.perf.record_count") as record_count:
            thread.start()
            validation_entered.wait(timeout=2)
            await coordinator.drain_global_broadcasts()
            release_validation.set()
            thread.join(timeout=2)
            await asyncio.sleep(0)
            ok = (
                len(errors) == 1
                and isinstance(errors[0], RuntimeError)
                and not coordinator._global_broadcast_tasks
                and not coordinator._global_broadcast_futures
                and not unhandled
                and record_count.call_args_list
                and record_count.call_args_list[-1].args
                == ("ws.broadcast_global.rejected_shutdown",)
            )
    finally:
        release_validation.set()
        thread.join(timeout=2)
        loop.set_exception_handler(previous_handler)
    print(f"{PASS if ok else FAIL} drain closes validation/submission race")
    return ok


async def test_admission_snapshots_nested_payload() -> bool:
    coordinator = Coordinator()
    received: list[dict] = []

    async def callback(event: dict) -> None:
        received.append(event)

    coordinator.register_global_ws(callback)
    payload = {"nested": {"items": [1]}}
    coordinator.schedule_global("projects_changed", payload)
    payload["nested"]["items"].append(2)
    await coordinator.drain_global_broadcasts()
    ok = received[0]["data"] == {"nested": {"items": [1]}}
    print(f"{PASS if ok else FAIL} admission snapshots nested payload")
    return ok


async def test_same_process_lifespan_reopens_after_drain() -> bool:
    coordinator = Coordinator()
    received: list[dict] = []

    async def callback(event: dict) -> None:
        task = getattr(event, "_bc_serialized_json_task")
        await task
        received.append(event)

    coordinator.register_global_ws(callback)
    coordinator.schedule_global("projects_changed", {"generation": 1})
    await coordinator.drain_global_broadcasts()
    shutdown_ws_json_executor()
    try:
        reopen_ws_json_executor()
        coordinator.reopen_global_broadcasts()
        coordinator.schedule_global("projects_changed", {"generation": 2})
        await coordinator.drain_global_broadcasts()
        ok = [event["data"]["generation"] for event in received] == [1, 2]
    finally:
        reopen_ws_json_executor()
    print(f"{PASS if ok else FAIL} same-process lifespan reopens after drain")
    return ok


async def test_shutdown_drains_before_serializer_shutdown() -> bool:
    source = (Path(_BACKEND) / "main.py").read_text()
    shutdown = source[source.index("async def on_shutdown():"):]
    unsubscribe_pos = shutdown.index("unbind_session_ws_broadcaster()")
    drain_pos = shutdown.index("await coordinator.drain_global_broadcasts()")
    serializer_pos = shutdown.index("shutdown_ws_json_executor()")
    ok = unsubscribe_pos < drain_pos < serializer_pos
    print(f"{PASS if ok else FAIL} shutdown detaches producer before broadcast drain")
    return ok


async def test_shutdown_unsubscribes_session_broadcast_producer() -> bool:
    from event_bus import BusEvent, bus
    from event_bus_subscribers import (
        bind_session_ws_broadcaster,
        unbind_session_ws_broadcaster,
    )

    class Broadcaster:
        def __init__(self) -> None:
            self.changes: list[tuple[str, dict]] = []

        def on_change(self, sid: str, change: dict) -> None:
            self.changes.append((sid, change))

    broadcaster = Broadcaster()
    bind_session_ws_broadcaster(broadcaster)
    unbind_session_ws_broadcaster()
    await bus.publish(BusEvent(
        type="session.archived_set",
        root_id="root",
        sid="sid",
        payload={"kind": "archived_set", "value": True},
        persist=False,
    ))
    ok = not broadcaster.changes and "session_ws_broadcaster_on_change" not in {
        sub["name"] for sub in bus.describe()
    }
    print(f"{PASS if ok else FAIL} shutdown detaches session broadcast producer")
    return ok


async def main_runner() -> int:
    tests = [
        test_global_broadcast_reaches_unsubscribed_ws,
        test_global_broadcast_dedupes_session_subscribed_ws,
        test_global_broadcast_shares_serialization_task,
        test_invalid_global_event_rejects_before_task_creation,
        test_global_broadcast_drain_owns_delivery,
        test_extension_event_validation,
        test_owned_task_exception_is_retrieved,
        test_cross_thread_schedule_is_drained,
        test_drain_closes_validation_submission_race,
        test_admission_snapshots_nested_payload,
        test_same_process_lifespan_reopens_after_drain,
        test_shutdown_unsubscribes_session_broadcast_producer,
        test_shutdown_drains_before_serializer_shutdown,
    ]
    results = [await test() for test in tests]
    failed = sum(1 for result in results if not result)
    print()
    if failed:
        print(f"{FAIL} {failed}/{len(results)} tests failed")
        return 1
    print(f"{PASS} all {len(results)} tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main_runner()))
