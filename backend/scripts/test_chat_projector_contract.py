#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from chat_models import CanonicalEvent, Explanation, ModelChange, ProviderIdentity, ScopedTurn, SteeringMessage, Turn, VisibilityPlan
from chat_projector import canonical_quick_reply_text, model_marker_targets, project_chat


FIXTURE = ROOT / "test-contracts" / "chat-panel" / "v1" / "canonical-session.json"


def _fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _result(value):
    if value is None:
        return None
    result = {"type": value.type, "part_ids": list(value.part_ids)}
    result["text"] = value.text
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
    return {
        "type": value.type,
        "id": value.id,
        "prompt": value.prompt.text,
        "body": [_body(item) for item in value.body],
        "result": _result(value.result),
        "children": list(value.children),
    }


def _oracle_shape(value):
    if isinstance(value, list):
        return [_oracle_shape(item) for item in value]
    if not isinstance(value, dict):
        return value
    projected = {
        key: _oracle_shape(item)
        for key, item in value.items()
        if key not in {"text", "concatenated_text"}
    }
    if projected.get("type") in {"NativeSubagentTurn", "WorkerTurn"} and projected.get("children"):
        return {"type": projected["type"], "id": projected["id"], "children": projected["children"]}
    for key in ("children",):
        if projected.get(key) == []:
            projected.pop(key)
    return projected


def _provider(identity="p", model="m", effort="medium"):
    return {"id": identity, "model": model, "effort": effort}


def _event(event_id, sequence, event_type, *, turn_id="turn", message_id="assistant",
           context_id="root", parent_event_id=None, data=None, provider_final=False,
           metadata_only=False, provider=None):
    return {
        "event_id": event_id,
        "timestamp": f"2026-01-02T00:00:{sequence:02d}.000Z",
        "journal_seq": sequence,
        "content_version": 1,
        "context_id": context_id,
        "turn_id": turn_id,
        "message_id": message_id,
        "parent_event_id": parent_event_id,
        "type": event_type,
        "metadata_only": metadata_only,
        "provider_final": provider_final,
        "provider": provider or _provider(),
        "data": data or {},
    }


def _messages(turn_id="turn", assistant_id="assistant"):
    return [
        {"id": "user", "turn_id": turn_id, "seq": 1, "role": "user", "content": "Run it"},
        {"id": assistant_id, "turn_id": turn_id, "seq": 2, "role": "assistant", "content": ""},
    ]


def _assert_rejected(messages, events, schema_version=1):
    try:
        project_chat(messages, events, schema_version=schema_version)
    except (TypeError, ValueError):
        return
    raise AssertionError("malformed canonical input was accepted")


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
    projected = _chat(project_chat(fixture["messages"], fixture["events"], schema_version=fixture["schema_version"]))
    expected = json.loads(json.dumps(fixture["expected"]["chat_tree_completed"]))
    assert _oracle_shape(projected) == _oracle_shape(expected)
    turn1 = next(item for item in project_chat(fixture["messages"], fixture["events"], schema_version=1).items if isinstance(item, Turn) and item.id == "turn-1")
    assert turn1.result.text == "The report is ready."


def test_order_dedup_ownership_and_metadata_contract() -> None:
    fixture = _fixture()
    chat = project_chat(fixture["messages"], list(reversed(fixture["events"])), schema_version=1)
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
    targets = model_marker_targets(fixture["events"], schema_version=1)
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
        schema_version=1,
    )
    assert [target.target_event_id for target in targets] == [plan["marker_target_id"]]


def test_quick_reply_text_uses_canonical_result_text() -> None:
    fixture = _fixture()
    chat = project_chat(fixture["messages"], fixture["events"], schema_version=1)
    assert canonical_quick_reply_text(chat, fixture["events"], schema_version=1) == fixture["expected"]["visible_semantics"]["quick_replies"]["canonical_assistant_text"]


def test_non_text_final_fallback() -> None:
    edge = _fixture()["expected"]["formal_edge_cases"]["non_text_final_fallback"]
    event = edge["source_items"][0]
    chat = project_chat(
        [{"id": "edge-user", "turn_id": edge["case_id"], "seq": 1, "role": "user", "content": "Run it"}],
        [_event(event["event_id"], event["sequence"], "tool_interaction", turn_id=edge["case_id"], message_id=None)],
        schema_version=1,
    )
    turn = next(item for item in chat.items if isinstance(item, Turn))
    assert turn.result.type == edge["expected_result"]["type"]
    assert list(turn.result.part_ids) == edge["expected_result"]["part_ids"]
    assert turn.result.text == ""
    assert turn.body == ()


def test_multiple_leading_text_is_concatenated() -> None:
    edge = _fixture()["expected"]["formal_edge_cases"]["multiple_leading_assistant_text"]
    events = []
    for sequence, item in enumerate(edge["source_items"], 1):
        events.append(_event(
            item["event_id"], sequence,
            "assistant_text" if item["type"] == "AssistantText" else "tool_interaction",
            turn_id=edge["case_id"], message_id=None, data={"text": item.get("text", "")},
        ))
    chat = project_chat(
        [{"id": "edge-user", "turn_id": edge["case_id"], "seq": 1, "role": "user", "content": "Run it"}],
        events + [_event("edge-final", 4, "assistant_text", turn_id=edge["case_id"], message_id=None, provider_final=True, data={"text": "Done"})],
        schema_version=1,
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
        None, "tool_interaction", {"nested": {"values": [1, 2]}}, ProviderIdentity("p", "m", "medium"),
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


def test_provider_final_associates_unmarked_descendant_text_and_multiple_finals() -> None:
    events = [
        _event("work", 1, "tool_interaction"),
        _event("final-1", 2, "other_typed_work", provider_final=True),
        _event("text-1", 3, "assistant_text", parent_event_id="final-1", data={"text": "First."}),
        _event("final-2", 4, "other_typed_work", provider_final=True),
        _event("middle", 5, "tool_interaction", parent_event_id="final-2"),
        _event("text-2", 6, "assistant_text", parent_event_id="middle", data={"text": " Second."}),
        _event("unrelated", 7, "assistant_text", data={"text": "Not final."}),
    ]
    turn = next(item for item in project_chat(_messages(), events, schema_version=1).items if isinstance(item, Turn))
    assert turn.result.part_ids == ("final-1", "text-1", "final-2", "text-2")
    assert turn.result.text == "First. Second."
    assert turn.body[-1].text == "Not final."


def test_recursive_scoped_turn_uses_full_funnel_and_filters_metadata() -> None:
    events = [
        _event("worker", 1, "worker_turn", data={"prompt": "Delegate"}),
        _event("native", 2, "native_subagent_turn", context_id="worker-context", turn_id="worker-turn", parent_event_id="worker", data={"prompt": "Inspect"}),
        _event("meta", 3, "ai_title", context_id="worker-context", turn_id="worker-turn", parent_event_id="native", metadata_only=True, data={"title": "Hidden"}),
        _event("thought", 4, "assistant_text", context_id="worker-context", turn_id="worker-turn", parent_event_id="native", data={"text": "Reasoning"}),
        _event("tool", 5, "tool_interaction", context_id="worker-context", turn_id="worker-turn", parent_event_id="native"),
        _event("final", 6, "other_typed_work", context_id="worker-context", turn_id="worker-turn", parent_event_id="native", provider_final=True),
        _event("answer", 7, "assistant_text", context_id="worker-context", turn_id="worker-turn", parent_event_id="final", data={"text": "Nested answer"}),
    ]
    chat = project_chat(_messages(), events, schema_version=1)
    worker = next(item for item in next(item for item in chat.items if isinstance(item, Turn)).body if isinstance(item, ScopedTurn))
    native = next(item for item in worker.body if isinstance(item, ScopedTurn))
    assert _body(worker) == {
        "type": "WorkerTurn", "id": "worker", "prompt": "Delegate",
        "body": [{
            "type": "NativeSubagentTurn", "id": "native", "prompt": "Inspect",
            "body": [{"type": "Explanation", "text_event_ids": ["thought"], "item_ids": ["tool"]}],
            "result": {"type": "ProviderResult", "part_ids": ["final", "answer"], "text": "Nested answer"},
            "children": ["thought", "tool", "final"],
        }],
        "result": None, "children": ["native"],
    }
    assert "meta" not in repr(native)


def test_cross_boundary_parent_edges_are_rejected() -> None:
    events = [
        _event("parent", 1, "tool_interaction"),
        _event("child", 2, "assistant_text", message_id="other", parent_event_id="parent", data={"text": "escape"}),
    ]
    messages = _messages() + [{"id": "other", "turn_id": "turn", "seq": 3, "role": "assistant", "content": ""}]
    _assert_rejected(messages, events)
    scoped = [
        _event("worker", 1, "worker_turn"),
        _event("child", 2, "assistant_text", message_id="other", context_id="nested", turn_id="nested", parent_event_id="worker"),
    ]
    _assert_rejected(messages, scoped)
    cycle = [
        _event("cycle-a", 1, "tool_interaction", parent_event_id="cycle-b"),
        _event("cycle-b", 2, "tool_interaction", parent_event_id="cycle-a"),
    ]
    _assert_rejected(_messages(), cycle)


def test_strict_schema_rejects_malformed_unknown_and_duplicate_inputs() -> None:
    base = _event("valid", 1, "assistant_text", data={"text": "ok"})
    _assert_rejected(_messages(), [base], schema_version=2)
    for key, value in (
        ("journal_seq", 0), ("timestamp", "not-a-time"), ("context_id", ""),
        ("type", "unknown_event"), ("provider", {"id": "p", "model": "", "effort": "medium"}),
        ("content_version", "1"), ("data", []),
    ):
        malformed = dict(base)
        malformed[key] = value
        _assert_rejected(_messages(), [malformed])
    missing = dict(base)
    missing.pop("provider")
    _assert_rejected(_messages(), [missing])
    duplicate_prompts = _messages() + [{"id": "user-2", "turn_id": "turn", "seq": 3, "role": "user", "content": "Again"}]
    _assert_rejected(duplicate_prompts, [base])


def test_metadata_model_change_is_filtered_before_projection() -> None:
    event = _event("model", 1, "model_change", metadata_only=True, data={"to": _provider("p2", "m2", "high")})
    chat = project_chat(_messages(), [event], schema_version=1)
    assert all(not isinstance(item, ModelChange) for item in chat.items)


def test_hidden_provider_run_does_not_merge_visible_runs() -> None:
    events = [
        _event("visible-a", 1, "assistant_text", provider=_provider("p1", "m1", "high"), data={"text": "a"}),
        _event("hidden", 2, "assistant_text", provider=_provider("p2", "m2", "low"), data={"text": "b"}),
        _event("visible-c", 3, "assistant_text", provider=_provider("p1", "m1", "high"), data={"text": "c"}),
    ]
    plan = VisibilityPlan("root", ("visible-a", "visible-c"))
    targets = model_marker_targets(events, [plan], schema_version=1)
    assert [(item.provider.id, item.target_event_id) for item in targets] == [
        ("p1", "visible-a"), ("p1", "visible-c"),
    ]


def test_quick_reply_skips_textless_results() -> None:
    messages = _messages("turn-1", "assistant-1") + [
        {"id": "user-2", "turn_id": "turn-2", "seq": 3, "role": "user", "content": "Again"},
        {"id": "assistant-2", "turn_id": "turn-2", "seq": 4, "role": "assistant", "content": ""},
    ]
    events = [
        _event("text-result", 1, "assistant_text", turn_id="turn-1", message_id="assistant-1", provider_final=True, data={"text": "Use me"}),
        _event("tool-result", 2, "tool_interaction", turn_id="turn-2", message_id="assistant-2", provider_final=True),
    ]
    chat = project_chat(messages, events, schema_version=1)
    assert canonical_quick_reply_text(chat, events, schema_version=1) == "Use me"


def main() -> None:
    tests = [value for name, value in globals().items() if name.startswith("test_")]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")


if __name__ == "__main__":
    main()
