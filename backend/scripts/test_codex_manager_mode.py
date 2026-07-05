import asyncio
import os
import sys
import tempfile
from pathlib import Path

import pytest

import _test_home
_test_home.isolate("bc-test-codex-manager-")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from provider_codex import CodexProvider  # noqa: E402
import runner_codex  # noqa: E402
import codex_normalize  # noqa: E402


def check(cond: bool, msg: str, failures: list[str]) -> None:
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


@pytest.fixture
def failures() -> list[str]:
    collected: list[str] = []
    yield collected
    assert not collected


def test_codex_provider_advertises_manager_mode(failures: list[str]) -> None:
    check(CodexProvider.supports_manager_mode is True, "Codex supports manager mode", failures)


def test_ask_dynamic_tool_contract(failures: list[str]) -> None:
    spec = runner_codex._build_ask_dynamic_tool()
    check(spec["name"] == "ask", "dynamic tool is named ask", failures)
    check("inputSchema" in spec, "dynamic tool has input schema", failures)
    required = set(spec["inputSchema"]["required"])
    check("message" in required, "ask requires message", failures)
    properties = spec["inputSchema"]["properties"]
    check("target_session_id" in properties, "ask accepts session target", failures)
    check("target_worker_id" in properties, "ask accepts worker target", failures)
    check("target_worker_pool" in properties, "ask accepts pool target", failures)
    check("pool_affinity_key" in properties, "ask accepts pool affinity hint", failures)
    check("run_mode" in properties, "ask accepts run mode", failures)
    check("mode" in properties, "ask accepts wait/async behavior mode", failures)
    check("worker_description" in properties, "ask accepts optional session label for fork mode", failures)


def test_create_worker_dynamic_tool_contract(failures: list[str]) -> None:
    spec = runner_codex._build_create_worker_dynamic_tool()
    check(spec["name"] == "create_worker", "dynamic tool is named create_worker", failures)
    required = set(spec["inputSchema"]["required"])
    check("worker_description" in required, "create_worker requires description", failures)
    check("justification" in required, "create_worker requires justification", failures)
    check("orchestration_mode" in required, "create_worker requires mode", failures)


def test_create_session_dynamic_tool_contract(failures: list[str]) -> None:
    spec = runner_codex._build_create_session_dynamic_tool()
    check(spec["name"] == "create_session", "dynamic tool is named create_session", failures)
    check("complex tasks" in spec["description"], "create_session description scopes team mode", failures)
    properties = spec["inputSchema"]["properties"]
    check("provider_id" in properties, "create_session accepts provider override", failures)
    check("model" in properties, "create_session accepts model override", failures)
    check("reasoning_effort" in properties, "create_session accepts effort override", failures)
    mode_description = properties["orchestration_mode"]["description"]
    check("complex tasks" in mode_description, "create_session mode description scopes team mode", failures)


def test_create_sub_session_dynamic_tool_contract(failures: list[str]) -> None:
    spec = runner_codex._build_create_sub_session_dynamic_tool()
    check(spec["name"] == "create_sub_session", "dynamic tool is named create_sub_session", failures)
    properties = spec["inputSchema"]["properties"]
    check("prompt" not in properties, "create_sub_session does not accept prompt", failures)
    check("description" in properties, "create_sub_session accepts description", failures)
    check("provider_id" in properties, "create_sub_session accepts provider override", failures)
    check("model" in properties, "create_sub_session accepts model override", failures)
    check("reasoning_effort" in properties, "create_sub_session accepts effort override", failures)


def test_delegate_task_dynamic_tool_contract(failures: list[str]) -> None:
    spec = runner_codex._build_delegate_task_dynamic_tool()
    check(spec["name"] == "delegate_task", "dynamic tool is named delegate_task", failures)
    properties = spec["inputSchema"]["properties"]
    check("provider_id" in properties, "delegate_task accepts provider override", failures)
    check("model" in properties, "delegate_task accepts model override", failures)
    check("reasoning_effort" in properties, "delegate_task accepts effort override", failures)
    check("sub_session" in properties, "delegate_task accepts sub-session override", failures)


def test_native_loopback_registers_mssg_tool(failures: list[str]) -> None:
    tools, handlers = runner_codex._build_dynamic_tool_set(
        mode="native",
        app_session_id="sender-1",
        backend_url="http://backend",
        internal_token="tok",
        mssg_sender_session_id="sender-1",
        cwd="/tmp",
        model="model-1",
        open_file_panel_enabled=False,
        file_editing_mode=False,
        team_orchestration_enabled=True,
        disabled_builtin_tools=set(),
        existing_tool_names=set(),
    )
    names = {tool.get("name") for tool in tools}
    check("mssg" in names, "native loopback registers mssg dynamic tool", failures)
    check("mssg" in handlers, "native loopback registers mssg handler", failures)
    check("delegate_task" in names, "native loopback keeps generic handoff tool", failures)
    mssg_spec = next(tool for tool in tools if tool.get("name") == "mssg")
    properties = (mssg_spec.get("inputSchema") or {}).get("properties") or {}
    check("collapse_key" in properties, "mssg accepts collapse key", failures)
    check("collapse_policy" in properties, "mssg accepts collapse policy", failures)


def test_dynamic_tool_json_result_is_compact(failures: list[str]) -> None:
    result = runner_codex._dynamic_tool_json_result(
        {"success": True, "value": {"nested": ["x", "y"]}},
        success=True,
    )
    text = result["contentItems"][0]["text"]
    check(text == '{"success":true,"value":{"nested":["x","y"]}}', "dynamic tool JSON is compact", failures)
    check("\n" not in text, "dynamic tool JSON has no pretty-print newlines", failures)


def test_subagent_notification_response_item_is_ingested(failures: list[str]) -> None:
    event = codex_normalize._normalize_response_item_event({
        "type": "message",
        "role": "user",
        "content": [{
            "type": "input_text",
            "text": (
                "<subagent_notification>\n"
                "{\"agent_path\":\"agent-1\",\"status\":{\"completed\":\"done\"}}\n"
                "</subagent_notification>"
            ),
        }],
    }, "parent-uuid")

    content = (event or {}).get("message", {}).get("content", [])
    block = content[0] if content else {}
    check(event is not None, "subagent notification is normalized", failures)
    check(event.get("type") == "user", "subagent notification stays user event", failures)
    check(block.get("type") == "tool_result", "subagent notification renders as tool result", failures)
    check(block.get("tool_use_id") == "agent-1", "subagent notification carries agent id", failures)
    check(block.get("content") == '{"completed": "done"}', "subagent status is preserved", failures)


def test_regular_user_response_item_is_not_ingested(failures: list[str]) -> None:
    event = codex_normalize._normalize_response_item_event({
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": "ordinary user history"}],
    }, "parent-uuid")
    check(event is None, "regular user response_item stays dropped", failures)


async def _exercise_ask_direct_handler(failures: list[str]) -> None:
    captured = {}
    original = runner_codex._post_loopback_sync

    def fake_post(payload: dict, *, backend_url: str, internal_token: str, **kwargs) -> dict:
        captured["payload"] = payload
        captured["backend_url"] = backend_url
        captured["internal_token"] = internal_token
        captured["url_path"] = kwargs.get("url_path")
        return {"success": True, "response": "ok"}

    runner_codex._post_loopback_sync = fake_post
    try:
        handler = runner_codex._build_ask_tool_handler(
            sender_session_id="sender-1",
            app_session_id="app1",
            model="gpt-5.4",
            cwd="/tmp/project",
            backend_url="http://backend",
            internal_token="tok",
        )
        result = await handler({
            "arguments": {
                "target_session_id": "worker-1",
                "message": "do work",
            }
        })
    finally:
        runner_codex._post_loopback_sync = original

    check(result["success"] is True, "ask direct handler reports success", failures)
    check(captured["backend_url"] == "http://backend", "ask uses backend URL", failures)
    check(captured["internal_token"] == "tok", "ask uses internal token", failures)
    check(captured["url_path"] == "/api/internal/ask", "ask direct uses ask endpoint", failures)
    payload = captured["payload"]
    check(payload["sender_session_id"] == "sender-1", "ask payload has sender", failures)
    check(payload["target_session_id"] == "worker-1", "ask payload has target", failures)
    check(payload["message"] == "do work", "ask payload has message", failures)
    check(str(payload.get("ask_id", "")).startswith("ask_"), "ask payload has ask id", failures)


async def _exercise_ask_fork_handler(failures: list[str]) -> None:
    captured = {}
    original = runner_codex._post_loopback_sync

    def fake_post(payload: dict, *, backend_url: str, internal_token: str, **kwargs) -> dict:
        captured["payload"] = payload
        captured["url_path"] = kwargs.get("url_path")
        return {"success": True, "worker_session_id": "w1"}

    runner_codex._post_loopback_sync = fake_post
    try:
        handler = runner_codex._build_ask_tool_handler(
            sender_session_id="sender-1",
            app_session_id="app1",
            model="gpt-5.4",
            cwd="/tmp/project",
            backend_url="http://backend",
            internal_token="tok",
        )
        result = await handler({
            "arguments": {
                "target_session_id": "worker-1",
                "message": "do work",
                "run_mode": "fork",
            }
        })
    finally:
        runner_codex._post_loopback_sync = original

    check(result["success"] is True, "ask fork handler reports success", failures)
    check(captured["url_path"] == "/api/internal/ask-fork", "ask fork uses ask-fork endpoint", failures)
    payload = captured["payload"]
    check(payload["app_session_id"] == "app1", "ask fork payload has app session", failures)
    check(payload["instructions"] == "do work", "ask fork payload has instructions", failures)
    check(payload["worker_session_id"] == "worker-1", "ask fork payload has existing worker", failures)
    check(payload["worker_description"] == "", "ask fork payload allows empty session label", failures)
    check(payload["model"] == "gpt-5.4", "ask fork payload has model", failures)
    check(payload["cwd"] == "/tmp/project", "ask fork payload has cwd", failures)
    check(payload["run_mode"] == "fork", "ask fork payload has run mode", failures)


async def _exercise_create_worker_handler(failures: list[str]) -> None:
    captured = {}
    original = runner_codex._post_loopback_sync

    def fake_post(payload: dict, *, backend_url: str, internal_token: str, **kwargs) -> dict:
        captured["payload"] = payload
        captured["url_path"] = kwargs.get("url_path")
        return {"success": True, "worker_session_id": "w-new"}

    runner_codex._post_loopback_sync = fake_post
    try:
        handler = runner_codex._build_create_worker_tool_handler(
            app_session_id="app1",
            backend_url="http://backend",
            internal_token="tok",
            model="gpt-5.4",
            cwd="/tmp/project",
        )
        result = await handler({
            "arguments": {
                "worker_description": "reviewer",
                "justification": "need independent review",
                "orchestration_mode": "native",
            }
        })
    finally:
        runner_codex._post_loopback_sync = original

    check(result["success"] is True, "create_worker dynamic handler reports success", failures)
    check(captured["url_path"] == "/api/internal/create-worker", "create_worker uses endpoint", failures)
    payload = captured["payload"]
    check(payload["app_session_id"] == "app1", "create_worker payload has app session", failures)
    check(payload["worker_description"] == "reviewer", "create_worker payload has description", failures)
    check(payload["orchestration_mode"] == "native", "create_worker payload has mode", failures)
    check("model" not in payload, "create_worker does not pass model unless requested", failures)


async def _exercise_ensure_named_worker_handler(failures: list[str]) -> None:
    captured = {}
    original = runner_codex._post_loopback_sync

    def fake_post(payload: dict, *, backend_url: str, internal_token: str, **kwargs) -> dict:
        captured["payload"] = payload
        captured["url_path"] = kwargs.get("url_path")
        return {
            "workers": [{
                "agent_session_id": "worker-1",
                "name": "worker:testape",
                "created": True,
                "orchestration_mode": "team",
                "registry_cwd": "/repo",
            }]
        }

    runner_codex._post_loopback_sync = fake_post
    try:
        handler = runner_codex._build_ensure_named_worker_tool_handler(
            cwd="/inherited",
            backend_url="http://backend",
            internal_token="tok",
        )
        result = await handler({
            "arguments": {
                "name": "testape",
                "cwd": "/repo",
                "orchestration_mode": "team",
                "provision_prompt": "seed",
            }
        })
    finally:
        runner_codex._post_loopback_sync = original

    check(result["success"] is True, "ensure_named_worker dynamic handler reports success", failures)
    check(captured["url_path"] == "/api/internal/workers/provision", "ensure_named_worker uses endpoint", failures)
    payload = captured["payload"]
    check(payload["cwd"] == "/repo", "ensure_named_worker payload has cwd", failures)
    spec = payload["workers"][0]
    check(spec["role_key"] == "testape", "ensure_named_worker payload has singleton key", failures)
    check(spec["orchestration_mode"] == "team", "ensure_named_worker payload has mode", failures)
    check(spec["provision_prompt"] == "seed", "ensure_named_worker payload has seed", failures)
    check(spec["tags"] == ["testape"], "ensure_named_worker payload has pool tag", failures)

    captured.clear()
    runner_codex._post_loopback_sync = fake_post
    try:
        handler = runner_codex._build_ensure_named_worker_tool_handler(
            cwd="/inherited",
            backend_url="http://backend",
            internal_token="tok",
        )
        inherited_result = await handler({
            "arguments": {
                "name": "testape",
                "orchestration_mode": "team",
            }
        })
    finally:
        runner_codex._post_loopback_sync = original

    check(inherited_result["success"] is True, "ensure_named_worker dynamic handler accepts omitted cwd", failures)
    check(captured["payload"]["cwd"] == "/inherited", "ensure_named_worker inherits cwd when omitted", failures)


async def _exercise_delegate_task_handler(failures: list[str]) -> None:
    captured = {}
    original = runner_codex._post_loopback_sync

    def fake_post(payload: dict, *, backend_url: str, internal_token: str, **kwargs) -> dict:
        captured["payload"] = payload
        captured["url_path"] = kwargs.get("url_path")
        return {"success": True, "target_session_id": "target-1"}

    runner_codex._post_loopback_sync = fake_post
    try:
        handler = runner_codex._build_delegate_task_tool_handler(
            sender_session_id="sender-1",
            cwd="/tmp/project",
            model="gpt-5.4",
            backend_url="http://backend",
            internal_token="tok",
        )
        result = await handler({
            "arguments": {
                "task": "do work",
                "provider_id": "provider-1",
                "model": "model-1",
                "reasoning_effort": "high",
                "sub_session": False,
            }
        })
    finally:
        runner_codex._post_loopback_sync = original

    check(result["success"] is True, "delegate_task dynamic handler reports success", failures)
    check(captured["url_path"] == "/api/internal/delegate-task", "delegate_task uses endpoint", failures)
    payload = captured["payload"]
    check(payload["sender_session_id"] == "sender-1", "delegate_task payload has sender", failures)
    check(payload["provider_id"] == "provider-1", "delegate_task payload has provider", failures)
    check(payload["model"] == "model-1", "delegate_task payload has explicit model", failures)
    check(payload["reasoning_effort"] == "high", "delegate_task payload has effort", failures)
    check(payload["sub_session"] is False, "delegate_task payload has sub-session flag", failures)


async def _exercise_ask_async_mode_handler(failures: list[str]) -> None:
    captured = {}
    original = runner_codex._post_loopback_sync

    def fake_post(payload: dict, *, backend_url: str, internal_token: str, **kwargs) -> dict:
        captured["payload"] = payload
        captured["url_path"] = kwargs.get("url_path")
        return {"success": True, "queued_id": "queued-1", "expects_response": True}

    runner_codex._post_loopback_sync = fake_post
    try:
        handler = runner_codex._build_ask_tool_handler(
            sender_session_id="sender-1",
            app_session_id="app1",
            model="gpt-5.4",
            cwd="/tmp/project",
            backend_url="http://backend",
            internal_token="tok",
        )
        result = await handler({
            "arguments": {
                "target_session_id": "worker-1",
                "message": "run async",
                "mode": "continue_and_expect_mssg_back_async",
            }
        })
    finally:
        runner_codex._post_loopback_sync = original

    check(result["success"] is True, "ask async mode handler reports success", failures)
    check(captured["url_path"] == "/api/internal/ask", "ask async mode uses ask endpoint", failures)
    payload = captured["payload"]
    check(payload["sender_session_id"] == "sender-1", "ask async mode payload has sender", failures)
    check(payload["target_session_id"] == "worker-1", "ask async mode payload has target", failures)
    check(payload["message"] == "run async", "ask async mode payload has message", failures)
    check(payload["mode"] == "continue_and_expect_mssg_back_async", "ask async mode payload has mode", failures)


async def _exercise_create_session_handler(failures: list[str]) -> None:
    captured = {}
    original = runner_codex._post_loopback_sync

    def fake_post(payload: dict, *, backend_url: str, internal_token: str, **kwargs) -> dict:
        captured["payload"] = payload
        captured["url_path"] = kwargs.get("url_path")
        return {"success": True, "session_id": "session-new"}

    runner_codex._post_loopback_sync = fake_post
    try:
        handler = runner_codex._build_create_session_tool_handler(
            sender_session_id="sender-1",
            cwd="/tmp/project",
            model="gpt-5.4",
            backend_url="http://backend",
            internal_token="tok",
        )
        result = await handler({
            "arguments": {
                "name": "scratch",
                "orchestration_mode": "native",
                "provider_id": "provider-1",
                "model": "model-1",
                "reasoning_effort": "high",
            }
        })
    finally:
        runner_codex._post_loopback_sync = original

    check(result["success"] is True, "create_session dynamic handler reports success", failures)
    check(captured["url_path"] == "/api/internal/create-session", "create_session uses endpoint", failures)
    payload = captured["payload"]
    check(payload["sender_session_id"] == "sender-1", "create_session payload has sender", failures)
    check(payload["provider_id"] == "provider-1", "create_session payload has provider", failures)
    check(payload["model"] == "model-1", "create_session payload has explicit model", failures)
    check(payload["reasoning_effort"] == "high", "create_session payload has effort", failures)


async def _exercise_create_sub_session_handler(failures: list[str]) -> None:
    captured = {}
    original = runner_codex._post_loopback_sync

    def fake_post(payload: dict, *, backend_url: str, internal_token: str, **kwargs) -> dict:
        captured["payload"] = payload
        captured["url_path"] = kwargs.get("url_path")
        return {"success": True, "target_session_id": "sub-new"}

    runner_codex._post_loopback_sync = fake_post
    try:
        handler = runner_codex._build_create_sub_session_tool_handler(
            sender_session_id="sender-1",
            cwd="/tmp/project",
            model="gpt-5.4",
            backend_url="http://backend",
            internal_token="tok",
        )
        result = await handler({
            "arguments": {
                "description": "hidden reviewer",
                "provider_id": "provider-1",
                "model": "model-1",
                "reasoning_effort": "high",
            }
        })
    finally:
        runner_codex._post_loopback_sync = original

    check(result["success"] is True, "create_sub_session dynamic handler reports success", failures)
    check(captured["url_path"] == "/api/internal/create-sub-session", "create_sub_session uses endpoint", failures)
    payload = captured["payload"]
    check(payload["sender_session_id"] == "sender-1", "create_sub_session payload has sender", failures)
    check("prompt" not in payload, "create_sub_session payload has no prompt", failures)
    check(payload["description"] == "hidden reviewer", "create_sub_session payload has description", failures)
    check(payload["provider_id"] == "provider-1", "create_sub_session payload has provider", failures)
    check(payload["model"] == "model-1", "create_sub_session payload has explicit model", failures)
    check(payload["reasoning_effort"] == "high", "create_sub_session payload has effort", failures)


def main() -> int:
    failures: list[str] = []
    test_codex_provider_advertises_manager_mode(failures)
    test_ask_dynamic_tool_contract(failures)
    test_create_worker_dynamic_tool_contract(failures)
    test_create_session_dynamic_tool_contract(failures)
    test_create_sub_session_dynamic_tool_contract(failures)
    test_delegate_task_dynamic_tool_contract(failures)
    test_native_loopback_registers_mssg_tool(failures)
    test_dynamic_tool_json_result_is_compact(failures)
    test_subagent_notification_response_item_is_ingested(failures)
    test_regular_user_response_item_is_not_ingested(failures)
    asyncio.run(_exercise_ask_direct_handler(failures))
    asyncio.run(_exercise_ask_fork_handler(failures))
    asyncio.run(_exercise_create_worker_handler(failures))
    asyncio.run(_exercise_ensure_named_worker_handler(failures))
    asyncio.run(_exercise_delegate_task_handler(failures))
    asyncio.run(_exercise_ask_async_mode_handler(failures))
    asyncio.run(_exercise_create_session_handler(failures))
    asyncio.run(_exercise_create_sub_session_handler(failures))
    if failures:
        print(f"\n{len(failures)} failure(s)")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
