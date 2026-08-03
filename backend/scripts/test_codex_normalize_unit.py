#!/usr/bin/env python3
"""Unit coverage for codex_normalize.py.

Pure-logic normalizers: raw Codex payloads/items -> Claude-shaped events.
stdlib-only module (no BA imports), so this test imports nothing heavy —
no main app, no conftest fixtures, no provider config. Tier = UNIT.
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import codex_normalize as cn  # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _text_block(event: dict) -> str:
    """First text content of a normalized event."""
    for block in event["message"]["content"]:
        if block.get("type") == "text":
            return block["text"]
    return ""


def _thinking(event: dict) -> str:
    for block in event["message"]["content"]:
        if block.get("type") == "thinking":
            return block["thinking"]
    return ""


def _tool_use(event: dict) -> dict:
    for block in event["message"]["content"]:
        if block.get("type") == "tool_use":
            return block
    raise AssertionError(f"no tool_use in {event}")


def _tool_result_content(event: dict) -> str:
    for block in event["message"]["content"]:
        if block.get("type") == "tool_result":
            return block["content"]
    raise AssertionError(f"no tool_result in {event}")


PARENT = "root-uuid"


# ---------------------------------------------------------------------------
# _codex_text_content
# ---------------------------------------------------------------------------

class TestCodexTextContent:
    def test_plain_string_is_stripped(self):
        assert cn._codex_text_content("  hi  ") == "hi"

    def test_non_list_non_str_is_empty(self):
        assert cn._codex_text_content(42) == ""
        assert cn._codex_text_content(None) == ""

    def test_non_dict_blocks_skipped(self):
        # L26: non-dict block continue
        assert cn._codex_text_content([123, {"text": "kept"}]) == "kept"

    def test_text_falls_back_to_content_key(self):
        # L29: block.get("text") not str -> fall back to "content"
        assert cn._codex_text_content([{"text": 5, "content": "fallback"}]) == "fallback"

    def test_blocks_joined_with_newline(self):
        assert cn._codex_text_content([{"text": "a"}, {"text": "b"}]) == "a\nb"

    def test_dict_block_no_valid_text_or_content(self):
        # 30->24: text not str, content fallback also not a non-empty str -> skip
        assert cn._codex_text_content([{"text": 5, "content": 6}, {"text": "kept"}]) == "kept"
        assert cn._codex_text_content([{"text": 5, "content": ""}]) == ""


# ---------------------------------------------------------------------------
# _codex_primary_assistant_text
# ---------------------------------------------------------------------------

class TestCodexPrimaryAssistantText:
    def test_event_msg_agent_message_string(self):
        # L40-41
        rec = {"type": "event_msg", "payload": {"type": "agent_message", "message": "  hello "}}
        assert cn._codex_primary_assistant_text(rec) == "hello"

    def test_event_msg_agent_message_non_string(self):
        rec = {"type": "event_msg", "payload": {"type": "agent_message", "message": 7}}
        assert cn._codex_primary_assistant_text(rec) == ""

    def test_response_item_assistant_content(self):
        rec = {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"text": "body"}],
            },
        }
        assert cn._codex_primary_assistant_text(rec) == "body"

    def test_non_matching_record_returns_empty(self):
        # L48
        assert cn._codex_primary_assistant_text({"type": "other", "payload": {}}) == ""
        assert cn._codex_primary_assistant_text({"type": "event_msg", "payload": "x"}) == ""


# ---------------------------------------------------------------------------
# _mark_final_answer
# ---------------------------------------------------------------------------

class TestMarkFinalAnswer:
    def test_with_origin(self):
        # L57-60
        out = cn._mark_final_answer({"a": 1}, origin="subagent-A")
        assert out["final_answer"] is True
        assert out["final_answer_origin"] == "subagent-A"
        assert out["a"] == 1

    def test_without_origin(self):
        out = cn._mark_final_answer({})
        assert out["final_answer"] is True
        assert "final_answer_origin" not in out


# ---------------------------------------------------------------------------
# _codex_terminal_state
# ---------------------------------------------------------------------------

class TestCodexTerminalState:
    def test_task_complete_true(self):
        assert cn._codex_terminal_state({"type": "event_msg", "payload": {"type": "task_complete"}}) is True

    def test_task_failed_false(self):
        # L70
        assert cn._codex_terminal_state({"type": "event_msg", "payload": {"type": "task_failed"}}) is False

    def test_turn_failed_false(self):
        assert cn._codex_terminal_state({"type": "event_msg", "payload": {"type": "turn_failed"}}) is False

    def test_turn_completed(self):
        assert cn._codex_terminal_state({"type": "turn.completed"}) is True

    def test_turn_failed_top(self):
        # L74
        assert cn._codex_terminal_state({"type": "turn.failed"}) is False

    def test_non_terminal_none(self):
        assert cn._codex_terminal_state({"type": "event_msg", "payload": {"type": "other"}}) is None
        assert cn._codex_terminal_state({"type": "event_msg", "payload": "x"}) is None
        assert cn._codex_terminal_state({"type": "unrelated"}) is None


# ---------------------------------------------------------------------------
# _codex_reasoning_text
# ---------------------------------------------------------------------------

class TestCodexReasoningText:
    def test_summary_key_first(self):
        assert cn._codex_reasoning_text({"summary": "s", "content": "c"}) == "s"

    def test_text_key(self):
        assert cn._codex_reasoning_text({"text": "t"}) == "t"

    def test_empty(self):
        assert cn._codex_reasoning_text({}) == ""


# ---------------------------------------------------------------------------
# uuid helpers
# ---------------------------------------------------------------------------

class TestUuidHelpers:
    def test_new_uuid_is_valid_uuid(self):
        u = cn._new_uuid()
        assert uuid.UUID(u) is not None

    def test_stable_uuid_deterministic(self):
        a = cn._stable_uuid("ns", "key1")
        b = cn._stable_uuid("ns", "key1")
        c = cn._stable_uuid("ns", "key2")
        d = cn._stable_uuid("other", "key1")
        assert a == b
        assert a != c
        assert a != d

    def test_stable_payload_key_prefers_id(self):
        assert cn._stable_payload_key({"id": "x", "z": 1}) == "x"
        assert cn._stable_payload_key({"call_id": "y"}) == "y"

    def test_stable_payload_key_falls_back_to_json(self):
        key = cn._stable_payload_key({"z": 1, "a": 2})
        assert json.loads(key) == {"a": 2, "z": 1}

    def test_response_item_uuid_uses_type_and_payload(self):
        a = cn._response_item_uuid("root", {"type": "message", "id": "1"})
        b = cn._response_item_uuid("root", {"type": "message", "id": "1"}, ":native")
        c = cn._response_item_uuid("root", {"type": "reasoning", "id": "1"})
        assert a == cn._response_item_uuid("root", {"type": "message", "id": "1"})
        assert a != b
        assert a != c

    def test_response_item_uuid_missing_type(self):
        u = cn._response_item_uuid("root", {"id": "1"})
        assert "unknown" in u or u  # stable, non-empty


# ---------------------------------------------------------------------------
# _file_size
# ---------------------------------------------------------------------------

class TestFileSize:
    def test_none(self):
        assert cn._file_size(None) == 0

    def test_real_file(self, tmp_path):
        f = tmp_path / "f.bin"
        f.write_bytes(b"12345")
        assert cn._file_size(f) == 5

    def test_missing_file_oserror(self):
        # L122-123
        assert cn._file_size(Path("/nonexistent/no-such-file-xyz")) == 0


# ---------------------------------------------------------------------------
# _normalize_agent_message / _normalize_reasoning
# ---------------------------------------------------------------------------

class TestNormalizeAgentMessage:
    def test_basic(self):
        ev = cn._normalize_agent_message({"text": "hi"}, PARENT, event_uuid="ev-1")
        assert ev["uuid"] == "ev-1"
        assert ev["parentUuid"] == PARENT
        assert ev["type"] == "assistant"
        assert _text_block(ev) == "hi"
        assert ev["message"]["model"] == "codex"

    def test_new_uuid_when_none(self):
        ev = cn._normalize_agent_message({"text": "hi"}, PARENT)
        uuid.UUID(ev["uuid"])

    def test_attaches_parent_tool_use_id(self):
        ev = cn._normalize_agent_message(
            {"text": "hi", "parent_tool_use_id": "ptu-1"}, PARENT,
        )
        assert ev["parent_tool_use_id"] == "ptu-1"

    def test_camel_parent_id_key(self):
        ev = cn._normalize_agent_message(
            {"text": "hi", "parentToolUseId": "ptu-2"}, PARENT,
        )
        assert ev["parent_tool_use_id"] == "ptu-2"


class TestNormalizeReasoning:
    def test_basic(self):
        # L168-169
        ev = cn._normalize_reasoning({"text": "thinking..."}, PARENT, event_uuid="r-1")
        assert ev["uuid"] == "r-1"
        assert _thinking(ev) == "thinking..."
        assert ev["message"]["content"][0]["type"] == "thinking"


# ---------------------------------------------------------------------------
# _normalize_command_started / _normalize_file_change
# ---------------------------------------------------------------------------

class TestNormalizeCommandStarted:
    def test_uses_item_id(self):
        ev = cn._normalize_command_started({"id": "c1", "command": "ls -la"}, PARENT)
        tu = _tool_use(ev)
        assert tu["name"] == "Bash"
        assert tu["id"] == "c1"
        assert tu["input"]["command"] == "ls -la"

    def test_missing_id_generates_uuid(self):
        ev = cn._normalize_command_started({"command": "echo"}, PARENT)
        uuid.UUID(_tool_use(ev)["id"])


class TestNormalizeFileChange:
    def test_all_kinds_completed(self):
        # L202-217: delete / add / update kinds, status completed
        item = {
            "id": "fc1",
            "status": "completed",
            "changes": [
                {"path": "a.txt", "kind": "delete"},
                {"path": "b.txt", "kind": "add"},
                {"path": "c.txt", "kind": "update"},
                {"path": "d.txt"},  # default kind update
            ],
        }
        tool_use_ev, result_ev = cn._normalize_file_change(item, PARENT)
        tu = _tool_use(tool_use_ev)
        tr = result_ev["message"]["content"][0]
        assert tu["name"] == "Edit"
        desc = tu["input"]["description"]
        assert "Delete: a.txt" in desc
        assert "Add: b.txt" in desc
        assert "Update: c.txt" in desc
        assert "Update: d.txt" in desc
        # completed -> result content equals description (no Failed prefix)
        assert tr["content"] == desc
        assert tr["tool_use_id"] == "fc1"

    def test_failed_status_prefix(self):
        item = {"id": "fc2", "status": "failed", "changes": [{"path": "x", "kind": "update"}]}
        _, result_ev = cn._normalize_file_change(item, PARENT)
        assert result_ev["message"]["content"][0]["content"] == "Failed: Update: x"


# ---------------------------------------------------------------------------
# _normalize_mcp_tool_started / _normalize_mcp_tool_completed
# ---------------------------------------------------------------------------

class TestNormalizeMcpToolStarted:
    def test_with_server(self):
        ev = cn._normalize_mcp_tool_started(
            {"id": "m1", "tool": "search", "server": "svc", "arguments": {"q": 1}}, PARENT,
        )
        tu = _tool_use(ev)
        assert tu["name"] == "mcp__svc__search"
        assert tu["input"] == {"q": 1}

    def test_without_server(self):
        ev = cn._normalize_mcp_tool_started({"id": "m2", "tool": "raw"}, PARENT)
        assert _tool_use(ev)["name"] == "raw"

    def test_non_dict_arguments_wrapped(self):
        ev = cn._normalize_mcp_tool_started(
            {"id": "m3", "tool": "t", "server": "s", "arguments": "literal"}, PARENT,
        )
        assert _tool_use(ev)["input"] == {"value": "literal"}


class TestNormalizeMcpToolCompleted:
    def test_error_dict_with_message(self):
        # L274
        ev = cn._normalize_mcp_tool_completed({"id": "m", "error": {"message": "boom"}}, PARENT)
        assert _tool_result_content(ev) == "Error: boom"

    def test_error_non_dict(self):
        ev = cn._normalize_mcp_tool_completed({"id": "m", "error": "oops"}, PARENT)
        assert _tool_result_content(ev) == "Error: oops"

    def test_result_content_list_of_dicts(self):
        ev = cn._normalize_mcp_tool_completed(
            {"id": "m", "result": {"content": [{"text": "a"}, {"x": 1}]}}, PARENT,
        )
        # second entry has no text -> json.dumps
        content = _tool_result_content(ev)
        assert "a" in content
        assert json.loads(content.split("\n")[1]) == {"x": 1}

    def test_result_content_list_with_non_dict(self):
        # L285
        ev = cn._normalize_mcp_tool_completed(
            {"id": "m", "result": {"content": [42]}}, PARENT,
        )
        assert _tool_result_content(ev) == "42"

    def test_result_content_not_list(self):
        # L288
        ev = cn._normalize_mcp_tool_completed(
            {"id": "m", "result": {"content": "stringified"}}, PARENT,
        )
        assert _tool_result_content(ev) == "{'content': 'stringified'}"

    def test_no_error_no_result_empty(self):
        ev = cn._normalize_mcp_tool_completed({"id": "m"}, PARENT)
        assert _tool_result_content(ev) == ""


# ---------------------------------------------------------------------------
# _collab_agent_description / collab agent normalizers
# ---------------------------------------------------------------------------

class TestCollabAgentDescription:
    def test_prompt_string(self):
        assert cn._collab_agent_description({"prompt": "  do thing "}) == "do thing"

    def test_receivers_list(self):
        # L309-312
        d = cn._collab_agent_description({"tool": "worker", "receiverThreadIds": ["r1", "r2"]})
        assert d == "worker: r1, r2"

    def test_receivers_uses_subagent_default_tool(self):
        d = cn._collab_agent_description({"receiverThreadIds": ["r1"]})
        assert d == "subagent: r1"

    def test_empty_receivers_falls_back(self):
        d = cn._collab_agent_description({"tool": "t", "receiverThreadIds": []})
        assert d == "t"

    def test_no_prompt_no_receivers_no_tool(self):
        assert cn._collab_agent_description({}) == "subagent"


class TestNormalizeCollabAgentStarted:
    def test_basic_with_model_and_effort(self):
        ev = cn._normalize_collab_agent_started(
            {
                "id": "ca1",
                "tool": "researcher",
                "prompt": "go",
                "model": "gpt-x",
                "reasoningEffort": "high",
            },
            PARENT,
        )
        tu = _tool_use(ev)
        assert tu["name"] == "Agent"
        assert tu["input"]["subagent_type"] == "researcher"
        assert tu["input"]["model"] == "gpt-x"
        assert tu["input"]["reasoning_effort"] == "high"
        assert tu["id"] == "ca1"

    def test_missing_tool_defaults(self):
        ev = cn._normalize_collab_agent_started({"prompt": "p"}, PARENT)
        assert _tool_use(ev)["input"]["subagent_type"] == "default"

    def test_missing_id_generates_uuid(self):
        ev = cn._normalize_collab_agent_started({"prompt": "p"}, PARENT)
        uuid.UUID(_tool_use(ev)["id"])


class TestNormalizeCollabAgentCompleted:
    def test_states_dict_lines(self):
        ev = cn._normalize_collab_agent_completed(
            {
                "id": "ca",
                "agentsStates": {
                    "t1": {"status": "done", "message": "ok"},
                    "t2": {"status": None, "message": None},  # empty -> skipped
                },
            },
            PARENT,
        )
        content = _tool_result_content(ev)
        assert "t1: done ok" in content
        assert "t2" not in content

    def test_non_dict_state_skipped(self):
        # L349
        ev = cn._normalize_collab_agent_completed(
            {"id": "ca", "agentsStates": {"t1": "not-a-dict", "t2": {"status": "ok"}}}, PARENT,
        )
        assert "t2: ok" in _tool_result_content(ev)

    def test_empty_states_uses_status_fallback(self):
        ev = cn._normalize_collab_agent_completed({"id": "ca", "status": "completed"}, PARENT)
        assert _tool_result_content(ev) == "completed"

    def test_empty_states_no_status_default(self):
        ev = cn._normalize_collab_agent_completed({"id": "ca"}, PARENT)
        assert _tool_result_content(ev) == "completed"


# ---------------------------------------------------------------------------
# web search normalizers
# ---------------------------------------------------------------------------

class TestNormalizeWebSearch:
    def test_basic_with_tool_id(self):
        ev = cn._normalize_web_search({"id": "w", "query": "q", "action": "search"}, PARENT, tool_id="tid")
        tu = _tool_use(ev)
        assert tu["id"] == "tid"
        assert tu["name"] == "WebSearch"
        assert tu["input"] == {"query": "q", "action": "search"}

    def test_id_fallbacks(self):
        ev = cn._normalize_web_search({"id": "w2", "query": "q", "action": ""}, PARENT)
        assert _tool_use(ev)["id"] == "w2"
        ev2 = cn._normalize_web_search({"query": "q"}, PARENT)
        uuid.UUID(_tool_use(ev2)["id"])


class TestWebSearchResultText:
    def test_empty_when_no_result_key(self):
        assert cn._web_search_result_text({"foo": "bar"}) == ""

    def test_string_result(self):
        # L403
        assert cn._web_search_result_text({"result": "  hits  "}) == "hits"

    def test_list_of_dicts(self):
        txt = cn._web_search_result_text(
            {"results": [{"title": "T", "url": "U", "snippet": "S"}, {"name": "N", "link": "L"}]}
        )
        assert "T - U - S" in txt
        assert "N - L" in txt

    def test_list_with_non_dict_entry(self):
        # L421-422
        assert cn._web_search_result_text({"results": ["bare"]}) == "bare"

    def test_dict_result_content_list(self):
        # L425-437
        txt = cn._web_search_result_text({"result": {"content": [{"text": "a"}, {"content": "b"}, 5]}})
        assert txt == "a\nb\n5"

    def test_dict_result_content_list_empty(self):
        txt = cn._web_search_result_text({"result": {"content": []}})
        # falls through to scalar keys -> none -> json.dumps
        assert json.loads(txt) == {"content": []}

    def test_dict_result_scalar_text(self):
        # L438-441
        assert cn._web_search_result_text({"result": {"snippet": "  snip  "}}) == "snip"

    def test_dict_result_no_scalar_keys_json(self):
        # L442
        txt = cn._web_search_result_text({"result": {"weird": 1}})
        assert json.loads(txt) == {"weird": 1}

    def test_other_type_result(self):
        # L444
        assert cn._web_search_result_text({"result": 42}).strip() == "42"

    def test_list_entry_all_empty_fields_skipped(self):
        # 419->407: dict entry where title/url/snippet all empty -> line empty -> skip
        assert cn._web_search_result_text({"results": [{"title": "", "url": "", "snippet": ""}]}) == ""

    def test_list_none_entry_skipped(self):
        # 421->407: entry is None -> elif entry is not None false -> skip
        assert cn._web_search_result_text({"results": [None]}) == ""

    def test_dict_content_list_empty_text_skipped(self):
        # 432->429: dict entry in content with no text/content -> skip; later entry kept
        out = cn._web_search_result_text({"result": {"content": [{"text": None, "content": None}, {"text": "kept"}]}})
        assert out == "kept"

    def test_dict_content_list_none_entry_skipped(self):
        # 434->429: None entry in content list -> elif entry is not None false -> skip; later entry kept
        out = cn._web_search_result_text({"result": {"content": [None, {"text": "kept"}]}})
        assert out == "kept"


class TestNormalizeWebSearchResult:
    def test_returns_none_when_no_content(self):
        assert cn._normalize_web_search_result({"id": "w"}, PARENT) is None

    def test_basic(self):
        ev = cn._normalize_web_search_result({"id": "w", "result": "hits"}, PARENT, tool_id="tid")
        tr = ev["message"]["content"][0]
        assert tr["tool_use_id"] == "tid"
        assert tr["content"] == "hits"
        assert ev["type"] == "user"

    def test_id_fallback(self):
        ev = cn._normalize_web_search_result({"id": "w9", "result": "h"}, PARENT)
        assert ev["message"]["content"][0]["tool_use_id"] == "w9"


class TestNormalizeWebSearchEvents:
    def test_pair_with_result(self):
        events = cn._normalize_web_search_events({"id": "w", "query": "q", "result": "hits"}, PARENT)
        assert len(events) == 2
        assert events[0]["message"]["content"][0]["type"] == "tool_use"
        assert events[1]["message"]["content"][0]["type"] == "tool_result"

    def test_tool_use_only_when_no_result(self):
        events = cn._normalize_web_search_events({"id": "w", "query": "q"}, PARENT)
        assert len(events) == 1


class TestWebSearchItemHelpers:
    def test_item_from_payload(self):
        item = cn._web_search_item_from_payload(
            {"call_id": "c", "query": "q", "action": {"a": 1}}
        )
        assert item == {"id": "c", "query": "q", "action": {"a": 1}}

    def test_item_from_payload_query_from_action(self):
        item = cn._web_search_item_from_payload({"id": "i", "action": {"query": "qq"}})
        assert item["query"] == "qq"

    def test_item_from_payload_id_fallback(self):
        item = cn._web_search_item_from_payload({"query": "q"})
        uuid.UUID(item["id"])

    def test_dedupe_key_deterministic(self):
        a = cn._web_search_dedupe_key({"query": "q", "action": "a"})
        b = cn._web_search_dedupe_key({"action": "a", "query": "q"})
        assert a == b
        assert json.loads(a) == {"query": "q", "action": "a"}


# ---------------------------------------------------------------------------
# _json_obj / _first_text
# ---------------------------------------------------------------------------

class TestJsonObj:
    def test_dict_passthrough(self):
        assert cn._json_obj({"a": 1}) == {"a": 1}

    def test_string_dict(self):
        assert cn._json_obj('{"a": 1}') == {"a": 1}

    def test_string_non_dict_parsed(self):
        # L489
        assert cn._json_obj("[1, 2]") == {"value": [1, 2]}

    def test_string_invalid(self):
        assert cn._json_obj("not json") == {"value": "not json"}

    def test_none(self):
        assert cn._json_obj(None) == {}

    def test_other_type(self):
        # L492
        assert cn._json_obj(42) == {"value": 42}


class TestFirstText:
    def test_first_string(self):
        assert cn._first_text("", None, "x", "y") == "x"

    def test_none(self):
        assert cn._first_text("", None, []) == ""


# ---------------------------------------------------------------------------
# parent tool use id helpers
# ---------------------------------------------------------------------------

class TestParentToolUseId:
    def test_all_keys(self):
        keys = [
            "parent_tool_use_id", "parentToolUseId", "parent_call_id", "parentCallId",
            "parent_item_id", "parentItemId", "parent_id", "parentId",
        ]
        for k in keys:
            assert cn._parent_tool_use_id({k: "v"}) == "v"

    def test_empty_string_value_skipped(self):
        assert cn._parent_tool_use_id({"parent_id": ""}) == ""

    def test_non_string_skipped(self):
        assert cn._parent_tool_use_id({"parent_id": 5}) == ""

    def test_with_parent_attaches(self):
        ev = cn._with_parent_tool_use_id({"x": 1}, {"parent_id": "p"})
        assert ev["parent_tool_use_id"] == "p"

    def test_with_parent_no_id(self):
        ev = cn._with_parent_tool_use_id({"x": 1}, {})
        assert "parent_tool_use_id" not in ev


class TestAttachCollabParent:
    def test_attaches_known_thread(self):
        out = cn._attach_collab_parent_from_thread(
            {"id": "x", "threadId": "t1"}, {"t1": "parent-id"},
        )
        assert out["parentToolUseId"] == "parent-id"

    def test_no_thread_id(self):
        out = cn._attach_collab_parent_from_thread({"id": "x"}, {"t1": "p"})
        assert "parentToolUseId" not in out

    def test_unknown_thread(self):
        out = cn._attach_collab_parent_from_thread({"threadId": "t2"}, {"t1": "p"})
        assert "parentToolUseId" not in out

    def test_already_has_parent_id(self):
        out = cn._attach_collab_parent_from_thread(
            {"threadId": "t1", "parent_id": "own"}, {"t1": "p"},
        )
        assert "parentToolUseId" not in out
        assert out["parent_id"] == "own"


class TestRememberCollabReceivers:
    def test_records_receivers(self):
        parents: dict = {}
        cn._remember_collab_receivers({"id": "call-1", "receiverThreadIds": ["r1", "r2"]}, parents)
        assert parents == {"r1": "call-1", "r2": "call-1"}

    def test_id_not_str_returns(self):
        # L543
        parents: dict = {}
        cn._remember_collab_receivers({"id": 5, "receiverThreadIds": ["r"]}, parents)
        assert parents == {}

    def test_id_empty_returns(self):
        parents: dict = {}
        cn._remember_collab_receivers({"id": "", "receiverThreadIds": ["r"]}, parents)
        assert parents == {}

    def test_receivers_not_list_returns(self):
        # L546
        parents: dict = {}
        cn._remember_collab_receivers({"id": "x", "receiverThreadIds": "r1"}, parents)
        assert parents == {}

    def test_non_string_receivers_skipped(self):
        parents: dict = {}
        cn._remember_collab_receivers({"id": "x", "receiverThreadIds": [1, "r2", ""]}, parents)
        assert parents == {"r2": "x"}


# ---------------------------------------------------------------------------
# _normalize_agent_args
# ---------------------------------------------------------------------------

class TestNormalizeAgentArgs:
    def test_full(self):
        out = cn._normalize_agent_args({
            "prompt": "p", "description": "d", "agent_type": "researcher",
        })
        assert out["prompt"] == "p"
        assert out["description"] == "d"
        assert out["subagent_type"] == "researcher"
        # original keys preserved
        assert out["agent_type"] == "researcher"

    def test_fallbacks_and_defaults(self):
        out = cn._normalize_agent_args({"task": "t", "name": "nm"})
        assert out["description"] == "t"
        assert out["prompt"] == "t"
        assert out["subagent_type"] == "nm"

    def test_default_subagent_type(self):
        out = cn._normalize_agent_args({})
        assert out["subagent_type"] == "default"
        assert "description" not in out
        assert "prompt" not in out


# ---------------------------------------------------------------------------
# _response_text_content / _response_input_text_content
# ---------------------------------------------------------------------------

class TestResponseTextContent:
    def test_string_content(self):
        # L583
        assert cn._response_text_content({"content": "raw"}) == "raw"

    def test_non_list_non_str(self):
        # L585
        assert cn._response_text_content({"content": 7}) == ""

    def test_list_with_str_and_dict_blocks(self):
        # L592-593
        out = cn._response_text_content({"content": ["s1", {"text": "s2"}, {"content": "s3"}]})
        assert out == "s1\ns2\ns3"

    def test_empty_blocks_filtered(self):
        assert cn._response_text_content({"content": [{"text": ""}, ""]}) == ""

    def test_non_str_non_dict_block_skipped(self):
        # 592->587: block is neither dict nor str -> elif false -> skip
        assert cn._response_text_content({"content": [5, {"text": "ok"}]}) == "ok"
        assert cn._response_text_content({"content": [5]}) == ""


class TestResponseInputTextContent:
    def test_string_content(self):
        # L600
        assert cn._response_input_text_content({"content": "raw"}) == "raw"

    def test_non_list_non_str(self):
        # L602
        assert cn._response_input_text_content({"content": 7}) == ""

    def test_list_with_str_and_dict_blocks(self):
        # L609-610 (note: only "text" key, not "content")
        out = cn._response_input_text_content({"content": ["s1", {"text": "s2"}, {"content": "ignored"}]})
        assert out == "s1\ns2"

    def test_non_str_non_dict_block_skipped(self):
        # 609->604: block is neither dict nor str -> elif false -> skip
        assert cn._response_input_text_content({"content": [9, {"text": "ok"}]}) == "ok"
        assert cn._response_input_text_content({"content": [9]}) == ""


# ---------------------------------------------------------------------------
# _extract_subagent_notification / _normalize_subagent_notification
# ---------------------------------------------------------------------------

class TestExtractSubagentNotification:
    def test_valid(self):
        txt = 'noise <subagent_notification>{"agent_path":"a","status":"done"}</subagent_notification>'
        assert cn._extract_subagent_notification(txt) == {"agent_path": "a", "status": "done"}

    def test_missing_tags(self):
        assert cn._extract_subagent_notification("no tags here") is None

    def test_invalid_json(self):
        # L622
        txt = "<subagent_notification>not json</subagent_notification>"
        assert cn._extract_subagent_notification(txt) is None

    def test_parsed_not_dict(self):
        # L623
        txt = "<subagent_notification>[1,2]</subagent_notification>"
        assert cn._extract_subagent_notification(txt) is None

    def test_end_before_start(self):
        assert cn._extract_subagent_notification("</subagent_notification><subagent_notification>") is None


class TestNormalizeSubagentNotification:
    def test_with_string_status(self):
        payload = {"content": '<subagent_notification>{"agent_path":"a/b","status":"completed"}</subagent_notification>'}
        ev = cn._normalize_subagent_notification(payload, PARENT)
        assert ev["codex_subagent_id"] == "a/b"
        tr = ev["message"]["content"][0]
        assert tr["content"] == "completed"
        assert tr["tool_use_id"] == "a/b"

    def test_with_non_string_status(self):
        payload = {"content": '<subagent_notification>{"agent_path":"a","status":{"k":1}}</subagent_notification>'}
        ev = cn._normalize_subagent_notification(payload, PARENT)
        assert json.loads(ev["message"]["content"][0]["content"]) == {"k": 1}

    def test_none_when_no_notification(self):
        assert cn._normalize_subagent_notification({"content": "plain text"}, PARENT) is None

    def test_missing_agent_path(self):
        payload = {"content": '<subagent_notification>{"status":"x"}</subagent_notification>'}
        ev = cn._normalize_subagent_notification(payload, PARENT)
        assert ev["codex_subagent_id"] == ""


# ---------------------------------------------------------------------------
# _normalize_response_message / _normalize_response_reasoning
# ---------------------------------------------------------------------------

class TestNormalizeResponseMessage:
    def test_assistant_text(self):
        payload = {"type": "message", "role": "assistant", "content": [{"text": "hello"}], "id": "m1"}
        ev = cn._normalize_response_message(payload, PARENT)
        assert ev["type"] == "assistant"
        assert _text_block(ev) == "hello"
        assert ev["uuid"] == cn._response_item_uuid(PARENT, payload)

    def test_assistant_empty_text_returns_none(self):
        # L661
        payload = {"type": "message", "role": "assistant", "content": [], "id": "m2"}
        assert cn._normalize_response_message(payload, PARENT) is None

    def test_non_assistant_routes_to_subagent_notification(self):
        payload = {"type": "message", "role": "user", "content": "plain"}
        # no notification tag -> None
        assert cn._normalize_response_message(payload, PARENT) is None

    def test_non_assistant_with_notification(self):
        notif = '<subagent_notification>{"agent_path":"x","status":"ok"}</subagent_notification>'
        payload = {"type": "message", "role": "user", "content": notif}
        ev = cn._normalize_response_message(payload, PARENT)
        assert ev is not None
        assert ev["codex_subagent_id"] == "x"


class TestNormalizeResponseReasoning:
    def test_basic(self):
        payload = {"type": "reasoning", "summary": [{"text": "think"}], "id": "r1"}
        ev = cn._normalize_response_reasoning(payload, PARENT)
        assert _thinking(ev) == "think"

    def test_summary_not_list(self):
        assert cn._normalize_response_reasoning({"type": "reasoning", "summary": "x"}, PARENT) is None

    def test_empty_summary(self):
        assert cn._normalize_response_reasoning({"type": "reasoning", "summary": []}, PARENT) is None

    def test_string_blocks(self):
        # L686-687
        payload = {"type": "reasoning", "summary": ["s1", {"summary": "s2"}], "id": "r2"}
        assert _thinking(cn._normalize_response_reasoning(payload, PARENT)) == "s1\ns2"

    def test_all_empty_text_returns_none(self):
        # L690
        payload = {"type": "reasoning", "summary": [{"text": ""}], "id": "r3"}
        assert cn._normalize_response_reasoning(payload, PARENT) is None

    def test_non_str_non_dict_block_skipped(self):
        # 686->681: block neither dict nor str -> elif false -> loop continues, empty -> None
        payload = {"type": "reasoning", "summary": [7, {"text": "ok"}], "id": "r4"}
        assert _thinking(cn._normalize_response_reasoning(payload, PARENT)) == "ok"
        assert cn._normalize_response_reasoning({"type": "reasoning", "summary": [7], "id": "r5"}, PARENT) is None


# ---------------------------------------------------------------------------
# native payload helpers
# ---------------------------------------------------------------------------

class TestNativePayloadLabel:
    def test_with_payload_type(self):
        assert cn._native_payload_label("response_item", {"type": "custom"}) == "response_item.custom"

    def test_without_payload_type(self):
        assert cn._native_payload_label("response_item", {}) == "response_item"


class TestNativePayloadText:
    def test_dict(self):
        txt = cn._native_payload_text("response_item", {"type": "custom", "a": 1})
        assert "response_item.custom" in txt
        assert "```json" in txt
        assert '"a": 1' in txt

    def test_non_dict_no_payload_type_in_label(self):
        txt = cn._native_payload_text("event_msg", [1, 2])
        assert txt.startswith("Codex native event_msg")

    def test_typeerror_fallback(self):
        # L712-713: nested object whose str() raises -> json.dumps(default=str)
        # raises TypeError -> body = str(payload) (dict str works via repr).
        class EvilStr:
            def __str__(self):
                raise TypeError("nope")

        txt = cn._native_payload_text("response_item", {"x": EvilStr()})
        assert "Codex native response_item" in txt
        assert "EvilStr" in txt


class TestNormalizeNativePayload:
    def test_dict_attaches_parent(self):
        ev = cn._normalize_native_payload("event_msg", {"type": "t", "parent_id": "p"}, PARENT)
        assert ev["parent_tool_use_id"] == "p"
        assert _text_block(ev).startswith("Codex native event_msg.t")

    def test_non_dict_no_parent(self):
        # L731
        ev = cn._normalize_native_payload("event_msg", [1, 2], PARENT)
        assert "parent_tool_use_id" not in ev


# ---------------------------------------------------------------------------
# event_msg normalizers (text / reasoning / patch_apply_end / notice)
# ---------------------------------------------------------------------------

class TestNormalizeEventMsgText:
    def test_basic(self):
        ev = cn._normalize_event_msg_text({"parent_id": "p"}, PARENT, "hello")
        assert _text_block(ev) == "hello"
        assert ev["parent_tool_use_id"] == "p"


class TestNormalizeEventMsgReasoning:
    def test_basic(self):
        ev = cn._normalize_event_msg_reasoning({}, PARENT, "think")
        assert _thinking(ev) == "think"


class TestNormalizeEventMsgPatchApplyEnd:
    def test_stdout_completed(self):
        ev = cn._normalize_event_msg_patch_apply_end(
            {"call_id": "c", "success": True, "stdout": "done"}, PARENT,
        )
        tr = ev["message"]["content"][0]
        assert tr["tool_use_id"] == "c"
        assert tr["content"] == "done"

    def test_non_str_output_jsonified(self):
        # L765
        ev = cn._normalize_event_msg_patch_apply_end(
            {"id": "c", "success": True, "stdout": {"k": 1}}, PARENT,
        )
        assert json.loads(ev["message"]["content"][0]["content"]) == {"k": 1}

    def test_failed_with_output_prefix(self):
        # L767
        ev = cn._normalize_event_msg_patch_apply_end(
            {"id": "c", "success": False, "stderr": "err"}, PARENT,
        )
        assert ev["message"]["content"][0]["content"] == "Patch failed\nerr"

    def test_no_output_success_true(self):
        # L769
        ev = cn._normalize_event_msg_patch_apply_end({"id": "c", "success": True}, PARENT)
        assert ev["message"]["content"][0]["content"] == "Patch applied"

    def test_no_output_success_false(self):
        ev = cn._normalize_event_msg_patch_apply_end({"id": "c", "success": False}, PARENT)
        assert ev["message"]["content"][0]["content"] == "Patch finished"

    def test_no_output_no_success(self):
        ev = cn._normalize_event_msg_patch_apply_end({"id": "c"}, PARENT)
        assert ev["message"]["content"][0]["content"] == "Patch finished"


# ---------------------------------------------------------------------------
# _duration_text / _event_msg_notice_text
# ---------------------------------------------------------------------------

class TestDurationText:
    def test_none_for_non_int(self):
        # L782-783
        assert cn._duration_text("1000") is None
        assert cn._duration_text(None) is None

    def test_none_for_under_one_second(self):
        # L783
        assert cn._duration_text(999) is None

    def test_seconds_only(self):
        # L791
        assert cn._duration_text(5000) == "5s"

    def test_minutes(self):
        # L790
        assert cn._duration_text(65_000) == "1m 5s"

    def test_hours(self):
        # L788
        assert cn._duration_text(3_661_000) == "1h 1m 1s"


class TestEventMsgNoticeText:
    def test_context_compacted(self):
        assert cn._event_msg_notice_text({"type": "context_compacted"}) == "Context compacted"

    def test_turn_aborted_interrupted(self):
        assert cn._event_msg_notice_text({"type": "turn_aborted", "reason": "interrupted"}) == "Turn interrupted"

    def test_turn_aborted_other(self):
        assert cn._event_msg_notice_text({"type": "turn_aborted", "reason": "x"}) == "Turn aborted"

    def test_turn_aborted_with_duration(self):
        assert cn._event_msg_notice_text(
            {"type": "turn_aborted", "reason": "x", "duration_ms": 65_000}
        ) == "Turn aborted after 1m 5s"

    def test_unknown_type(self):
        # L803
        assert cn._event_msg_notice_text({"type": "weird"}) == "Codex event"


class TestNormalizeEventMsgNotice:
    def test_basic(self):
        ev = cn._normalize_event_msg_notice(
            {"type": "context_compacted", "reason": None, "duration_ms": None}, PARENT,
        )
        assert ev["type"] == "lifecycle_notice"
        assert ev["data"]["kind"] == "context_compacted"
        assert ev["data"]["message"] == "Context compacted"


# ---------------------------------------------------------------------------
# _normalize_sub_agent_activity
# ---------------------------------------------------------------------------

class TestNormalizeSubAgentActivity:
    def test_updated_kind(self):
        # L838
        ev = cn._normalize_sub_agent_activity({"agent_path": "a/b", "kind": "updated"}, PARENT)
        assert ev["data"]["message"] == "Sub-agent a/b updated"
        assert "codex_subagent_id" not in ev

    def test_started_assigns_subagent_id(self):
        ev = cn._normalize_sub_agent_activity(
            {"agent_path": "a", "kind": "started", "agent_thread_id": "child-1", "event_id": "e1"}, PARENT,
        )
        assert ev["codex_subagent_id"] == "child-1"
        assert ev["parent_tool_use_id"] == "e1"

    def test_started_no_child_id(self):
        # L842
        ev = cn._normalize_sub_agent_activity({"agent_path": "a", "kind": "started", "event_id": "e1"}, PARENT)
        assert "codex_subagent_id" not in ev

    def test_started_child_id_non_string(self):
        ev = cn._normalize_sub_agent_activity(
            {"agent_path": "a", "kind": "started", "agent_thread_id": 5, "event_id": "e1"}, PARENT,
        )
        assert "codex_subagent_id" not in ev

    def test_started_no_event_id(self):
        # L844
        ev = cn._normalize_sub_agent_activity(
            {"agent_path": "a", "kind": "started", "agent_thread_id": "c1"}, PARENT,
        )
        assert "codex_subagent_id" not in ev

    def test_started_event_id_non_string(self):
        ev = cn._normalize_sub_agent_activity(
            {"agent_path": "a", "kind": "started", "agent_thread_id": "c1", "event_id": 9}, PARENT,
        )
        assert "codex_subagent_id" not in ev

    def test_started_parent_tool_use_id_mismatch(self):
        # L847: existing parent_tool_use_id != event_id -> skip
        ev = cn._normalize_sub_agent_activity(
            {
                "agent_path": "a", "kind": "started",
                "agent_thread_id": "c1", "event_id": "e1",
                "parent_tool_use_id": "other",
            },
            PARENT,
        )
        assert ev["parent_tool_use_id"] == "other"
        assert "codex_subagent_id" not in ev

    def test_started_with_existing_matching_event_id(self):
        # parent_tool_use_id == event_id -> proceeds, overwrites id same value
        ev = cn._normalize_sub_agent_activity(
            {
                "agent_path": "a", "kind": "started",
                "agent_thread_id": "c1", "event_id": "e1",
                "parent_tool_use_id": "e1",
            },
            PARENT,
        )
        assert ev["codex_subagent_id"] == "c1"
        assert ev["parent_tool_use_id"] == "e1"

    def test_agent_fallback(self):
        ev = cn._normalize_sub_agent_activity({"agent": "z"}, PARENT)
        assert ev["data"]["agent_path"] == "z"
        assert ev["data"]["status"] == "updated"


# ---------------------------------------------------------------------------
# _codex_agent_message_parts
# ---------------------------------------------------------------------------

class TestCodexAgentMessageParts:
    def test_message_type_and_payload(self):
        # L854-866
        text = "intro\nMessage Type: foo\nmiddle\nPayload: the real content"
        mt, payload = cn._codex_agent_message_parts({"content": text})
        assert mt == "foo"
        assert payload == "the real content"

    def test_message_type_no_payload_returns_full(self):
        text = "Message Type: bar\nno payload marker"
        mt, payload = cn._codex_agent_message_parts({"content": text})
        assert mt == "bar"
        assert payload == "Message Type: bar\nno payload marker".strip()

    def test_no_message_type_with_payload(self):
        text = "just text\nPayload: extracted"
        mt, payload = cn._codex_agent_message_parts({"content": text})
        assert mt == ""
        assert payload == "extracted"

    def test_empty_text(self):
        assert cn._codex_agent_message_parts({"content": ""}) == ("", "")
        assert cn._codex_agent_message_parts({}) == ("", "")


# ---------------------------------------------------------------------------
# _normalize_response_tool_call / _codex_update_plan_to_todos
# ---------------------------------------------------------------------------

class TestNormalizeResponseToolCall:
    def test_exec_command_renames_to_bash_and_maps_cmd(self):
        ev, tid = cn._normalize_response_tool_call(
            {"type": "function_call", "call_id": "x", "name": "exec_command", "arguments": {"cmd": "ls"}},
            PARENT,
        )
        tu = _tool_use(ev)
        assert tu["name"] == "Bash"
        assert tu["input"]["command"] == "ls"
        assert tu["input"]["cmd"] == "ls"  # original key preserved
        assert tid == "x"

    def test_exec_command_keeps_existing_command(self):
        ev, _ = cn._normalize_response_tool_call(
            {"type": "function_call", "call_id": "x", "name": "exec_command",
             "arguments": {"command": "echo"}},
            PARENT,
        )
        assert _tool_use(ev)["input"]["command"] == "echo"

    def test_spawn_agent_renames_to_agent(self):
        ev, _ = cn._normalize_response_tool_call(
            {"type": "function_call", "call_id": "x", "name": "spawn_agent",
             "arguments": {"prompt": "p", "agent_type": "r"}},
            PARENT,
        )
        tu = _tool_use(ev)
        assert tu["name"] == "Agent"
        assert tu["input"]["subagent_type"] == "r"

    def test_tool_search_call_renames(self):
        ev, _ = cn._normalize_response_tool_call(
            {"type": "tool_search_call", "call_id": "x", "name": "anything"}, PARENT,
        )
        assert _tool_use(ev)["name"] == "tool_search_tool"

    def test_update_plan_renames_to_todowrite(self):
        ev, _ = cn._normalize_response_tool_call(
            {"type": "function_call", "call_id": "x", "name": "update_plan",
             "arguments": {"plan": [{"step": "s", "status": "in_progress"}]}},
            PARENT,
        )
        tu = _tool_use(ev)
        assert tu["name"] == "TodoWrite"
        assert tu["input"]["todos"][0]["content"] == "s"

    def test_unknown_name_with_string_arguments(self):
        ev, tid = cn._normalize_response_tool_call(
            {"type": "custom_tool_call", "id": "y", "name": "weird", "arguments": '{"k":1}'},
            PARENT,
        )
        tu = _tool_use(ev)
        assert tu["name"] == "weird"
        assert tu["input"] == {"k": 1}
        assert tid == "y"

    def test_missing_name_defaults_unknown(self):
        ev, _ = cn._normalize_response_tool_call({"type": "function_call", "call_id": "z"}, PARENT)
        assert _tool_use(ev)["name"] == "unknown"

    def test_missing_call_id_generates(self):
        ev, tid = cn._normalize_response_tool_call(
            {"type": "function_call", "name": "n"}, PARENT,
        )
        uuid.UUID(tid)


class TestCodexUpdatePlanToTodos:
    def test_basic(self):
        out = cn._codex_update_plan_to_todos(
            {"plan": [{"step": "a", "status": "completed"}, {"step": "b"}]}
        )
        assert out == {
            "todos": [
                {"content": "a", "status": "completed", "activeForm": None},
                {"content": "b", "status": "pending", "activeForm": None},
            ]
        }

    def test_non_dict_entry_skipped(self):
        # L924
        out = cn._codex_update_plan_to_todos({"plan": ["bad", {"step": "ok"}]})
        assert len(out["todos"]) == 1
        assert out["todos"][0]["content"] == "ok"

    def test_empty_status_defaults_pending(self):
        # L927
        out = cn._codex_update_plan_to_todos({"plan": [{"step": "x", "status": ""}]})
        assert out["todos"][0]["status"] == "pending"

    def test_non_string_status_defaults_pending(self):
        out = cn._codex_update_plan_to_todos({"plan": [{"step": "x", "status": 5}]})
        assert out["todos"][0]["status"] == "pending"

    def test_non_dict_args(self):
        assert cn._codex_update_plan_to_todos(None) == {"todos": []}
        assert cn._codex_update_plan_to_todos("x") == {"todos": []}

    def test_non_list_plan(self):
        assert cn._codex_update_plan_to_todos({"plan": "x"}) == {"todos": []}


# ---------------------------------------------------------------------------
# _normalize_response_tool_result
# ---------------------------------------------------------------------------

class TestNormalizeResponseToolResult:
    def test_output_string(self):
        ev, tid = cn._normalize_response_tool_result(
            {"type": "function_call_output", "call_id": "r", "output": "done"}, PARENT,
        )
        assert _tool_result_content(ev) == "done"
        assert tid == "r"

    def test_empty_output_falls_back_to_result_key(self):
        ev, _ = cn._normalize_response_tool_result(
            {"call_id": "r", "output": "", "result": "fallback"}, PARENT,
        )
        assert _tool_result_content(ev) == "fallback"

    def test_empty_output_falls_back_to_content_key(self):
        ev, _ = cn._normalize_response_tool_result(
            {"call_id": "r", "output": "", "content": "c"}, PARENT,
        )
        assert _tool_result_content(ev) == "c"

    def test_empty_output_falls_back_to_tools_key(self):
        ev, _ = cn._normalize_response_tool_result(
            {"call_id": "r", "output": "", "tools": [{"a": 1}]}, PARENT,
        )
        assert json.loads(_tool_result_content(ev)) == [{"a": 1}]

    def test_non_string_output_jsonified(self):
        ev, _ = cn._normalize_response_tool_result({"call_id": "r", "output": {"k": 2}}, PARENT)
        assert json.loads(_tool_result_content(ev)) == {"k": 2}

    def test_missing_call_id_empty(self):
        ev, tid = cn._normalize_response_tool_result({"output": "x"}, PARENT)
        assert tid == ""

    def test_empty_output_no_fallback_keys(self):
        # 940->944: output == "" and none of result/content/tools present -> loop exhausts
        ev, tid = cn._normalize_response_tool_result({"call_id": "r", "output": ""}, PARENT)
        assert _tool_result_content(ev) == ""
        assert tid == "r"


# ---------------------------------------------------------------------------
# _normalize_response_item_event (dispatcher)
# ---------------------------------------------------------------------------

class TestNormalizeResponseItemEvent:
    def test_message(self):
        payload = {"type": "message", "role": "assistant", "content": [{"text": "hi"}], "id": "m"}
        ev = cn._normalize_response_item_event(payload, PARENT)
        assert ev["type"] == "assistant"
        assert _text_block(ev) == "hi"

    def test_reasoning(self):
        payload = {"type": "reasoning", "summary": [{"text": "t"}], "id": "r"}
        ev = cn._normalize_response_item_event(payload, PARENT)
        assert _thinking(ev) == "t"

    def test_function_call(self):
        payload = {"type": "function_call", "call_id": "f", "name": "exec_command", "arguments": {"cmd": "ls"}}
        ev = cn._normalize_response_item_event(payload, PARENT)
        assert _tool_use(ev)["name"] == "Bash"
        assert ev["uuid"] == cn._response_item_uuid(PARENT, payload, ":tool_use")

    def test_custom_tool_call_output(self):
        payload = {"type": "custom_tool_call_output", "call_id": "f", "output": "out"}
        ev = cn._normalize_response_item_event(payload, PARENT)
        assert _tool_result_content(ev) == "out"
        assert ev["uuid"] == cn._response_item_uuid(PARENT, payload, ":tool_result")

    def test_tool_search_output(self):
        payload = {"type": "tool_search_output", "call_id": "f", "output": "out"}
        ev = cn._normalize_response_item_event(payload, PARENT)
        assert _tool_result_content(ev) == "out"

    def test_web_search_call(self):
        payload = {"type": "web_search_call", "call_id": "w", "query": "q"}
        ev = cn._normalize_response_item_event(payload, PARENT)
        assert _tool_use(ev)["name"] == "WebSearch"
        assert ev["uuid"] == cn._response_item_uuid(PARENT, payload, ":web_search")

    def test_unknown_type_native_payload(self):
        payload = {"type": "mystery", "id": "u", "data": 1}
        ev = cn._normalize_response_item_event(payload, PARENT)
        assert _text_block(ev).startswith("Codex native response_item.mystery")
        assert ev["uuid"] == cn._response_item_uuid(PARENT, payload, ":native")

    def test_none_for_empty_assistant_message(self):
        payload = {"type": "message", "role": "assistant", "content": [], "id": "e"}
        assert cn._normalize_response_item_event(payload, PARENT) is None


# ---------------------------------------------------------------------------
# _normalize_error_item
# ---------------------------------------------------------------------------

class TestNormalizeErrorItem:
    def test_with_message(self):
        # L1010-1019
        ev = cn._normalize_error_item({"message": "crashed"}, PARENT)
        assert _text_block(ev) == "Error: crashed"
        assert ev["isStreamError"] is True
        assert ev["parentUuid"] == PARENT

    def test_missing_message(self):
        ev = cn._normalize_error_item({}, PARENT)
        assert _text_block(ev) == "Error: unknown error"


# ---------------------------------------------------------------------------
# _normalize_todo_list
# ---------------------------------------------------------------------------

class TestNormalizeTodoList:
    def test_status_progression(self):
        ev = cn._normalize_todo_list(
            {
                "id": "item_0",
                "items": [
                    {"text": "done one", "completed": True},
                    {"text": "active", "completed": False},
                    {"text": "later", "completed": False},
                ],
            },
            PARENT,
            "stable-uuid",
        )
        tu = _tool_use(ev)
        assert tu["name"] == "TodoWrite"
        assert tu["id"] == "item_0"
        todos = tu["input"]["todos"]
        assert todos[0] == {"content": "done one", "status": "completed", "activeForm": None}
        assert todos[1]["status"] == "in_progress"
        assert todos[2]["status"] == "pending"
        assert ev["uuid"] == "stable-uuid"

    def test_missing_id_generates(self):
        ev = cn._normalize_todo_list({"items": [{"text": "x", "completed": False}]}, PARENT, "u")
        uuid.UUID(_tool_use(ev)["id"])

    def test_items_not_list_returns_none(self):
        # L1044
        assert cn._normalize_todo_list({"items": "x"}, PARENT, "u") is None
        assert cn._normalize_todo_list({}, PARENT, "u") is None

    def test_non_dict_entry_skipped(self):
        # L1049
        ev = cn._normalize_todo_list(
            {"items": ["bad", {"text": "ok", "completed": False}]}, PARENT, "u",
        )
        todos = _tool_use(ev)["input"]["todos"]
        assert len(todos) == 1
        assert todos[0]["content"] == "ok"

    def test_first_incomplete_is_in_progress(self):
        ev = cn._normalize_todo_list(
            {"items": [{"text": "a", "completed": False}, {"text": "b", "completed": False}]},
            PARENT, "u",
        )
        todos = _tool_use(ev)["input"]["todos"]
        assert todos[0]["status"] == "in_progress"
        assert todos[1]["status"] == "pending"

    def test_missing_text_key(self):
        ev = cn._normalize_todo_list({"items": [{"completed": False}]}, PARENT, "u")
        assert _tool_use(ev)["input"]["todos"][0]["content"] == ""


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
