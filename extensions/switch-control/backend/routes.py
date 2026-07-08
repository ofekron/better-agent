"""Switch-control extension backend routes.

Runs inside the core backend. GET state / POST switch. The switch writes the
active-checkout pointer (intent) and triggers the supervisor restart via
core's internal switch-restart route; the launcher is the fixed point that
honors the pointer and auto-reverts on failed starts.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

from better_agent_sdk import Client


def _repo_root() -> Path:
    """The checkout the running backend was started from."""
    env = os.environ.get("BETTER_AGENT_ACTIVE_CHECKOUT", "").strip()
    if env:
        return Path(env)
    # Backend runs with cwd=<checkout>/backend under both launchers.
    return Path.cwd().parent


def _pointer_module():
    root = _repo_root()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from daemonhost import pointer

    return pointer


def _lines() -> dict[str, str]:
    """Configured lines: {\"dev\": path, \"main\": path}, auto-seeded from the
    running checkout and its '-main' sibling worktree. Stored under
    ba_home()/switch_lines.json and user-editable."""
    from daemonhost.jsonio import read_json, write_json
    from daemonhost.paths import ba_home

    pointer = _pointer_module()
    config_path = ba_home() / "switch_lines.json"
    lines = {
        key: value
        for key, value in read_json(config_path).items()
        if isinstance(value, str) and pointer._is_runnable_checkout(value)
    }
    if not lines:
        root = str(_repo_root().resolve())
        if root.endswith("-main"):
            seeded = {"main": root, "dev": root[: -len("-main")]}
        else:
            seeded = {"dev": root, "main": root + "-main"}
        lines = {
            key: value for key, value in seeded.items() if pointer._is_runnable_checkout(value)
        }
        if lines:
            write_json(config_path, lines)
    return lines


def create_router(_context) -> APIRouter:
    router = APIRouter()

    @router.get("/state")
    def get_state() -> dict[str, Any]:
        pointer = _pointer_module()
        lines = _lines()
        current = str(_repo_root().resolve())
        active_line = next((name for name, path in lines.items() if path == current), "")
        return {
            "lines": lines,
            "running_checkout": current,
            "active_line": active_line,
            "pointer": pointer.read(),
            "switchable": len(lines) >= 2,
        }

    @router.post("/switch")
    def switch(body: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="body must be an object")
        target = str(body.get("target") or "").strip()
        lines = _lines()
        if target not in lines:
            raise HTTPException(status_code=400, detail=f"unknown line: {target!r}")
        target_path = lines[target]
        if target_path == str(_repo_root().resolve()):
            raise HTTPException(status_code=409, detail=f"line {target!r} is already active")
        pointer = _pointer_module()
        request_id = str(uuid.uuid4())
        try:
            pointer.set_active(target_path, request_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        try:
            result = Client().call_internal(
                "/api/internal/switch-restart", {"request_id": request_id}, timeout=30.0
            )
        except Exception as exc:
            # Fail closed: no restart means no switch — undo the intent.
            pointer.revert(f"restart trigger failed: {exc}")
            raise HTTPException(status_code=502, detail=f"restart trigger failed: {exc}") from exc
        return {"request_id": request_id, "target": target, "restart": result}

    return router
