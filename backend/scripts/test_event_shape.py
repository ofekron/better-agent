"""Unit tests for the pure event-shape helpers in event_shape.py.

These are deterministic pure-function tests: no backend, no home, no I/O.
Run with:

    cd backend && .venv/bin/python -m pytest scripts/test_event_shape.py -o addopts=""
"""

from __future__ import annotations

import sys
from pathlib import Path

backend = Path(__file__).resolve().parents[1]
if str(backend) not in sys.path:
    sys.path.insert(0, str(backend))

import event_shape as es  # noqa: E402


# ---------------------------------------------------------------------------
# event_uuid
# ---------------------------------------------------------------------------

def test_event_uuid_top_level_str() -> None:
    assert es.event_uuid({"uuid": "abc"}) == "abc"


def test_event_uuid_data_uuid() -> None:
    assert es.event_uuid({"data": {"uuid": "u1"}}) == "u1"


def test_event_uuid_manager_inner_data_uuid() -> None:
    ev = {"type": "manager_event", "data": {"event": {"data": {"uuid": "deep"}}}}
    assert es.event_uuid(ev) == "deep"


def test_event_uuid_top_level_beats_data() -> None:
    assert es.event_uuid({"uuid": "top", "data": {"uuid": "inner"}}) == "top"


def test_event_uuid_non_dict_returns_none() -> None:
    assert es.event_uuid(None) is None  # type: ignore[arg-type]
    assert es.event_uuid("x") is None  # type: ignore[arg-type]
    assert es.event_uuid(7) is None  # type: ignore[arg-type]


def test_event_uuid_empty_strings_skipped() -> None:
    assert es.event_uuid({"uuid": ""}) is None
    assert es.event_uuid({"data": {"uuid": ""}}) is None


def test_event_uuid_non_str_uuid_ignored() -> None:
    assert es.event_uuid({"uuid": 123}) is None
    assert es.event_uuid({"data": {}}) is None


def test_event_uuid_data_not_dict() -> None:
    assert es.event_uuid({"data": "nope"}) is None


def test_event_uuid_inner_not_dict() -> None:
    # data.event present but not a dict -> falls through to None
    assert es.event_uuid({"data": {"event": "x"}}) is None


def test_event_uuid_inner_data_missing_uuid() -> None:
    assert es.event_uuid({"data": {"event": {"data": {}}}}) is None


def test_event_uuid_inner_dict_but_inner_data_not_dict() -> None:
    # inner.event is a dict but inner.event.data is absent/non-dict
    assert es.event_uuid({"data": {"event": {"other": 1}}}) is None


def test_event_alias_is_same_function() -> None:
    assert es._event_uuid is es.event_uuid


# ---------------------------------------------------------------------------
# frontend_event_from_journal_row
# ---------------------------------------------------------------------------

def test_frontend_row_non_render_type_dropped() -> None:
    assert es.frontend_event_from_journal_row({"type": "user_message", "data": {}}) is None


def test_frontend_row_metadata_dropped() -> None:
    row = {"type": "agent_message", "data": {"type": "ai-title", "title": "x"}}
    assert es.frontend_event_from_journal_row(row) is None


def test_frontend_row_codex_envelope_dropped() -> None:
    row = {"type": "agent_message", "data": {"type": "response_item"}}
    assert es.frontend_event_from_journal_row(row) is None


def test_frontend_row_plain_render_event_wrapped() -> None:
    row = {"type": "agent_message", "data": {"type": "assistant", "uuid": "a"}}
    out = es.frontend_event_from_journal_row(row)
    assert out == {"type": "agent_message", "data": {"type": "assistant", "uuid": "a"}}


def test_frontend_row_include_seq() -> None:
    row = {"type": "steer_prompt", "data": {"x": 1}, "seq": 42}
    out = es.frontend_event_from_journal_row(row, include_seq=True)
    assert out == {"type": "steer_prompt", "data": {"x": 1}, "seq": 42}


def test_frontend_row_include_seq_false_omits() -> None:
    row = {"type": "steer_prompt", "data": {"x": 1}, "seq": 42}
    out = es.frontend_event_from_journal_row(row, include_seq=False)
    assert "seq" not in out


def test_frontend_row_manager_event_unwrapped() -> None:
    inner = {"type": "agent_message", "data": {"type": "assistant", "uuid": "m"}}
    row = {"type": "manager_event", "data": {"event": inner}}
    assert es.frontend_event_from_journal_row(row) == inner


def test_frontend_row_manager_event_missing_inner_dropped() -> None:
    row = {"type": "manager_event", "data": {"event": None}}
    assert es.frontend_event_from_journal_row(row) is None
    row2 = {"type": "manager_event", "data": {}}
    assert es.frontend_event_from_journal_row(row2) is None


def test_frontend_row_data_defaults_to_empty_dict() -> None:
    out = es.frontend_event_from_journal_row({"type": "agent_message"})
    assert out == {"type": "agent_message", "data": {}}


# ---------------------------------------------------------------------------
# frontend_events_from_journal_rows
# ---------------------------------------------------------------------------

def test_frontend_rows_dedup_replace_same_uuid() -> None:
    rows = [
        {"type": "agent_message", "data": {"uuid": "u", "n": 1}},
        {"type": "user_message", "data": {}},  # dropped
        {"type": "agent_message", "data": {"uuid": "u", "n": 2}},  # replaces
        {"type": "agent_message", "data": {"uuid": "v", "n": 9}},
    ]
    out = es.frontend_events_from_journal_rows(rows)
    assert len(out) == 2
    assert out[0]["data"]["n"] == 2  # replaced in place
    assert out[1]["data"]["n"] == 9


def test_frontend_rows_anonymous_events_appended() -> None:
    rows = [
        {"type": "steer_prompt", "data": {}},  # no uuid
        {"type": "model_switched", "data": {}},  # no uuid
    ]
    out = es.frontend_events_from_journal_rows(rows)
    assert len(out) == 2


def test_frontend_rows_all_dropped_empty() -> None:
    rows = [{"type": "user_message", "data": {}}]
    assert es.frontend_events_from_journal_rows(rows) == []


# ---------------------------------------------------------------------------
# is_synthetic_event / strip_synthetic_events
# ---------------------------------------------------------------------------

def _synth(**over: object) -> dict:
    base = {
        "type": "agent_message",
        "data": {
            "type": "assistant",
            "message": {"model": "<synthetic>"},
        },
    }
    base["data"].update(over)  # type: ignore[union-attr]
    return base


def test_is_synthetic_true() -> None:
    assert es.is_synthetic_event(_synth()) is True


def test_is_synthetic_false_when_api_error() -> None:
    assert es.is_synthetic_event(_synth(isApiErrorMessage=True)) is False


def test_is_synthetic_false_when_not_synthetic_model() -> None:
    ev = _synth()
    ev["data"]["message"]["model"] = "gpt-x"  # type: ignore[index]
    assert es.is_synthetic_event(ev) is False


def test_is_synthetic_non_dict_false() -> None:
    assert es.is_synthetic_event(None) is False  # type: ignore[arg-type]
    assert es.is_synthetic_event("x") is False  # type: ignore[arg-type]


def test_is_synthetic_data_not_dict_false() -> None:
    assert es.is_synthetic_event({"type": "agent_message", "data": "x"}) is False


def test_is_synthetic_manager_wrap_inner_synthetic() -> None:
    ev = {"type": "manager_event", "data": {"event": _synth()}}
    assert es.is_synthetic_event(ev) is True


def test_is_synthetic_manager_wrap_inner_not_dict() -> None:
    ev = {"type": "manager_event", "data": {"event": None}}
    assert es.is_synthetic_event(ev) is False
    ev2 = {"type": "manager_event", "data": {}}
    assert es.is_synthetic_event(ev2) is False


def test_is_synthetic_wrong_data_type() -> None:
    ev = {
        "type": "agent_message",
        "data": {"type": "user", "message": {"model": "<synthetic>"}},
    }
    assert es.is_synthetic_event(ev) is False


def test_is_synthetic_message_not_dict() -> None:
    ev = {"type": "agent_message", "data": {"type": "assistant", "message": "x"}}
    assert es.is_synthetic_event(ev) is False


def test_strip_synthetic_removes_only_synthetic() -> None:
    real = {"type": "agent_message", "data": {"type": "assistant", "message": {"model": "gpt"}}}
    out = es.strip_synthetic_events([_synth(), real, _synth()])
    assert out == [real]


# ---------------------------------------------------------------------------
# is_metadata_event
# ---------------------------------------------------------------------------

def test_is_metadata_agent_data_type() -> None:
    for t in ("system", "queue-operation", "last-prompt", "attachment",
              "ai-title", "file-history-snapshot", "file-history-delta", "mode"):
        assert es.is_metadata_event({"type": "agent_message", "data": {"type": t}}) is True


def test_is_metadata_provider_envelope_type() -> None:
    for t in ("response_item", "event_msg", "session_meta", "turn_context",
              "compacted", "thread.started"):
        assert es.is_metadata_event({"type": "agent_message", "data": {"type": t}}) is True


def test_is_metadata_false_for_renderable() -> None:
    assert es.is_metadata_event({"type": "agent_message", "data": {"type": "assistant"}}) is False


def test_is_metadata_non_dict_false() -> None:
    assert es.is_metadata_event(None) is False  # type: ignore[arg-type]


def test_is_metadata_data_not_dict_false() -> None:
    assert es.is_metadata_event({"type": "agent_message", "data": "x"}) is False


def test_is_metadata_worker_wrap_inner_metadata() -> None:
    inner = {"type": "agent_message", "data": {"type": "ai-title"}}
    assert es.is_metadata_event({"type": "worker_event", "data": {"event": inner}}) is True


def test_is_metadata_manager_wrap_inner_not_dict() -> None:
    assert es.is_metadata_event({"type": "manager_event", "data": {"event": None}}) is False
    assert es.is_metadata_event({"type": "worker_event", "data": {}}) is False


# ---------------------------------------------------------------------------
# _text_units
# ---------------------------------------------------------------------------

def _msg_event(content: object, **over: object) -> dict:
    data: dict = {"type": "assistant", "message": {"content": content, "model": "gpt"}}
    data.update(over)
    return {"type": "agent_message", "data": data}


def test_text_units_non_dict_empty() -> None:
    assert es._text_units(None) == []  # type: ignore[arg-type]


def test_text_units_wrong_type_empty() -> None:
    assert es._text_units({"type": "manager_event", "data": {}}) == []


def test_text_units_wrong_data_type_empty() -> None:
    ev = {"type": "agent_message", "data": {"type": "user", "message": {"content": "x"}}}
    assert es._text_units(ev) == []


def test_text_units_parent_tool_use_id_empty() -> None:
    ev = _msg_event("hello", parent_tool_use_id="tool_1")
    assert es._text_units(ev) == []


def test_text_units_stream_error_empty() -> None:
    ev = _msg_event("hello", isStreamError=True)
    assert es._text_units(ev) == []


def test_text_units_synthetic_message_empty() -> None:
    ev = {
        "type": "agent_message",
        "data": {"type": "assistant", "message": {"content": "x", "model": "<synthetic>"}},
    }
    assert es._text_units(ev) == []


def test_text_units_synthetic_with_api_error_kept() -> None:
    # synthetic marker is ignored when it's an API error message
    ev = {
        "type": "agent_message",
        "data": {
            "type": "assistant",
            "message": {"content": "boom", "model": "<synthetic>"},
            "isApiErrorMessage": True,
            "uuid": "u",
        },
    }
    units = es._text_units(ev)
    assert units == [("text", "u", ["boom"], False, "")]


def test_text_units_message_not_dict_empty() -> None:
    ev = {"type": "agent_message", "data": {"type": "assistant", "message": "nope"}}
    assert es._text_units(ev) == []


def test_text_units_string_content() -> None:
    ev = _msg_event("plain", uuid="u1")
    assert es._text_units(ev) == [("text", "u1", ["plain"], False, "")]


def test_text_units_empty_string_content_no_units() -> None:
    ev = _msg_event("", uuid="u1")
    assert es._text_units(ev) == []


def test_text_units_content_not_list_or_str_empty() -> None:
    ev = _msg_event(123, uuid="u1")  # type: ignore[arg-type]
    assert es._text_units(ev) == []


def test_text_units_list_text_and_boundary() -> None:
    content = [
        {"type": "text", "text": "alpha"},
        {"type": "tool_use", "name": "Bash"},  # boundary
        {"type": "text", "text": "beta"},
    ]
    ev = _msg_event(content, uuid="u1", final_answer=True, final_answer_origin="worker-1")
    units = es._text_units(ev)
    assert units == [
        ("text", "u1", ["alpha"], True, "worker-1"),
        ("boundary", None, None, False, ""),
        ("text", "u1", ["beta"], True, "worker-1"),
    ]


def test_text_units_text_block_empty_or_non_str_skipped() -> None:
    content = [
        {"type": "text", "text": ""},  # skipped (empty)
        {"type": "text", "text": 5},   # skipped (non-str)
        {"type": "text", "text": "kept"},
    ]
    ev = _msg_event(content, uuid="u1")
    units = es._text_units(ev)
    assert units == [("text", "u1", ["kept"], False, "")]


def test_text_units_non_text_block_is_boundary() -> None:
    content = [{"type": "thinking"}, {"type": "text", "text": "x"}]
    ev = _msg_event(content, uuid="u1")
    units = es._text_units(ev)
    assert units[0] == ("boundary", None, None, False, "")
    assert units[1] == ("text", "u1", ["x"], False, "")


def test_text_units_non_dict_block_is_boundary() -> None:
    content = ["rawstr", {"type": "text", "text": "x"}]  # type: ignore[list-item]
    ev = _msg_event(content, uuid="u1")
    units = es._text_units(ev)
    assert units[0][0] == "boundary"


def test_text_units_final_answer_origin_non_str_coerced() -> None:
    ev = _msg_event("hi", uuid="u1", final_answer=True, final_answer_origin=99)  # type: ignore[arg-type]
    units = es._text_units(ev)
    assert units[0][4] == ""  # origin coerced to ""


def test_text_units_no_uuid_anonymous() -> None:
    ev = _msg_event("hi")
    units = es._text_units(ev)
    assert units[0][1] is None


def test_text_units_data_none_object() -> None:
    ev = {"type": "agent_message", "data": None}  # type: ignore[dict-item]
    assert es._text_units(ev) == []


# ---------------------------------------------------------------------------
# _final_marked_text
# ---------------------------------------------------------------------------

def test_final_marked_single_no_origin_plain() -> None:
    units = [("text", "u1", ["hello world"], True, "")]
    assert es._final_marked_text(units) == "hello world"


def test_final_marked_multiple_labeled_by_origin() -> None:
    units = [
        ("text", "u1", ["main answer"], True, ""),
        ("text", "u2", ["worker reply"], True, "worker-1"),
    ]
    out = es._final_marked_text(units)
    assert "[final answer · main agent]" in out
    assert "[final answer · worker-1]" in out
    assert "main answer" in out
    assert "worker reply" in out


def test_final_marked_dedup_identical_origin_text() -> None:
    units = [
        ("text", "u1", ["same"], True, "worker-1"),
        ("text", "u2", ["same"], True, "worker-1"),  # identical (origin, text)
    ]
    out = es._final_marked_text(units)
    # collapsed to a single labeled block
    assert out.count("[final answer") == 1


def test_final_marked_no_finals_empty() -> None:
    units = [("text", "u1", ["x"], False, "")]
    assert es._final_marked_text(units) == ""


def test_final_marked_last_snapshot_per_uuid_wins() -> None:
    units = [
        ("text", "u1", ["old"], True, ""),
        ("text", "u1", ["new"], True, ""),
    ]
    assert es._final_marked_text(units) == "new"


def test_final_marked_empty_text_part_skipped_in_label() -> None:
    units = [("text", "u1", ["   "], True, "worker-1")]
    # text strips to empty -> skipped -> falls to empty result
    assert es._final_marked_text(units) == ""


def test_final_marked_anonymous_with_origin() -> None:
    units = [("text", None, ["ghost"], True, "worker-1")]
    out = es._final_marked_text(units)
    assert "[final answer · worker-1]" in out
    assert "ghost" in out


# ---------------------------------------------------------------------------
# _collect_text_units (boundary between different uuids)
# ---------------------------------------------------------------------------

def test_collect_inserts_boundary_between_different_uuids() -> None:
    events = [
        _msg_event("a", uuid="u1"),
        _msg_event("b", uuid="u2"),
    ]
    units = es._collect_text_units(events)
    kinds = [u[0] for u in units]
    assert kinds == ["text", "boundary", "text"]


def test_collect_no_boundary_same_uuid() -> None:
    events = [_msg_event("a", uuid="u1"), _msg_event("b", uuid="u1")]
    units = es._collect_text_units(events)
    assert all(u[0] == "text" for u in units)
    assert len(units) == 2


def test_collect_anonymous_no_boundary_insert() -> None:
    # anonymous (no uuid) events never trigger a boundary insert
    events = [_msg_event("a"), _msg_event("b")]
    units = es._collect_text_units(events)
    assert [u[0] for u in units] == ["text", "text"]


def test_collect_event_with_no_units_skips_uuid_tracking() -> None:
    # a non-text event (no units) between two text events does not break the
    # same-uuid streak and does not insert a boundary
    events = [
        _msg_event("a", uuid="u1"),
        {"type": "manager_event", "data": {}},  # yields no units
        _msg_event("b", uuid="u1"),
    ]
    units = es._collect_text_units(events)
    assert [u[0] for u in units] == ["text", "text"]


def test_trailing_run_text_skips_unit_with_empty_parts() -> None:
    # defensive guard: a non-boundary unit with falsy parts is skipped, not
    # appended, and does not stop the trailing run.
    units = [
        ("text", "u1", ["kept"], False, ""),
        ("text", "u2", [], False, ""),  # falsy parts -> continue
        ("text", None, None, False, ""),  # falsy parts -> continue
    ]
    assert es._trailing_run_text(units) == "kept"


# ---------------------------------------------------------------------------
# extract_output_text / extract_trailing_output_text
# ---------------------------------------------------------------------------

def test_extract_output_text_prefers_final_mark() -> None:
    events = [
        _msg_event("final answer", uuid="u1", final_answer=True),
        _msg_event("trailing noise", uuid="u2"),
    ]
    assert es.extract_output_text(events) == "final answer"


def test_extract_output_text_falls_back_to_trailing_run() -> None:
    events = [_msg_event("first", uuid="u1"), _msg_event("last", uuid="u2")]
    assert es.extract_output_text(events) == "last"


def test_extract_output_text_empty() -> None:
    assert es.extract_output_text([]) == ""


def test_extract_output_text_trailing_after_tool_boundary() -> None:
    content = [{"type": "text", "text": "before"}, {"type": "tool_use"}, {"type": "text", "text": "after"}]
    events = [_msg_event(content, uuid="u1")]
    # trailing run is only "after" (boundary stops the reversed scan at tool)
    assert es.extract_output_text(events) == "after"


def test_extract_trailing_output_text_ignores_final_mark() -> None:
    events = [
        _msg_event("final answer", uuid="u1", final_answer=True),
        _msg_event("error tail", uuid="u2"),
    ]
    # trailing-run heuristic sees the literally-last text, ignoring the final mark
    assert es.extract_trailing_output_text(events) == "error tail"


def test_extract_trailing_output_text_streaming_last_snapshot_wins() -> None:
    events = [
        _msg_event("partial", uuid="u1"),
        _msg_event("partial more", uuid="u1"),
    ]
    assert es.extract_trailing_output_text(events) == "partial more"


def test_extract_trailing_output_text_anonymous_kept() -> None:
    events = [_msg_event("a"), _msg_event("b")]
    # both anonymous in the same trailing run -> concatenated
    assert es.extract_trailing_output_text(events) == "a b"


# ---------------------------------------------------------------------------
# has_final_answer_event / has_assistant_text
# ---------------------------------------------------------------------------

def test_has_final_answer_event_true() -> None:
    assert es.has_final_answer_event([{"data": {"final_answer": True}}]) is True


def test_has_final_answer_event_false() -> None:
    assert es.has_final_answer_event([{"data": {"final_answer": False}}]) is False
    assert es.has_final_answer_event([{"data": {}}]) is False
    assert es.has_final_answer_event([]) is False


def test_has_final_answer_event_non_dict_skipped() -> None:
    assert es.has_final_answer_event(["x", None, {"data": {"final_answer": True}}]) is True  # type: ignore[list-item]


def test_has_final_answer_event_data_not_dict() -> None:
    assert es.has_final_answer_event([{"data": "nope"}]) is False


def test_has_assistant_text_true() -> None:
    assert es.has_assistant_text([_msg_event("hi", uuid="u1")]) is True


def test_has_assistant_text_false() -> None:
    assert es.has_assistant_text([]) is False
    assert es.has_assistant_text([{"type": "manager_event"}]) is False


# ---------------------------------------------------------------------------
# project_content_snapshot
# ---------------------------------------------------------------------------

def test_project_content_snapshot_uses_projected() -> None:
    events = [_msg_event("new", uuid="u1", final_answer=True)]
    assert es.project_content_snapshot(events, "old") == "new"


def test_project_content_snapshot_keeps_current_when_empty() -> None:
    # events ending on a boundary -> empty projection -> current wins
    events = [_msg_event([{"type": "tool_use"}], uuid="u1")]
    assert es.project_content_snapshot(events, "existing") == "existing"


def test_project_content_snapshot_empty_current_when_nothing() -> None:
    events = [_msg_event([{"type": "tool_use"}], uuid="u1")]
    assert es.project_content_snapshot(events, None) == ""


def test_project_content_snapshot_strips_synthetic() -> None:
    synth = _synth(uuid="s")
    real = _msg_event("real", uuid="u1", final_answer=True)
    assert es.project_content_snapshot([synth, real], None) == "real"


def test_project_content_snapshot_none_events() -> None:
    assert es.project_content_snapshot(None, "cur") == "cur"  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# extract_subagent_types
# ---------------------------------------------------------------------------

def _tool_use_event(blocks: list) -> dict:
    return {
        "type": "agent_message",
        "data": {"type": "assistant", "message": {"content": blocks}},
    }


def test_extract_subagent_types_agent_and_task() -> None:
    events = [
        _tool_use_event([
            {"type": "tool_use", "name": "Agent", "input": {"subagent_type": "researcher"}},
            {"type": "tool_use", "name": "Task", "input": {}},  # falls back to name
        ]),
    ]
    assert es.extract_subagent_types(events) == ["researcher", "Task"]


def test_extract_subagent_types_unique_preserves_order() -> None:
    events = [
        _tool_use_event([
            {"type": "tool_use", "name": "Agent", "input": {"subagent_type": "x"}},
            {"type": "tool_use", "name": "Agent", "input": {"subagent_type": "x"}},  # dup
            {"type": "tool_use", "name": "Agent", "input": {"subagent_type": "y"}},
        ]),
    ]
    assert es.extract_subagent_types(events) == ["x", "y"]


def test_extract_subagent_types_ignores_other_tools() -> None:
    events = [
        _tool_use_event([{"type": "tool_use", "name": "Bash", "input": {}}]),
    ]
    assert es.extract_subagent_types(events) == []


def test_extract_subagent_types_ignores_non_tool_blocks() -> None:
    events = [
        _tool_use_event([{"type": "text", "text": "hi"}, {"type": "thinking"}]),
    ]
    assert es.extract_subagent_types(events) == []


def test_extract_subagent_types_skips_non_dict_block() -> None:
    events = [_tool_use_event(["strblock", 5])]  # type: ignore[list-item]
    assert es.extract_subagent_types(events) == []


def test_extract_subagent_types_wrong_event_shape() -> None:
    assert es.extract_subagent_types([{"type": "manager_event"}]) == []
    assert es.extract_subagent_types([{"type": "agent_message", "data": {"type": "user"}}]) == []
    assert es.extract_subagent_types([{"type": "agent_message", "data": {"type": "assistant", "message": "x"}}]) == []
    assert es.extract_subagent_types([{"type": "agent_message", "data": {"type": "assistant", "message": {"content": "str"}}}]) == []


def test_extract_subagent_types_non_dict_event_skipped() -> None:
    assert es.extract_subagent_types([None, "x", {"type": "agent_message"}]) == []  # type: ignore[list-item]


def test_extract_subagent_types_null_input() -> None:
    events = [
        _tool_use_event([{"type": "tool_use", "name": "Agent", "input": None}]),
    ]
    # input None -> subagent_type falls back to name "Agent"
    assert es.extract_subagent_types(events) == ["Agent"]


def test_extract_subagent_types_data_none() -> None:
    ev = {"type": "agent_message", "data": None}  # type: ignore[dict-item]
    assert es.extract_subagent_types([ev]) == []


if __name__ == "__main__":
    sys.exit(__import__("pytest").main([__file__, "-o", "addopts=", "-q"]))
