"""End-to-end contract for the facts→projector bridge.

Locks that wire-shaped canonical feed facts + a runtime session snapshot
adapt into inputs `chat_projector.project_chat` accepts, and that the
projected chat serializes through `chat_tree_wire` into the formal tree
shape the frontend's parseProjection consumes:

  A. user_prompt / message_ownership_declared facts become the message
     rows that give every assistant event its turn.
  B. assistant_output(final) resolves to a ProviderResult; thinking and
     tool_call(+tool_result pairing) land as body items; unknown types
     map to the typed other_typed_work catch-all.
  C. model_switched with a complete target identity becomes a
     ModelChange chat item before its turn; an incomplete identity is a
     typed drop, never a coerced fact.
  D. Provider identity joins from the session messages' run_meta and
     fails closed (typed drop) when unresolvable.
  E. Non-Z UTC timestamps normalize to the projector's Z form.

Run with:
    cd backend && .venv/bin/python scripts/test_chat_canonical_adapter.py
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from chat_canonical_adapter import adapt_chat_inputs, _json_safe
from chat_models import CHAT_SCHEMA_VERSION, Explanation, ModelChange, Turn
from chat_projector import project_chat
from chat_tree_wire import chat_to_wire

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"
_failures: list[str] = []


def check(label: str, condition: bool) -> None:
    print(f"{PASS if condition else FAIL}  {label}")
    if not condition:
        _failures.append(label)


def fact(seq: int, payload_type: str, payload: dict, **overrides) -> dict:
    return {
        "canonical_seq": seq,
        "fact_id": f"fact-{seq}",
        "source_event_id": f"event-{seq}",
        "sid": "root",
        "payload_type": payload_type,
        "payload": payload,
        "observed_at": f"2026-07-15T10:00:{seq:02d}+00:00",
        "source_timestamp": None,
        **overrides,
    }


SESSION = {
    "id": "root",
    "provider_id": "claude",
    "model": "sonnet-4-6",
    "reasoning_effort": "high",
    "messages": [
        {"id": "u1", "role": "user"},
        {"id": "a1", "role": "assistant",
         "run_meta": {"provider_id": "claude", "model": "sonnet-4-6", "reasoning_effort": "high"}},
    ],
}

FACTS = [
    fact(1, "user_prompt", {"message_id": "u1", "text": "Run it"}),
    fact(2, "message_ownership_declared", {"message_id": "a1", "prompt_message_id": "u1"}),
    fact(3, "model_switched", {
        "message_id": "a1",
        "previous_provider_id": "claude", "previous_model": "opus", "previous_reasoning_effort": "high",
        "provider_id": "claude", "model": "sonnet-4-6", "reasoning_effort": "high",
    }),
    fact(4, "thinking", {"message_id": "a1", "text": "planning"}),
    fact(5, "tool_call", {"message_id": "a1", "tool_use_id": "tu-1", "tool": "Bash", "args": {}}),
    fact(6, "tool_result", {"message_id": "a1", "tool_use_id": "tu-1", "output": {"stdout": "ok"}}),
    fact(7, "todos_snapshot", {"message_id": "a1", "todos": []}),
    fact(8, "assistant_output", {"message_id": "a1", "text": "All done.", "final": True}),
    # Incomplete model change: target lacks effort -> typed drop.
    fact(9, "model_switched", {"message_id": "a1", "provider_id": "codex", "model": "gpt-5-codex"}),
    # Unresolvable provider identity: unknown message, session lacks nothing
    # here, so simulate by pointing at a message with no run_meta and blanking
    # the session fallback via an unknown-session copy below.
    fact(10, "turn_complete", {"message_id": "a1"}),
]


def test_full_pipeline() -> None:
    adapted = adapt_chat_inputs(FACTS, SESSION)
    check("prompt and ownership become message rows",
          [(m["id"], m["role"], m["turn_id"]) for m in adapted.messages]
          == [("u1", "user", "u1"), ("a1", "assistant", "u1")])
    check("incomplete model change is a typed drop",
          {"fact_id": "fact-9", "code": "model_change_identity_incomplete"} in adapted.dropped)
    check("timestamps normalize to Z",
          all(event["timestamp"].endswith("Z") for event in adapted.events))
    tool_events = [event for event in adapted.events if event["type"] == "tool_interaction"]
    check("tool call and result stay as separate pairable interactions",
          [(e["event_id"], e["data"].get("status"), e["data"].get("tool_use_id")) for e in tool_events]
          == [("event-5", "running", "tu-1"), ("event-6", "complete", "tu-1")])
    check("tool result preserves result fact id and structured output",
          tool_events[1]["data"].get("output") == '{"stdout": "ok"}')

    unmatched = adapt_chat_inputs(
        [fact(1, "user_prompt", {"message_id": "u1", "text": "hi"}),
         fact(2, "message_ownership_declared", {"message_id": "a1", "prompt_message_id": "u1"}),
         fact(3, "tool_result", {"message_id": "a1", "tool_use_id": "missing", "output": "late"})],
        SESSION,
    )
    check("unmatched tool results are typed drops",
          {"fact_id": "fact-3", "code": "unmatched_tool_result"} in unmatched.dropped
          and [e for e in unmatched.events if e["type"] == "tool_interaction"] == [])

    chat = project_chat(adapted.messages, adapted.events, schema_version=CHAT_SCHEMA_VERSION)
    items = list(chat.items)
    check("model change renders before its turn",
          isinstance(items[0], ModelChange) and isinstance(items[1], Turn)
          and items[0].before_turn == items[1].id)
    turn = items[1]
    check("provider-final output resolves to ProviderResult",
          turn.result is not None and turn.result.type == "ProviderResult"
          and turn.result.text == "All done.")
    check("body is explanation-partitioned work",
          len(turn.body) == 1 and isinstance(turn.body[0], Explanation))
    explanation_items = turn.body[0].item_ids
    check("thinking, tool pair, and typed catch-all land as body items",
          set(explanation_items) == {"event-4", "event-5", "event-6", "event-7"})

    wire = chat_to_wire(chat)
    check("wire tree matches parseProjection contract shape",
          wire[0]["type"] == "ModelChange" and wire[1]["type"] == "Turn"
          and set(wire[1].keys()) == {"type", "id", "prompt", "body", "result"}
          and wire[1]["result"]["part_ids"] == list(turn.result.part_ids))


def test_unsupported_block_facts_stay_visible_as_typed_work() -> None:
    adapted = adapt_chat_inputs(
        [fact(1, "user_prompt", {"message_id": "u1", "text": "hi"}),
         fact(2, "message_ownership_declared", {"message_id": "a1", "prompt_message_id": "u1"}),
         fact(3, "unsupported_block", {"message_id": "a1", "block_type": "server_tool_search",
                                       "block": {"type": "server_tool_search"}})],
        SESSION,
    )
    work = [event for event in adapted.events if event["type"] == "other_typed_work"]
    check("unsupported block maps to visible typed work",
          len(work) == 1 and work[0]["data"] == {
              "kind": "unsupported_block",
              "label": "unsupported block: server_tool_search",
              "payload": {"message_id": "a1", "block_type": "server_tool_search",
                          "block": {"type": "server_tool_search"}},
          })


def test_identity_fails_closed() -> None:
    bare_session = {"id": "root", "messages": []}
    adapted = adapt_chat_inputs(
        [fact(1, "user_prompt", {"message_id": "u1", "text": "hi"}),
         fact(2, "assistant_output", {"message_id": "a-unknown", "text": "x", "final": False})],
        bare_session,
    )
    check("unresolvable provider identity is a typed drop, not an event",
          adapted.events == ()
          and {"fact_id": "fact-2", "code": "provider_identity_unresolvable"} in adapted.dropped)


def test_provider_stream_shadow_duplicate_is_ignored() -> None:
    session = {
        **SESSION,
        "messages": [
            *SESSION["messages"],
            {"id": "u2", "role": "user"},
            {"id": "a2", "role": "assistant",
             "run_meta": {"provider_id": "claude", "model": "sonnet-4-6", "reasoning_effort": "high"}},
        ],
    }
    adapted = adapt_chat_inputs(
        [
            fact(1, "user_prompt", {"message_id": "u1", "text": "first"}),
            fact(2, "message_ownership_declared", {"message_id": "a1", "prompt_message_id": "u1"}),
            fact(3, "user_prompt", {"message_id": "u2", "text": "second"}),
            fact(4, "message_ownership_declared", {"message_id": "a2", "prompt_message_id": "u2"}),
            fact(5, "assistant_output", {"message_id": "a1", "text": "same", "final": False},
                 source="provider_stream", source_event_id="dup"),
            fact(6, "assistant_output", {"message_id": "a2", "text": "same", "final": False},
                 source="apply_event", source_event_id="dup"),
            fact(7, "assistant_output", {"message_id": "a1", "text": "stream-only", "final": False},
                 source="provider_stream", source_event_id="stream-only"),
        ],
        session,
    )
    events_by_id = {event["event_id"]: event for event in adapted.events}
    check("authoritative duplicate replaces provider_stream shadow",
          events_by_id["dup"]["message_id"] == "a2"
          and events_by_id["dup"]["journal_seq"] == 6)
    check("provider_stream-only fact remains visible",
          events_by_id["stream-only"]["message_id"] == "a1")
    project_chat(adapted.messages, adapted.events, schema_version=CHAT_SCHEMA_VERSION)


def test_reused_source_event_id_across_messages_gets_scoped_projector_ids() -> None:
    session = {
        **SESSION,
        "messages": [
            *SESSION["messages"],
            {"id": "u2", "role": "user"},
            {"id": "a2", "role": "assistant",
             "run_meta": {"provider_id": "claude", "model": "sonnet-4-6", "reasoning_effort": "high"}},
        ],
    }
    adapted = adapt_chat_inputs(
        [
            fact(1, "user_prompt", {"message_id": "u1", "text": "first"}),
            fact(2, "message_ownership_declared", {"message_id": "a1", "prompt_message_id": "u1"}),
            fact(3, "user_prompt", {"message_id": "u2", "text": "second"}),
            fact(4, "message_ownership_declared", {"message_id": "a2", "prompt_message_id": "u2"}),
            fact(5, "assistant_output", {"message_id": "a1", "text": "same", "final": False},
                 source="apply_event", source_event_id="reused"),
            fact(6, "assistant_output", {"message_id": "a2", "text": "same", "final": False},
                 source="apply_event", source_event_id="reused"),
        ],
        session,
    )
    ids = [event["event_id"] for event in adapted.events if event["data"].get("text") == "same"]
    check("reused source event ids across messages are separated",
          ids == ["fact-5", "fact-6"])
    project_chat(adapted.messages, adapted.events, schema_version=CHAT_SCHEMA_VERSION)


def test_float_payload_values_are_sanitized_before_projector() -> None:
    # Regression: worker/trace passthrough facts (e.g. trace_step's
    # duration_ms, always a float in the raw payload) must not reach
    # chat_projector's strict no-float contract unsanitized, or every
    # session containing one 422s `GET /api/chat-tree`.
    adapted = adapt_chat_inputs(
        [fact(1, "user_prompt", {"message_id": "u1", "text": "hi"}),
         fact(2, "message_ownership_declared", {"message_id": "a1", "prompt_message_id": "u1"}),
         fact(3, "trace_step", {"message_id": "a1", "duration_ms": 4803.7,
                                 "nested": {"scores": [1.0, 2.25]}}),
         fact(4, "tool_call", {"message_id": "a1", "tool_use_id": "tu-1", "tool": "Bash",
                                "args": {"timeout_seconds": 2.5}})],
        SESSION,
    )
    work = next(e for e in adapted.events if e["type"] == "other_typed_work")
    assert work["data"]["payload"]["duration_ms"] == 4804
    assert isinstance(work["data"]["payload"]["duration_ms"], int)
    assert work["data"]["payload"]["nested"]["scores"] == [1, 2]
    tool_call = next(e for e in adapted.events if e["type"] == "tool_interaction")
    assert tool_call["data"]["args"]["timeout_seconds"] == 2
    assert isinstance(tool_call["data"]["args"]["timeout_seconds"], int)
    # Must not raise ChatProjectionInputError("invalid_payload", "... float").
    project_chat(adapted.messages, adapted.events, schema_version=CHAT_SCHEMA_VERSION)


def test_json_safe_leaves_non_finite_floats_for_the_projector_to_reject() -> None:
    assert _json_safe({"nan": float("nan")})["nan"] != _json_safe({"nan": float("nan")})["nan"]
    assert _json_safe(float("inf")) == float("inf")


if __name__ == "__main__":
    test_full_pipeline()
    test_unsupported_block_facts_stay_visible_as_typed_work()
    test_identity_fails_closed()
    test_provider_stream_shadow_duplicate_is_ignored()
    test_reused_source_event_id_across_messages_gets_scoped_projector_ids()
    test_float_payload_values_are_sanitized_before_projector()
    test_json_safe_leaves_non_finite_floats_for_the_projector_to_reject()
    if _failures:
        print(f"{len(_failures)} test(s) FAILED")
        sys.exit(1)
    print("all chat canonical adapter tests passed")
