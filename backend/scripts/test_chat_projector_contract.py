#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from chat_models import CanonicalEvent, Explanation, ModelChange, ScopedTurn, SteeringMessage, Turn, VisibilityPlan
from chat_projector import canonical_quick_reply_text, model_marker_targets, project_chat


FIXTURE = ROOT / "test-contracts" / "chat-panel" / "v1" / "canonical-session.json"


def _fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _result(value):
    if value is None:
        return None
    result = {"type": value.type, "part_ids": list(value.part_ids)}
    if value.text:
        result["concatenated_text"] = value.text
    return result


def _body(value):
    if isinstance(value, Explanation):
        return {
            "type": "Explanation",
            "text_event_ids": list(value.text_event_ids),
            "item_ids": list(value.item_ids),
        }
    if isinstance(value, SteeringMessage):
        return {"type": "SteeringMessage", "id": value.id}
    assert isinstance(value, ScopedTurn)
    projected = {
        "type": value.type,
        "id": value.id,
    }
    if value.children:
        projected["children"] = list(value.children)
    else:
        projected["prompt"] = value.prompt.text
        projected["body"] = [_body(item) for item in value.body]
        projected["result"] = _result(value.result)
    return projected


def _chat(value):
    result = []
    for item in value.items:
        if isinstance(item, ModelChange):
            result.append({"type": "ModelChange", "id": item.id, "before_turn": item.before_turn})
            continue
        assert isinstance(item, Turn)
        result.append({
            "type": "Turn",
            "id": item.id,
            "prompt": item.prompt.id,
            "body": [_body(body) for body in item.body],
            "result": _result(item.result),
        })
    return result


def test_completed_chat_matches_shared_oracle() -> None:
    fixture = _fixture()
    projected = _chat(project_chat(fixture["messages"], fixture["events"]))
    expected = json.loads(json.dumps(fixture["expected"]["chat_tree_completed"]))

    def strip_text(value) -> None:
        if isinstance(value, dict):
            value.pop("concatenated_text", None)
            for child in value.values():
                strip_text(child)
        elif isinstance(value, list):
            for child in value:
                strip_text(child)

    strip_text(projected)
    strip_text(expected)
    assert projected == expected


def test_order_dedup_ownership_and_metadata_contract() -> None:
    fixture = _fixture()
    chat = project_chat(fixture["messages"], list(reversed(fixture["events"])))
    turns = {item.id: item for item in chat.items if isinstance(item, Turn)}
    turn2 = turns["turn-2"]
    assert turn2.body[0].text_event_ids == ("e-orphan",)
    assert turn2.body[1].text_event_ids == ("e-mutable",)
    assert turn2.body[1].text == "Final replaced answer"
    encoded = repr(chat)
    for metadata_id in fixture["expected"]["persisted_render_tree"]["excluded_metadata_event_ids"]:
        if metadata_id != "mc1":
            assert metadata_id not in encoded
    assert encoded.count("e-rui") == 1


def test_model_markers_match_full_scope_oracle() -> None:
    fixture = _fixture()
    targets = model_marker_targets(fixture["events"])
    actual = [
        {
            "scope": target.scope,
            "provider": target.provider.id,
            "model": target.provider.model,
            "effort": target.provider.effort,
            "target_event_id": target.target_event_id,
        }
        for target in targets
    ]
    assert actual == fixture["expected"]["model_marker_targets"]["completed-at-seq-33"]


def test_visibility_plan_moves_marker_to_last_visible_event() -> None:
    fixture = _fixture()
    plan = fixture["expected"]["model_marker_targets"]["visible_render_plans"]["collapsed-turn-1"]
    targets = model_marker_targets(
        fixture["events"],
        [VisibilityPlan("root-1", tuple(plan["visible_event_ids"]))],
    )
    assert [target.target_event_id for target in targets] == [plan["marker_target_id"]]


def test_quick_reply_text_uses_canonical_result_text() -> None:
    fixture = _fixture()
    chat = project_chat(fixture["messages"], fixture["events"])
    assert canonical_quick_reply_text(chat, fixture["events"]) == fixture["expected"]["visible_semantics"]["quick_replies"]["canonical_assistant_text"]


def test_non_text_final_fallback() -> None:
    edge = _fixture()["expected"]["formal_edge_cases"]["non_text_final_fallback"]
    event = edge["source_items"][0]
    chat = project_chat(
        [{"id": "edge-user", "turn_id": edge["case_id"], "seq": 1, "role": "user", "content": "Run it"}],
        [{
            "event_id": event["event_id"], "timestamp": event["timestamp"],
            "journal_seq": event["sequence"], "context_id": "root-1",
            "turn_id": edge["case_id"], "message_id": None,
            "parent_event_id": None, "type": "tool_interaction", "data": {},
        }],
    )
    turn = next(item for item in chat.items if isinstance(item, Turn))
    assert _result(turn.result) == edge["expected_result"]
    assert turn.body == ()


def test_multiple_leading_text_is_concatenated() -> None:
    edge = _fixture()["expected"]["formal_edge_cases"]["multiple_leading_assistant_text"]
    events = []
    for sequence, item in enumerate(edge["source_items"], 1):
        events.append({
            "event_id": item["event_id"], "timestamp": "2026-01-02T00:00:00.000Z",
            "journal_seq": sequence, "context_id": "root-1", "turn_id": edge["case_id"],
            "message_id": None, "parent_event_id": None,
            "type": "assistant_text" if item["type"] == "AssistantText" else "tool_interaction",
            "data": {"text": item.get("text", "")},
        })
    chat = project_chat(
        [{"id": "edge-user", "turn_id": edge["case_id"], "seq": 1, "role": "user", "content": "Run it"}],
        events + [{
            "event_id": "edge-final", "timestamp": "2026-01-02T00:00:01.000Z",
            "journal_seq": 4, "context_id": "root-1", "turn_id": edge["case_id"],
            "message_id": None, "parent_event_id": None, "type": "assistant_text",
            "provider_final": True, "data": {"text": "Done"},
        }],
    )
    turn = next(item for item in chat.items if isinstance(item, Turn))
    explanation = turn.body[0]
    assert isinstance(explanation, Explanation)
    assert explanation.text == edge["expected_explanation"]["text"]
    assert explanation.text_event_ids == tuple(edge["expected_explanation"]["text_event_ids"])
    assert explanation.item_ids == tuple(edge["expected_explanation"]["item_ids"])


def test_canonical_event_is_deeply_immutable() -> None:
    event = CanonicalEvent(
        "immutable", "2026-01-01T00:00:00Z", 1, "root", "turn", "message",
        None, "tool_interaction", {"nested": {"values": [1, 2]}},
    )
    try:
        event.type = "changed"
        raise AssertionError("frozen event accepted assignment")
    except FrozenInstanceError:
        pass
    try:
        event.data["nested"] = {}
        raise AssertionError("frozen payload accepted assignment")
    except TypeError:
        pass
    assert event.data["nested"]["values"] == (1, 2)


def main() -> None:
    tests = [value for name, value in globals().items() if name.startswith("test_")]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")


if __name__ == "__main__":
    main()
