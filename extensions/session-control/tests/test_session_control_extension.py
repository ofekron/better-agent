from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_mcp_module():
    public_root = Path(__file__).resolve().parents[3]
    sys.path.insert(0, str(public_root / "sdk"))
    path = Path(__file__).resolve().parents[1] / "mcp" / "server.py"
    spec = importlib.util.spec_from_file_location("session_control_extension_mcp", path)
    if spec is None or spec.loader is None:
        raise AssertionError("could not load Session Control MCP server")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ambient_control_requires_explicit_target_and_forwards_valid_target() -> None:
    module = _load_mcp_module()
    calls: list[dict] = []

    class FakeClient:
        app_session_id = ""

        def invoke_capability(self, capability, action, payload=None, *, timeout=60.0):
            calls.append(dict(payload or {}))
            return {"success": True}

    module.Client = FakeClient
    missing = module.switch_model_response(model="model-a")
    assert missing == {"success": False, "error": "app_session_id is required for ambient use"}
    assert module.switch_model_response(model="model-a", app_session_id="target-a") == {
        "success": True
    }
    assert calls[-1]["app_session_id"] == "target-a"


def test_runtime_control_preserves_bound_target_and_denies_forgery() -> None:
    module = _load_mcp_module()
    calls: list[dict] = []

    class FakeClient:
        app_session_id = "bound-a"

        def invoke_capability(self, capability, action, payload=None, *, timeout=60.0):
            calls.append(dict(payload or {}))
            return {"success": True}

    module.Client = FakeClient
    assert module.switch_model_response(model="model-a") == {"success": True}
    assert calls[-1]["app_session_id"] == "bound-a"
    assert module.switch_model_response(model="model-a", app_session_id="forged-b") == {
        "success": False,
        "error": "target session does not match the bound Better Agent session",
    }
