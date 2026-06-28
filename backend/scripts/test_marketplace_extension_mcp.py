#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

TMP_HOME = Path(tempfile.mkdtemp(prefix="bc-test-marketplace-extension-"))
import _test_home
_test_home.isolate("ba-test-")
os.environ["BETTER_AGENT_SKIP_EXTENSION_DEPENDENCY_INSTALL"] = "1"
os.environ["BETTER_CLAUDE_TEST_AUTH_BYPASS"] = "1"

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent / "sdk"))

dist_dir = ROOT.parent / "frontend" / "dist"
created_dist = not dist_dir.exists()
if created_dist:
    dist_dir.mkdir(parents=True, exist_ok=True)
    (dist_dir / "index.html").write_text("<!doctype html><title>stub</title>", encoding="utf-8")

import builtin_mcp_config  # noqa: E402
import extension_store  # noqa: E402


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"PASS {message}")


def test_marketplace_extension_is_seeded_and_exposed_as_runtime_mcp() -> None:
    # Reconcile required extensions like app startup does so the marketplace
    # package is seeded before we assert on it.
    extension_store.list_extensions_with_reconciliation(include_hidden=True)
    extensions = extension_store.list_extensions()
    check(
        extension_store.MARKETPLACE_EXTENSION_ID in {item["manifest"]["id"] for item in extensions},
        "marketplace is listed in the public extension list",
    )
    hidden_extensions = extension_store.list_extensions(include_hidden=True)
    check(
        extension_store.MARKETPLACE_EXTENSION_ID in {item["manifest"]["id"] for item in hidden_extensions},
        "marketplace is available to settings extension list",
    )
    record = extension_store.get_extension(extension_store.MARKETPLACE_EXTENSION_ID)
    check(record is not None, "marketplace extension record is installed")
    check(record["enabled"] is True, "marketplace extension is enabled")
    check(record["source"]["type"] == "better_agent_local", "marketplace extension seeds from local package")

    configs = builtin_mcp_config.with_builtin_mcp_servers(
        {
            "app_session_id": "session-1",
            "backend_url": "http://localhost:8000",
            "internal_token": "token",
            "open_file_panel_enabled": True,
        },
        {},
    )["mcp_servers"]
    check("ofek-dev-marketplace" in configs, "marketplace MCP is exposed to user-facing runs")
    config = configs["ofek-dev-marketplace"]
    check(config["command"] == sys.executable, "marketplace MCP runs through python")
    check(
        config["env"].get("BETTER_AGENT_EXTENSION_ID") == extension_store.MARKETPLACE_EXTENSION_ID,
        "marketplace MCP receives its extension id",
    )
    # Identity is token-derived: the marketplace MCP receives its OWN minted
    # per-extension token (never the global token), so the backend derives its
    # identity from the secret instead of a spoofable header.
    import extension_token_registry

    minted = extension_token_registry.mint(extension_store.MARKETPLACE_EXTENSION_ID)
    check(
        config["env"].get("BETTER_AGENT_INTERNAL_TOKEN") == minted,
        "marketplace MCP receives its per-extension internal token",
    )
    check(
        config["env"].get("BETTER_AGENT_INTERNAL_TOKEN") != "token",
        "marketplace MCP does not receive the global internal token",
    )


def test_marketplace_mcp_wrapper_calls_internal_marketplace_endpoint() -> None:
    server_path = ROOT.parent / "extensions" / "marketplace" / "mcp" / "server.py"
    spec = importlib.util.spec_from_file_location("marketplace_mcp_server", server_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)

    calls: list[tuple[str, dict, float]] = []

    class FakeClient:
        def call_internal(self, path, body=None, *, timeout=60.0):
            calls.append((path, body or {}, timeout))
            return {"success": True, "body": body}

    module.Client = FakeClient
    result = module.marketplace_action("search", query="todos", limit=5)
    check(result["success"] is True, "marketplace MCP wrapper returns internal result")
    check(calls == [("/api/internal/marketplace", {"action": "search", "query": "todos", "limit": 5}, 60.0)], "marketplace MCP wrapper uses internal endpoint")


def test_internal_marketplace_endpoint_requires_marketplace_extension_and_valid_actions() -> None:
    from fastapi.testclient import TestClient
    import main
    import extension_token_registry

    client = TestClient(main.app)
    # Identity is token-derived: act as an extension by sending ITS minted token.
    other_token = extension_token_registry.mint("ofek-dev.coordination")
    marketplace_token = extension_token_registry.mint(extension_store.MARKETPLACE_EXTENSION_ID)

    response = client.post(
        "/api/internal/marketplace",
        headers={"X-Internal-Token": other_token},
        json={"action": "list_installed"},
    )
    check(response.status_code == 403, "marketplace endpoint rejects other extensions")

    response = client.post(
        "/api/internal/marketplace",
        headers={"X-Internal-Token": marketplace_token},
        json={"action": "list_installed"},
    )
    check(response.status_code == 200, "marketplace endpoint lists installed extensions")
    check(isinstance(response.json().get("extensions"), list), "marketplace list returns extensions")

    response = client.post(
        "/api/internal/marketplace",
        headers={"X-Internal-Token": marketplace_token},
        json={"action": "set_enabled", "extension_id": "ofek-dev.ask", "enabled": "yes"},
    )
    check(response.status_code == 400, "marketplace set_enabled requires boolean")

    response = client.post(
        "/api/internal/marketplace",
        headers={"X-Internal-Token": marketplace_token},
        json={"action": "unknown"},
    )
    check(response.status_code == 400, "marketplace endpoint rejects unknown actions")


def main() -> int:
    try:
        test_marketplace_extension_is_seeded_and_exposed_as_runtime_mcp()
        test_marketplace_mcp_wrapper_calls_internal_marketplace_endpoint()
        test_internal_marketplace_endpoint_requires_marketplace_extension_and_valid_actions()
    finally:
        if created_dist:
            index = dist_dir / "index.html"
            if index.exists() and index.read_text(encoding="utf-8") == "<!doctype html><title>stub</title>":
                index.unlink()
            try:
                dist_dir.rmdir()
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
