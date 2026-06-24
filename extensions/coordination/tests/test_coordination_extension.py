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
    calls: list[tuple[str, bool, str]] = []

    class FakeClient:
        def lock_ops(self, key, *, release=False, holder_token=""):
            calls.append((key, release, holder_token))
            return {"success": True, "holder_token": "tok"}

    module.Client = FakeClient

    assert module.lock_ops_response("file-a") == {"success": True, "holder_token": "tok"}
    assert calls == [("file-a", False, "")]


def test_lock_ops_validates_key_before_loopback() -> None:
    module = _load_mcp_module()

    class FakeClient:
        def lock_ops(self, key, *, release=False, holder_token=""):
            raise AssertionError("loopback should not be called")

    module.Client = FakeClient

    assert module.lock_ops_response("   ") == {"success": False, "error": "key_required"}


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
    assert "git rev-parse --show-toplevel" in content
    assert "holder_token" in content
    assert "when the `lock_ops` tool is available" in content
    assert "proceed with precise git operations" in content
