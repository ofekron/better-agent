from __future__ import annotations

import json
import sys
from typing import Any

from env_compat import get_env_stripped, require_env
from loopback_http import (
    LoopbackHTTPStatusError,
    loopback_http_error_message,
    loopback_request,
)
from mcp.server.fastmcp import FastMCP


def _env_optional(name: str) -> str:
    return get_env_stripped(name)


def _post_open_config_panel(payload: dict[str, Any]) -> dict[str, Any]:
    raw = loopback_request(
        "POST",
        "/api/internal/open-config-panel",
        json.dumps(payload).encode("utf-8"),
        internal_token=require_env("BETTER_CLAUDE_INTERNAL_TOKEN"),
        timeout=10.0,
    )
    return json.loads(raw.decode("utf-8"))


def open_config_panel_response(
    app_session_id: str,
    capability_id: str,
    scope: str = "project",
    cwd: str | None = None,
) -> dict[str, Any]:
    app_session_id = str(app_session_id or "").strip()
    capability_id = (capability_id or "").strip()
    scope = (scope or "project").strip()
    if not app_session_id:
        return {"success": False, "error": "`app_session_id` is required"}
    if not capability_id:
        return {"success": False, "error": "`capability_id` is required"}
    if scope not in ("global", "project"):
        return {"success": False, "error": "`scope` must be 'global' or 'project'"}
    try:
        return _post_open_config_panel({
            "app_session_id": app_session_id,
            "capability_id": capability_id,
            "scope": scope,
            "cwd": (cwd or _env_optional("BETTER_CLAUDE_CWD") or ""),
        })
    except LoopbackHTTPStatusError as exc:
        return {"success": False, "error": f"HTTP {exc.code}: {loopback_http_error_message(exc)}"}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def build_server() -> FastMCP:
    server = FastMCP(
        "open-config-panel",
        instructions=(
            "Embed a provider-config-sync capability panel inline in the "
            "Better Agent chat. Pass a `capability_id` (discoverable via the "
            "provider-config-sync `list_provider_config_capabilities` tool) "
            "and optionally `scope` ('global' | 'project', default 'project') "
            "and `cwd`. The user gets the same editor as the configs page, "
            "plus a button to pop it into the right side panel."
        ),
    )

    @server.tool()
    def open_config_panel(
        app_session_id: str,
        capability_id: str,
        scope: str = "project",
        cwd: str | None = None,
    ) -> dict[str, Any]:
        return open_config_panel_response(app_session_id, capability_id, scope, cwd)

    return server


def main() -> int:
    build_server().run("stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
