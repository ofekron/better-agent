from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from typing import Any

from env_compat import get_env_stripped, require_env
from loopback_http import loopback_urlopen
from mcp.server.fastmcp import FastMCP


def _env_required(name: str) -> str:
    return require_env(name)


def _env_optional(name: str) -> str:
    return get_env_stripped(name)


def _post_open_config_panel(payload: dict[str, Any]) -> dict[str, Any]:
    backend_url = _env_required("BETTER_CLAUDE_BACKEND_URL").rstrip("/")
    internal_token = _env_required("BETTER_CLAUDE_INTERNAL_TOKEN")
    req = urllib.request.Request(
        backend_url + "/api/internal/open-config-panel",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Internal-Token": internal_token,
        },
    )
    raw = loopback_urlopen(req, timeout=10.0)
    return json.loads(raw.decode("utf-8"))


def open_config_panel_response(
    capability_id: str,
    scope: str = "project",
    cwd: str | None = None,
) -> dict[str, Any]:
    capability_id = (capability_id or "").strip()
    scope = (scope or "project").strip()
    if not capability_id:
        return {"success": False, "error": "`capability_id` is required"}
    if scope not in ("global", "project"):
        return {"success": False, "error": "`scope` must be 'global' or 'project'"}
    try:
        return _post_open_config_panel({
            "app_session_id": _env_required("BETTER_CLAUDE_APP_SESSION_ID"),
            "capability_id": capability_id,
            "scope": scope,
            "cwd": (cwd or _env_optional("BETTER_CLAUDE_CWD") or ""),
        })
    except urllib.error.HTTPError as exc:
        return {"success": False, "error": f"HTTP {exc.code}: {exc.reason}"}
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
        capability_id: str,
        scope: str = "project",
        cwd: str | None = None,
    ) -> dict[str, Any]:
        return open_config_panel_response(capability_id, scope, cwd)

    return server


def main() -> int:
    build_server().run("stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
