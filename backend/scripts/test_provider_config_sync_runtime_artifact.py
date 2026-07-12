from __future__ import annotations

import importlib.metadata
import inspect
import json
import tempfile
from pathlib import Path

import provider_config_sync_backend
from provider_config_sync_backend import api


def _owned(_name: str, item: dict) -> bool:
    return item.get("env", {}).get("OWNER") == "better-agent"


def test_runtime_imports_vendored_011_package() -> None:
    assert provider_config_sync_backend.__version__ == "0.1.1"
    assert importlib.metadata.version("better-agent-provider-config-sync-backend") == "0.1.1"
    assert "site-packages" in inspect.getfile(api)


def test_runtime_reconciles_aliased_provider_target_once() -> None:
    with tempfile.TemporaryDirectory() as raw_root:
        root = Path(raw_root)
        providers = [
            {"kind": "gemini", "name": "Gemini Stable", "config_dir": str(root / ".gemini")},
            {"kind": "gemini", "name": "Gemini Preview", "config_dir": str(root / ".gemini")},
        ]
        original = api._atomic_replace_snapshot
        writes = 0

        def counted(snapshot, content):
            nonlocal writes
            writes += 1
            return original(snapshot, content)

        api._atomic_replace_snapshot = counted
        try:
            result = api.reconcile_global_mcp_servers(
                {"shared": {"command": "launcher", "env": {"OWNER": "better-agent"}}},
                owns_server=_owned,
                providers=providers,
            )
        finally:
            api._atomic_replace_snapshot = original

        assert writes == 1
        assert result["targets"] == 1
        target = json.loads((root / ".gemini" / "settings.json").read_text())
        assert target["mcpServers"]["shared"]["command"] == "launcher"


if __name__ == "__main__":
    test_runtime_imports_vendored_011_package()
    test_runtime_reconciles_aliased_provider_target_once()
