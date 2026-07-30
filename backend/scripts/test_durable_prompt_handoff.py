"""Regression coverage for watchdog-free durable prompt handoff."""
from __future__ import annotations

import asyncio
import inspect
import os
import shutil
import sys

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-durable-prompt-handoff-")
_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import main  # noqa: E402
import app_lifecycle  # noqa: E402
import offline_actions_api  # noqa: E402


class _SessionManager:
    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.admitted: dict[str, dict] = {}
        self.persisted = asyncio.Event()
        self.durable = False
        self.loop = loop

    def admit_queued_prompt_durable(self, sid: str, prompt: dict) -> dict:
        existing = self.admitted.get(prompt["id"])
        if existing is not None:
            return {
                "session": {"id": sid},
                "admitted": False,
                "existing_user_message": None,
                "existing_queued_prompt": existing,
            }
        self.admitted[prompt["id"]] = prompt
        self.durable = True
        self.loop.call_soon_threadsafe(self.persisted.set)
        return {
            "session": {"id": sid},
            "admitted": True,
            "existing_user_message": None,
            "existing_queued_prompt": None,
        }


class _Coordinator:
    def __init__(self, session_manager: _SessionManager) -> None:
        self.session_manager = session_manager
        self.release = asyncio.Event()
        self.submitted: list[tuple[str, dict]] = []

    async def submit_prompt_async(self, sid: str, params: dict) -> str:
        assert self.session_manager.durable, "dispatch happened before durable admission"
        self.submitted.append((sid, params))
        return params["_queued_id"]


class _FailingCoordinator:
    async def submit_prompt_async(self, _sid: str, _params: dict) -> str:
        raise RuntimeError("submission unavailable")


async def _disconnect_cannot_cancel_handoff() -> None:
    sm = _SessionManager(asyncio.get_running_loop())
    co = _Coordinator(sm)
    real_sm, real_co = main.session_manager, main.coordinator
    main.session_manager, main.coordinator = sm, co
    try:
        queued = {"id": "q1", "content": "new session prompt"}
        params = {"_queued_id": "q1", "prompt": "new session prompt"}
        caller = asyncio.create_task(offline_actions_api._start_prompt_handoff("sid", queued, params))
        await sm.persisted.wait()
        caller.cancel()
        try:
            await caller
        except asyncio.CancelledError:
            pass
        while offline_actions_api._PROMPT_HANDOFF_TASKS:
            await asyncio.sleep(0)
        assert co.submitted == [("sid", params)]
        assert not offline_actions_api._PROMPT_HANDOFF_TASKS
    finally:
        main.session_manager, main.coordinator = real_sm, real_co


async def _duplicate_admission_does_not_dispatch_twice() -> None:
    sm = _SessionManager(asyncio.get_running_loop())
    co = _Coordinator(sm)
    co.release.set()
    real_sm, real_co = main.session_manager, main.coordinator
    main.session_manager, main.coordinator = sm, co
    try:
        queued = {"id": "q2", "content": "once"}
        params = {"_queued_id": "q2", "prompt": "once"}
        first = await offline_actions_api._start_prompt_handoff("sid", queued, params.copy())
        second = await offline_actions_api._start_prompt_handoff("sid", queued, params.copy())
        while offline_actions_api._PROMPT_HANDOFF_TASKS:
            await asyncio.sleep(0)
        assert first["admitted"] is True
        assert second["admitted"] is False
        assert len(co.submitted) == 1
    finally:
        main.session_manager, main.coordinator = real_sm, real_co


async def _dispatch_failure_keeps_durable_outbox_item() -> None:
    sm = _SessionManager(asyncio.get_running_loop())
    real_sm, real_co = main.session_manager, main.coordinator
    main.session_manager, main.coordinator = sm, _FailingCoordinator()
    try:
        queued = {"id": "q3", "content": "recover after restart"}
        admission = await offline_actions_api._start_prompt_handoff(
            "sid",
            queued,
            {"_queued_id": "q3", "prompt": "recover after restart"},
        )
        await offline_actions_api._drain_prompt_handoffs()
        assert admission["admitted"] is True
        assert sm.admitted["q3"] == queued
    finally:
        main.session_manager, main.coordinator = real_sm, real_co


async def _real_outbox_id_is_idempotent_without_client_id() -> None:
    sm = main.session_manager
    session = await asyncio.to_thread(
        sm.create,
        name="durable-idempotency",
        cwd="/tmp",
        orchestration_mode="native",
    )
    prompt = {"id": "operation-id", "content": "once", "kind": "send"}
    first = await asyncio.to_thread(
        sm.admit_queued_prompt_durable, session["id"], prompt,
    )
    second = await asyncio.to_thread(
        sm.admit_queued_prompt_durable, session["id"], prompt,
    )
    assert first["admitted"] is True
    assert second["admitted"] is False
    assert second["existing_queued_prompt"]["id"] == "operation-id"


def main_test() -> int:
    asyncio.run(_disconnect_cannot_cancel_handoff())
    asyncio.run(_duplicate_admission_does_not_dispatch_twice())
    asyncio.run(_dispatch_failure_keeps_durable_outbox_item())
    asyncio.run(_real_outbox_id_is_idempotent_without_client_id())
    startup_source = inspect.getsource(app_lifecycle.on_startup)
    processor_source = inspect.getsource(
        type(main.coordinator)._run_session_processor,
    )
    init_source = inspect.getsource(type(main.coordinator)._init_turn_messages)
    assert "queue-reenqueue-watchdog" not in startup_source
    assert not hasattr(main, "_queue_reenqueue_watchdog")
    pre_dispatch = processor_source[
        processor_source.index("await self.turn_manager.wait_for_clear_runs"):
        processor_source.index("if is_review:")
    ]
    assert "await consume_queue_item()" not in pre_dispatch
    assert init_source.index("append_user_msg(") < init_source.index(
        "remove_queued_prompt(app_session_id, queue_item_id)",
    )
    print("PASS durable handoff survives disconnect and dispatches exactly once")
    print("PASS duplicate admission is idempotent")
    print("PASS dispatch failure remains in durable startup outbox")
    print("PASS operation ID is idempotent without client ID")
    print("PASS user persistence precedes durable outbox consumption")
    print("PASS runtime queue watchdog removed")
    shutil.rmtree(_TMP_HOME, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main_test())
