#!/usr/bin/env python3
"""`create_session` / `create_sub_session` MCP tools carry harness profiles through."""
from __future__ import annotations

import inspect
import os
import shutil
import sys
import tempfile
from pathlib import Path

_TMP_HOME = tempfile.mkdtemp(prefix="communicate_profile_test_home_")
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


def test_create_session_accepts_harness_profile():
    signature = inspect.signature(communicate_mcp.create_session_response)
    assert "harness_profile_id" in signature.parameters
    assert "preset" not in signature.parameters, "create_session still exposes removed presets"

    seen = _captured_payload(
        communicate_mcp.create_session_response,
        name="probe",
        harness_profile_id="  focused.profile  ",
    )
    assert seen["path"] == "/api/internal/create-session"
    assert seen["payload"]["harness_profile_id"] == "focused.profile"


def test_create_session_without_harness_profile_still_succeeds():
    seen = _captured_payload(communicate_mcp.create_session_response, name="probe")
    assert seen["payload"]["harness_profile_id"] is None
    assert seen["payload"]["name"] == "probe"


def test_create_sub_session_forwards_harness_profile():
    signature = inspect.signature(communicate_mcp.create_sub_session_response)
    assert "harness_profile_id" in signature.parameters
    assert "preset" not in signature.parameters, "create_sub_session still exposes removed presets"

    seen = _captured_payload(
        communicate_mcp.create_sub_session_response,
        description="probe",
        harness_profile_id="  focused.profile  ",
    )
    assert seen["path"] == "/api/internal/create-sub-session"
    assert seen["payload"]["harness_profile_id"] == "focused.profile"


def test_internal_create_session_route_reads_harness_profile():
    source = (_BACKEND / "internal_orchestration_api.py").read_text(encoding="utf-8")
    marker = '@router.post("/api/internal/create-session")'
    start = source.index(marker)
    end = source.index('@router.post("/api/internal/create-sub-session")', start)
    handler = source[start:end]
    assert "_harness_profile_selection(body)" in handler
    assert "harness_profile_id=harness_profile_id," in handler


if __name__ == "__main__":
    try:
        test_create_session_accepts_harness_profile()
        test_create_session_without_harness_profile_still_succeeds()
        test_create_sub_session_forwards_harness_profile()
        test_internal_create_session_route_reads_harness_profile()
        print("OK")
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
