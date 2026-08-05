from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import _test_home
_test_home.isolate("bc-test-global-ws-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from orchestrator import Coordinator  # noqa: E402
from global_events import EXTENSION_CHANGE_TOPICS, extension_event  # noqa: E402
from ws_serialization import (  # noqa: E402
    reopen_ws_json_executor,
    shutdown_ws_json_executor,
)

pytestmark = pytest.mark.anyio


async def test_global_broadcast_reaches_unsubscribed_ws() -> None:
    coordinator = Coordinator()
    received: list[dict] = []

    async def callback(event: dict) -> None:
        received.append(event)

    coordinator.register_global_ws(callback)
    await coordinator.broadcast_global("projects_changed", {})
    deadline = time.monotonic() + 1.0
    while not received and time.monotonic() < deadline:
        await asyncio.sleep(0.01)

    assert received == [{"type": "projects_changed", "data": {}}]


async def test_global_broadcast_dedupes_session_subscribed_ws() -> None:
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

    assert len(received) == 1
    assert received[0]["type"] == "projects_changed"


async def test_global_broadcast_shares_serialization_task() -> None:
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

    assert len(task_ids) == 2
    assert task_ids[0] == task_ids[1]
    assert task_ids[0] != 0


async def test_invalid_global_event_rejects_before_task_creation() -> None:
    coordinator = Coordinator()
    before = set(asyncio.all_tasks())
    with pytest.raises(ValueError):
        coordinator.schedule_global("not_registered", {})
    await asyncio.sleep(0)
    assert set(asyncio.all_tasks()) == before
    with pytest.raises(ValueError):
        coordinator.schedule_global("projects_changed", {"bad": float("nan")})
    with pytest.raises(ValueError):
        coordinator.schedule_global("projects_changed", {"bad": object()})


async def test_global_broadcast_drain_owns_delivery() -> None:
    coordinator = Coordinator()
    delivered = asyncio.Event()

    async def callback(_event: dict) -> None:
        await asyncio.sleep(0.01)
        delivered.set()

    coordinator.register_global_ws(callback)
    coordinator.schedule_global("projects_changed", {})
    await coordinator.drain_global_broadcasts()
    assert delivered.is_set()
    assert not coordinator._global_broadcast_tasks


async def test_extension_event_validation() -> None:
    coordinator = Coordinator()
    event_type, payload = extension_event("reviews.ext", "marker_changed", {"n": 1})
    coordinator.prepare_global_event(event_type, payload)
    with pytest.raises(ValueError):
        extension_event("../escape", "marker_changed", {})
    with pytest.raises(ValueError):
        extension_event("reviews.ext", "marker_changed", {"bad": float("inf")})


async def test_extension_change_topic_validation() -> None:
    coordinator = Coordinator()
    for topic in EXTENSION_CHANGE_TOPICS:
        coordinator.prepare_global_event(topic, {})
    with pytest.raises(ValueError):
        coordinator.prepare_global_event("extension.config.typo", {})


async def test_owned_task_exception_is_retrieved() -> None:
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
        assert not unhandled
        assert not coordinator._global_broadcast_tasks
    finally:
        loop.set_exception_handler(previous_handler)


async def test_cross_thread_schedule_is_drained() -> None:
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
    assert delivered.is_set()


async def test_drain_closes_validation_submission_race() -> None:
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
            assert len(errors) == 1
            assert isinstance(errors[0], RuntimeError)
            assert not coordinator._global_broadcast_tasks
            assert not coordinator._global_broadcast_futures
            assert not unhandled
            assert record_count.call_args_list
            assert record_count.call_args_list[-1].args == (
                "ws.broadcast_global.rejected_shutdown",
            )
    finally:
        release_validation.set()
        thread.join(timeout=2)
        loop.set_exception_handler(previous_handler)


async def test_admission_snapshots_nested_payload() -> None:
    coordinator = Coordinator()
    received: list[dict] = []

    async def callback(event: dict) -> None:
        received.append(event)

    coordinator.register_global_ws(callback)
    payload = {"nested": {"items": [1]}}
    coordinator.schedule_global("projects_changed", payload)
    payload["nested"]["items"].append(2)
    await coordinator.drain_global_broadcasts()
    assert received[0]["data"] == {"nested": {"items": [1]}}


async def test_same_process_lifespan_reopens_after_drain() -> None:
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
        assert [event["data"]["generation"] for event in received] == [1, 2]
    finally:
        reopen_ws_json_executor()


async def test_shutdown_drains_before_serializer_shutdown() -> None:
    source = (Path(_BACKEND) / "app_lifecycle.py").read_text()
    shutdown = source[source.index("async def on_shutdown():"):]
    unsubscribe_pos = shutdown.index("unbind_session_ws_broadcaster()")
    drain_pos = shutdown.index("drain_global_broadcasts()")
    serializer_pos = shutdown.index("shutdown_ws_json_executor()")
    assert unsubscribe_pos < drain_pos < serializer_pos


async def test_shutdown_unsubscribes_session_broadcast_producer() -> None:
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
    assert not broadcaster.changes
    assert "session_ws_broadcaster_on_change" not in {
        sub["name"] for sub in bus.describe()
    }


if __name__ == "__main__":
    for test in [
        test_global_broadcast_reaches_unsubscribed_ws,
        test_global_broadcast_dedupes_session_subscribed_ws,
        test_global_broadcast_shares_serialization_task,
        test_invalid_global_event_rejects_before_task_creation,
        test_global_broadcast_drain_owns_delivery,
        test_extension_event_validation,
        test_extension_change_topic_validation,
        test_owned_task_exception_is_retrieved,
        test_cross_thread_schedule_is_drained,
        test_drain_closes_validation_submission_race,
        test_admission_snapshots_nested_payload,
        test_same_process_lifespan_reopens_after_drain,
        test_shutdown_unsubscribes_session_broadcast_producer,
        test_shutdown_drains_before_serializer_shutdown,
    ]:
        asyncio.run(test())
