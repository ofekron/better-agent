#!/usr/bin/env python3
"""`create_session` / `create_sub_session` MCP tools carry `preset` through.

`create_session_response` used to build its payload with an undefined `preset`
name, so every call to the tool raised NameError and `_safe_result` turned it
into `{"success": False, "error": "name 'preset' is not defined"}` — the tool
was unusable from Codex, Gemini, and AGY. Its sibling had the mirror-image bug:
`create_sub_session_response` declared `preset` and then dropped it on the
floor, so the value never reached the route that reads it.
"""
from __future__ import annotations

import inspect
import os
import shutil
import sys
import tempfile
from pathlib import Path

_TMP_HOME = tempfile.mkdtemp(prefix="communicate_preset_test_home_")
os.environ["BETTER_AGENT_HOME"] = _TMP_HOME
os.environ.setdefault("BETTER_CLAUDE_HOME", _TMP_HOME)

_BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BACKEND))
# communicate_mcp is a standalone stdio server; the runner puts the bundled sdk
# on PYTHONPATH the same way when it launches it.
sys.path.insert(0, str(_BACKEND.parent / "sdk"))

import communicate_mcp  # noqa: E402


def _captured_payload(call, **kwargs) -> dict:
    seen: dict = {}

    def fake_post_json(path, payload, timeout=None):
        seen["path"] = path
        seen["payload"] = payload
        return {"success": True}

    original_post = communicate_mcp._post_json
    original_env = communicate_mcp._env_required
    communicate_mcp._post_json = fake_post_json
    communicate_mcp._env_required = lambda name: "sender-session"
    try:
        call(**kwargs)
    finally:
        communicate_mcp._post_json = original_post
        communicate_mcp._env_required = original_env
    return seen


def test_create_session_accepts_preset():
    assert "preset" in inspect.signature(
        communicate_mcp.create_session_response
    ).parameters, "create_session lost its preset parameter"

    seen = _captured_payload(
        communicate_mcp.create_session_response,
        name="probe",
        preset="  focused  ",
    )
    assert seen["path"] == "/api/internal/create-session"
    assert seen["payload"]["preset"] == "focused"


def test_create_session_without_preset_still_succeeds():
    """The regression surfaced as a NameError, not a validation error — a call
    that never mentions preset must reach the route all the same."""
    seen = _captured_payload(communicate_mcp.create_session_response, name="probe")
    assert seen["payload"]["preset"] == ""
    assert seen["payload"]["name"] == "probe"


def test_create_sub_session_forwards_preset():
    seen = _captured_payload(
        communicate_mcp.create_sub_session_response,
        description="probe",
        preset="  focused  ",
    )
    assert seen["path"] == "/api/internal/create-sub-session"
    assert seen["payload"]["preset"] == "focused"


def test_internal_create_session_route_reads_preset():
    """The route must consume the key the tool sends, or the round trip is a
    silent no-op."""
    source = (_BACKEND / "main.py").read_text(encoding="utf-8")
    marker = '@app.post("/api/internal/create-session")'
    start = source.index(marker)
    end = source.index('@app.post("/api/internal/create-sub-session")', start)
    handler = source[start:end]
    assert 'body.get("preset")' in handler
    assert "preset=preset," in handler


if __name__ == "__main__":
    try:
        test_create_session_accepts_preset()
        test_create_session_without_preset_still_succeeds()
        test_create_sub_session_forwards_preset()
        test_internal_create_session_route_reads_preset()
        print("OK")
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
