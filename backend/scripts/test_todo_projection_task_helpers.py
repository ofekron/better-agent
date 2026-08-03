"""Focused unit coverage for todo_projection's pure task/parse helpers.

These private helpers (`_parse_task_id_from_result`, `_apply_task_create`,
`_apply_task_result`, `_apply_task_update`, the `_find_*_block` locators) are
deterministic, side-effect-free list/dict transformations with many error
branches that the public-path suite in test_todos_extraction.py does not fully
reach. They are tested directly here — todo_projection has only stdlib imports
(hashlib/json/re), so no home isolation or backend process is needed.

The non-mutation invariant ("never mutates current or its items") is asserted
on every mutating helper: a deep copy is taken before each call and compared.
"""
from __future__ import annotations

import copy

import todo_projection as tp


# ── _parse_task_id_from_result ───────────────────────────────────


def test_parse_task_id_none():
    assert tp._parse_task_id_from_result(None) is None


def test_parse_task_id_list_with_text_block():
    assert tp._parse_task_id_from_result([{"type": "text", "text": "ID: 7"}]) == "7"


def test_parse_task_id_list_with_content_block():
    assert tp._parse_task_id_from_result([{"content": "task 3"}]) == "3"


def test_parse_task_id_list_no_text_returns_none():
    assert tp._parse_task_id_from_result([{"type": "text"}, {"foo": "bar"}]) is None


def test_parse_task_id_empty_list():
    assert tp._parse_task_id_from_result([]) is None


def test_parse_task_id_dict_task_id():
    assert tp._parse_task_id_from_result({"task_id": 12}) == "12"


def test_parse_task_id_dict_camel_case():
    assert tp._parse_task_id_from_result({"taskId": "abc"}) == "abc"


def test_parse_task_id_dict_without_id():
    assert tp._parse_task_id_from_result({"other": 1}) is None


def test_parse_task_id_int():
    assert tp._parse_task_id_from_result(5) == "5"


def test_parse_task_id_float_truncates():
    assert tp._parse_task_id_from_result(3.9) == "3"


def test_parse_task_id_plain_digit_string():
    assert tp._parse_task_id_from_result("  42  ") == "42"


def test_parse_task_id_id_keyword_patterns():
    assert tp._parse_task_id_from_result("ID: 100") == "100"
    assert tp._parse_task_id_from_result("task 8") == "8"
    assert tp._parse_task_id_from_result("id 9") == "9"


def test_parse_task_id_fallback_first_number():
    assert tp._parse_task_id_from_result("see ref 55 done") == "55"


def test_parse_task_id_no_digits():
    assert tp._parse_task_id_from_result("no digits here!") is None
    assert tp._parse_task_id_from_result("   ") is None


# ── _apply_task_create ───────────────────────────────────────────


def test_task_create_missing_subject_returns_none():
    assert tp._apply_task_create([], {"activeForm": "x"}, "tu_1") is None


def test_task_create_non_string_subject_returns_none():
    assert tp._apply_task_create([], {"subject": 5}, "tu_1") is None


def test_task_create_appends_new_pending_item():
    out = tp._apply_task_create([], {"subject": "  Buy milk  ", "activeForm": "Buying"}, "tu_1")
    assert out == [{
        "content": "Buy milk",
        "status": "pending",
        "activeForm": "Buying",
        "source_id": "tc:" + tp._content_hash_source_id({"subject": "  Buy milk  "}),
        "tool_use_id": "tu_1",
    }]


def test_task_create_dedup_by_content_is_noop():
    existing = [{"content": "Dupe", "status": "pending", "activeForm": None,
                 "source_id": "x", "tool_use_id": "tu_0"}]
    assert tp._apply_task_create(existing, {"subject": "Dupe"}, "tu_1") is None


def test_task_create_does_not_mutate_current():
    current = []
    snapshot = copy.deepcopy(current)
    tp._apply_task_create(current, {"subject": "New"}, "tu_1")
    assert current == snapshot


# ── _apply_task_result ───────────────────────────────────────────


def test_task_result_no_parseable_id_returns_none():
    current = [{"content": "A", "tool_use_id": "tu_1"}]
    assert tp._apply_task_result(current, "tu_1", "no digits") is None


def test_task_result_stamps_task_id_on_match():
    current = [{"content": "A", "status": "pending", "tool_use_id": "tu_1", "task_id": None}]
    out = tp._apply_task_result(current, "tu_1", "ID: 9")
    assert out == [{"content": "A", "status": "pending", "tool_use_id": "tu_1", "task_id": "9"}]


def test_task_result_no_matching_tool_use_id():
    current = [{"content": "A", "tool_use_id": "tu_other"}]
    assert tp._apply_task_result(current, "tu_1", "9") is None


def test_task_result_already_has_task_id_skipped():
    current = [{"content": "A", "tool_use_id": "tu_1", "task_id": "existing"}]
    assert tp._apply_task_result(current, "tu_1", "9") is None


def test_task_result_does_not_mutate_current():
    current = [{"content": "A", "tool_use_id": "tu_1"}]
    snapshot = copy.deepcopy(current)
    tp._apply_task_result(current, "tu_1", "9")
    assert current == snapshot


# ── _apply_task_update ───────────────────────────────────────────


def _item(content="A", status="pending", task_id="1", active_form=None):
    return {"content": content, "status": status, "task_id": task_id, "activeForm": active_form}


def test_task_update_missing_task_id_returns_none():
    assert tp._apply_task_update([_item()], {"status": "completed"}) is None


def test_task_update_exact_task_id_status():
    out = tp._apply_task_update([_item(status="pending", task_id="1")], {"taskId": "1", "status": "completed"})
    assert out == [_item(status="completed", task_id="1")]


def test_task_update_exact_task_id_active_form():
    out = tp._apply_task_update([_item(task_id="1")], {"taskId": "1", "activeForm": "Running"})
    assert out[0]["activeForm"] == "Running"


def test_task_update_exact_task_id_subject_rewrites_content():
    out = tp._apply_task_update([_item(content="old", task_id="1")], {"taskId": "1", "subject": "  new  "})
    assert out[0]["content"] == "new"


def test_task_update_exact_task_id_deleted_removes_item():
    out = tp._apply_task_update([_item("A", task_id="1"), _item("B", task_id="2")],
                                {"taskId": "1", "status": "deleted"})
    assert out == [_item("B", task_id="2")]


def test_task_update_subject_match_when_no_exact_id():
    out = tp._apply_task_update([_item(content="Find me", task_id="9")],
                                {"taskId": "1", "subject": "Find me", "status": "completed"})
    assert out[0]["status"] == "completed"


def test_task_update_subject_match_deleted_removes():
    out = tp._apply_task_update([_item(content="Drop", task_id="9")],
                                {"taskId": "1", "subject": "Drop", "status": "deleted"})
    assert out == []


def test_task_update_status_heuristic_in_progress_from_pending():
    out = tp._apply_task_update([_item(status="pending", task_id="9")],
                                {"taskId": "1", "status": "in_progress"})
    assert out[0]["status"] == "in_progress"


def test_task_update_status_heuristic_completed_from_in_progress():
    out = tp._apply_task_update([_item(status="in_progress", task_id="9")],
                                {"taskId": "1", "status": "completed"})
    assert out[0]["status"] == "completed"


def test_task_update_delete_removes_first_non_deleted():
    out = tp._apply_task_update(
        [_item("A", status="deleted", task_id="9"), _item("B", status="pending", task_id="10")],
        {"taskId": "1", "status": "deleted"},
    )
    assert out == [_item("A", status="deleted", task_id="9")]


def test_task_update_no_match_returns_none():
    out = tp._apply_task_update([_item(status="completed", task_id="9")],
                                {"taskId": "1", "status": "in_progress"})
    assert out is None


def test_task_update_does_not_mutate_current():
    current = [_item(status="pending", task_id="1")]
    snapshot = copy.deepcopy(current)
    tp._apply_task_update(current, {"taskId": "1", "status": "completed"})
    assert current == snapshot


# ── _find_tool_use_block / _find_todowrite_block ─────────────────


def _event(blocks):
    return {"data": {"message": {"content": blocks}}}


def test_find_tool_use_block_matches_name():
    block = {"type": "tool_use", "name": "TodoWrite", "id": "tu", "input": {}}
    assert tp._find_tool_use_block(_event([block]), "TodoWrite") is block


def test_find_tool_use_block_skips_non_matching():
    blocks = [{"type": "text", "text": "hi"}, {"type": "tool_use", "name": "Other"}, "raw"]
    assert tp._find_tool_use_block(_event(blocks), "TodoWrite") is None


def test_find_tool_use_block_non_dict_data():
    assert tp._find_tool_use_block({"data": "nope"}, "TodoWrite") is None
    assert tp._find_tool_use_block({}, "TodoWrite") is None


def test_find_tool_use_block_non_dict_message():
    assert tp._find_tool_use_block({"data": {"message": []}}, "TodoWrite") is None


def test_find_tool_use_block_non_list_content():
    assert tp._find_tool_use_block({"data": {"message": {"content": "x"}}}, "TodoWrite") is None


def test_find_todowrite_block_delegates():
    block = {"type": "tool_use", "name": "TodoWrite", "input": {"todos": []}}
    assert tp._find_todowrite_block(_event([block])) is block


# ── _find_tool_result_blocks ─────────────────────────────────────


def test_find_tool_result_blocks_filters():
    blocks = [{"type": "tool_result", "tool_use_id": "tu_1", "content": "9"},
              {"type": "text", "text": "x"}]
    assert tp._find_tool_result_blocks(_event(blocks)) == [blocks[0]]


def test_find_tool_result_blocks_non_dict_data():
    assert tp._find_tool_result_blocks({"data": "x"}) == []
    assert tp._find_tool_result_blocks({}) == []


def test_find_tool_result_blocks_non_dict_message():
    assert tp._find_tool_result_blocks({"data": {"message": "x"}}) == []


def test_find_tool_result_blocks_non_list_content():
    assert tp._find_tool_result_blocks({"data": {"message": {"content": {}}}}) == []


# ── public extract_* malformed-input branches ────────────────────


def test_content_hash_source_id_unserializable_returns_none():
    # A non-JSON-serializable input (e.g. a set) must skip, not crash.
    assert tp._content_hash_source_id({"x": {1, 2, 3}}) is None


def test_content_hash_source_id_returns_hex_prefix():
    digest = tp._content_hash_source_id({"subject": "x"})
    assert isinstance(digest, str) and len(digest) == 16


def test_extract_todos_todowrite_input_not_dict():
    block = {"type": "tool_use", "name": "TodoWrite", "input": "nope"}
    assert tp.extract_todos_from_normalized({"data": {"message": {"content": [block]}}}, []) is None


def test_extract_todos_todos_field_not_list():
    block = {"type": "tool_use", "name": "TodoWrite", "input": {"todos": "nope"}}
    assert tp.extract_todos_from_normalized({"data": {"message": {"content": [block]}}}, []) is None


def test_extract_todos_no_todowrite_block():
    assert tp.extract_todos_from_normalized({"data": {"message": {"content": []}}}, []) is None


def test_extract_tasks_taskcreate_input_not_dict():
    block = {"type": "tool_use", "name": "TaskCreate", "input": "nope"}
    assert tp.extract_tasks_from_normalized({"data": {"message": {"content": [block]}}}, []) is None


def test_extract_tasks_taskupdate_input_not_dict():
    block = {"type": "tool_use", "name": "TaskUpdate", "input": "nope"}
    assert tp.extract_tasks_from_normalized({"data": {"message": {"content": [block]}}}, []) is None


def test_extract_tasks_no_relevant_block():
    assert tp.extract_tasks_from_normalized({"data": {"message": {"content": []}}}, []) is None
