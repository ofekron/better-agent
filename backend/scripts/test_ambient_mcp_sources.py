from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path


home = tempfile.mkdtemp(prefix="ba-ambient-sources-")
os.environ["BETTER_AGENT_HOME"] = home
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ambient_mcp_sources
import ambient_user_mcp_store


def main() -> None:
    try:
        stored = ambient_user_mcp_store.put({
            "id": "notes",
            "name": "notes",
            "launcher": {"command": "notes-mcp", "args": ["--stdio"], "env": {}},
            "policy": {"native_exposure": True},
        })
        assert stored["id"] == "notes"
        projected = {item.id: item for item in ambient_mcp_sources.capabilities()}
        assert projected["user:notes"].launcher["command"] == "notes-mcp"
        assert projected["user:notes"].ownership == "user"
        assert projected["core:capabilities"].available is True
        assert projected["core:capabilities"].launcher["args"][-1] == "capabilities"
        assert projected["core:capabilities"].policy["permissions"] == [
            "capabilities.read", "capabilities.write"
        ]

        ambient_mcp_sources.register_adapter(
            "future-test",
            lambda: [ambient_mcp_sources.AmbientMcpCapability(
                id="future:one",
                name="future-one",
                launcher={"command": "future", "args": [], "env": {}},
                policy={},
                ownership="better-agent-core",
                available=True,
            )],
        )
        assert any(item.id == "future:one" for item in ambient_mcp_sources.capabilities())
        assert ambient_user_mcp_store.remove("notes") is True
        assert ambient_user_mcp_store.list_records() == []
        print("PASS ambient MCP canonical sources")
    finally:
        shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    main()
