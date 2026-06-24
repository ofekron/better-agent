#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import _test_home
_test_home.isolate("bc-test-provider-config-sync-extension-")
os.environ["BETTER_AGENT_SKIP_EXTENSION_DEPENDENCY_INSTALL"] = "1"

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import config_store  # noqa: E402
import extension_store  # noqa: E402


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"PASS {message}")


def configure_review_model() -> None:
    provider = config_store.list_providers()["providers"][0]
    assignments = config_store.get_internal_llm_assignments()
    assignments["provider_config_sync_review"] = {
        "provider_id": provider["id"],
        "model": provider["default_model"],
        "reasoning_effort": provider.get("default_reasoning_effort") or "",
    }
    config_store.set_internal_llm_assignments(assignments)


def main() -> int:
    configure_review_model()
    # Seed bundled public extensions from the repo before reading. Reads via
    # get_extension() are pure; list_extensions_with_reconciliation is the seed path.
    extension_store.list_extensions_with_reconciliation(include_hidden=True)
    record = extension_store.get_extension(extension_store.BUILTIN_PROVIDER_CONFIG_SYNC_EXTENSION_ID)
    check(record is not None, "provider-config-sync extension is installed")
    assert record is not None
    manifest = record["manifest"]
    check(record["source"]["type"] == "better_agent_bundled", "provider-config-sync is a public builtin")
    check(manifest["entrypoints"]["backend"] == "backend/routes.py", "backend route is declared")
    check(manifest["entrypoints"]["page"]["open"]["path"] == "/provider-config-sync", "page hook opens PCS UI")
    check(extension_store.has_permission(record, "backend_routes"), "backend_routes permission is active")
    check(extension_store.has_permission(record, "provider_config"), "provider_config permission is active")

    hooks = extension_store.ui_hooks()
    pages = hooks["pages"]
    check(
        any(page["extension_id"] == manifest["id"] and page["label"] == "Provider Config Sync" for page in pages),
        "provider-config-sync page hook is exposed",
    )

    configs = extension_store.runtime_mcp_server_configs(
        {
            "backend_url": "http://127.0.0.1:8000",
            "internal_token": "token",
            "app_session_id": "session-1",
            "cwd": str(Path.cwd()),
            "model": "model",
        },
        user_facing=True,
        bare=False,
    )
    check(configs.get("provider-config-sync") is None, "native harness mode does not inject PCS per-turn")
    extension_store.set_harness_delivery_mode(manifest["id"], "runtime")
    configs = extension_store.runtime_mcp_server_configs(
        {
            "backend_url": "http://127.0.0.1:8000",
            "internal_token": "token",
            "app_session_id": "session-1",
            "cwd": str(Path.cwd()),
            "model": "model",
        },
        user_facing=True,
        bare=False,
    )
    server = configs.get("provider-config-sync")
    check(server is not None, "provider-config-sync MCP replaces the reserved server")
    assert server is not None
    env = server["env"]
    check(env.get("PROVIDER_CONFIG_SYNC_CONFIG", "").endswith("better-agent-config.json"), "MCP env includes config path")
    check(env.get("PROVIDER_CONFIG_SYNC_PACKAGE_SRC", ""), "MCP env includes package source")
    check(env.get("BETTER_CLAUDE_EXTENSION_ID") == manifest["id"], "MCP env includes extension id")
    wrapper = Path(server["args"][0])
    check(wrapper.name == "server.py" and wrapper.parent.name == "mcp", "MCP wrapper script is used")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
