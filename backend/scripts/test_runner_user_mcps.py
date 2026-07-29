"""Provider-run MCP config validation and builtin precedence parity."""

from __future__ import annotations

import copy
import sys
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import builtin_mcp_config  # noqa: E402
import provider_run_config  # noqa: E402


def test_alias_normalizes_without_sharing_mutable_state() -> None:
    source = {
        "mcpServers": {
            "user-a": {
                "command": "echo",
                "args": ["ready"],
            },
        },
    }
    normalized = provider_run_config.normalize_provider_run_config(source)
    source["mcpServers"]["user-a"]["args"].append("mutated")
    assert normalized == {
        "mcp_servers": {
            "user-a": {
                "command": "echo",
                "args": ["ready"],
            },
        },
    }


def test_malformed_mcp_config_fails_closed() -> None:
    malformed = [
        [],
        "",
        {"mcpServers": None},
        {"mcpServers": []},
        {"mcp_servers": ""},
        {"mcp_servers": {"": {"command": "echo"}}},
        {"mcp_servers": {" padded ": {"command": "echo"}}},
        {"mcp_servers": {"user-a": []}},
        {"mcp_servers": {}, "mcpServers": {}},
        {"skills": []},
    ]
    for value in malformed:
        try:
            provider_run_config.normalize_provider_run_config(value)
        except ValueError:
            continue
        raise AssertionError(f"malformed provider run config was accepted: {value!r}")


def test_override_merge_is_explicit_and_immutable() -> None:
    base = {
        "mcp_servers": {
            "shared": {"command": "base"},
            "base-only": {"command": "base-only"},
        },
    }
    override = {
        "mcp_servers": {
            "shared": {"command": "override"},
            "override-only": {"command": "override-only"},
        },
    }
    merged = provider_run_config.merge_provider_run_configs(base, override)
    base["mcp_servers"]["base-only"]["command"] = "mutated"
    override["mcp_servers"]["shared"]["command"] = "mutated"
    assert merged["mcp_servers"] == {
        "shared": {"command": "override"},
        "base-only": {"command": "base-only"},
        "override-only": {"command": "override-only"},
    }


def test_builtin_mcp_precedence_matches_openai_codex_and_agy() -> None:
    explicit = {
        "mcp_servers": {
            "user-a": {"command": "echo", "args": ["user"]},
            "capabilities": {"command": "malicious"},
        },
    }
    effective = {}
    for provider_kind in ("openai", "codex", "agy"):
        config = builtin_mcp_config.with_builtin_mcp_servers(
            {
                "app_session_id": "session",
                "backend_url": "http://127.0.0.1:8000",
                "cwd": "/tmp",
                "provider_id": f"{provider_kind}-provider",
                "provider_kind": provider_kind,
                "user_facing": True,
                "bare_config": False,
            },
            copy.deepcopy(explicit),
            runtime_broker={"kind": "runner_operation_broker"},
            integrations_enabled=True,
            runtime_mcp_servers={},
            launcher_mcp_servers={},
        )
        servers = config["mcp_servers"]
        assert servers["user-a"] == {"command": "echo", "args": ["user"]}
        assert servers["capabilities"]["command"] == sys.executable
        assert servers["capabilities"]["command"] != "malicious"
        effective[provider_kind] = {
            "user-a": servers["user-a"],
            "capabilities": {
                "command": servers["capabilities"]["command"],
                "args": servers["capabilities"]["args"],
                "tool_names": servers["capabilities"].get("tool_names"),
            },
        }
    assert effective["openai"] == effective["codex"] == effective["agy"]


def main() -> int:
    tests = [
        (name, fn)
        for name, fn in sorted(globals().items())
        if name.startswith("test_") and callable(fn)
    ]
    for name, fn in tests:
        fn()
        print(f"PASS {name}")
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
