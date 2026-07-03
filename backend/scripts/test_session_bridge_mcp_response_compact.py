from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import _test_home
_test_home.isolate("bc-test-session-bridge-mcp-")
os.environ["BETTER_CLAUDE_API_ONLY"] = "1"

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import extension_store  # noqa: E402
import config_store  # noqa: E402
import main  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def _enable_team_extension() -> None:
    provider = config_store.list_providers()["providers"][0]
    assignments = config_store.get_internal_llm_assignments()
    assignments["default_session"] = {
        "provider_id": provider["id"],
        "model": provider["default_model"],
        "reasoning_effort": provider.get("default_reasoning_effort") or "",
    }
    config_store.set_internal_llm_assignments(assignments)
    data = extension_store._load()  # type: ignore[attr-defined]
    data["extensions"][extension_store.BUILTIN_TEAM_ORCHESTRATION_EXTENSION_ID] = {
        "manifest": {"id": extension_store.BUILTIN_TEAM_ORCHESTRATION_EXTENSION_ID},
        "enabled": True,
        "source": {"type": "test", "install_path": ""},
        "entitlement": {"status": "not_required"},
    }
    extension_store._save(data)  # type: ignore[attr-defined]


def test_internal_session_bridge_search_omits_empty_fields() -> None:
    _enable_team_extension()
    provider = config_store.list_providers()["providers"][0]
    caller = session_manager.create(
        name="caller",
        cwd="/repo",
        orchestration_mode="native",
        provider_id=provider["id"],
    )
    captured: dict = {}

    async def fake_search(*args, **kwargs):
        captured["provider_id"] = kwargs.get("provider_id")
        return {"session_ids": [], "reasoning": "", "error": None}

    original_search = main.session_search.run_search_sessions_session
    original_stubs = main.session_search.index_stub_map
    main.session_search.run_search_sessions_session = fake_search
    main.session_search.index_stub_map = lambda: {}
    try:
        response = TestClient(main.app).post(
            "/api/internal/session-bridge/search",
            headers={"X-Internal-Token": main.coordinator.internal_token},
            json={"query": "needle", "app_session_id": caller["id"]},
        )
    finally:
        main.session_search.run_search_sessions_session = original_search
        main.session_search.index_stub_map = original_stubs

    assert response.status_code == 200
    assert response.json() == {"results": []}
    assert captured["provider_id"] == provider["id"]


def test_internal_session_bridge_search_any_disables_provider_filter() -> None:
    _enable_team_extension()
    provider = config_store.list_providers()["providers"][0]
    caller = session_manager.create(
        name="caller-any",
        cwd="/repo",
        orchestration_mode="native",
        provider_id=provider["id"],
    )
    captured: dict = {}

    async def fake_search(*args, **kwargs):
        captured["provider_id"] = kwargs.get("provider_id")
        return {"session_ids": [], "reasoning": "", "error": None}

    original_search = main.session_search.run_search_sessions_session
    original_stubs = main.session_search.index_stub_map
    main.session_search.run_search_sessions_session = fake_search
    main.session_search.index_stub_map = lambda: {}
    try:
        response = TestClient(main.app).post(
            "/api/internal/session-bridge/search",
            headers={"X-Internal-Token": main.coordinator.internal_token},
            json={
                "query": "needle",
                "app_session_id": caller["id"],
                "provider_id": "ANY",
            },
        )
    finally:
        main.session_search.run_search_sessions_session = original_search
        main.session_search.index_stub_map = original_stubs

    assert response.status_code == 200
    assert response.json() == {"results": []}
    assert captured["provider_id"] is None


def test_internal_delegate_task_auto_route_defaults_to_sender_provider() -> None:
    provider = config_store.list_providers()["providers"][0]
    sender = session_manager.create(
        name="delegate-sender",
        cwd="/repo",
        orchestration_mode="native",
        provider_id=provider["id"],
    )
    captured: dict = {}

    async def fake_delegate_task(**kwargs):
        captured.update(kwargs)
        return {"success": True, "target_session_id": "created"}

    original = main.coordinator.run_delegate_task
    main.coordinator.run_delegate_task = fake_delegate_task  # type: ignore[assignment]
    try:
        response = TestClient(main.app).post(
            "/api/internal/delegate-task",
            headers={"X-Internal-Token": main.coordinator.internal_token},
            json={"sender_session_id": sender["id"], "task": "find target"},
        )
    finally:
        main.coordinator.run_delegate_task = original  # type: ignore[assignment]

    assert response.status_code == 200
    assert response.json()["success"] is True
    assert captured["provider_id"] == provider["id"]
    assert captured["target_session_id"] is None


def test_internal_delegate_task_target_bypass_does_not_default_provider() -> None:
    provider = config_store.list_providers()["providers"][0]
    sender = session_manager.create(
        name="delegate-target-sender",
        cwd="/repo",
        orchestration_mode="native",
        provider_id=provider["id"],
    )
    captured: dict = {}

    async def fake_delegate_task(**kwargs):
        captured.update(kwargs)
        return {"success": True, "target_session_id": kwargs["target_session_id"]}

    original = main.coordinator.run_delegate_task
    main.coordinator.run_delegate_task = fake_delegate_task  # type: ignore[assignment]
    try:
        response = TestClient(main.app).post(
            "/api/internal/delegate-task",
            headers={"X-Internal-Token": main.coordinator.internal_token},
            json={
                "sender_session_id": sender["id"],
                "task": "direct target",
                "target_session_id": "target-1",
            },
        )
    finally:
        main.coordinator.run_delegate_task = original  # type: ignore[assignment]

    assert response.status_code == 200
    assert response.json()["success"] is True
    assert captured["provider_id"] == ""
    assert captured["target_session_id"] == "target-1"


if __name__ == "__main__":
    test_internal_session_bridge_search_omits_empty_fields()
    test_internal_session_bridge_search_any_disables_provider_filter()
    test_internal_delegate_task_auto_route_defaults_to_sender_provider()
    test_internal_delegate_task_target_bypass_does_not_default_provider()
    print("OK: session bridge MCP response compact")
