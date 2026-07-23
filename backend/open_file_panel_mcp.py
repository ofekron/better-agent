from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

from better_agent_sdk.surfaces import OperationSpec, build_mcp_server, run_mcp_or_cli
from env_compat import require_env


def _env_required(name: str) -> str:
    return require_env(name)


def _post_open_file_panel(payload: dict[str, Any]) -> dict[str, Any]:
    backend_url = _env_required("BETTER_CLAUDE_BACKEND_URL").rstrip("/")
    internal_token = _env_required("BETTER_CLAUDE_INTERNAL_TOKEN")
    req = urllib.request.Request(
        backend_url + "/api/internal/open-file-panel",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Internal-Token": internal_token,
        },
    )
    with urllib.request.urlopen(req, timeout=10.0) as resp:
        raw = resp.read()
    return json.loads(raw.decode("utf-8"))


def _post_user_input(payload: dict[str, Any]) -> dict[str, Any]:
    backend_url = _env_required("BETTER_CLAUDE_BACKEND_URL").rstrip("/")
    internal_token = _env_required("BETTER_CLAUDE_INTERNAL_TOKEN")
    req = urllib.request.Request(
        backend_url + "/api/internal/user-input/request",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Internal-Token": internal_token,
        },
    )
    with urllib.request.urlopen(req, timeout=24 * 60 * 60) as resp:
        raw = resp.read()
    return json.loads(raw.decode("utf-8"))


def _post_start_discussion(payload: dict[str, Any]) -> dict[str, Any]:
    backend_url = _env_required("BETTER_CLAUDE_BACKEND_URL").rstrip("/")
    internal_token = _env_required("BETTER_CLAUDE_INTERNAL_TOKEN")
    req = urllib.request.Request(
        backend_url + "/api/internal/file-editor/start-discussion",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Internal-Token": internal_token,
        },
    )
    with urllib.request.urlopen(req, timeout=10.0) as resp:
        raw = resp.read()
    return json.loads(raw.decode("utf-8"))


def open_file_panel_response(
    mode: str,
    path: str,
    start_line: int | None = None,
    end_line: int | None = None,
    selected_start: int | None = None,
    selected_end: int | None = None,
) -> dict[str, Any]:
    mode = (mode or "").strip()
    path = (path or "").strip()
    if mode not in ("panel", "inline") or not path:
        return {"success": False, "error": "`mode` (panel|inline) and `path` are required"}
    try:
        return _post_open_file_panel({
            "app_session_id": _env_required("BETTER_CLAUDE_APP_SESSION_ID"),
            "mode": mode,
            "path": path,
            "start_line": start_line,
            "end_line": end_line,
            "selected_start": selected_start,
            "selected_end": selected_end,
        })
    except urllib.error.HTTPError as exc:
        return {"success": False, "error": f"HTTP {exc.code}: {exc.reason}"}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def request_user_input_response(
    questions: list[dict[str, Any]],
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    if not isinstance(questions, list) or not questions:
        return {"success": False, "error": "`questions` must be a non-empty array"}
    try:
        return _post_user_input({
            "app_session_id": _env_required("BETTER_CLAUDE_APP_SESSION_ID"),
            "kind": "input",
            "questions": questions,
            "timeout_seconds": timeout_seconds,
        })
    except urllib.error.HTTPError as exc:
        return {"success": False, "error": f"HTTP {exc.code}: {exc.reason}"}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def request_user_approval_response(
    prompt: str,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    prompt = (prompt or "").strip()
    if not prompt:
        return {"success": False, "error": "`prompt` is required"}
    try:
        return _post_user_input({
            "app_session_id": _env_required("BETTER_CLAUDE_APP_SESSION_ID"),
            "kind": "approval",
            "prompt": prompt,
            "timeout_seconds": timeout_seconds,
        })
    except urllib.error.HTTPError as exc:
        return {"success": False, "error": f"HTTP {exc.code}: {exc.reason}"}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def start_file_discussion_response(
    file_path: str,
    line: int,
    title: str = "",
) -> dict[str, Any]:
    file_path = (file_path or "").strip()
    if not file_path:
        return {"success": False, "error": "`file_path` is required"}
    if line < 1:
        return {"success": False, "error": "`line` must be >= 1"}
    try:
        return _post_start_discussion({
            "app_session_id": _env_required("BETTER_CLAUDE_APP_SESSION_ID"),
            "file_path": file_path,
            "line": line,
            "title": title,
        })
    except urllib.error.HTTPError as exc:
        return {"success": False, "error": f"HTTP {exc.code}: {exc.reason}"}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


_INSTRUCTIONS = (
    "Communicate with the user from an active Better Agent session. "
    "Open files in the UI, ask bounded questions, or request explicit approval."
)


def _specs() -> tuple[OperationSpec, ...]:
    specs = [
        OperationSpec("open_file_panel", open_file_panel_response, operation="runtime_ui_open_file_panel"),
        OperationSpec("request_user_input", request_user_input_response, operation="runtime_ui_request_user_input"),
        OperationSpec("request_user_approval", request_user_approval_response, operation="runtime_ui_request_user_approval"),
    ]
    if os.environ.get("BETTER_CLAUDE_FILE_EDITING") == "1":
        specs.append(OperationSpec("start_file_discussion", start_file_discussion_response, operation="runtime_ui_start_file_discussion"))
    return tuple(specs)


def build_server():
    return build_mcp_server(
        "ui",
        _specs(),
        instructions=_INSTRUCTIONS,
    )


def main() -> int:
    return run_mcp_or_cli("ui", _specs(), instructions=_INSTRUCTIONS)


if __name__ == "__main__":
    sys.exit(main())
