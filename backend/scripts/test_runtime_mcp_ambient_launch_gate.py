#!/usr/bin/env python3
"""Regression: runtime/subagent MCP spawns must always get an internal token.

Bug: `_runtime_mcp_server_config_for_item` treated ANY runtime MCP spawn for
a `native_exposure.allowed` extension with a missing `app_session_id` as an
"ambient launch" and skipped minting `BETTER_CLAUDE_INTERNAL_TOKEN`. Real
ambient connections never reach this function with an empty token, though —
they either go through `extension_mcp_launcher.py` (which sets
`extension_mcp_launcher_context` and fetches its own credential from
`ambient_mcp_broker`) or the broker directly. Ordinary runtime/subagent
spawns (`runtime_mcp_server_configs`) can also have an empty `app_session_id`
if the caller didn't thread it through, and those subprocesses have no other
way to authenticate — every `/api/internal/*` call they make then 403s with
"invalid internal token".

Run with: python3 backend/scripts/test_runtime_mcp_ambient_launch_gate.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import _test_home  # noqa: E402

_TMP_HOME = Path(_test_home.isolate("ba-runtime-mcp-ambient-launch-gate-"))

import extension_store  # noqa: E402

FAILURES: list[str] = []


def check(condition: bool, message: str) -> None:
    print(f"  {'✓' if condition else '✗'} {message}")
    if not condition:
        FAILURES.append(message)


def _install_extension(extension_id: str, server_name: str) -> None:
    manifest = extension_store.validate_manifest({
        "kind": extension_store.MANIFEST_KIND,
        "id": extension_id,
        "name": extension_id,
        "version": "1.0.0",
        "description": "Ambient-launch-gate test extension",
        "surfaces": ["runtime_mcp"],
        "entrypoints": {
            "mcp": [{
                "name": server_name,
                "python": "mcp/server.py",
                "args": [],
                "env": {},
                "user_facing": False,
                "bare_allowed": False,
                "requires_backend_auth": True,
                "native_exposure": {"allowed": True, "permissions": ["internal_loopback"]},
            }]
        },
        "permissions": {"internal_loopback": True},
        "marketplace": {},
        "protocol": {
            "version": 1,
            "smoke_test": {
                "required_paths": ["better-agent-extension.json", "mcp/server.py"],
                "python_modules": ["mcp.server"],
            },
        },
    })
    install_root = _TMP_HOME / f"{extension_id}-install"
    server_dir = install_root / "mcp"
    server_dir.mkdir(parents=True, exist_ok=True)
    (server_dir / "server.py").write_text("", encoding="utf-8")
    (install_root / "better-agent-extension.json").write_text(json.dumps(manifest), encoding="utf-8")

    data = extension_store._load()  # type: ignore[attr-defined]
    data["extensions"][extension_id] = {
        "manifest": manifest,
        "enabled": True,
        "installed_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "source": {
            "type": "git",
            "repo_url": "https://example.test/ambient-launch-gate.git",
            "extension_path": f"extensions/{extension_id}",
            "ref": "",
            "commit_sha": f"{extension_id}-sha",
            "install_path": str(install_root),
        },
        "entitlement": {
            "status": "not_required",
            "product_id": "",
            "token_present": False,
            "last_checked_at": "",
            "expires_at": "",
        },
    }
    extension_store._save(data)  # type: ignore[attr-defined]


def test_runtime_spawn_without_app_session_id_still_gets_token() -> None:
    extension_id = "ofek.ambient-launch-gate"
    server_name = "ambient-launch-gate"
    _install_extension(extension_id, server_name)

    # Simulate a runtime/subagent MCP spawn whose caller did not thread
    # `app_session_id` through — this must NOT be mistaken for a genuine
    # ambient launch (which always sets `extension_mcp_launcher_context`).
    inputs = {
        "open_file_panel_enabled": False,
        "backend_url": "http://127.0.0.1:1",
        "internal_token": "run-token",
        "mode": "native",
        "cwd": str(ROOT.parent),
        "model": "m",
        "provider_id": "provider-ambient-launch-gate",
    }
    config = extension_store.runtime_mcp_server_configs(
        inputs, user_facing=False, bare=False,
    ).get(server_name)
    check(config is not None, "runtime MCP config resolves without app_session_id")
    env = (config or {}).get("env") or {}
    minted = str(env.get("BETTER_CLAUDE_INTERNAL_TOKEN") or env.get("BETTER_AGENT_INTERNAL_TOKEN") or "")
    check(bool(minted), "runtime MCP spawn without app_session_id still mints an internal token")

    # The genuine ambient launcher path (sets extension_mcp_launcher_context)
    # must still skip minting — it authenticates via the ambient broker
    # instead.
    launcher_inputs = {**inputs, "extension_mcp_launcher_context": True}
    launcher_config = extension_store.runtime_mcp_server_configs(
        launcher_inputs, user_facing=False, bare=False,
    ).get(server_name)
    check(launcher_config is not None, "runtime MCP config resolves for launcher context")
    launcher_env = (launcher_config or {}).get("env") or {}
    launcher_minted = str(
        launcher_env.get("BETTER_CLAUDE_INTERNAL_TOKEN")
        or launcher_env.get("BETTER_AGENT_INTERNAL_TOKEN")
        or ""
    )
    check(not launcher_minted, "genuine ambient launcher context still skips minting a per-extension token")


def main() -> int:
    test_runtime_spawn_without_app_session_id_still_gets_token()
    if FAILURES:
        print(f"\nFAILED: {len(FAILURES)} assertion(s)")
        for message in FAILURES:
            print(f"  - {message}")
        return 1
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
