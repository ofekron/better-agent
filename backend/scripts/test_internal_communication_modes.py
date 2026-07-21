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
from communication_modes import (  # noqa: E402
    ASK_MODE_CONTINUE_AND_EXPECT_INBOX_BACK_ASYNC,
    ASK_MODE_WAIT_AND_GRAB_LAST_ASSISTANT_MSSG_IN_TURN,
    append_ask_response_contract,
    normalize_ask_execution,
)
from orchs.manager._delegation import (  # noqa: E402
    _ask_assistant_content,
    _build_worker_prompt,
)


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


def test_ask_execution_matrix() -> None:
    assert normalize_ask_execution(None, None) == (
        ASK_MODE_WAIT_AND_GRAB_LAST_ASSISTANT_MSSG_IN_TURN,
        "direct",
    )
    assert normalize_ask_execution(
        ASK_MODE_WAIT_AND_GRAB_LAST_ASSISTANT_MSSG_IN_TURN,
        "fork",
    ) == (ASK_MODE_WAIT_AND_GRAB_LAST_ASSISTANT_MSSG_IN_TURN, "fork")
    assert normalize_ask_execution(
        ASK_MODE_CONTINUE_AND_EXPECT_INBOX_BACK_ASYNC,
        "direct",
    ) == (ASK_MODE_CONTINUE_AND_EXPECT_INBOX_BACK_ASYNC, "direct")
    try:
        normalize_ask_execution(
            ASK_MODE_CONTINUE_AND_EXPECT_INBOX_BACK_ASYNC,
            "fork",
        )
    except ValueError as exc:
        assert str(exc) == "async ask mode requires run_mode='direct'"
    else:
        raise AssertionError("asynchronous fork ask was accepted")


def test_ask_response_contracts_are_mode_specific() -> None:
    sync_prompt = append_ask_response_contract(
        "task",
        ASK_MODE_WAIT_AND_GRAB_LAST_ASSISTANT_MSSG_IN_TURN,
    )
    assert "last assistant text batch" in sync_prompt
    assert "Do not call mssg or inbox" in sync_prompt

    async_prompt = append_ask_response_contract(
        "task",
        ASK_MODE_CONTINUE_AND_EXPECT_INBOX_BACK_ASYNC,
        sender_session_id='sender&"1',
    )
    assert 'recipient_session_id="sender&amp;&quot;1"' in async_prompt
    assert "exactly once" in async_prompt
    assert "sender is not waiting" in async_prompt


def test_fork_prompt_appends_sync_contract_after_user_prompt() -> None:
    prompt = _build_worker_prompt(
        "generic team guidance permits mssg",
        "review this",
        ASK_MODE_WAIT_AND_GRAB_LAST_ASSISTANT_MSSG_IN_TURN,
        "caller",
    )
    assert prompt.index("</user_prompt>") < prompt.index("<response_contract>")
    assert "Do not call mssg or inbox" in prompt
    detached = _build_worker_prompt(
        "generic team guidance permits mssg",
        "review this",
        "",
        "caller",
    )
    assert "<response_contract>" not in detached


def test_fork_ask_returns_only_final_assistant_text_batch() -> None:
    def assistant(text: str) -> dict:
        return {
            "type": "agent_message",
            "data": {
                "type": "assistant",
                "uuid": text,
                "message": {"content": [{"type": "text", "text": text}]},
            },
        }

    events = [
        assistant("working"),
        {
            "type": "agent_message",
            "data": {
                "type": "assistant",
                "uuid": "tool",
                "message": {"content": [{"type": "tool_use", "name": "Read"}]},
            },
        },
        assistant("final result"),
    ]
    assert _ask_assistant_content(
        events,
        ASK_MODE_WAIT_AND_GRAB_LAST_ASSISTANT_MSSG_IN_TURN,
    ) == "final result"
    assert _ask_assistant_content(events, "") is None


if __name__ == "__main__":
    test_internal_communication_modes()
    print("ALL PASS")
