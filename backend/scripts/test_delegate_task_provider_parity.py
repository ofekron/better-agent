from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import _test_home

_test_home.isolate("bc-test-delegate-task-provider-parity-")
os.environ["BETTER_CLAUDE_BACKEND_URL"] = "http://backend"
os.environ["BETTER_CLAUDE_INTERNAL_TOKEN"] = "tok"
os.environ["BETTER_CLAUDE_MSSG_SENDER_SESSION_ID"] = "sender-1"
os.environ["BETTER_CLAUDE_CWD"] = "/tmp/project"

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import communicate_mcp  # noqa: E402
import runner  # noqa: E402
import runner_codex  # noqa: E402
import runner_openai  # noqa: E402


def _delegate_schema_properties(schema: dict) -> dict:
    return schema["properties"]


def test_delegate_task_schema_is_shared_by_runner_providers() -> None:
    claude_tool = runner._build_delegate_task_tool(
        sender_session_id="sender-1",
        cwd="/tmp/project",
        model="model-x",
        backend_url="http://backend",
        internal_token="tok",
    )
    codex_tool = runner_codex._build_delegate_task_dynamic_tool()
    openai_tool = next(
        schema for schema in runner_openai._tool_schemas_for_run(
            inputs={
                "backend_url": "http://backend",
                "internal_token": "tok",
                "app_session_id": "sender-1",
            },
            capabilities_enabled=False,
            loopback_enabled=True,
            team_manager_enabled=False,
            team_orchestration_enabled=False,
            open_file_panel_enabled=False,
            file_editing_mode=False,
        )
        if schema.get("function", {}).get("name") == "delegate_task"
    )

    assert claude_tool.input_schema == codex_tool["inputSchema"]
    assert claude_tool.input_schema == openai_tool["function"]["parameters"]
    properties = _delegate_schema_properties(claude_tool.input_schema)
    assert set(properties) == {
        "task",
        "target_session_id",
        "provider_id",
        "model",
        "reasoning_effort",
        "sub_session",
    }
    assert properties["target_session_id"]["type"] == ["string", "null"]
    assert properties["provider_id"]["type"] == ["string", "null"]
    assert properties["model"]["type"] == ["string", "null"]
    assert properties["reasoning_effort"]["type"] == ["string", "null"]


def test_claude_delegate_task_posts_shared_payload() -> None:
    captured: list[tuple[str, dict, float]] = []
    original_post = runner._post_loopback_sync
    original_success = runner._tool_success_result

    def fake_post(payload, *, backend_url, internal_token, url_path,
                  timeout, non_json_t_key, log_prefix, backoff_cap, recover=None):
        captured.append((url_path, payload, timeout))
        return {"success": True, "target_session_id": "target-1"}

    runner._post_loopback_sync = fake_post  # type: ignore[assignment]
    runner._tool_success_result = lambda result: result  # type: ignore[assignment]
    try:
        tool = runner._build_delegate_task_tool(
            sender_session_id="sender-1",
            cwd="/tmp/project",
            model="model-x",
            backend_url="http://backend",
            internal_token="tok",
        )
        result = asyncio.run(tool.handler({
            "task": "do work",
            "target_session_id": "",
            "provider_id": "provider-1",
            "model": "model-1",
            "reasoning_effort": "high",
            "sub_session": False,
        }))
    finally:
        runner._post_loopback_sync = original_post  # type: ignore[assignment]
        runner._tool_success_result = original_success  # type: ignore[assignment]

    assert result["success"] is True
    endpoint, payload, timeout = captured[0]
    assert endpoint == "/api/internal/delegate-task"
    assert timeout == runner._DELEGATE_HTTP_TIMEOUT
    assert payload == {
        "sender_session_id": "sender-1",
        "task": "do work",
        "target_session_id": None,
        "cwd": "/tmp/project",
        "provider_id": "provider-1",
        "model": "model-1",
        "reasoning_effort": "high",
        "sub_session": False,
    }


def test_codex_delegate_task_posts_shared_payload() -> None:
    captured: list[tuple[str, dict, float]] = []
    original_post = runner_codex._post_loopback_sync

    def fake_post(payload: dict, *, backend_url: str, internal_token: str, **kwargs) -> dict:
        captured.append((kwargs["url_path"], payload, kwargs["timeout_s"]))
        return {"success": True, "target_session_id": "target-1"}

    runner_codex._post_loopback_sync = fake_post  # type: ignore[assignment]
    try:
        handler = runner_codex._build_delegate_task_tool_handler(
            sender_session_id="sender-1",
            cwd="/tmp/project",
            model="model-x",
            backend_url="http://backend",
            internal_token="tok",
        )
        result = asyncio.run(handler({"arguments": {
            "task": "do work",
            "target_session_id": "",
            "provider_id": "provider-1",
            "model": "model-1",
            "reasoning_effort": "high",
            "sub_session": False,
        }}))
    finally:
        runner_codex._post_loopback_sync = original_post  # type: ignore[assignment]

    assert result["success"] is True
    endpoint, payload, timeout = captured[0]
    assert endpoint == "/api/internal/delegate-task"
    assert timeout == runner_codex.DELEGATE_HTTP_TIMEOUT_S
    assert payload["target_session_id"] is None
    assert payload["provider_id"] == "provider-1"
    assert payload["model"] == "model-1"
    assert payload["reasoning_effort"] == "high"
    assert payload["sub_session"] is False


def test_gemini_delegate_task_posts_shared_payload() -> None:
    captured: list[tuple[str, dict, float]] = []
    original_post = communicate_mcp._post_json

    def fake_post(endpoint: str, payload: dict, timeout: float) -> dict:
        captured.append((endpoint, payload, timeout))
        return {"success": True, "target_session_id": "target-1"}

    communicate_mcp._post_json = fake_post  # type: ignore[assignment]
    try:
        result = communicate_mcp.delegate_task_response(
            "do work",
            target_session_id="",
            provider_id="provider-1",
            model="model-1",
            reasoning_effort="high",
            sub_session=False,
        )
    finally:
        communicate_mcp._post_json = original_post  # type: ignore[assignment]

    assert result["success"] is True
    endpoint, payload, timeout = captured[0]
    assert endpoint == "/api/internal/delegate-task"
    assert timeout == communicate_mcp._LONG_TIMEOUT
    assert payload["target_session_id"] is None
    assert payload["provider_id"] == "provider-1"
    assert payload["model"] == "model-1"
    assert payload["reasoning_effort"] == "high"
    assert payload["sub_session"] is False


def test_openai_delegate_task_posts_shared_payload() -> None:
    captured: list[tuple[str, dict, float]] = []
    original_post = runner_openai._post_loopback_sync

    def fake_post(payload: dict, *, backend_url: str, internal_token: str, **kwargs) -> dict:
        captured.append((kwargs["url_path"], payload, kwargs["timeout_s"]))
        return {"success": True, "target_session_id": "target-1"}

    runner_openai._post_loopback_sync = fake_post  # type: ignore[assignment]
    try:
        handlers = runner_openai._build_loopback_tool_handlers(
            {
                "backend_url": "http://backend",
                "internal_token": "tok",
                "app_session_id": "sender-1",
            },
            cwd="/tmp/project",
            model="model-x",
        )
        result = asyncio.run(handlers["delegate_task"]({"arguments": {
            "task": "do work",
            "target_session_id": "",
            "provider_id": "provider-1",
            "model": "model-1",
            "reasoning_effort": "high",
            "sub_session": False,
        }}))
    finally:
        runner_openai._post_loopback_sync = original_post  # type: ignore[assignment]

    assert json.loads(result)["success"] is True
    endpoint, payload, timeout = captured[0]
    assert endpoint == "/api/internal/delegate-task"
    assert timeout == runner_openai.DELEGATE_HTTP_TIMEOUT_S
    assert payload["target_session_id"] is None
    assert payload["provider_id"] == "provider-1"
    assert payload["model"] == "model-1"
    assert payload["reasoning_effort"] == "high"
    assert payload["sub_session"] is False


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
