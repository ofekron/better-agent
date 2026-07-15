from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


def _load_mcp_module():
    public_root = Path(__file__).resolve().parents[3]
    sys.path.insert(0, str(public_root / "sdk"))
    mcp_path = Path(__file__).resolve().parents[1] / "mcp" / "server.py"
    spec = importlib.util.spec_from_file_location("coordination_extension_mcp", mcp_path)
    if spec is None or spec.loader is None:
        raise AssertionError("could not load Coordination MCP server")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_lock_ops_proxies_to_internal_substrate() -> None:
    module = _load_mcp_module()
    calls: list[dict] = []

    class FakeClient:
        def lock_ops(self, key, **kwargs):
            calls.append({"key": key, **kwargs})
            return {"success": True, "holder_token": "tok"}

    module.Client = FakeClient

    assert module.lock_ops_response("file-a") == {"success": True, "holder_token": "tok"}
    assert calls == [{
        "key": "file-a", "keys": None, "op": "", "release": False, "renew": False,
        "validate": False, "reattach": False, "owned": False, "holder_token": "",
        "timeout_seconds": None, "lease_seconds": None,
    }]


def test_lock_ops_proxies_multi_key_args() -> None:
    module = _load_mcp_module()
    calls: list[dict] = []

    class FakeClient:
        def lock_ops(self, key, **kwargs):
            calls.append({"key": key, **kwargs})
            return {"success": True, "keys": kwargs.get("keys")}

    module.Client = FakeClient

    assert module.lock_ops_response("", keys=[" a ", "b"], timeout_seconds=3, lease_seconds=30, op="renew") == {"success": True, "keys": ["a", "b"]}
    assert calls == [{
        "key": "", "keys": ["a", "b"], "op": "renew", "release": False, "renew": False,
        "validate": False, "reattach": False, "owned": False, "holder_token": "",
        "timeout_seconds": 3, "lease_seconds": 30,
    }]


def test_lock_ops_allows_hyphenated_owned_ops_without_key() -> None:
    module = _load_mcp_module()
    calls: list[dict] = []

    class FakeClient:
        def lock_ops(self, key, **kwargs):
            calls.append({"key": key, **kwargs})
            return {"success": True, "keys": []}

    module.Client = FakeClient

    assert module.lock_ops_response("", op="list-owned") == {"success": True, "keys": []}
    assert calls[0]["op"] == "list_owned"


def test_lock_ops_validates_key_before_loopback() -> None:
    module = _load_mcp_module()

    class FakeClient:
        def lock_ops(self, key, **kwargs):
            raise AssertionError("loopback should not be called")

    module.Client = FakeClient

    assert module.lock_ops_response("   ") == {"success": False, "error": "key_required"}


def test_lock_ops_surfaces_clear_error_for_malformed_backend_url(monkeypatch) -> None:
    """A corrupted BETTER_*_BACKEND_URL (multi-line port leaked into the env)
    must fail closed with an actionable message, not urllib's opaque
    "nonnumeric port" raised mid-request."""
    module = _load_mcp_module()
    dirty = "http://127.0.0.1:Stopping previous Better Agent BFF process(es): 58127\n18765"
    for name in ("BETTER_AGENT_BACKEND_URL", "BETTER_CLAUDE_BACKEND_URL"):
        monkeypatch.setenv(name, dirty)
    monkeypatch.setenv("BETTER_AGENT_INTERNAL_TOKEN", "test-token")

    result = module.lock_ops_response("file_edit:/tmp/example")

    assert result["success"] is False
    assert "invalid backend URL" in result["error"]
    assert "nonnumeric port" not in result["error"]


def test_manifest_declares_git_ops_lock_instruction() -> None:
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / "better-agent-extension.json").read_text(encoding="utf-8"))
    instructions = manifest["entrypoints"]["instructions"]
    assert "instructions" in manifest["surfaces"]
    assert {
        "name": "coordination-git-ops-lock",
        "path": "instructions/git_ops_lock.md",
        "level": "global",
    } in instructions
    content = (root / "instructions" / "git_ops_lock.md").read_text(encoding="utf-8")
    assert 'key="git_ops:<absolute-repo-root>"' in content
    assert 'keys=["file_edit:<absolute-path>", ...]' in content
    assert "Do not include the repo-scoped" in content
    assert "git rev-parse --show-toplevel" in content
    assert "holder_token" in content
    assert "waited_keys" in content
    assert "proceed with precise git operations" in content
