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

`capabilities` and `ui` are wired here. `ui`'s ambient tool set is narrowed
to `open_file_panel(mode="panel")` only (a real per-session state mutation,
takes an explicit `session_id`) -- `mode="inline"`, `request_user_approval`,
`request_user_input`, and `start_file_discussion` all attach to the
in-flight assistant message of a live Better Agent turn, which does not
exist for a standalone/ambient caller, so they are not advertised here at
all (`open_file_panel_mcp.main` narrows its own tool list when
`BETTER_CLAUDE_AMBIENT_LAUNCH=1`) rather than offered and left to always
fail closed.

`open-config-panel` is NOT wired: its one tool has no non-inline mode --
every ambient call would fail closed with the same "app session id is
required" error every time, with no scoping split available the way `ui`
has one. A server whose only tool can never do real work in this context
is better left unregistered than discoverable-but-permanently-inert; if a
non-inline mode is ever added for it, wire it here the same way as `ui`.

Run with:
    core_ambient_mcp_launcher.py capabilities
"""
from __future__ import annotations

import os
import sys

from env_compat import dual_env_many, get_env
from execution_environment import isolated_subprocess_environment


_AMBIENT_ELIGIBLE_SERVERS = {
    "capabilities": "capabilities_mcp.py",
    "ui": "open_file_panel_mcp.py",
}

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
        **isolated_subprocess_environment(),
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
