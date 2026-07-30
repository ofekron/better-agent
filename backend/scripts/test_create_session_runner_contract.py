from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import _test_home

_test_home.isolate("bc-test-create-session-runner-")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import orchestration_tool_schemas  # noqa: E402
import runner  # noqa: E402
import runner_better_agent  # noqa: E402
import runner_codex  # noqa: E402


EXPECTED_RUNNERS = ["native", "better_agent_runner"]


def _assert_schema(schema: dict, label: str) -> None:
    assert schema["properties"]["cwd"]["type"] == ["string", "null"], (
        f"{label} must expose the optional remote working directory"
    )
    assert "cwd" not in schema.get("required", [])
    runner_property = schema["properties"]["runner"]
    advertised = [
        value for value in runner_property["enum"] if value is not None
    ]
    assert advertised == EXPECTED_RUNNERS, (
        f"{label} must expose only canonical runtime-profile runner IDs"
    )
    if "null" in runner_property["type"]:
        assert None in runner_property["enum"]


def test_create_session_schema_runner_ids_have_provider_parity() -> None:
    assert orchestration_tool_schemas.RUNTIME_PROFILE_RUNNER_IDS == tuple(
        EXPECTED_RUNNERS,
    )
    _assert_schema(runner._CREATE_SESSION_INPUT_SCHEMA, "Claude")
    _assert_schema(runner_codex._CREATE_SESSION_INPUT_SCHEMA, "Codex")
    _assert_schema(
        runner_better_agent._CREATE_SESSION_INPUT_SCHEMA,
        "Better Agent",
    )


async def _exercise_handler(
    handler,
    *,
    invalid_arguments: dict,
    valid_arguments: dict,
    calls: list[dict],
) -> None:
    invalid_result = await handler({"arguments": invalid_arguments})
    if isinstance(invalid_result, str):
        assert invalid_result.startswith("Error:")
    else:
        assert invalid_result.get("success") is False
    assert "native" in str(invalid_result)
    assert calls == [], "invalid runner must be rejected before loopback POST"

    malformed_result = await handler({
        "arguments": {"name": "malformed", "runner": 1},
    })
    assert "string" in str(malformed_result)
    assert calls == [], "non-string runner must be rejected before loopback POST"

    valid_result = await handler({"arguments": valid_arguments})
    if isinstance(valid_result, str):
        valid_result = json.loads(valid_result)
    assert "error" not in str(valid_result).lower()
    assert calls[-1]["runner"] == "native"
    assert calls[-1]["cwd"] == "C:\\Users\\Lenovo\\remote-project"

    inherited_result = await handler({"arguments": {"name": "inherit"}})
    if isinstance(inherited_result, str):
        inherited_result = json.loads(inherited_result)
    assert "error" not in str(inherited_result).lower()
    assert calls[-1]["runner"] is None
    assert calls[-1]["cwd"] == "/repo"


def test_dynamic_handlers_reject_provider_kind_before_session_creation() -> None:
    calls: list[dict] = []

    def fake_post(payload: dict, **_kwargs) -> dict:
        calls.append(payload)
        return {"success": True, "session_id": "created"}

    original_codex = runner_codex._post_loopback_sync
    original_better_agent = runner_better_agent._post_loopback_sync
    runner_codex._post_loopback_sync = fake_post
    runner_better_agent._post_loopback_sync = fake_post
    try:
        codex = runner_codex._build_create_session_tool_handler(
            sender_session_id="sender",
            cwd="/repo",
            model="model",
            backend_url="http://backend",
            internal_token="token",
        )
        asyncio.run(_exercise_handler(
            codex,
            invalid_arguments={"name": "bad", "runner": "codex"},
            valid_arguments={
                "name": "valid",
                "runner": "native",
                "cwd": "C:\\Users\\Lenovo\\remote-project",
            },
            calls=calls,
        ))
        calls.clear()
        better_agent = runner_better_agent._build_loopback_tool_handlers(
            inputs={
                "app_session_id": "sender",
                "cwd": "/repo",
                "model": "model",
                "backend_url": "http://backend",
                "internal_token": "token",
            },
            cwd="/repo",
            model="model",
            lock_registry=runner_better_agent.LockRegistry(),
        )["create_session"]
        asyncio.run(_exercise_handler(
            better_agent,
            invalid_arguments={"name": "bad", "runner": "codex"},
            valid_arguments={
                "name": "valid",
                "runner": "native",
                "cwd": "C:\\Users\\Lenovo\\remote-project",
            },
            calls=calls,
        ))
    finally:
        runner_codex._post_loopback_sync = original_codex
        runner_better_agent._post_loopback_sync = original_better_agent


def test_claude_handler_rejects_provider_kind_before_session_creation() -> None:
    calls: list[dict] = []

    def fake_post(payload: dict) -> dict:
        calls.append(payload)
        return {"success": True, "session_id": "created"}

    tool = runner._build_create_session_tool(
        sender_session_id="sender",
        cwd="/repo",
        model="model",
        backend_url="http://backend",
        internal_token="token",
    )
    original = runner._post_loopback_sync
    runner._post_loopback_sync = lambda payload, **_kwargs: fake_post(payload)
    try:
        invalid = asyncio.run(tool.handler({"name": "bad", "runner": "claude"}))
        assert invalid["is_error"] is True
        assert "native" in invalid["content"][0]["text"]
        assert calls == []

        valid = asyncio.run(tool.handler({
            "name": "valid",
            "runner": "native",
            "cwd": "C:\\Users\\Lenovo\\remote-project",
        }))
        assert valid["is_error"] is False
        assert calls[-1]["runner"] == "native"
        assert calls[-1]["cwd"] == "C:\\Users\\Lenovo\\remote-project"

        inherited = asyncio.run(tool.handler({"name": "inherit"}))
        assert inherited["is_error"] is False
        assert calls[-1]["runner"] is None
        assert calls[-1]["cwd"] == "/repo"
    finally:
        runner._post_loopback_sync = original


if __name__ == "__main__":
    test_create_session_schema_runner_ids_have_provider_parity()
    test_dynamic_handlers_reject_provider_kind_before_session_creation()
    test_claude_handler_rejects_provider_kind_before_session_creation()
    print("create-session runner contract: OK")
