"""Unit coverage for session_local_projection.py.

Thin projection layer over todo_projection's two extractors
(extract_todos_from_normalized / extract_tasks_from_normalized). It forwards
each extractor's delta, and — when an extractor returns None but the agent's
message text carries the `<ALL_TASKS__DONE>` marker — marks every current item
in that collection completed. Pure dict shaping; no pydantic, no async, no
disk, no event bus.

Covers every branch of the four module functions directly, then locks the
public `project_event_fields` contract end to end (TodoWrite delta, TaskCreate
delta, marker-completes-both, no-op event).
"""
from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_BACKEND = _HERE.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import _test_home  # noqa: E402

_test_home.isolate("session-local-projection-test-")

import session_local_projection as proj  # noqa: E402
from todo_projection import (  # noqa: E402
    extract_tasks_from_normalized,
    extract_todos_from_normalized,
)

_MARKER = "<ALL_TASKS__DONE>"


# ── event builders ────────────────────────────────────────────────────────── #

def _todowrite_event(todos: list) -> dict:
    return {
        "data": {
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "TodoWrite",
                        "id": "tu_1",
                        "input": {"todos": todos},
                    }
                ]
            }
        }
    }


def _taskcreate_event(subject: str) -> dict:
    return {
        "data": {
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "TaskCreate",
                        "id": "tc_1",
                        "input": {"subject": subject},
                    }
                ]
            }
        }
    }


def _text_event(text: str) -> dict:
    return {"data": {"message": {"content": [{"type": "text", "text": text}]}}}


# ── _agent_message_text ───────────────────────────────────────────────────── #

def test_agent_message_text_string_content():
    assert proj._agent_message_text({"data": {"message": {"content": "hi"}}}) == "hi"


def test_agent_message_text_list_content_joins_text_blocks():
    event = {
        "data": {
            "message": {
                "content": [
                    {"type": "text", "text": "alpha"},
                    {"type": "text", "text": "bravo"},
                ]
            }
        }
    }
    assert proj._agent_message_text(event) == "alpha\nbravo"


def test_agent_message_text_list_content_filters_non_text_blocks():
    event = {
        "data": {
            "message": {
                "content": [
                    {"type": "tool_use", "text": "ignored"},  # wrong type
                    "not-a-dict",  # not a dict at all
                    {"type": "text"},  # text block missing `text` -> "" entry
                    {"type": "text", "text": "only-me"},
                ]
            }
        }
    }
    # Non-text / non-dict blocks are filtered out of the join; a text block
    # missing `text` still contributes an empty string (via `or ""`), so the
    # join keeps its leading newline.
    assert proj._agent_message_text(event) == "\nonly-me"


def test_agent_message_text_data_not_dict():
    assert proj._agent_message_text({"data": "nope"}) == ""
    assert proj._agent_message_text({"data": None}) == ""
    assert proj._agent_message_text({}) == ""


def test_agent_message_text_message_not_dict():
    assert proj._agent_message_text({"data": {"message": "nope"}}) == ""
    assert proj._agent_message_text({"data": {}}) == ""


def test_agent_message_text_content_not_str_or_list():
    assert proj._agent_message_text({"data": {"message": {"content": 7}}}) == ""
    assert proj._agent_message_text({"data": {"message": {"content": None}}}) == ""


# ── _all_tasks_done_items ─────────────────────────────────────────────────── #

def test_all_tasks_done_items_empty_returns_none():
    assert proj._all_tasks_done_items([]) is None


def test_all_tasks_done_items_marks_completed_and_filters_non_dicts():
    items = [
        {"content": "a", "status": "pending"},
        "not-a-dict",
        {"content": "b", "status": "in_progress"},
    ]
    result = proj._all_tasks_done_items(items)
    assert result == [
        {"content": "a", "status": "completed"},
        {"content": "b", "status": "completed"},
    ]
    # originals untouched (no mutation)
    assert items[0]["status"] == "pending"


def test_all_tasks_done_items_only_non_dicts_returns_empty_list():
    # Non-empty input -> not None; all filtered -> empty list (distinct from
    # the empty-input None branch).
    assert proj._all_tasks_done_items(["x", 1]) == []


# ── _project_collection ───────────────────────────────────────────────────── #

def test_project_collection_extractor_returns_delta_sets_it():
    fields: dict = {}
    proj._project_collection(
        fields,
        "current_todos",
        _text_event("anything"),
        [{"content": "old"}],
        lambda normalized, current: [{"content": "fresh"}],
    )
    assert fields == {"current_todos": [{"content": "fresh"}]}


def test_project_collection_marker_completes_current_items():
    fields: dict = {}
    current = [{"content": "a", "status": "pending"}, {"content": "b", "status": "pending"}]
    proj._project_collection(
        fields,
        "current_todos",
        _text_event(f"done now {_MARKER}"),
        current,
        lambda normalized, cur: None,  # extractor has no delta
    )
    assert fields == {
        "current_todos": [
            {"content": "a", "status": "completed"},
            {"content": "b", "status": "completed"},
        ]
    }


def test_project_collection_marker_with_empty_current_sets_nothing():
    # Marker present but `_all_tasks_done_items([])` is None -> key not set.
    fields: dict = {}
    proj._project_collection(
        fields,
        "current_todos",
        _text_event(_MARKER),
        [],
        lambda normalized, cur: None,
    )
    assert fields == {}


def test_project_collection_no_delta_no_marker_sets_nothing():
    fields: dict = {}
    proj._project_collection(
        fields,
        "current_todos",
        _text_event("plain text, no marker"),
        [{"content": "a"}],
        lambda normalized, cur: None,
    )
    assert fields == {}


def test_project_collection_extractor_delta_takes_precedence_over_marker():
    # Even with the marker present, a non-None extractor result wins.
    fields: dict = {}
    proj._project_collection(
        fields,
        "current_todos",
        _text_event(_MARKER),
        [{"content": "a", "status": "pending"}],
        lambda normalized, cur: [{"content": "delta"}],
    )
    assert fields == {"current_todos": [{"content": "delta"}]}


# ── project_event_fields (public contract) ────────────────────────────────── #

def test_project_event_fields_todowrite_delta():
    event = _todowrite_event([{"content": "do thing", "status": "pending"}])
    fields = proj.project_event_fields(event, [], [])
    assert fields["current_todos"] == [{"content": "do thing", "status": "pending", "activeForm": None}]
    # No task delta in this event -> tasks key absent.
    assert "current_tasks" not in fields


def test_project_event_fields_taskcreate_delta():
    event = _taskcreate_event("ship it")
    fields = proj.project_event_fields(event, [], [])
    assert len(fields["current_tasks"]) == 1
    assert fields["current_tasks"][0]["content"] == "ship it"
    # No todo delta in this event -> todos key absent.
    assert "current_todos" not in fields


def test_project_event_fields_marker_completes_both_collections():
    current_todos = [{"content": "t1", "status": "pending"}]
    current_tasks = [{"content": "k1", "status": "in_progress"}]
    fields = proj.project_event_fields(_text_event(_MARKER), current_todos, current_tasks)
    assert fields == {
        "current_todos": [{"content": "t1", "status": "completed"}],
        "current_tasks": [{"content": "k1", "status": "completed"}],
    }


def test_project_event_fields_noop_event_returns_empty_dict():
    # Plain text, no marker, no tool deltas -> nothing projected for either.
    fields = proj.project_event_fields(_text_event("hello"), [{"content": "x"}], [{"content": "y"}])
    assert fields == {}


def test_project_event_fields_does_not_mutate_current_inputs():
    current_todos = [{"content": "t1", "status": "pending"}]
    current_tasks = [{"content": "k1", "status": "in_progress"}]
    proj.project_event_fields(_text_event(_MARKER), current_todos, current_tasks)
    # The marker path builds fresh dicts; caller's items stay as they were.
    assert current_todos == [{"content": "t1", "status": "pending"}]
    assert current_tasks == [{"content": "k1", "status": "in_progress"}]


def test_project_event_fields_uses_real_extractors_for_deltas():
    # Sanity that the wired extractors (not lambdas) flow through: a TodoWrite
    # UNION-merges with existing current items rather than replacing them.
    current = [{"content": "keep", "status": "completed"}]
    event = _todowrite_event([{"content": "new", "status": "pending"}])
    fields = proj.project_event_fields(event, current, [])
    contents = {t["content"] for t in fields["current_todos"]}
    assert contents == {"keep", "new"}


def test_extractors_are_the_canonical_ones():
    # Locks the wiring: the module must forward to the real todo_projection
    # extractors, not a copy. Guards against a silent fork.
    assert proj.extract_todos_from_normalized is extract_todos_from_normalized
    assert proj.extract_tasks_from_normalized is extract_tasks_from_normalized
