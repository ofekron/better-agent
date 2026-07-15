from __future__ import annotations

import json
import os
import sys
from typing import Any

from env_compat import require_env
from loopback_http import (
    LoopbackHTTPStatusError,
    loopback_http_error_message,
    request_internal,
)
from mcp.server.fastmcp import FastMCP


def _post_internal(url_path: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    raw = request_internal(
        "POST",
        url_path,
        json.dumps(payload).encode("utf-8"),
        internal_token=require_env("BETTER_CLAUDE_INTERNAL_TOKEN"),
        timeout=timeout,
    )
    return json.loads(raw.decode("utf-8"))


def _post_open_file_panel(payload: dict[str, Any]) -> dict[str, Any]:
    return _post_internal("/api/internal/open-file-panel", payload, 10.0)


def _post_user_input(payload: dict[str, Any]) -> dict[str, Any]:
    return _post_internal("/api/internal/user-input/request", payload, 24 * 60 * 60)


def _post_start_discussion(payload: dict[str, Any]) -> dict[str, Any]:
    return _post_internal("/api/internal/file-editor/start-discussion", payload, 10.0)


def open_file_panel_response(
    app_session_id: str,
    mode: str,
    path: str,
    start_line: int | None = None,
    end_line: int | None = None,
    selected_start: int | None = None,
    selected_end: int | None = None,
) -> dict[str, Any]:
    app_session_id = str(app_session_id or "").strip()
    mode = (mode or "").strip()
    path = (path or "").strip()
    if not app_session_id:
        return {"success": False, "error": "`app_session_id` is required"}
    if mode not in ("panel", "inline") or not path:
        return {"success": False, "error": "`mode` (panel|inline) and `path` are required"}
    try:
        return _post_open_file_panel({
            "app_session_id": app_session_id,
            "mode": mode,
            "path": path,
            "start_line": start_line,
            "end_line": end_line,
            "selected_start": selected_start,
            "selected_end": selected_end,
        })
    except LoopbackHTTPStatusError as exc:
        return {"success": False, "error": f"HTTP {exc.code}: {loopback_http_error_message(exc)}"}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def request_user_input_response(
    app_session_id: str,
    questions: list[dict[str, Any]],
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    app_session_id = str(app_session_id or "").strip()
    if not app_session_id:
        return {"success": False, "error": "`app_session_id` is required"}
    if not isinstance(questions, list) or not questions:
        return {"success": False, "error": "`questions` must be a non-empty array"}
    try:
        return _post_user_input({
            "app_session_id": app_session_id,
            "questions": questions,
            "timeout_seconds": timeout_seconds,
        })
    except LoopbackHTTPStatusError as exc:
        return {"success": False, "error": f"HTTP {exc.code}: {loopback_http_error_message(exc)}"}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def start_file_discussion_response(
    app_session_id: str,
    file_path: str,
    line: int,
    title: str = "",
) -> dict[str, Any]:
    app_session_id = str(app_session_id or "").strip()
    if not app_session_id:
        return {"success": False, "error": "`app_session_id` is required"}
    file_path = (file_path or "").strip()
    if not file_path:
        return {"success": False, "error": "`file_path` is required"}
    if line < 1:
        return {"success": False, "error": "`line` must be >= 1"}
    try:
        return _post_start_discussion({
            "app_session_id": app_session_id,
            "file_path": file_path,
            "line": line,
            "title": title,
        })
    except LoopbackHTTPStatusError as exc:
        return {"success": False, "error": f"HTTP {exc.code}: {loopback_http_error_message(exc)}"}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def build_server() -> FastMCP:
    server = FastMCP(
        "ui",
        instructions=(
            "Communicate with the user from an active Better Agent session. "
            "Open files in the UI or ask bounded questions when user input is required."
        ),
    )

    @server.tool()
    def open_file_panel(
        app_session_id: str,
        mode: str,
        path: str,
        start_line: int | None = None,
        end_line: int | None = None,
        selected_start: int | None = None,
        selected_end: int | None = None,
    ) -> dict[str, Any]:
        return open_file_panel_response(
            app_session_id,
            mode,
            path,
            start_line,
            end_line,
            selected_start,
            selected_end,
        )

    @server.tool()
    def request_user_input(
        app_session_id: str,
        questions: list[dict[str, Any]],
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        return request_user_input_response(app_session_id, questions, timeout_seconds)

    if os.environ.get("BETTER_CLAUDE_FILE_EDITING") == "1":
        @server.tool()
        def start_file_discussion(
            app_session_id: str,
            file_path: str,
            line: int,
            title: str = "",
        ) -> dict[str, Any]:
            return start_file_discussion_response(app_session_id, file_path, line, title)

    return server


def main() -> int:
    build_server().run("stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
