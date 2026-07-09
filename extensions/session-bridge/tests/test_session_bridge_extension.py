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


def test_manifest_keeps_session_bridge_user_facing_only() -> None:
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / "better-agent-extension.json").read_text(encoding="utf-8"))
    assert manifest["permissions"]["backend_routes"] is True
    assert "instructions" not in manifest["surfaces"]
    assert "instructions" not in manifest["entrypoints"]
    assert not (root / "instructions" / "git_ops_lock.md").exists()
    content = (root / "mcp" / "server.py").read_text(encoding="utf-8")
    assert "lock_ops" not in content
