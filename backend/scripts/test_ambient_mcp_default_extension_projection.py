from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path


home = tempfile.mkdtemp(prefix="ba-ambient-default-extensions-")
os.environ["BETTER_AGENT_HOME"] = home
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ambient_mcp_sources
import extension_mcp
import extension_store


def _launcher(extension_id: str, server_name: str) -> dict:
    return extension_mcp.launcher_server_item(extension_id, server_name)


def main() -> None:
    original_configs = extension_store.eligible_native_mcp_launcher_server_configs
    original_configure = extension_mcp._configure_pcs
    had_reconcile = hasattr(extension_mcp._pcs, "reconcile_global_mcp_servers")
    original_reconcile = getattr(extension_mcp._pcs, "reconcile_global_mcp_servers", None)
    captured: dict[str, dict] = {}
    try:
        extension_store.eligible_native_mcp_launcher_server_configs = lambda *_args, **_kwargs: {
            "better-agent-session-bridge": _launcher(
                "ofek-dev.session-bridge", "better-agent-session-bridge"
            ),
            "ofek-dev-coordination": _launcher(
                "ofek-dev.coordination", "ofek-dev-coordination"
            ),
        }
        projected = {item.id: item for item in ambient_mcp_sources.capabilities()}
        session_id = "extension:ofek-dev.session-bridge:better-agent-session-bridge"
        coordination_id = "extension:ofek-dev.coordination:ofek-dev-coordination"
        assert projected[session_id].exposed is True
        assert projected[coordination_id].exposed is True

        extension_mcp._configure_pcs = lambda: None

        def reconcile(desired, *, owns_server):
            del owns_server
            captured.update(desired)
            return {"changed": list(desired)}

        extension_mcp._pcs.reconcile_global_mcp_servers = reconcile
        extension_mcp.reconcile_native_mcp_servers([])
        expected = {
            "better-agent-session-bridge": session_id,
            "ofek-dev-coordination": coordination_id,
        }
        for name, capability_id in expected.items():
            marker = captured[name]["env"]["BETTER_AGENT_AMBIENT_MCP_CAPABILITY_ID"]
            assert marker == capability_id
        print("PASS default ambient extension projection")
    finally:
        extension_store.eligible_native_mcp_launcher_server_configs = original_configs
        extension_mcp._configure_pcs = original_configure
        if had_reconcile:
            extension_mcp._pcs.reconcile_global_mcp_servers = original_reconcile
        else:
            delattr(extension_mcp._pcs, "reconcile_global_mcp_servers")
        shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    main()
