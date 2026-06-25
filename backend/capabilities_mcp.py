"""Standalone stdio MCP server for Better Agent capability management.

This is the cross-provider home for the capability-scoping management tools
(list/load/release). Better Agent's own Claude SDK runner wires the same three
tools in-process (`runner._build_capability_tools`); CLI-backed providers that
launch their harness with an `mcpServers` config (Codex / AGY / Gemini) get them
through this stdio server, injected by `builtin_mcp_config.with_builtin_mcp_servers`.

The tools are thin triggers: Better Agent core owns the active-capability write
(`session_manager.active_capability_ids`). Each tool POSTs to the internal
loopback endpoint `/api/internal/sessions/{sid}/capabilities`, authenticating
with the per-run internal token. Loading a capability makes its MCP + skill
available on the *next* turn (the capability's MCP self-gates on the active set,
skills merge at assembly).
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from typing import Any

from env_compat import get_env, require_env
from loopback_http import loopback_urlopen
from mcp.server.fastmcp import FastMCP


_TIMEOUT = 30.0


def _post_capabilities(payload: dict) -> dict[str, Any]:
    """POST an action to the core capabilities endpoint for the current
    session. Core owns the active-capability write; this is the authorized
    trigger."""
    backend_url = require_env("BETTER_CLAUDE_BACKEND_URL").rstrip("/")
    internal_token = require_env("BETTER_CLAUDE_INTERNAL_TOKEN")
    app_session_id = require_env("BETTER_CLAUDE_APP_SESSION_ID")
    endpoint = f"{backend_url}/api/internal/sessions/{app_session_id}/capabilities"
    req = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Internal-Token": internal_token,
        },
    )
    raw = loopback_urlopen(req, timeout=_TIMEOUT)
    return json.loads(raw.decode("utf-8") or "{}")


def _safe_result(fn):
    """Wrap a tool body so HTTP/infra errors come back as {success: False}
    instead of crashing the stdio MCP server (mirrors communicate_mcp)."""

    def wrapper(*a, **kw) -> dict[str, Any]:
        try:
            return fn(*a, **kw)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace") if exc.fp else exc.reason
            return {"success": False, "error": f"HTTP {exc.code}: {detail}"}
        except Exception as exc:  # noqa: BLE001 — surface to the model
            return {"success": False, "error": str(exc)}

    return wrapper


def list_capabilities_response() -> dict[str, Any]:
    return _post_capabilities({"action": "list"})


def load_capability_response(capability_id: str) -> dict[str, Any]:
    capability_id = str(capability_id or "").strip()
    if not capability_id:
        return {"success": False, "error": "capability_id is required"}
    return _post_capabilities({"action": "load", "capability_id": capability_id})


def release_capability_response(capability_id: str) -> dict[str, Any]:
    capability_id = str(capability_id or "").strip()
    if not capability_id:
        return {"success": False, "error": "capability_id is required"}
    return _post_capabilities({"action": "release", "capability_id": capability_id})


def build_server() -> FastMCP:
    server = FastMCP(
        "capabilities",
        instructions=(
            "Better Agent runtime-capability management for this session. "
            "list_capabilities shows the scoped capabilities loadable here and "
            "which are active; load_capability(capability_id) makes a "
            "capability's MCP + skill available on the next turn; "
            "release_capability(capability_id) removes it. Use the full "
            "capability id, e.g. 'ofek.testape:testape'."
        ),
    )

    @server.tool(
        description=(
            "List the scoped capabilities loadable in this session and which "
            "are currently active."
        )
    )
    def list_capabilities() -> dict[str, Any]:
        return _safe_result(list_capabilities_response)()

    @server.tool(
        description=(
            "Load a scoped capability into this session. Its MCP + skill "
            "become available on the next turn. Pass the full capability id "
            "(e.g. 'ofek.testape:testape')."
        )
    )
    def load_capability(capability_id: str) -> dict[str, Any]:
        return _safe_result(load_capability_response)(capability_id)

    @server.tool(
        description=(
            "Release a previously loaded capability from this session. Pass "
            "the full capability id (e.g. 'ofek.testape:testape')."
        )
    )
    def release_capability(capability_id: str) -> dict[str, Any]:
        return _safe_result(release_capability_response)(capability_id)

    return server


def _enabled() -> bool:
    """Capabilities are internal, non-bare runtime glue. The launcher only
    injects this server for non-bare sessions with a backend channel, but guard
    here too so a bare/credential-less run never advertises the tools."""
    if get_env("BETTER_CLAUDE_BARE_CONFIG").strip() == "1":
        return False
    return bool(
        get_env("BETTER_CLAUDE_APP_SESSION_ID").strip()
        and get_env("BETTER_CLAUDE_BACKEND_URL").strip()
        and get_env("BETTER_CLAUDE_INTERNAL_TOKEN").strip()
    )


def main() -> int:
    build_server().run("stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
