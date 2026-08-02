from __future__ import annotations

import base64
import os
import sys
import tempfile
from pathlib import Path

# Isolate state before importing backend modules (the autouse conftest fixture
# mkdirs under paths.ba_home(); point it at a tempdir, never real state).
_TMP_HOME = tempfile.mkdtemp(prefix="runner_session_events_home_")
os.environ["BETTER_AGENT_HOME"] = _TMP_HOME
os.environ.setdefault("BETTER_CLAUDE_HOME", _TMP_HOME)

_BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BACKEND))

import runner_session_events as rse  # noqa: E402


# ---------------------------------------------------------------------------
# _map_tool — native tool name + input keys -> Claude canonical shapes
# ---------------------------------------------------------------------------

def test_map_tool_translates_known_name_and_command_key():
    name, inp = rse._map_tool("run_shell_command", {"shell_command": "echo hi", "note": "n"})
    assert name == "Bash"
    assert inp == {"command": "echo hi", "note": "n"}  # shell_command->command, unmapped key passthrough


def test_map_tool_command_alias_cmd_key():
    assert rse._map_tool("run_shell_command", {"cmd": "ls"}) == ("Bash", {"command": "ls"})


def test_map_tool_write_and_read_key_maps():
    assert rse._map_tool("write_file", {"path": "/p", "contents": "c"}) == ("Write", {"file_path": "/p", "content": "c"})
    assert rse._map_tool("read_file", {"path": "/p"}) == ("Read", {"file_path": "/p"})


def test_map_tool_unknown_name_passes_through_verbatim():
    # Unmapped tool name and unmapped keys forward unchanged.
    assert rse._map_tool("brand_new_tool", {"anything": 1}) == ("brand_new_tool", {"anything": 1})


def test_map_tool_non_dict_input_wrapped_as_value():
    # A non-dict input is boxed so the renderer still gets a dict.
    assert rse._map_tool("read_file", "literal-string") == ("Read", {"value": "literal-string"})


# ---------------------------------------------------------------------------
# _normalize_unknown — surface unknown stream-json events, never drop
# ---------------------------------------------------------------------------

def test_normalize_unknown_surfaces_raw_event_without_dropping_fields():
    raw = {"type": "weird", "timestamp": "2024-01-01T00:00:00", "foo": 1}
    out = rse._normalize_unknown(raw, parent_uuid="parent-1")
    assert out["type"] == "unknown_event"
    assert out["raw_type"] == "weird"
    assert out["raw"] is raw
    assert out["parentUuid"] == "parent-1"
    assert out["uuid"]
    assert out["timestamp"] == "2024-01-01T00:00:00"


def test_normalize_unknown_fills_timestamp_when_missing():
    out = rse._normalize_unknown({"type": "x"}, "p")
    assert out["timestamp"]


# ---------------------------------------------------------------------------
# _extract_error_message — unified extractor for result/error events
# ---------------------------------------------------------------------------

def test_extract_error_message_none_for_falsy():
    assert rse._extract_error_message(None) is None
    assert rse._extract_error_message("") is None


def test_extract_error_message_dict_prefers_message_then_error():
    assert rse._extract_error_message({"message": "boom", "error": "x"}) == "boom"
    assert rse._extract_error_message({"error": "oops"}) == "oops"


def test_extract_error_message_dict_falls_back_to_str():
    msg = rse._extract_error_message({"unexpected": 1})
    assert "unexpected" in msg


def test_extract_error_message_non_dict_returns_str():
    assert rse._extract_error_message("plain string") == "plain string"


# ---------------------------------------------------------------------------
# _is_network_error_message — transient failure detection
# ---------------------------------------------------------------------------

def test_is_network_error_true_for_known_transient_patterns():
    assert rse._is_network_error_message("connect ECONNRESET peer")
    assert rse._is_network_error_message("HTTP 503 service unavailable")
    assert rse._is_network_error_message("rate limited - retry later")


def test_is_network_error_false_for_unrelated_messages():
    assert rse._is_network_error_message("syntax error in tool input") is False
    assert rse._is_network_error_message("file not found") is False


# ---------------------------------------------------------------------------
# _sum_usage — merge token-usage dicts
# ---------------------------------------------------------------------------

def test_sum_usage_merges_numeric_keys_across_two_dicts():
    a = {"input_tokens": 10, "output_tokens": 5}
    b = {"input_tokens": 3, "cache_read": 2.0}
    assert rse._sum_usage(a, b) == {"input_tokens": 13, "output_tokens": 5, "cache_read": 2}


def test_sum_usage_handles_none_and_filters_non_numeric():
    assert rse._sum_usage(None, {"x": "big", "y": 4}) == {"y": 4}
    assert rse._sum_usage(None, None) == {}


# ---------------------------------------------------------------------------
# _materialize_attachments / _apply_image_attachments — image fold-in
# ---------------------------------------------------------------------------

def test_materialize_attachments_decodes_and_names_files(tmp_path):
    png = base64.b64encode(b"\x89PNG\r\n").decode()
    jpeg = base64.b64encode(b"\xff\xd8\xff").decode()
    images = [
        {"media_type": "image/png", "data": png},
        {"media_type": "image/jpeg", "data": jpeg},
    ]
    paths = rse._materialize_attachments(tmp_path, images)
    assert [p.name for p in paths] == ["attachment_0.png", "attachment_1.jpg"]
    assert paths[0].read_bytes() == b"\x89PNG\r\n"
    assert paths[1].read_bytes() == b"\xff\xd8\xff"
    assert paths[0].parent == tmp_path / "attachments"


def test_apply_image_attachments_no_images_returns_prompt_and_none_dir(tmp_path):
    prompt, att_dir = rse._apply_image_attachments(tmp_path, "hello", [])
    assert prompt == "hello"
    assert att_dir is None


def test_apply_image_attachments_appends_refs_to_existing_prompt(tmp_path):
    images = [{"media_type": "image/png", "data": base64.b64encode(b"x").decode()}]
    prompt, att_dir = rse._apply_image_attachments(tmp_path, "do thing", images)
    # Refs are absolute @-paths to the materialized files.
    assert prompt.startswith("do thing\n\n@")
    assert "attachment_0.png" in prompt
    assert att_dir == tmp_path / "attachments"


def test_apply_image_attachments_prompt_none_uses_only_refs(tmp_path):
    images = [{"media_type": "image/png", "data": base64.b64encode(b"x").decode()}]
    prompt, att_dir = rse._apply_image_attachments(tmp_path, None, images)
    assert prompt.startswith("@")  # no leading blank block when there is no prompt
    assert "attachment_0.png" in prompt
    assert att_dir == tmp_path / "attachments"
