from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _load_routes_module():
    public_root = Path(__file__).resolve().parents[3]
    sys.path.insert(0, str(public_root / "sdk"))
    routes_path = Path(__file__).resolve().parents[1] / "backend" / "routes.py"
    spec = importlib.util.spec_from_file_location("session_bridge_extension_routes", routes_path)
    if spec is None or spec.loader is None:
        raise AssertionError("could not load Session Bridge routes")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_mcp_module():
    public_root = Path(__file__).resolve().parents[3]
    sys.path.insert(0, str(public_root / "sdk"))
    mcp_path = Path(__file__).resolve().parents[1] / "mcp" / "server.py"
    spec = importlib.util.spec_from_file_location("session_bridge_extension_mcp", mcp_path)
    if spec is None or spec.loader is None:
        raise AssertionError("could not load Session Bridge MCP server")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_resolve_delegation_proxies_to_internal_substrate() -> None:
    module = _load_routes_module()
    calls: list[tuple[str, dict]] = []

    class FakeClient:
        def invoke_capability(self, capability, action, payload=None, *, timeout=60.0):
            calls.append((f"{capability}.{action}", dict(payload or {})))
            return {"success": True}

    module.Client = FakeClient
    app = FastAPI()
    app.include_router(module.create_router(None))
    client = TestClient(app)

    response = client.post("/delegate/d1/resolve", json={"chosen_session_id": "s1"})
    assert response.status_code == 200
    assert response.json() == {"success": True}
    assert calls == [
        (
            "session-bridge.delegation.resolve",
            {"chosen_session_id": "s1", "delegation_id": "d1"},
        )
    ]


def test_resolve_delegation_preserves_loopback_status() -> None:
    module = _load_routes_module()

    class FakeClient:
        def invoke_capability(self, capability, action, payload=None, *, timeout=60.0):
            return {"success": False, "status": 409, "error": "already resolved"}

    module.Client = FakeClient
    app = FastAPI()
    app.include_router(module.create_router(None))
    client = TestClient(app)

    response = client.post("/delegate/d1/resolve", json={})
    assert response.status_code == 409
    assert response.json()["detail"] == "already resolved"


def test_search_sessions_empty_query_is_compact() -> None:
    module = _load_mcp_module()
    assert module.search_sessions_response("   ") == {"results": [], "error": "empty_query"}


def test_search_sessions_transport_error_is_compact() -> None:
    module = _load_mcp_module()

    class FakeClient:
        app_session_id = "caller"

        def invoke_capability(self, capability, action, payload=None, *, timeout=60.0):
            raise RuntimeError("offline")

    module.Client = FakeClient
    assert module.search_sessions_response("needle") == {"results": [], "error": "offline"}


def test_search_sessions_polls_when_initial_fire_transport_fails() -> None:
    module = _load_mcp_module()
    calls: list[tuple[str, str, dict]] = []

    class FakeClient:
        app_session_id = "caller"

        def invoke_capability(self, capability, action, payload=None, *, timeout=60.0):
            payload = dict(payload or {})
            calls.append((capability, action, payload))
            if action == "sessions.search":
                raise RuntimeError("accepted request timed out")
            assert action == "mcp-jobs.results"
            return {
                "success": True,
                "ready": True,
                "result": {"results": [{"id": "s1"}]},
            }

    module.Client = FakeClient
    assert module.search_sessions_response("needle") == {"results": [{"id": "s1"}]}
    assert calls[0][1] == "sessions.search"
    assert calls[1][0:2] == ("core", "mcp-jobs.results")
    assert calls[1][2]["operation"] == "session-bridge-search"


def test_search_sessions_success_omits_empty_fields() -> None:
    module = _load_mcp_module()

    class FakeClient:
        app_session_id = "caller"

        def invoke_capability(self, capability, action, payload=None, *, timeout=60.0):
            return {"results": [{"id": "s1"}], "reasoning": "", "error": None}

    module.Client = FakeClient
    assert module.search_sessions_response("needle") == {"results": [{"id": "s1"}]}


def test_search_sessions_success_keeps_nonempty_fields() -> None:
    module = _load_mcp_module()

    class FakeClient:
        app_session_id = "caller"

        def invoke_capability(self, capability, action, payload=None, *, timeout=60.0):
            return {"results": [], "reasoning": "matched", "error": "timeout"}

    module.Client = FakeClient
    assert module.search_sessions_response("needle") == {
        "results": [],
        "reasoning": "matched",
        "error": "timeout",
    }


def test_ambient_search_requires_explicit_target_and_forwards_valid_target() -> None:
    module = _load_mcp_module()
    calls: list[dict] = []

    class FakeClient:
        app_session_id = ""

        def invoke_capability(self, capability, action, payload=None, *, timeout=60.0):
            calls.append(dict(payload or {}))
            return {"results": []}

    module.Client = FakeClient
    assert "app_session_id is required" in module.search_sessions_response("needle")["error"]
    assert module.search_sessions_response("needle", app_session_id="target-a") == {"results": []}
    assert calls[-1]["app_session_id"] == "target-a"


def test_runtime_target_parity_and_cross_session_target_denial() -> None:
    module = _load_mcp_module()
    calls: list[dict] = []

    class FakeClient:
        app_session_id = "bound-a"

        def invoke_capability(self, capability, action, payload=None, *, timeout=60.0):
            calls.append(dict(payload or {}))
            return {"results": []}

    module.Client = FakeClient
    assert module.search_sessions_response("needle") == {"results": []}
    assert calls[-1]["app_session_id"] == "bound-a"
    result = module.search_sessions_response("needle", app_session_id="forged-b")
    assert "does not match" in result["error"]


def test_manifest_keeps_session_bridge_user_facing_only() -> None:
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / "better-agent-extension.json").read_text(encoding="utf-8"))
    assert manifest["permissions"]["backend_routes"] is True
    assert "instructions" not in manifest["surfaces"]
    assert "instructions" not in manifest["entrypoints"]
    assert not (root / "instructions" / "git_ops_lock.md").exists()
    content = (root / "mcp" / "server.py").read_text(encoding="utf-8")
    assert "lock_ops" not in content
