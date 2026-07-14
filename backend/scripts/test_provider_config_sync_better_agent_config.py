from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path


def check(condition: bool, message: str, failures: list[str]) -> None:
    print(f"  {'PASS' if condition else 'FAIL'} {message}")
    if not condition:
        failures.append(message)


def main() -> int:
    failures: list[str] = []
    wipe = Path(tempfile.mkdtemp(prefix="bc-provider-config-sync-config-"))
    old_home = os.environ.get("BETTER_CLAUDE_HOME")
    old_agent_home = os.environ.get("BETTER_AGENT_HOME")
    old_sync_config = os.environ.get("PROVIDER_CONFIG_SYNC_CONFIG")
    try:
        os.environ["BETTER_CLAUDE_HOME"] = str(wipe / "bc-home")
        os.environ["BETTER_AGENT_HOME"] = str(wipe / "bc-home")
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

        import project_store
        import runtime_project_catalog
        import config_store
        import provider_config_sync_api
        from paths import ba_home

        local_project = wipe / "local-project"
        local_project.mkdir()
        remote_project = wipe / "remote-project"
        projects_path = ba_home() / "projects.json"
        projects_path.write_text(
            json.dumps(
                {
                    "version": 2,
                    "projects": [
                        {"path": str(local_project), "node_id": "primary", "name": "local", "created_at": "1", "last_used": "2"},
                        {"path": str(remote_project), "node_id": "remote", "name": "remote", "created_at": "1", "last_used": "3"},
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        check(len(project_store.list_projects()) == 2, "test fixture has local and remote projects", failures)
        runtime_project_catalog.replace(project_store.list_projects())

        config_path = provider_config_sync_api.write_better_agent_config()
        payload = json.loads(config_path.read_text(encoding="utf-8"))
        check(config_path == ba_home() / "provider-config-sync" / "better-agent-config.json", "config lives under Better Agent home", failures)
        check(os.environ.get("PROVIDER_CONFIG_SYNC_CONFIG") == str(config_path), "Better Agent sets Provider Config Sync config env", failures)
        check(payload["sync_home"] == str(ba_home()), "config points sync_home at Better Agent home", failures)
        check(payload["providers"] == config_store.list_provider_metadata(), "config includes configured Better Agent providers", failures)
        check(
            payload["projects"]
            == [{"path": str(local_project), "node_id": "primary", "name": "local", "git_remote": ""}],
            "config exports local Better Agent projects only",
            failures,
        )
        provider_config_sync_api._discover(str(local_project))
        refreshed = json.loads(config_path.read_text(encoding="utf-8"))
        check(refreshed == payload, "internal discover refreshes the generated config without drift", failures)
        mcp_env = provider_config_sync_api.provider_config_sync_mcp_env(
            backend_url="http://127.0.0.1:8000",
            internal_token="test-token",
        )
        check("PROVIDER_CONFIG_SYNC_PACKAGE_SRC" not in mcp_env, "MCP env does not inject provider-config-sync source", failures)
        check(
            mcp_env["PROVIDER_CONFIG_SYNC_CONFIG"] == str(config_path),
            "MCP env points at generated Better Agent config",
            failures,
        )
        check(
            mcp_env["PROVIDER_CONFIG_SYNC_CHANGE_WEBHOOK_URL"]
            == "http://127.0.0.1:8000/api/internal/provider-config-sync/broadcast",
            "MCP env includes Better Agent broadcast webhook",
            failures,
        )
    finally:
        if old_home is None:
            os.environ.pop("BETTER_CLAUDE_HOME", None)
        else:
            os.environ["BETTER_CLAUDE_HOME"] = old_home
        if old_agent_home is None:
            os.environ.pop("BETTER_AGENT_HOME", None)
        else:
            os.environ["BETTER_AGENT_HOME"] = old_agent_home
        if old_sync_config is None:
            os.environ.pop("PROVIDER_CONFIG_SYNC_CONFIG", None)
        else:
            os.environ["PROVIDER_CONFIG_SYNC_CONFIG"] = old_sync_config
        shutil.rmtree(wipe)
    if failures:
        print("\nFAILURES:")
        for failure in failures:
            print(f" - {failure}")
        return 1
    print("\nprovider config sync Better Agent config test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
