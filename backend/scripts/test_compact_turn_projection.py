#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from compact_turn_projection import (
    ProjectionRejected,
    build_compact_turn_page,
    compact_session_metadata,
    historical_root_manifest,
    project_historical_children,
)


SECRET = "SECRET_TOOL_BODY_MUST_NOT_CROSS_PROJECTION"


def test_compact_session_metadata_keeps_ui_state_without_render_payloads() -> None:
    session = {
        "id": "root", "name": "Root", "model": "gpt-5.5", "cwd": "/repo",
        "draft_input": "keep", "messages": [{"content": SECRET}],
        "root_events": [{"data": SECRET}], "max_seq_by_sid": {"root": 9},
        "forks": [{"id": "fork", "name": "Fork", "model": "gpt-5.5", "cwd": "/repo", "messages": [{"content": SECRET}]}],
    }
    projected = compact_session_metadata(session)
    assert projected["draft_input"] == "keep"
    assert projected["messages"] == []
    assert projected["forks"] == []
    assert "root_events" not in projected
    assert "max_seq_by_sid" not in projected
    assert SECRET not in json.dumps(projected)


def _messages() -> list[dict]:
    rows = []
    for turn in range(1, 5):
        rows.extend([
            {"id": f"u{turn}", "seq": turn * 2 - 1, "role": "user", "content": f"prompt {turn}"},
            {
                "id": f"a{turn}",
                "seq": turn * 2,
                "role": "assistant",
                "content": f"answer {turn}",
                "events": [{"type": "tool_call", "data": {"body": SECRET}}],
                "workers": [{"events": [{"data": SECRET}]}],
                "last_events": [{"data": SECRET}],
            },
        ])
    return rows


def test_latest_pairing_and_forbidden_payload_absence() -> None:
    page = build_compact_turn_page(_messages(), turn_limit=3, revision="r1")
    assert [turn["prompt"]["id"] for turn in page["turns"]] == ["u2", "u3", "u4"]
    assert [turn["assistant"]["id"] for turn in page["turns"]] == ["a2", "a3", "a4"]
    encoded = json.dumps(page, sort_keys=True)
    assert SECRET not in encoded
    for forbidden in ('"events"', '"workers"', '"last_events"', '"tool_call"'):
        assert forbidden not in encoded


def test_running_only_projects_visible_text_groups() -> None:
    rows = _messages()
    rows[-1]["isStreaming"] = True
    rows[-1]["events"] = [
        {"type": "tool_call", "data": {"body": SECRET}},
        {"type": "agent_message", "data": {"uuid": "visible-1", "message": {"content": [{"type": "text", "text": "stream one"}]}}},
        {"type": "tool_result", "data": {"result": SECRET}},
        {"type": "text_delta", "data": {"text": "stream two"}},
    ]
    page = build_compact_turn_page(rows, turn_limit=1, revision="live-r")
    assistant = page["turns"][0]["assistant"]
    assert assistant["running"] is True
    assert [group["text"] for group in assistant["visible_text_groups"]] == ["stream one", "stream two"]
    assert assistant["hydration_root"]["direct_child_count"] == 4
    assert SECRET not in json.dumps(page)


def test_older_pages_do_not_duplicate() -> None:
    latest = build_compact_turn_page(_messages(), turn_limit=2, revision="r")
    older = build_compact_turn_page(
        _messages(),
        turn_limit=2,
        before_seq=latest["page_cursor"]["before_seq"],
        revision="r",
    )
    latest_ids = {turn["id"] for turn in latest["turns"]}
    older_ids = {turn["id"] for turn in older["turns"]}
    assert not latest_ids & older_ids
    assert [turn["prompt"]["id"] for turn in older["turns"]] == ["u1", "u2"]
    assert older["page_cursor"]["has_older"] is False


def test_turn_id_is_stable_when_assistant_is_appended() -> None:
    prompt = {"id": "user-stable", "seq": 1, "role": "user", "content": "prompt"}
    before = build_compact_turn_page([prompt], turn_limit=1, revision="r1")
    after = build_compact_turn_page([
        prompt,
        {"id": "assistant-later", "seq": 2, "role": "assistant", "content": "answer"},
    ], turn_limit=1, revision="r2")
    assert before["turns"][0]["id"] == after["turns"][0]["id"]


def test_completed_stub_preserves_canonical_direct_child_count() -> None:
    full = _messages()[-1]
    from render_stub import build_stub

    rows = [_messages()[-2], {**full, "events": [], "stub": build_stub(full)}]
    page = build_compact_turn_page(rows, turn_limit=1, revision="r")
    root = page["turns"][0]["assistant"]["hydration_root"]
    assert root["direct_child_count"] == len(full["events"]) + len(full["workers"])
    expanded = project_historical_children(
        full, parent_id=root["id"], expected_revision=root["revision"],
    )
    assert len(expanded["children"]) == len(full["events"]) + len(full["workers"])


def test_completed_worker_only_stub_exposes_one_root_child() -> None:
    from render_stub import build_stub

    worker = {
        "delegation_id": "worker-only",
        "worker_session_id": "worker-session",
        "worker_description": "worker",
        "is_new": False,
        "instructions_preview": "inspect",
        "events": [],
    }
    full = {
        "id": "assistant-worker-only",
        "seq": 2,
        "role": "assistant",
        "content": "done",
        "events": [],
        "workers": [worker],
    }
    compact = {**full, "workers": [worker], "stub": build_stub(full)}
    page = build_compact_turn_page([
        {"id": "user-worker-only", "seq": 1, "role": "user", "content": "delegate"},
        compact,
    ], turn_limit=1, revision="r")
    root = page["turns"][0]["assistant"]["hydration_root"]
    assert root["direct_child_count"] == 1
    expanded = project_historical_children(
        full, parent_id=root["id"], expected_revision=root["revision"],
    )
    assert len(expanded["children"]) == 1
    assert expanded["children"][0]["type"] == "worker"
    assert expanded["children"][0]["render_payload"]["delegation_id"] == "worker-only"


def test_running_root_revision_expands_against_full_message() -> None:
    rows = _messages()[-2:]
    rows[-1]["isStreaming"] = True
    page = build_compact_turn_page(rows, turn_limit=1, revision="cursor-only")
    root = page["turns"][0]["assistant"]["hydration_root"]
    expanded = project_historical_children(
        rows[-1], parent_id=root["id"], expected_revision=root["revision"],
    )
    assert expanded["parent"]["revision"] == root["revision"]


def test_actionable_card_preserves_exact_picker_contract() -> None:
    rows = _messages()[-2:]
    ask_result = {
        "reasoning": "Choose a destination",
        "results": [
            {"id": "s1", "name": "one", "cwd": "/one", "first_user_prompt": "first"},
            {"id": "s2", "name": "two", "cwd": "/two", "first_user_prompt": "second"},
        ],
        "proposed_project_path": "/project",
        "proposed_project_node_id": "node-a",
        "purpose": "delegate_approval",
        "delegation_id": "delegation-a",
        "run_mode": "continue",
        "prompt_preview": "Do the work",
        "create_new": False,
    }
    rows[-1]["ask_result"] = ask_result
    page = build_compact_turn_page(rows, turn_limit=1, revision="r")
    cards = page["turns"][0]["assistant"]["actionable_cards"]
    assert cards == [{
        "type": "propose_sessions",
        "status": "pending",
        "ask_result": ask_result,
        "chosen_session_id": None,
    }]
    assert cards[0]["ask_result"] is not ask_result


def test_malformed_order_is_deterministic() -> None:
    rows = [
        {"id": "a-orphan", "seq": 2, "role": "assistant", "content": "orphan"},
        {"id": "u-late", "seq": 3, "role": "user", "content": "late"},
        {"id": "u-first", "seq": 1, "role": "user", "content": "first"},
    ]
    first = build_compact_turn_page(rows, turn_limit=10, revision="r")
    second = build_compact_turn_page(list(reversed(rows)), turn_limit=10, revision="r")
    assert first == second
    assert [turn["prompt"]["id"] for turn in first["turns"]] == ["u-first", "u-late"]


def _historical_message() -> dict:
    return {
        "id": "history-a1",
        "content": "final answer",
        "events": [
            {"type": "tool_call", "data": {"uuid": "parent", "input": SECRET}},
            {"type": "tool_result", "data": {"uuid": "child", "parentUuid": "parent", "output": SECRET}},
            {"type": "text", "data": {"uuid": "grandchild", "parentUuid": "child", "text": f"visible {SECRET}"}},
            {"type": "text", "data": {"uuid": "sibling", "text": "safe sibling"}},
        ],
        "workers": [{
            "id": "worker-1",
            "name": "worker one",
            "events": [{"type": "tool_result", "data": {"uuid": "worker-child", "output": SECRET}}],
        }],
    }


def test_historical_projection_is_strictly_one_level() -> None:
    message = _historical_message()
    root = historical_root_manifest(message)
    page = project_historical_children(
        message, parent_id=root["id"], expected_revision=root["revision"],
    )
    encoded = json.dumps(page, sort_keys=True).encode()
    assert len(page["children"]) == 3
    assert not any(b"grandchild" in json.dumps(child).encode() for child in page["children"])
    parent = next(child for child in page["children"] if child["type"] == "tool_call")
    assert parent["direct_child_count"] == 1
    assert parent["render_payload"] == message["events"][0]
    assert SECRET in json.dumps(parent["render_payload"])
    assert "grandchild" not in json.dumps(parent)

    worker = next(child for child in page["children"] if child["type"] == "worker")
    assert worker["render_payload"]["events"] == []

    next_page = project_historical_children(
        message, parent_id=parent["id"], expected_revision=parent["revision"],
    )
    assert [child["type"] for child in next_page["children"]] == ["tool_result"]
    assert next_page["children"][0]["direct_child_count"] == 1
    assert next_page["children"][0]["render_payload"] == message["events"][1]
    assert "visible super-secret-tool-body" not in json.dumps(next_page["children"][0])


def test_historical_ids_and_revisions_are_stable_and_mismatch_fails_closed() -> None:
    message = _historical_message()
    root = historical_root_manifest(message)
    first = project_historical_children(
        message, parent_id=root["id"], expected_revision=root["revision"],
    )
    second = project_historical_children(
        message, parent_id=root["id"], expected_revision=root["revision"],
    )
    assert first == second
    for parent_id, revision in (("missing", root["revision"]), (root["id"], "stale")):
        try:
            project_historical_children(
                message, parent_id=parent_id, expected_revision=revision,
            )
        except ProjectionRejected:
            pass
        else:
            raise AssertionError("unknown parent/revision mismatch must fail closed")


def test_historical_actionable_ui_payloads_round_trip_at_requested_level() -> None:
    propose = {
        "type": "propose_sessions",
        "data": {
            "uuid": "propose",
            "results": [{"id": "s1", "name": "one", "cwd": "/one", "first_user_prompt": "first"}],
            "reasoning": "Choose one",
            "proposed_project_path": "/project",
        },
    }
    request = {
        "type": "request_user_input",
        "data": {
            "uuid": "request",
            "questions": [{"id": "choice", "question": "Which?", "options": ["A", "B"]}],
            "action_id": "action-1",
        },
    }
    hidden = {
        "type": "tool_result",
        "data": {"uuid": "hidden", "parentUuid": "request", "output": SECRET},
    }
    message = {"id": "ui-mcp", "content": "", "events": [propose, request, hidden]}
    root = historical_root_manifest(message)
    page = project_historical_children(
        message, parent_id=root["id"], expected_revision=root["revision"],
    )
    by_type = {child["type"]: child for child in page["children"]}
    assert by_type["propose_sessions"]["render_payload"] == propose
    assert by_type["request_user_input"]["render_payload"] == request
    assert SECRET not in json.dumps(by_type["request_user_input"])


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
