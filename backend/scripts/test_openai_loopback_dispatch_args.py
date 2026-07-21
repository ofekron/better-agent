"""Regression: Better Agent runner must forward tool arguments to loopback handlers.

`_dispatch_tool` decodes the provider's JSON arguments into a bare dict, but
every loopback handler unwraps its input via `_args(params)` (i.e. it reads
`params["arguments"]`). If the dispatcher hands the handler the bare args dict
instead of `{"arguments": args}`, the handler sees empty args and rejects every
valid call ("one target and message are required", "name is required",
...). That regression forced the model to fall back from
ask/mssg/create_sub_session onto create_session — and that, too, failed — so adv
review ended up spawning standalone/provisioned sessions.

This test runs the real `_dispatch_tool` against a fake loopback handler and
asserts the handler receives the decoded arguments wrapped the way `_args`
expects.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

import _test_home
_test_home.isolate("bc-test-openai-loopback-")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import runner_better_agent
import extension_store


def _make_emitter() -> runner_better_agent.EventEmitter:
    tmp = tempfile.NamedTemporaryFile(
        prefix="events-", suffix=".jsonl", delete=False
    )
    tmp.close()
    return runner_better_agent.EventEmitter(Path(tmp.name))


def test_dispatch_forwards_args_to_loopback_handler() -> None:
    seen: list[dict] = []

    async def fake_ask(params: dict) -> str:
        # Mirror the real handler contract: unwrap via _args.
        args = runner_better_agent._args(params)
        seen.append(args)
        return runner_better_agent._dynamic_tool_json_result(
            {"success": True}, success=True
        )

    emitter = _make_emitter()
    call = {
        "id": "call_1",
        "name": "ask",
        "arguments": json.dumps(
            {"target_session_id": "w1", "message": "review please"}
        ),
    }

    result = asyncio.run(
        runner_better_agent._dispatch_tool(
            call,
            Path("/tmp"),
            "app-1",
            Path("/tmp"),
            True,  # bypass
            True,  # interactive
            "http://backend",
            "tok",
            emitter,
            {"ask": fake_ask},
            runner_better_agent.LockRegistry(),
            False,
        )
    )

    assert seen, "loopback handler was never invoked"
    assert seen[0].get("target_session_id") == "w1", seen[0]
    assert seen[0].get("message") == "review please", seen[0]
    assert json.loads(result).get("success") is True


def test_real_create_sub_session_preserves_dispatched_args() -> None:
    """create_sub_session has no required user args, so the broken dispatcher
    did not fail loudly; it silently created a default unnamed/default-provider
    sub-session. Lock that optional args survive dispatch too."""
    captured: list[tuple[str, dict]] = []
    original_post = runner_better_agent._post_loopback_sync

    def fake_post(payload: dict, *, backend_url: str, internal_token: str, **kwargs) -> dict:
        captured.append((kwargs["url_path"], payload))
        return {"success": True, "target_session_id": "sub-1"}

    runner_better_agent._post_loopback_sync = fake_post  # type: ignore[assignment]
    try:
        handlers = runner_better_agent._build_loopback_tool_handlers(
            {
                "backend_url": "http://backend",
                "internal_token": "tok",
                "app_session_id": "sender-1",
            },
            cwd="/tmp/project",
            model="model-x",
            lock_registry=runner_better_agent.LockRegistry(),
        )
        emitter = _make_emitter()
        call = {
            "id": "call_sub",
            "name": "create_sub_session",
            "arguments": json.dumps(
                {
                    "description": "Adversarial reviewer",
                    "provider_id": "provider-1",
                    "model": "model-1",
                    "reasoning_effort": "high",
                    "node_id": "node-1",
                }
            ),
        }
        result = asyncio.run(
            runner_better_agent._dispatch_tool(
                call,
                Path("/tmp"),
                "sender-1",
                Path("/tmp"),
                True,
                True,
                "http://backend",
                "tok",
                emitter,
                handlers,
                runner_better_agent.LockRegistry(),
                False,
            )
        )
    finally:
        runner_better_agent._post_loopback_sync = original_post  # type: ignore[assignment]

    assert json.loads(result).get("success") is True
    assert captured, "create_sub_session handler never posted to backend"
    endpoint, payload = captured[0]
    assert endpoint == "/api/internal/create-sub-session"
    assert payload["description"] == "Adversarial reviewer"
    assert payload["provider_id"] == "provider-1"
    assert payload["model"] == "model-1"
    assert payload["reasoning_effort"] == "high"
    assert payload["node_id"] == "node-1"



def test_real_ask_handler_accepts_dispatched_args() -> None:
    """End-to-end through the real ask handler: a valid call must NOT be
    rejected with the 'required' error that triggered the fallback cascade."""
    captured: list[tuple[str, dict]] = []
    original_post = runner_better_agent._post_loopback_sync

    def fake_post(payload: dict, *, backend_url: str, internal_token: str, **kwargs) -> dict:
        captured.append((kwargs["url_path"], payload))
        return {"success": True}

    runner_better_agent._post_loopback_sync = fake_post  # type: ignore[assignment]
    try:
        handlers = runner_better_agent._build_loopback_tool_handlers(
            {
                "backend_url": "http://backend",
                "internal_token": "tok",
                "app_session_id": "sender-1",
            },
            cwd="/tmp/project",
            model="model-x",
            lock_registry=runner_better_agent.LockRegistry(),
        )
        emitter = _make_emitter()
        call = {
            "id": "call_2",
            "name": "ask",
            "arguments": json.dumps(
                {"target_session_id": "w1", "message": "review"}
            ),
        }
        result = asyncio.run(
            runner_better_agent._dispatch_tool(
                call,
                Path("/tmp"),
                "sender-1",
                Path("/tmp"),
                True,
                True,
                "http://backend",
                "tok",
                emitter,
                handlers,
                runner_better_agent.LockRegistry(),
                False,
            )
        )
    finally:
        runner_better_agent._post_loopback_sync = original_post  # type: ignore[assignment]

    assert "required" not in result, result
    assert captured, "ask handler never posted to backend"
    assert captured[0][0] == "/api/internal/ask"
    assert captured[0][1]["target_session_id"] == "w1"
    assert captured[0][1]["message"] == "review"


def test_real_ask_async_mode_accepts_dispatched_args() -> None:
    captured: list[tuple[str, dict]] = []
    original_post = runner_better_agent._post_loopback_sync

    def fake_post(payload: dict, *, backend_url: str, internal_token: str, **kwargs) -> dict:
        captured.append((kwargs["url_path"], payload))
        return {"success": True, "queued_id": "queued-1", "expects_response": True}

    runner_better_agent._post_loopback_sync = fake_post  # type: ignore[assignment]
    try:
        handlers = runner_better_agent._build_loopback_tool_handlers(
            {
                "backend_url": "http://backend",
                "internal_token": "tok",
                "app_session_id": "sender-1",
            },
            cwd="/tmp/project",
            model="model-x",
            lock_registry=runner_better_agent.LockRegistry(),
        )
        emitter = _make_emitter()
        call = {
            "id": "call_ask_async",
            "name": "ask",
            "arguments": json.dumps(
                {
                    "target_worker_pool": "testape",
                    "pool_affinity_key": "thread-1",
                    "message": "run async",
                    "mode": "continue_and_expect_inbox_back_async",
                }
            ),
        }
        result = asyncio.run(
            runner_better_agent._dispatch_tool(
                call,
                Path("/tmp"),
                "sender-1",
                Path("/tmp"),
                True,
                True,
                "http://backend",
                "tok",
                emitter,
                handlers,
                runner_better_agent.LockRegistry(),
                False,
            )
        )
    finally:
        runner_better_agent._post_loopback_sync = original_post  # type: ignore[assignment]

    parsed = json.loads(result)
    assert parsed["expects_response"] is True
    assert captured, "ask async mode handler never posted to backend"
    assert captured[0][0] == "/api/internal/ask"
    assert captured[0][1]["sender_session_id"] == "sender-1"
    assert captured[0][1]["target_worker_pool"] == "testape"
    assert captured[0][1]["pool_affinity_key"] == "thread-1"
    assert captured[0][1]["message"] == "run async"
    assert captured[0][1]["mode"] == "continue_and_expect_inbox_back_async"


def test_real_ensure_named_worker_handler_accepts_dispatched_args() -> None:
    captured: list[tuple[str, dict]] = []
    original_post = runner_better_agent._post_loopback_sync
    original_ready = extension_store.is_extension_runtime_ready

    def fake_post(payload: dict, *, backend_url: str, internal_token: str, **kwargs) -> dict:
        captured.append((kwargs["url_path"], payload))
        return {
            "workers": [
                {
                    "agent_session_id": "worker-1",
                    "name": "worker:testape",
                    "created": True,
                    "orchestration_mode": "team",
                    "registry_cwd": "/repo",
                }
            ]
        }

    runner_better_agent._post_loopback_sync = fake_post  # type: ignore[assignment]
    extension_store.is_extension_runtime_ready = lambda _extension_id: True  # type: ignore[assignment]
    try:
        handlers = runner_better_agent._build_loopback_tool_handlers(
            {
                "backend_url": "http://backend",
                "internal_token": "tok",
                "app_session_id": "sender-1",
            },
            cwd="/repo",
            model="model-x",
            lock_registry=runner_better_agent.LockRegistry(),
        )
        assert "ensure_named_worker" in handlers
        emitter = _make_emitter()
        call = {
            "id": "call_named_worker",
            "name": "ensure_named_worker",
            "arguments": json.dumps(
                {
                    "name": "testape",
                    "orchestration_mode": "team",
                    "provision_prompt": "seed",
                }
            ),
        }
        result = asyncio.run(
            runner_better_agent._dispatch_tool(
                call,
                Path("/tmp"),
                "sender-1",
                Path("/tmp"),
                True,
                True,
                "http://backend",
                "tok",
                emitter,
                handlers,
                runner_better_agent.LockRegistry(),
                False,
            )
        )
    finally:
        runner_better_agent._post_loopback_sync = original_post  # type: ignore[assignment]
        extension_store.is_extension_runtime_ready = original_ready  # type: ignore[assignment]

    parsed = json.loads(result)
    assert parsed["agent_session_id"] == "worker-1"
    assert captured, "ensure_named_worker handler never posted to backend"
    endpoint, payload = captured[0]
    assert endpoint == "/api/internal/workers/provision"
    assert payload["cwd"] == "/repo"
    spec = payload["workers"][0]
    assert spec["role_key"] == "testape"
    assert spec["orchestration_mode"] == "team"
    assert spec["provision_prompt"] == "seed"
    assert spec["tags"] == ["testape"]


def test_ensure_named_worker_schema_requires_team_orchestration() -> None:
    base = {
        "backend_url": "http://backend",
        "internal_token": "tok",
        "app_session_id": "sender-1",
    }

    without_team = runner_better_agent._tool_schemas_for_run(
        inputs=base,
        capabilities_enabled=False,
        loopback_enabled=True,
        team_manager_enabled=False,
        team_orchestration_enabled=False,
        open_file_panel_enabled=False,
        file_editing_mode=False,
        coordination_enabled=False,
    )
    assert all(
        schema.get("function", {}).get("name") != "ensure_named_worker"
        for schema in without_team
    )

    with_team = runner_better_agent._tool_schemas_for_run(
        inputs=base,
        capabilities_enabled=False,
        loopback_enabled=True,
        team_manager_enabled=False,
        team_orchestration_enabled=True,
        open_file_panel_enabled=False,
        file_editing_mode=False,
        coordination_enabled=False,
    )
    tool = next(
        schema for schema in with_team
        if schema.get("function", {}).get("name") == "ensure_named_worker"
    )
    assert tool["function"]["parameters"]["required"] == ["name", "orchestration_mode"]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
