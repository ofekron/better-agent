from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

import _test_home

_test_home.isolate("bc-test-communication-modes-")
os.environ["BETTER_CLAUDE_TEST_AUTH_BYPASS"] = "1"

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import internal_extension_api  # noqa: E402
import internal_guards  # noqa: E402
import internal_messaging_api  # noqa: E402
import main  # noqa: E402
import worker_pools_api  # noqa: E402
from stores import worker_store  # noqa: E402
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

    async def start_session_processor_async(self, session_id: str):
        self.calls.append({
            "method": "start_session_processor_async",
            "session_id": session_id,
        })

    async def ask_team_message(self, **kwargs):
        self.calls.append({"method": "ask_team_message", **kwargs})
        return {"success": True, "response": "ok"}


async def _run() -> None:
    from fastapi import BackgroundTasks

    original_coordinator = internal_messaging_api._coordinator_ref
    original_validate = internal_messaging_api._validate_optional_run_selector
    original_resolve = internal_messaging_api._resolve_communication_target
    original_pick_pool_worker = worker_pools_api._pick_pool_worker_for_sender
    original_enqueue_pool_message = worker_pools_api._enqueue_worker_pool_message
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
        internal_messaging_api._coordinator_ref = coordinator  # type: ignore[assignment]
        internal_messaging_api._validate_optional_run_selector = validate  # type: ignore[assignment]
        internal_messaging_api._resolve_communication_target = resolve  # type: ignore[assignment]
        worker_pools_api._pick_pool_worker_for_sender = pick_pool_worker  # type: ignore[assignment]
        worker_pools_api._enqueue_worker_pool_message = enqueue_pool_message  # type: ignore[assignment]

        await internal_messaging_api._handle_internal_mssg({
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

        await internal_messaging_api._handle_internal_mssg({
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

        background_tasks = BackgroundTasks()
        await internal_messaging_api._handle_internal_ask({
            "sender_session_id": "sender-1",
            "target_session_id": "target-1",
            "message": "continue",
            "mode": "continue_and_expect_inbox_back_async",
        }, background_tasks)
        assert coordinator.calls[-1]["method"] == "submit_team_message"
        assert coordinator.calls[-1]["detach"] is True
        assert coordinator.calls[-1]["expect_inbox_response"] is True
        assert coordinator.calls[-1]["start_turn"] is False
        await background_tasks()
        assert coordinator.calls[-1] == {
            "method": "start_session_processor_async",
            "session_id": "target-1",
        }

        await internal_messaging_api._handle_internal_ask({
            "sender_session_id": "sender-1",
            "target_worker_pool": "review",
            "message": "continue when a worker is free",
            "mode": "continue_and_expect_inbox_back_async",
        })
        assert pool_enqueues[-1]["expect_inbox_response"] is True

        await internal_messaging_api._handle_internal_ask({
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
        assert coordinator.calls[-1]["user_initiated"] is False

        await internal_messaging_api._handle_internal_ask({
            "sender_session_id": "sender-1",
            "target_session_id": "target-1",
            "message": "user-facing QA turn",
            "mode": "wait_and_grab_last_assistant_mssg_in_turn",
            "user_initiated": True,
        })
        assert coordinator.calls[-1]["user_initiated"] is False

        await internal_messaging_api._handle_internal_ask(
            {
                "sender_session_id": "sender-1",
                "target_session_id": "target-1",
                "message": "trusted user-facing QA turn",
                "mode": "wait_and_grab_last_assistant_mssg_in_turn",
            },
            trusted_user_initiated=True,
        )
        assert coordinator.calls[-1]["user_initiated"] is True
    finally:
        internal_messaging_api._coordinator_ref = original_coordinator
        internal_messaging_api._validate_optional_run_selector = original_validate  # type: ignore[assignment]
        internal_messaging_api._resolve_communication_target = original_resolve  # type: ignore[assignment]
        worker_pools_api._pick_pool_worker_for_sender = original_pick_pool_worker  # type: ignore[assignment]
        worker_pools_api._enqueue_worker_pool_message = original_enqueue_pool_message  # type: ignore[assignment]


def test_internal_communication_modes() -> None:
    asyncio.run(_run())


def test_testape_qa_ask_requires_runtime_capability_and_exact_session(
    monkeypatch,
) -> None:
    import testape_qa_runtime

    body = {
        "sender_session_id": "sender",
        "target_session_id": "target",
        "message": "run QA",
    }
    monkeypatch.delenv("BETTER_AGENT_TESTAPE_QA_TOKEN", raising=False)
    monkeypatch.delenv("BETTER_AGENT_TESTAPE_QA_RUNTIME_ID", raising=False)
    monkeypatch.setattr(testape_qa_runtime, "_expected_token", None)
    monkeypatch.setattr(testape_qa_runtime, "_runtime_id", None)
    monkeypatch.setattr(internal_guards, "authority_is_valid", lambda: True)
    with pytest.raises(main.HTTPException) as disabled:
        asyncio.run(internal_messaging_api.internal_testape_qa_ask(body, "internal", "qa"))
    assert disabled.value.status_code == 403

    monkeypatch.setenv("BETTER_AGENT_TESTAPE_QA_TOKEN", "qa")
    monkeypatch.setenv("BETTER_AGENT_TESTAPE_QA_RUNTIME_ID", "runtime")
    monkeypatch.setattr(testape_qa_runtime, "_expected_token", None)
    monkeypatch.setattr(testape_qa_runtime, "_runtime_id", None)
    testape_qa_runtime.claim("runtime", "sender")
    testape_qa_runtime.claim("runtime", "target")
    captured = {}

    async def handle(request_body, *, trusted_user_initiated=False):
        captured["body"] = request_body
        captured["trusted"] = trusted_user_initiated
        return {"success": True}

    monkeypatch.setattr(internal_messaging_api, "_handle_internal_ask", handle)
    assert asyncio.run(internal_messaging_api.internal_testape_qa_ask(body, "internal", "qa")) == {
        "success": True,
    }
    assert captured == {
        "body": {**body, "_testape_qa_runtime_id": "runtime"},
        "trusted": True,
    }

    with pytest.raises(main.HTTPException) as foreign:
        asyncio.run(internal_messaging_api.internal_testape_qa_ask(
            {**body, "target_session_id": "foreign"},
            "internal",
            "qa",
        ))
    assert foreign.value.status_code == 403

    with pytest.raises(main.HTTPException) as pooled:
        asyncio.run(internal_messaging_api.internal_testape_qa_ask(
            {
                "sender_session_id": "sender",
                "target_worker_pool": "pool",
                "message": "run QA",
            },
            "internal",
            "qa",
        ))
    assert pooled.value.status_code == 400


def test_testape_qa_ask_uses_durable_idempotent_operation(monkeypatch) -> None:
    import testape_qa_runtime

    monkeypatch.setenv("BETTER_AGENT_TESTAPE_QA_TOKEN", "qa")
    monkeypatch.setenv("BETTER_AGENT_TESTAPE_QA_RUNTIME_ID", "runtime")
    monkeypatch.setattr(testape_qa_runtime, "_expected_token", None)
    monkeypatch.setattr(testape_qa_runtime, "_runtime_id", None)
    monkeypatch.setattr(internal_guards, "authority_is_valid", lambda: True)
    testape_qa_runtime.claim("runtime", "sender")
    testape_qa_runtime.claim("runtime", "target")
    captured = {}

    async def durable(operation, body):
        captured.update(operation=operation, body=body)
        return {"success": True, "id": body["_mcp_job_id"], "ready": False}

    monkeypatch.setattr(internal_extension_api, "maybe_run_core_mcp_job", durable)
    body = {
        "sender_session_id": "sender",
        "target_session_id": "target",
        "message": "run QA",
        "_mcp_job_id": "stable-job",
    }
    result = asyncio.run(
        internal_messaging_api.internal_testape_qa_ask(body, "internal", "qa")
    )

    assert result == {"success": True, "id": "stable-job", "ready": False}
    assert captured == {
        "operation": "testape-qa-ask",
        "body": {**body, "_testape_qa_runtime_id": "runtime"},
    }
    assert "testape-qa-ask" in internal_extension_api._CORE_MCP_RESUMABLE_OPERATIONS
    assert internal_extension_api._core_mcp_job_handler("testape-qa-ask") is (
        internal_messaging_api._handle_internal_testape_qa_ask
    )


def test_pool_enqueue_endpoint_maps_unavailable_inbox_to_400(monkeypatch) -> None:
    sender = main.session_manager.create(
        name="inbox blocked endpoint caller",
        cwd="/repo",
        orchestration_mode="native",
        disallowed_tools=["inbox"],
    )
    monkeypatch.setattr(
        worker_pools_api.internal_guards, "require_role_internal", lambda _role: None
    )

    try:
        asyncio.run(worker_pools_api.internal_enqueue_worker_pool_prompt(
            body={
                "tag": "review",
                "sender_session_id": sender["id"],
                "prompt": "review later",
                "expect_inbox_response": True,
            },
            x_internal_token="test",
        ))
    except main.HTTPException as exc:
        assert exc.status_code == 400
        assert exc.detail == "async response requires inbox for the sender session"
    else:
        raise AssertionError("pool enqueue endpoint returned success for an unavailable inbox")


def test_pool_dispatch_rejects_inbox_blocked_target(monkeypatch) -> None:
    sender = main.session_manager.create(
        name="pool caller",
        cwd="/repo",
        orchestration_mode="native",
    )
    target = main.session_manager.create(
        name="inbox blocked pool worker",
        cwd="/repo",
        orchestration_mode="native",
        disallowed_tools=["inbox"],
    )
    item = {
        "id": "pool-item",
        "sender_session_id": sender["id"],
        "prompt": "review later",
        "expect_inbox_response": True,
    }
    queued = iter((item, None))
    failures = []
    submitted = []
    original_coordinator = worker_pools_api._coordinator_ref
    coordinator = main.Coordinator()

    monkeypatch.setattr(worker_store, "peek_pool_task", lambda _tag: next(queued))
    monkeypatch.setattr(
        worker_store,
        "record_pool_task_failure",
        lambda _tag, _item_id, error: failures.append(error) or {"action": "failed"},
    )
    monkeypatch.setattr(
        worker_pools_api,
        "_pick_pool_worker_for_sender",
        lambda *_args: {"agent_session_id": target["id"]},
    )
    monkeypatch.setattr(coordinator, "submit_prompt", lambda *_args, **_kwargs: submitted.append(True))

    async def broadcast_workers_changed(_cwd) -> None:
        return None

    monkeypatch.setattr(coordinator, "broadcast_workers_changed", broadcast_workers_changed)
    try:
        worker_pools_api._coordinator_ref = coordinator
        asyncio.run(worker_pools_api._process_worker_pool_queue("review"))
    finally:
        worker_pools_api._coordinator_ref = original_coordinator

    assert failures == ["async response requires inbox for the target session"]
    assert submitted == []


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
