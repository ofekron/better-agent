#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from chat_models import CanonicalEvent, Explanation, ModelChange, ProviderIdentity, ScopedTurn, SteeringMessage, Turn, VisibilityPlan
import chat_projector
from chat_projector import ChatProjectionInputError, canonical_quick_reply_text, model_marker_targets, project_chat


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
    projected = {}
    stack = [(value, projected)]
    while stack:
        item, target = stack.pop()
        if isinstance(item, Explanation):
            target.update({
                "type": "Explanation", "text": item.text,
                "text_event_ids": list(item.text_event_ids), "item_ids": list(item.item_ids),
            })
            continue
        if isinstance(item, SteeringMessage):
            target.update({"type": "SteeringMessage", "id": item.id, "text": item.text})
            continue
        assert isinstance(item, ScopedTurn)
        target.update({
            "type": item.type, "id": item.id, "prompt": item.prompt.text,
            "body": [{} for _ in item.body], "result": _result(item.result),
            "children": list(item.children),
        })
        for child, child_target in reversed(list(zip(item.body, target["body"]))):
            stack.append((child, child_target))
    return projected


def _provider(identity="p", model="m", effort="medium"):
    return {"id": identity, "model": model, "effort": effort}


def _model_identity(provider="p", model="m", effort="medium"):
    return {"provider": provider, "model": model, "effort": effort}


def _event(event_id, sequence, event_type, *, turn_id="turn", message_id="assistant",
           context_id="root", parent_event_id=None, data=None, provider_final=False,
           metadata_only=False, provider=None):
    defaults = {
        "ai_title": {"title": "Title"},
        "assistant_text": {"text": "Text"}, "text": {"text": "Text"},
        "output_text": {"text": "Text"},
        "file_history_snapshot": {"snapshot_id": "snapshot"},
        "message_ownership_declared": {"owns_event_ids": []},
        "model_change": {"to": _model_identity()},
        "native_subagent_turn": {"prompt": "Native"},
        "other_typed_work": {"kind": "work", "label": "Work"},
        "steering_message": {"text": "Steer"},
        "thinking": {"text": "Think", "status": "complete"},
        "tool_interaction": {"tool_name": "tool", "tool_use_id": event_id, "status": "complete"},
        "turn_completed": {}, "turn_started": {},
        "worker_turn": {"prompt": "Worker"},
    }
    payload = defaults[event_type] if data is None else data
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
        "data": payload,
    }


def _messages(turn_id="turn", assistant_id="assistant"):
    return [
        {"id": "user", "turn_id": turn_id, "seq": 1, "role": "user", "content": "Run it"},
        {"id": assistant_id, "turn_id": turn_id, "seq": 2, "role": "assistant", "content": ""},
    ]


def _assert_rejected(code, messages, events, schema_version=1):
    try:
        project_chat(messages, events, schema_version=schema_version)
    except ChatProjectionInputError as exc:
        assert exc.code == code
        assert exc.detail
        return
    raise AssertionError(f"expected ChatProjectionInputError({code})")


def _assert_input_error(code, messages, events):
    try:
        project_chat(messages, events, schema_version=1)
    except ChatProjectionInputError as exc:
        assert exc.code == code
        assert exc.detail
        return
    raise AssertionError(f"expected ChatProjectionInputError({code})")


def _with_limit(name, value, callback):
    original = getattr(chat_projector, name)
    setattr(chat_projector, name, value)
    try:
        callback()
    finally:
        setattr(chat_projector, name, original)


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
    assert projected == fixture["expected"]["chat_tree_completed"]


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
        event_type = "assistant_text" if item["type"] == "AssistantText" else "tool_interaction"
        events.append(_event(
            item["event_id"], sequence, event_type,
            turn_id=edge["case_id"], message_id=None,
            data={"text": item.get("text", "")} if event_type == "assistant_text" else None,
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
            "body": [{"type": "Explanation", "text": "Reasoning", "text_event_ids": ["thought"], "item_ids": ["tool"]}],
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
    _assert_rejected("parent_message_boundary", messages, events)
    scoped = [
        _event("worker", 1, "worker_turn"),
        _event("child", 2, "assistant_text", message_id="other", context_id="nested", turn_id="nested", parent_event_id="worker"),
    ]
    _assert_rejected("parent_message_boundary", messages, scoped)
    cycle = [
        _event("cycle-a", 1, "tool_interaction", parent_event_id="cycle-b"),
        _event("cycle-b", 2, "tool_interaction", parent_event_id="cycle-a"),
    ]
    _assert_rejected("parent_cycle", _messages(), cycle)


def test_strict_schema_rejects_malformed_unknown_and_duplicate_inputs() -> None:
    base = _event("valid", 1, "assistant_text", data={"text": "ok"})
    _assert_rejected("unsupported_schema", _messages(), [base], schema_version=2)
    for key, value, code in (
        ("journal_seq", 0, "invalid_scalar"), ("timestamp", "not-a-time", "invalid_event_model"),
        ("context_id", "", "invalid_scalar"), ("type", "unknown_event", "invalid_payload"),
        ("provider", {"id": "p", "model": "", "effort": "medium"}, "invalid_scalar"),
        ("content_version", "1", "invalid_scalar"), ("data", [], "invalid_event_data"),
    ):
        malformed = dict(base)
        malformed[key] = value
        _assert_rejected(code, _messages(), [malformed])
    missing = dict(base)
    missing.pop("provider")
    _assert_rejected("missing_event_fields", _messages(), [missing])
    duplicate_prompts = _messages() + [{"id": "user-2", "turn_id": "turn", "seq": 3, "role": "user", "content": "Again"}]
    _assert_rejected("duplicate_prompt", duplicate_prompts, [base])


def test_metadata_model_change_is_filtered_before_projection() -> None:
    event = _event(
        "model", 1, "model_change", metadata_only=True,
        data={"to": _model_identity("p2", "m2", "high")},
        provider=_provider("p2", "m2", "high"),
    )
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


def test_ownership_cannot_duplicate_scoped_structural_child() -> None:
    events = [
        _event("worker", 1, "worker_turn"),
        _event("child", 2, "assistant_text", context_id="nested", turn_id="nested", parent_event_id="worker", data={"text": "nested"}),
        _event("owner", 3, "message_ownership_declared", metadata_only=True, data={"owns_event_ids": ["child"]}),
    ]
    _assert_rejected("ownership_scoped_conflict", _messages(), events)


def test_metadata_descendant_is_excluded_from_provider_result() -> None:
    events = [
        _event("final", 1, "other_typed_work", provider_final=True),
        _event("hidden-text", 2, "assistant_text", parent_event_id="final", metadata_only=True, data={"text": "secret"}),
        _event("visible-text", 3, "assistant_text", parent_event_id="final", data={"text": "visible"}),
    ]
    turn = next(item for item in project_chat(_messages(), events, schema_version=1).items if isinstance(item, Turn))
    assert turn.result.part_ids == ("final", "visible-text")
    assert turn.result.text == "visible"
    assert "hidden-text" not in repr(turn)


def test_provider_result_association_stops_at_nested_scoped_turn() -> None:
    events = [
        _event("final", 1, "other_typed_work", provider_final=True),
        _event("worker", 2, "worker_turn", parent_event_id="final", data={"prompt": "nested"}),
        _event("nested-text", 3, "assistant_text", parent_event_id="worker", context_id="nested", turn_id="nested", data={"text": "nested only"}),
    ]
    turn = next(item for item in project_chat(_messages(), events, schema_version=1).items if isinstance(item, Turn))
    assert turn.result.part_ids == ("final",)
    assert "nested-text" not in turn.result.part_ids


def test_same_id_versions_require_stable_identity_and_versioned_render_updates() -> None:
    base = _event("versioned", 1, "assistant_text", data={"text": "one"})
    for field, value, code in (
        ("context_id", "other-context", "version_identity_changed"),
        ("turn_id", "other-turn", "version_identity_changed"),
        ("message_id", "other-message", "version_identity_changed"),
        ("parent_event_id", "parent", "version_identity_changed"),
        ("type", "tool_interaction", "missing_payload_fields"),
        ("provider", _provider("other"), "version_identity_changed"),
    ):
        changed = dict(base)
        changed.update({field: value, "journal_seq": 2, "content_version": 2})
        _assert_rejected(code, _messages(), [base, changed])
    unversioned = dict(base)
    unversioned.update({"journal_seq": 2, "data": {"text": "two"}})
    _assert_rejected("version_not_incremented", _messages(), [base, unversioned])
    updated = dict(unversioned)
    updated["content_version"] = 2
    turn = next(item for item in project_chat(_messages(), [base, updated], schema_version=1).items if isinstance(item, Turn))
    assert turn.result.text == "two"


def test_closed_envelopes_and_nested_identities_reject_unknown_fields() -> None:
    base = _event("valid", 1, "assistant_text", data={"text": "ok"})
    extra_event = dict(base, surprise=True)
    _assert_rejected("unexpected_fields", _messages(), [extra_event])
    extra_provider = dict(base)
    extra_provider["provider"] = dict(_provider(), surprise=True)
    _assert_rejected("unexpected_fields", _messages(), [extra_provider])
    extra_message = [dict(_messages()[0], surprise=True), _messages()[1]]
    _assert_rejected("unexpected_fields", extra_message, [base])
    model_change = _event(
        "model", 1, "model_change",
        data={"to": dict(_model_identity(), surprise=True)},
    )
    _assert_rejected("invalid_payload", _messages(), [model_change])
    ownership = _event(
        "owner", 1, "message_ownership_declared", metadata_only=True,
        data={"owns_event_ids": [], "surprise": True},
    )
    _assert_rejected("unexpected_fields", _messages(), [ownership])
    invalid_json_key = _event("invalid-json", 1, "assistant_text", data={1: "value"})
    _assert_rejected("invalid_payload", _messages(), [invalid_json_key])


def test_timestamps_require_canonical_utc_and_sort_as_instants() -> None:
    base = _event("valid", 1, "assistant_text", data={"text": "ok"})
    for timestamp in ("2026-01-02", "2026-01-02T00:00:00+00:00", "2026-01-02T02:00:00+02:00"):
        malformed = dict(base, timestamp=timestamp)
        _assert_rejected("invalid_event_model", _messages(), [malformed])
    first = dict(_event("first", 2, "assistant_text", data={"text": "A"}), timestamp="2026-01-02T00:00:00Z")
    second = dict(_event("second", 1, "assistant_text", data={"text": "B"}), timestamp="2026-01-02T00:00:00.1Z")
    turn = next(item for item in project_chat(_messages(), [second, first], schema_version=1).items if isinstance(item, Turn))
    assert turn.result.text == "AB"
    older_version = dict(_event("same", 2, "assistant_text", data={"text": "old"}), timestamp="2026-01-02T00:00:00Z")
    newer_version = dict(older_version, timestamp="2026-01-02T00:00:00.1Z", journal_seq=1, content_version=2, data={"text": "new"})
    updated = next(event for event in project_chat(_messages(), [newer_version, older_version], schema_version=1).items if isinstance(event, Turn))
    assert updated.result.text == "new"


def test_duplicate_root_sequence_is_rejected_across_contexts_in_both_orders() -> None:
    first = _event("first", 1, "assistant_text", data={"text": "first"})
    second = _event("second", 1, "assistant_text", data={"text": "second"})
    _assert_rejected("duplicate_journal_seq", _messages(), [first, second])
    _assert_rejected("duplicate_journal_seq", _messages(), [second, first])
    other_context = dict(second, context_id="other")
    _assert_input_error("duplicate_journal_seq", _messages(), [first, other_context])
    _assert_input_error("duplicate_journal_seq", _messages(), [other_context, first])


def test_scoped_projection_and_serialization_are_stack_safe_beyond_1100_depth() -> None:
    depth = 1101
    events = []
    for index in range(depth):
        event = _event(
            f"worker-{index}", index + 1, "worker_turn",
            parent_event_id=f"worker-{index - 1}" if index else None,
            data={"prompt": f"Depth {index}"},
        )
        event["timestamp"] = "2026-01-02T00:00:00.000Z"
        events.append(event)
    chat = project_chat(_messages(), events, schema_version=1)
    serialized = _chat(chat)
    node = serialized[0]["body"][0]
    visited = 1
    while node["body"]:
        node = node["body"][0]
        visited += 1
    assert visited == depth
    assert node["prompt"] == f"Depth {depth - 1}"


def test_model_change_requires_closed_non_null_matching_target() -> None:
    provider = _provider("p2", "m2", "high")
    valid = _event(
        "model", 1, "model_change", provider=provider,
        data={"from": None, "to": _model_identity("p2", "m2", "high")},
    )
    project_chat(_messages(), [valid], schema_version=1)
    for data, code in (
        ({}, "missing_payload_fields"), ({"to": None}, "missing_payload_fields"),
        ({"to": _model_identity(), "extra": True}, "unexpected_fields"),
    ):
        _assert_rejected(code, _messages(), [_event("bad-model", 1, "model_change", data=data)])
    conflict = dict(valid, provider=_provider("p3", "m2", "high"))
    _assert_rejected("model_provider_mismatch", _messages(), [conflict])


def test_every_supported_event_type_has_closed_required_payload() -> None:
    malformed = {
        "ai_title": {}, "assistant_text": {}, "file_history_snapshot": {},
        "message_ownership_declared": {}, "model_change": {},
        "native_subagent_turn": {}, "other_typed_work": {"kind": "work"},
        "output_text": {}, "steering_message": {}, "text": {},
        "thinking": {"text": "thinking"}, "tool_interaction": {"tool_name": "tool"},
        "turn_completed": {"extra": True}, "turn_started": {"extra": True},
        "worker_turn": {},
    }
    for index, (event_type, data) in enumerate(malformed.items(), 1):
        event = _event(f"bad-{event_type}", index, event_type, data=data)
        code = "unexpected_fields" if event_type in {"turn_completed", "turn_started"} else "missing_payload_fields"
        if event_type == "message_ownership_declared":
            code = "invalid_payload"
        _assert_rejected(code, _messages(), [event])
    bad_typed_work = _event(
        "bad-work", 20, "other_typed_work",
        data={"kind": "", "label": "Work"},
    )
    _assert_rejected("invalid_scalar", _messages(), [bad_typed_work])
    malformed_session = _event(
        "bad-session", 21, "tool_interaction",
        data={
            "tool_name": "tool", "tool_use_id": "use", "status": "complete",
            "sessions": [{"id": "session", "title": "Title", "extra": True}],
        },
    )
    _assert_rejected("invalid_payload", _messages(), [malformed_session])


def test_owned_parent_cycle_fails_before_ownership_traversal() -> None:
    events = [
        _event("a", 1, "worker_turn", parent_event_id="b"),
        _event("b", 2, "worker_turn", parent_event_id="a"),
        _event(
            "owner", 3, "message_ownership_declared", metadata_only=True,
            data={"owns_event_ids": ["a", "b"]},
        ),
    ]
    _assert_input_error("parent_cycle", _messages(), events)


def test_parent_graph_validation_observer_is_linear_on_large_chain() -> None:
    events = []
    for index in range(5000):
        event = _event(
            f"node-{index}", index + 1, "worker_turn",
            parent_event_id=f"node-{index - 1}" if index else None,
        )
        event["timestamp"] = "2026-01-02T00:00:00Z"
        events.append(event)
    canonical = chat_projector._canonical_events(events, 1)
    observed = []
    chat_projector._validate_parent_graph(
        canonical, {event.event_id: event for event in canonical}, observed.append,
    )
    assert len(observed) == len(events)
    assert len(set(observed)) == len(events)


def test_admission_bounds_and_source_fail_closed_with_typed_errors() -> None:
    base = _event("base", 1, "assistant_text", data={"text": "ok"})
    _with_limit(
        "MAX_CANONICAL_ROWS", 0,
        lambda: _assert_input_error("too_many_rows", _messages(), [base]),
    )
    _with_limit(
        "MAX_CANONICAL_JSON_BYTES", 1,
        lambda: _assert_input_error("canonical_bytes_exceeded", _messages(), [base]),
    )
    _with_limit(
        "MAX_STRING_LENGTH", 2,
        lambda: _assert_input_error("string_too_long", _messages(), [base]),
    )
    oversized_list = _event(
        "list", 1, "tool_interaction",
        data={
            "tool_name": "tool", "tool_use_id": "use", "status": "complete",
            "options": ["a", "b"],
        },
    )
    _with_limit(
        "MAX_LIST_ITEMS", 1,
        lambda: _assert_input_error("list_too_large", _messages(), [oversized_list]),
    )
    _with_limit(
        "MAX_OPTIONS", 1,
        lambda: _assert_input_error("too_many_options", _messages(), [oversized_list]),
    )
    sessions = _event(
        "sessions", 1, "tool_interaction",
        data={
            "tool_name": "tool", "tool_use_id": "use", "status": "complete",
            "sessions": [{"id": "a", "title": "A"}, {"id": "b", "title": "B"}],
        },
    )
    _with_limit(
        "MAX_SESSIONS", 1,
        lambda: _assert_input_error("too_many_sessions", _messages(), [sessions]),
    )
    nested = _event("nested", 1, "assistant_text", data={"text": "ok"})
    nested["data"]["text"] = "ok"
    nested["data"]["extra"] = {"deeper": {"value": "x"}}
    _with_limit(
        "MAX_PAYLOAD_DEPTH", 2,
        lambda: _assert_input_error("payload_depth_exceeded", _messages(), [nested]),
    )
    for source in ("", 1, None):
        invalid_source = dict(base, source=source)
        _assert_rejected("invalid_scalar", _messages(), [invalid_source])
    cyclic = {}
    cyclic["self"] = cyclic
    cyclic_event = dict(base, data=cyclic)
    _assert_input_error("payload_not_tree", _messages(), [cyclic_event])


def test_message_sequences_are_root_unique_in_both_orders() -> None:
    messages = _messages()
    duplicate = dict(messages[1], id="assistant-2", seq=messages[0]["seq"])
    _assert_rejected("duplicate_message_seq", [messages[0], duplicate], [])
    _assert_rejected("duplicate_message_seq", [duplicate, messages[0]], [])
    _assert_rejected("invalid_message_content", ["not-an-object"], [])


def test_non_object_event_and_invalid_unicode_are_typed() -> None:
    _assert_rejected("invalid_event_data", _messages(), ["not-an-object"])
    invalid_unicode = _event("unicode", 1, "assistant_text", data={"text": "\ud800"})
    _assert_rejected("invalid_scalar", _messages(), [invalid_unicode])


def test_message_count_and_bytes_admission_exact_boundaries() -> None:
    messages = _messages()
    _with_limit(
        "MAX_MESSAGES", len(messages),
        lambda: project_chat(messages, [], schema_version=1),
    )
    _with_limit(
        "MAX_MESSAGES", len(messages) - 1,
        lambda: _assert_input_error("too_many_messages", messages, []),
    )
    exact_bytes = len(json.dumps(
        messages, ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8"))
    assert chat_projector._measure_json(messages) == exact_bytes
    _with_limit(
        "MAX_MESSAGE_JSON_BYTES", exact_bytes,
        lambda: project_chat(messages, [], schema_version=1),
    )
    _with_limit(
        "MAX_MESSAGE_JSON_BYTES", exact_bytes - 1,
        lambda: _assert_input_error("message_bytes_exceeded", messages, []),
    )


def test_exact_json_bytes_match_oracle_for_events_escaping_and_unicode() -> None:
    text = 'quote " slash \\ newline\n control\u0001 עברית 😀'
    event = _event("escaped", 1, "assistant_text", data={"text": text})
    expected_event_bytes = len(json.dumps(
        [event], ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8"))
    assert chat_projector._measure_json([event]) == expected_event_bytes
    _with_limit(
        "MAX_CANONICAL_JSON_BYTES", expected_event_bytes,
        lambda: project_chat(_messages(), [event], schema_version=1),
    )
    _with_limit(
        "MAX_CANONICAL_JSON_BYTES", expected_event_bytes - 1,
        lambda: _assert_input_error("canonical_bytes_exceeded", _messages(), [event]),
    )
    messages = _messages()
    messages[0]["content"] = text
    expected_message_bytes = len(json.dumps(
        messages, ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8"))
    assert chat_projector._measure_json(messages) == expected_message_bytes
    _with_limit(
        "MAX_MESSAGE_JSON_BYTES", expected_message_bytes,
        lambda: project_chat(messages, [], schema_version=1),
    )
    _with_limit(
        "MAX_MESSAGE_JSON_BYTES", expected_message_bytes - 1,
        lambda: _assert_input_error("message_bytes_exceeded", messages, []),
    )


def test_each_missing_message_field_has_stable_code_before_sorting() -> None:
    complete = _messages()[0]
    for field in ("id", "turn_id", "seq", "role", "content"):
        missing = dict(complete)
        missing.pop(field)
        _assert_rejected("missing_message_fields", [missing], [])


def test_floats_and_non_json_values_are_rejected() -> None:
    for value in (1.5, float("nan"), object()):
        event = _event("non-json", 1, "assistant_text", data={"text": value})
        _assert_rejected("invalid_payload", _messages(), [event])


def test_associated_text_index_is_linear_with_many_scoped_finals() -> None:
    scope_count = 2000
    events = []
    sequence = 0
    for index in range(scope_count):
        sequence += 1
        events.append(_event(f"worker-{index}", sequence, "worker_turn"))
        sequence += 1
        events.append(_event(
            f"final-{index}", sequence, "other_typed_work",
            parent_event_id=f"worker-{index}", provider_final=True,
        ))
        sequence += 1
        events.append(_event(
            f"text-{index}", sequence, "assistant_text",
            parent_event_id=f"final-{index}", data={"text": f"answer-{index}"},
        ))
    for event in events:
        event["timestamp"] = "2026-01-02T00:00:00Z"
    canonical = chat_projector._canonical_events(events, 1)
    event_by_id = {event.event_id: event for event in canonical}
    observed = []
    index = chat_projector._build_associated_text_index(
        canonical, event_by_id, {}, {"assistant": "turn"}, observed.append,
    )
    assert len(observed) == len(events)
    assert len(set(observed)) == len(events)
    assert len(index) == scope_count
    assert all(len(items) == 1 for items in index.values())


def main() -> None:
    tests = [value for name, value in globals().items() if name.startswith("test_")]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")


if __name__ == "__main__":
    main()
