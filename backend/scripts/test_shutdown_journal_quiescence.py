from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path

import _test_home

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)
_TMP_HOME = _test_home.isolate("ba-test-shutdown-journal-")

from event_ingester import event_ingester
from event_journal import Event, EventJournalWriter
from orchestrator import Coordinator


async def test_shutdown_waits_for_terminal_journal_write() -> None:
    coordinator = Coordinator()
    writer = EventJournalWriter()
    root_id = str(uuid.uuid4())
    entered = asyncio.Event()
    release = asyncio.Event()

    async def processor() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            entered.set()
            await release.wait()
            await writer.submit_event_async(Event(
                root_id=root_id,
                sid=root_id,
                event_type="run_state",
                data={"runs": []},
                source="shutdown-regression",
                event_id=str(uuid.uuid4()),
            ))

    task = asyncio.create_task(processor(), name="prompt-processor-regression")
    coordinator._processor_tasks[root_id] = task
    shutdown = asyncio.create_task(coordinator.quiesce_prompt_processors())
    await entered.wait()
    assert not shutdown.done()
    assert not writer._closed
    release.set()
    await shutdown
    writer.close()

    rows, _, _ = event_ingester.read_events(root_id, after_seq=0)
    assert any(row.get("type") == "run_state" for row in rows)


async def test_shutdown_fences_claim_and_enqueue() -> None:
    coordinator = Coordinator()
    assert coordinator.try_claim_prompt_client_id("sid", "item", "client") is None
    await coordinator.quiesce_prompt_processors()
    try:
        coordinator.try_claim_prompt_client_id("sid", "item", "client")
    except RuntimeError as exc:
        assert str(exc) == "prompt admission is closed"
    else:
        raise AssertionError("claim admitted after shutdown fence")
    assert coordinator._active_prompt_client_ids == {("sid", "client"): "item"}

    params = {"_queued_id": "item", "_client_id_claimed": True}
    try:
        coordinator.submit_prompt("sid", params)
    except RuntimeError as exc:
        assert str(exc) == "prompt admission is closed"
    else:
        raise AssertionError("enqueue admitted after shutdown fence")
    assert not coordinator._prompt_queues
    assert not coordinator._active_prompt_client_ids
    assert not coordinator._prompt_client_id_by_item


async def test_writer_reopens_without_losing_prior_rows() -> None:
    writer = EventJournalWriter()
    root_id = str(uuid.uuid4())

    async def append(event_id: str) -> None:
        await writer.submit_event_async(Event(
            root_id=root_id,
            sid=root_id,
            event_type="run_state",
            data={"runs": [event_id]},
            source="shutdown-regression",
            event_id=event_id,
        ))

    await append("before-close")
    writer.close()
    writer.reopen()
    await append("after-reopen")
    writer.close()
    rows, _, _ = event_ingester.read_events(root_id, after_seq=0)
    assert [row.get("data", {}).get("runs") for row in rows] == [
        ["before-close"], ["after-reopen"],
    ]


def test_production_shutdown_order_is_fail_closed() -> None:
    main_source = (Path(_BACKEND) / "main.py").read_text(encoding="utf-8")
    shutdown = main_source[main_source.index("async def on_shutdown():"):]
    assert shutdown.index("await coordinator.quiesce_prompt_processors()") < shutdown.index(
        "event_journal_writer.close",
    )
    between = shutdown[
        shutdown.index("await coordinator.quiesce_prompt_processors()"):
        shutdown.index("event_journal_writer.close")
    ]
    assert "except Exception" not in between[:120]

    orchestrator_source = (Path(_BACKEND) / "orchestrator.py").read_text(encoding="utf-8")
    assert "broadcast_session skipped after journal close" not in orchestrator_source


async def main() -> None:
    await test_shutdown_waits_for_terminal_journal_write()
    await test_shutdown_fences_claim_and_enqueue()
    await test_writer_reopens_without_losing_prior_rows()
    test_production_shutdown_order_is_fail_closed()
    print("PASS shutdown journal quiescence")


if __name__ == "__main__":
    asyncio.run(main())
