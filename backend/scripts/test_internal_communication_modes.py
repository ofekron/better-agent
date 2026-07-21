from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import _test_home

_test_home.isolate("bc-test-communication-modes-")
os.environ["BETTER_CLAUDE_TEST_AUTH_BYPASS"] = "1"

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main  # noqa: E402


class _Coordinator:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def is_internal_caller(self, _token: str) -> bool:
        return True

    async def submit_team_message(self, **kwargs):
        self.calls.append({"method": "submit_team_message", **kwargs})
        return {"success": True}

    async def ask_team_message(self, **kwargs):
        self.calls.append({"method": "ask_team_message", **kwargs})
        return {"success": True, "response": "ok"}


async def _run() -> None:
    original_coordinator = main.coordinator
    original_validate = main._validate_optional_run_selector
    original_resolve = main._resolve_communication_target
    original_pick_pool_worker = main._pick_pool_worker_for_sender
    original_enqueue_pool_message = main._enqueue_worker_pool_message
    coordinator = _Coordinator()
    pool_enqueues: list[dict] = []

    async def validate(*_args, **_kwargs) -> None:
        return None

    async def resolve(_body: dict) -> str:
        return "target-1"

    def pick_pool_worker(*_args, **_kwargs):
        return None

    async def enqueue_pool_message(**kwargs):
        pool_enqueues.append(kwargs)
        return {"item": {"id": "pool-item-1"}}

    try:
        main.coordinator = coordinator  # type: ignore[assignment]
        main._validate_optional_run_selector = validate  # type: ignore[assignment]
        main._resolve_communication_target = resolve  # type: ignore[assignment]
        main._pick_pool_worker_for_sender = pick_pool_worker  # type: ignore[assignment]
        main._enqueue_worker_pool_message = enqueue_pool_message  # type: ignore[assignment]

        await main._handle_internal_mssg({
                "sender_session_id": "sender-1",
                "target_session_id": "target-1",
                "message": "fire and forget",
                "collapse_key": "assistant-waker",
                "collapse_policy": "take_latest",
            }
        )
        assert coordinator.calls[-1]["method"] == "submit_team_message"
        assert coordinator.calls[-1]["detach"] is True
        assert coordinator.calls[-1].get("expect_inbox_response") in (None, False)
        assert coordinator.calls[-1]["collapse_key"] == "assistant-waker"
        assert coordinator.calls[-1]["collapse_policy"] == "take_latest"
        assert coordinator.calls[-1]["target_selector"] == {
            "kind": "session",
            "value": "target-1",
        }

        await main._handle_internal_mssg({
                "sender_session_id": "sender-1",
                "target_worker_id": "worker-session-1",
                "message": "worker target",
            }
        )
        assert coordinator.calls[-1]["method"] == "submit_team_message"
        assert coordinator.calls[-1]["target_selector"] == {
            "kind": "worker",
            "value": "worker-session-1",
        }

        await main._handle_internal_ask({
            "sender_session_id": "sender-1",
            "target_session_id": "target-1",
            "message": "continue",
            "mode": "continue_and_expect_inbox_back_async",
        })
        assert coordinator.calls[-1]["method"] == "submit_team_message"
        assert coordinator.calls[-1]["detach"] is True
        assert coordinator.calls[-1]["expect_inbox_response"] is True

        await main._handle_internal_ask({
            "sender_session_id": "sender-1",
            "target_worker_pool": "review",
            "message": "continue when a worker is free",
            "mode": "continue_and_expect_inbox_back_async",
        })
        assert pool_enqueues[-1]["expect_inbox_response"] is True

        await main._handle_internal_ask({
            "sender_session_id": "sender-1",
            "target_session_id": "target-1",
            "message": "wait",
            "mode": "wait_and_grab_last_assistant_mssg_in_turn",
        })
        assert coordinator.calls[-1]["method"] == "ask_team_message"
        assert coordinator.calls[-1]["target_selector"] == {
            "kind": "session",
            "value": "target-1",
        }
    finally:
        main.coordinator = original_coordinator
        main._validate_optional_run_selector = original_validate  # type: ignore[assignment]
        main._resolve_communication_target = original_resolve  # type: ignore[assignment]
        main._pick_pool_worker_for_sender = original_pick_pool_worker  # type: ignore[assignment]
        main._enqueue_worker_pool_message = original_enqueue_pool_message  # type: ignore[assignment]


def test_internal_communication_modes() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    test_internal_communication_modes()
    print("ALL PASS")
