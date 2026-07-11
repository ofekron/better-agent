from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path


BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import _test_home

_test_home.isolate("ba-requirements-projection-")

import event_bus_subscribers
from event_journal import publish_event
from requirements_query_runner import (
    REQUIREMENTS_PROCESSOR_EXECUTOR,
    run_requirements_processor_query,
)


class _BlockedProjection:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.applied: list[int] = []

    def apply_written_journal_event(
        self, _root_id, _sid, _msg_id, _event_type, _data, seq,
    ) -> None:
        self.started.set()
        self.release.wait()
        self.applied.append(seq)

    def mark_reconcile_dirty(self, _root_id: str) -> None:
        raise AssertionError("projection unexpectedly failed")


async def _main() -> None:
    original_manager = event_bus_subscribers.session_manager
    projection = _BlockedProjection()
    event_bus_subscribers.session_manager = projection
    event_bus_subscribers.register_default_subscribers()
    loop = asyncio.get_running_loop()

    def _processor(index: int) -> str:
        future = asyncio.run_coroutine_threadsafe(
            publish_event(
                session_id="root",
                context_id="session",
                message_id="message",
                event_type="agent_message",
                data={"uuid": f"event-{index}"},
                source="requirements-test",
            ),
            loop,
        )
        future.result(timeout=2)
        return f"done-{index}"

    try:
        first = await asyncio.gather(*(
            run_requirements_processor_query(
                f"requirements.projection.{index}",
                _processor,
                executor=REQUIREMENTS_PROCESSOR_EXECUTOR,
                index=index,
                admission_timeout_seconds=1,
                result_timeout_seconds=3,
            )
            for index in (1, 2)
        ))
        await asyncio.wait_for(asyncio.to_thread(projection.started.wait), timeout=1)
        third = await run_requirements_processor_query(
            "requirements.projection.3",
            _processor,
            executor=REQUIREMENTS_PROCESSOR_EXECUTOR,
            index=3,
            admission_timeout_seconds=1,
            result_timeout_seconds=3,
        )
        assert first == ["done-1", "done-2"]
        assert third == "done-3"
        assert projection.applied == []
        projection.release.set()
        await asyncio.wait_for(
            asyncio.to_thread(
                event_bus_subscribers._SESSION_PROJECTION_DISPATCHER.barrier,
                "root",
            ),
            timeout=2,
        )
        assert len(projection.applied) == 3
        assert projection.applied == sorted(projection.applied)
    finally:
        projection.release.set()
        event_bus_subscribers.session_manager = original_manager


def main() -> int:
    asyncio.run(_main())
    print("PASS: projection latency cannot retain requirements admission")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
