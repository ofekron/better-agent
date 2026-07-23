"""Regression test for extension_job_delivery's caller-terminal fallback.

Locks the fix for the gap where `session-bridge-delegate` (the
`delegate_to_session` MCP tool) runs its whole synchronous "join a target
turn and return its output" operation as a durable `extension_jobs` job
under `asyncio.shield` — surviving a disconnected/cancelled HTTP request —
but had no delivery path at all if the caller's own turn ended (same
`lifecycle.turn_complete`/`turn_stopped` event `ask_delivery.on_caller_terminal`
reacts to for plain `ask`) while the job kept running: the completed result
would sit in the durable record forever, unread.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import sys
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-extjob-delivery-")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import extension_job_delivery
import extension_jobs
import inbox_store
from event_bus import BusEvent
from session_manager import manager as session_manager

_OWNER = "core-mcp"
_OPERATION = "session-bridge-delegate"


def teardown_module():
    shutil.rmtree(_TMP_HOME, ignore_errors=True)


async def _fire_and_finish(job_id: str, caller_id: str, target_id: str, result: dict) -> None:
    async def runner(payload, *, request_id):
        return result

    task = extension_jobs.fire(
        _OWNER, _OPERATION, job_id,
        {"app_session_id": caller_id, "session_id": target_id},
        runner,
    )
    await task


def _turn_stopped_event(caller_id: str) -> BusEvent:
    root_id = session_manager._root_id_for(caller_id)
    return BusEvent(
        type="lifecycle.turn_stopped",
        root_id=root_id,
        sid=caller_id,
        msg_id="",
        payload={},
    )


def test_ignores_incomplete_and_unrelated_jobs():
    """A running (not yet complete) job, and a complete job for a DIFFERENT
    caller, must not be touched."""
    caller = session_manager.create(name="caller ignore", cwd="/repo", orchestration_mode="native")
    other_caller = session_manager.create(name="other caller", cwd="/repo", orchestration_mode="native")
    target = session_manager.create(name="target ignore", cwd="/repo", orchestration_mode="native")

    running_still_in_progress = {}

    async def main():
        running_gate = asyncio.Event()

        async def never_finishes(payload, *, request_id):
            await running_gate.wait()
            return {}

        task = extension_jobs.fire(
            _OWNER, _OPERATION, "job_running",
            {"app_session_id": caller["id"]},
            never_finishes,
        )
        try:
            await extension_job_delivery.on_caller_terminal(_turn_stopped_event(caller["id"]))
            # Check while genuinely still running, BEFORE letting it finish.
            running_still_in_progress["record"] = extension_jobs.read_record(
                _OWNER, _OPERATION, "job_running",
            )
        finally:
            running_gate.set()
            await task

    asyncio.run(main())

    assert running_still_in_progress["record"]["status"] == "running"
    assert running_still_in_progress["record"].get("delivered") is not True
    inbox_while_running = inbox_store.read_new(recipient_session_id=caller["id"])
    assert inbox_while_running["count"] == 0

    # A complete job belonging to a DIFFERENT caller must not be touched by
    # a terminal event for the FIRST (unrelated) caller.
    asyncio.run(_fire_and_finish(
        "job_other_caller", other_caller["id"], target["id"],
        {"session_id": target["id"], "run_mode": "fork", "final_message": "hi", "turn_id": "t1"},
    ))
    asyncio.run(extension_job_delivery.on_caller_terminal(_turn_stopped_event(caller["id"])))

    assert extension_jobs.read_record(_OWNER, _OPERATION, "job_other_caller").get("delivered") is not True


def test_delivers_completed_job_to_caller_inbox_once():
    caller = session_manager.create(name="caller deliver", cwd="/repo", orchestration_mode="native")
    target = session_manager.create(name="target deliver", cwd="/repo", orchestration_mode="native")
    job_id = "job_deliver_once"

    asyncio.run(_fire_and_finish(
        job_id, caller["id"], target["id"],
        {"session_id": target["id"], "run_mode": "fork", "final_message": "the delegated answer", "turn_id": "t1"},
    ))

    asyncio.run(extension_job_delivery.on_caller_terminal(_turn_stopped_event(caller["id"])))

    record = extension_jobs.read_record(_OWNER, _OPERATION, job_id)
    assert record["delivered"] is True
    inbox = inbox_store.read_new(recipient_session_id=caller["id"])
    assert inbox["count"] == 1
    assert inbox["new_messages"][0]["text"] == "the delegated answer"
    assert inbox["new_messages"][0]["delivery_id"] == f"extjob:{_OWNER}:{_OPERATION}:{job_id}"

    # A second terminal event for the same caller must not re-deliver.
    asyncio.run(extension_job_delivery.on_caller_terminal(_turn_stopped_event(caller["id"])))
    inbox_again = inbox_store.read_new(recipient_session_id=caller["id"])
    assert inbox_again["count"] == 0


def test_delivers_failed_job_error_to_caller_inbox():
    """A hard failure (task cancelled, or an exception raised before
    session_bridge.delegate() ever returned) has status=="failed" and no
    `result` payload — must still be delivered, from the job's own `error`
    field, not silently dropped by the status filter."""
    caller = session_manager.create(name="caller failed", cwd="/repo", orchestration_mode="native")
    job_id = "job_failed_delivery"

    async def raises(payload, *, request_id):
        raise RuntimeError("boom")

    async def main():
        task = extension_jobs.fire(
            _OWNER, _OPERATION, job_id,
            {"app_session_id": caller["id"]},
            raises,
        )
        with contextlib.suppress(RuntimeError):
            await task

    asyncio.run(main())
    assert extension_jobs.read_record(_OWNER, _OPERATION, job_id)["status"] == "failed"

    asyncio.run(extension_job_delivery.on_caller_terminal(_turn_stopped_event(caller["id"])))

    record = extension_jobs.read_record(_OWNER, _OPERATION, job_id)
    assert record["delivered"] is True
    inbox = inbox_store.read_new(recipient_session_id=caller["id"])
    assert inbox["count"] == 1
    assert inbox["new_messages"][0]["text"] == "boom"
    assert inbox["new_messages"][0]["delivery_id"] == f"extjob:{_OWNER}:{_OPERATION}:{job_id}"


def test_worker_inner_reason_is_ignored():
    caller = session_manager.create(name="caller worker-inner", cwd="/repo", orchestration_mode="native")
    target = session_manager.create(name="target worker-inner", cwd="/repo", orchestration_mode="native")
    job_id = "job_worker_inner"

    asyncio.run(_fire_and_finish(
        job_id, caller["id"], target["id"],
        {"session_id": target["id"], "run_mode": "fork", "final_message": "answer", "turn_id": "t1"},
    ))

    root_id = session_manager._root_id_for(caller["id"])
    event = BusEvent(
        type="lifecycle.turn_stopped", root_id=root_id, sid=caller["id"], msg_id="",
        payload={"reason": "worker_inner"},
    )
    asyncio.run(extension_job_delivery.on_caller_terminal(event))

    assert extension_jobs.read_record(_OWNER, _OPERATION, job_id).get("delivered") is not True
