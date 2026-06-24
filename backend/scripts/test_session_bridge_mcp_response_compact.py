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

    async def fake_search(*args, **kwargs):
        return {"session_ids": [], "reasoning": "", "error": None}

    original_search = main.session_search.run_search_sessions_session
    original_stubs = main.session_search.index_stub_map
    main.session_search.run_search_sessions_session = fake_search
    main.session_search.index_stub_map = lambda: {}
    try:
        response = TestClient(main.app).post(
            "/api/internal/session-bridge/search",
            headers={"X-Internal-Token": main.coordinator.internal_token},
            json={"query": "needle"},
        )
    finally:
        main.session_search.run_search_sessions_session = original_search
        main.session_search.index_stub_map = original_stubs

    assert response.status_code == 200
    assert response.json() == {"results": []}


if __name__ == "__main__":
    test_internal_session_bridge_search_omits_empty_fields()
    print("OK: session bridge MCP response compact")
