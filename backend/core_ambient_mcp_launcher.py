"""Ambient (session-less) launcher for BA's own built-in MCP servers.

Unlike extension MCP servers, core servers (`capabilities`, `open-config-panel`,
`ui`) have no manifest and no `native_mcp_grants` entry -- there is no
extension identity to mint a token for. This launcher reuses the coordination
extension's already-durable, reconciliation-safe token (minted the same way
`extension_mcp_launcher.py` mints it) as the ambient credential: the target
endpoints (`/api/internal/sessions/{sid}/capabilities`) gate on "any valid
internal-loopback principal", not a specific extension identity, so this
grants no more than what coordination's own ambient `lock_ops` server already
holds.

Only `capabilities` is wired here. `open-config-panel` and the `ui` server's
`open_config_panel`/`request_user_approval`/`request_user_input` tools are
NOT ambient-eligible: their "inline" contract attaches a UI widget to the
in-flight assistant message of a live Better Agent turn, which does not
exist for a standalone/ambient caller -- serving them here would either
silently no-op or misattach UI onto an unrelated session's conversation.
`ui`'s `open_file_panel(mode="panel")` is a real per-session state mutation
and could be made ambient-eligible in a future, narrower launcher; it is not
wired here because the module also carries `request_user_approval`/
`request_user_input`, which share the same in-flight-turn problem.

Run with:
    core_ambient_mcp_launcher.py capabilities
"""
from __future__ import annotations

import os
import sys

from env_compat import dual_env_many, get_env


_AMBIENT_ELIGIBLE_SERVERS = {"capabilities": "capabilities_mcp.py"}

_DEFAULT_BACKEND_PORT = 18765


def _backend_url() -> str:
    explicit = get_env("BETTER_CLAUDE_BACKEND_URL").strip()
    if explicit:
        return explicit
    port = get_env("BETTER_CLAUDE_BACKEND_PORT").strip() or str(_DEFAULT_BACKEND_PORT)
    return f"http://localhost:{port}"


def _internal_token() -> str:
    import extension_store
    import extension_token_registry

    return extension_token_registry.mint(extension_store.BUILTIN_COORDINATION_EXTENSION_ID)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: core_ambient_mcp_launcher.py <server-name>", file=sys.stderr)
        return 2
    server_name = args[0]
    script_name = _AMBIENT_ELIGIBLE_SERVERS.get(server_name)
    if script_name is None:
        print(
            f"core MCP server {server_name!r} is not ambient-eligible "
            f"(only {sorted(_AMBIENT_ELIGIBLE_SERVERS)} are)",
            file=sys.stderr,
        )
        return 1
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), script_name)
    sdk_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sdk")
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONPATH": sdk_path,
        **dual_env_many({
            "BETTER_CLAUDE_BACKEND_URL": _backend_url(),
            "BETTER_CLAUDE_INTERNAL_TOKEN": _internal_token(),
            "BETTER_CLAUDE_AMBIENT_LAUNCH": "1",
        }),
    }
    os.execvpe(sys.executable, [sys.executable, script], env)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
