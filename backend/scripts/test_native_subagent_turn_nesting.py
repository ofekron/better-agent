"""Bug-fix tests: NativeSubagentTurn nesting in the BFF chat-tree grammar.

Regression bug: sidechain events for a native subagent call (Claude
Agent/Task tool, and provider-equivalents that stamp `parent_tool_use_id`,
e.g. Codex) rendered as flat siblings under the parent turn instead of a
nested `NativeSubagentTurn` scoped to that subagent
(manual-requirements/docs/chat-panel.md lines 19-26, 66, 155).

Root cause: the producer side never carried `parent_tool_use_id` from
journal rows into canonical facts, never emitted a `native_subagent_turn`
scoped fact, and hardcoded `parent_event_id=None` everywhere — so the
consumer side (`chat_projector._project_scoped_turns`) was unreachable
dead code.

  A. `canonical_facts_from_journal_row` + `adapt_chat_inputs` +
     `project_chat` on synthetic journal rows shaped like the real
     reproduction session (~/.better-claude/sessions/
     20772aff-aefa-4254-af5f-ee8b33020a98, seq 37-120: an Agent tool_use
     plus isSidechain rows) nest the sidechain content under one
     `NativeSubagentTurn` body item instead of flat top-level siblings.
  B. A Codex-shaped fixture (parent_tool_use_id stamped without Claude's
     isSidechain/uuid/parentUuid convention) nests identically — the fix
     is keyed on the normalized `parent_tool_use_id` field, never a
     provider branch.
  C. Facts without subagent linkage (a plain tool call, a Gemini-shaped
     row) render exactly as before — flat, no parent_event_id, no
     native_subagent_turn event.
  D. `chat_projection_ingestion.admit_canonical_fact` stamps a real
     `parent_event_id` on the stored projection for a linked fact, and
     `None` for an unlinked one (was hardcoded `None` unconditionally).

Run with:
    cd backend && .venv/bin/python scripts/test_native_subagent_turn_nesting.py
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-native-subagent-turn-")

import chat_projection_ingestion  # noqa: E402
from canonical_event_adapter import canonical_facts_from_journal_row, fact_to_wire  # noqa: E402
from chat_canonical_adapter import adapt_chat_inputs  # noqa: E402
from chat_models import CHAT_SCHEMA_VERSION, Explanation, ScopedTurn, Turn  # noqa: E402
from chat_projector import project_chat  # noqa: E402
from chat_tree_wire import chat_to_wire  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"
_failures: list[str] = []


def check(label: str, condition: bool) -> None:
    print(f"{PASS if condition else FAIL}  {label}")
    if not condition:
        _failures.append(label)


SESSION = {
    "id": "r", "provider_id": "claude", "model": "sonnet-4-6", "reasoning_effort": "high",
    "messages": [
        {"id": "u1", "role": "user"},
        {"id": "a1", "role": "assistant",
         "run_meta": {"provider_id": "claude", "model": "sonnet-4-6", "reasoning_effort": "high"}},
    ],
}


def _prefix_facts() -> list[dict]:
    return [
        {"canonical_seq": 1, "fact_id": "fact-1", "source_event_id": "event-1", "sid": "r",
         "payload_type": "user_prompt", "payload": {"message_id": "u1", "text": "go find it"},
         "observed_at": "2026-07-16T10:00:00+00:00", "source_timestamp": None},
        {"canonical_seq": 2, "fact_id": "fact-2", "source_event_id": "event-2", "sid": "r",
         "payload_type": "message_ownership_declared",
         "payload": {"message_id": "a1", "prompt_message_id": "u1"},
         "observed_at": "2026-07-16T10:00:01+00:00", "source_timestamp": None},
    ]


def _claude_shaped_rows() -> list[dict]:
    """Mirrors the real reproduction session's shape: an Agent tool_use,
    isSidechain rows carrying parent_tool_use_id, and the outer
    tool_result closing the call."""
    return [
        {"root_id": "r", "sid": "r", "seq": 1, "type": "agent_message", "source": "claude",
         "msg_id": "a1", "data": {
             "uuid": "e1", "type": "assistant", "isSidechain": False,
             "message": {"content": [{
                 "type": "tool_use", "id": "toolu_1", "name": "Agent",
                 "input": {"description": "Find X", "subagent_type": "Explore",
                           "prompt": "Explore the codebase for X"},
             }]},
         }},
        {"root_id": "r", "sid": "r", "seq": 2, "type": "agent_message", "source": "claude",
         "msg_id": "a1", "data": {
             "uuid": "c1", "parentUuid": None, "isSidechain": True,
             "parent_tool_use_id": "toolu_1", "type": "assistant",
             "message": {"content": [{
                 "type": "tool_use", "id": "tu_nested", "name": "Read",
                 "input": {"path": "file.py"},
             }]},
         }},
        {"root_id": "r", "sid": "r", "seq": 3, "type": "agent_message", "source": "claude",
         "msg_id": "a1", "data": {
             "uuid": "c2", "parentUuid": "c1", "isSidechain": True,
             "parent_tool_use_id": "toolu_1", "type": "user",
             "message": {"content": [{
                 "type": "tool_result", "tool_use_id": "tu_nested", "content": "file contents",
             }]},
         }},
        {"root_id": "r", "sid": "r", "seq": 4, "type": "agent_message", "source": "claude",
         "msg_id": "a1", "data": {
             "uuid": "c3", "parentUuid": "c2", "isSidechain": True,
             "parent_tool_use_id": "toolu_1", "type": "assistant", "final_answer": True,
             "message": {"content": [
                 {"type": "text", "text": "X is defined in file.py line 10"},
             ]},
         }},
        {"root_id": "r", "sid": "r", "seq": 5, "type": "agent_message", "source": "claude",
         "msg_id": "a1", "data": {
             "uuid": "e2", "parentUuid": "e1", "isSidechain": False, "type": "user",
             "message": {"content": [{
                 "type": "tool_result", "tool_use_id": "toolu_1",
                 "content": "X is defined in file.py line 10",
             }]},
         }},
    ]


def _codex_shaped_rows() -> list[dict]:
    """Codex normalization (codex_normalize.py `_with_parent_tool_use_id`)
    produces the same agent_message/assistant/user shape with
    `parent_tool_use_id` stamped from parent_call_id/threadId hints; no
    isSidechain/parentUuid at all. The fix must key on
    `parent_tool_use_id` alone, never a Claude-specific field."""
    return [
        {"root_id": "r", "sid": "r", "seq": 1, "type": "agent_message", "source": "codex",
         "msg_id": "a1", "data": {
             "uuid": "e1", "type": "assistant",
             "message": {"content": [{
                 "type": "tool_use", "id": "call_1", "name": "collab_agent_tool_call",
                 "input": {"description": "Find X", "subagent_type": "explore",
                           "prompt": "Explore the codebase for X"},
             }]},
         }},
        {"root_id": "r", "sid": "r", "seq": 2, "type": "agent_message", "source": "codex",
         "msg_id": "a1", "data": {
             "uuid": "c1", "parent_tool_use_id": "call_1", "type": "assistant", "final_answer": True,
             "message": {"content": [
                 {"type": "text", "text": "X is defined in file.py line 10"},
             ]},
         }},
        {"root_id": "r", "sid": "r", "seq": 3, "type": "agent_message", "source": "codex",
         "msg_id": "a1", "data": {
             "uuid": "e2", "type": "user",
             "message": {"content": [{
                 "type": "tool_result", "tool_use_id": "call_1", "content": "X is defined",
             }]},
         }},
    ]


def _unlinked_rows() -> list[dict]:
    """A plain (non-subagent) tool call/result and a Gemini-shaped row:
    none carry parent_tool_use_id."""
    return [
        {"root_id": "r", "sid": "r", "seq": 1, "type": "agent_message", "source": "claude",
         "msg_id": "a1", "data": {
             "uuid": "e1", "type": "assistant", "isSidechain": False,
             "message": {"content": [{
                 "type": "tool_use", "id": "tu_plain", "name": "Bash", "input": {"command": "ls"},
             }]},
         }},
        {"root_id": "r", "sid": "r", "seq": 2, "type": "agent_message", "source": "gemini",
         "msg_id": "a1", "data": {
             "uuid": "e2", "type": "user",
             "message": {"content": [{
                 "type": "tool_result", "tool_use_id": "tu_plain", "content": "ok",
             }]},
         }},
        {"root_id": "r", "sid": "r", "seq": 3, "type": "agent_message", "source": "claude",
         "msg_id": "a1", "data": {
             "uuid": "e3", "type": "assistant", "final_answer": True,
             "message": {"content": [{"type": "text", "text": "Done."}]},
         }},
    ]


def _render(rows: list[dict]):
    facts = list(_prefix_facts())
    seq = len(facts)
    for row in rows:
        for f in canonical_facts_from_journal_row(row):
            seq += 1
            facts.append(fact_to_wire(f, seq))
    adapted = adapt_chat_inputs(facts, SESSION)
    chat = project_chat(adapted.messages, adapted.events, schema_version=CHAT_SCHEMA_VERSION)
    return adapted, chat


def _visible_ids(turn: Turn) -> set[str]:
    ids: set[str] = set()
    for item in turn.body:
        if isinstance(item, Explanation):
            ids.update(item.item_ids)
            ids.update(item.text_event_ids)
        elif isinstance(item, ScopedTurn):
            ids.add(item.id)
    if turn.result:
        ids.update(turn.result.part_ids)
    return ids


def test_claude_sidechain_nests_under_native_subagent_turn() -> None:
    adapted, chat = _render(_claude_shaped_rows())
    check("no typed drops", adapted.dropped == ())
    turn = next(item for item in chat.items if isinstance(item, Turn))
    check("turn body has exactly one item: the NativeSubagentTurn",
          len(turn.body) == 1 and isinstance(turn.body[0], ScopedTurn)
          and turn.body[0].type == "NativeSubagentTurn")
    scoped = turn.body[0]
    check("scoped turn prompt carries the Agent tool's task text",
          scoped.prompt.text == "Explore the codebase for X")
    check("scoped turn result derives from its own nested final answer",
          scoped.result is not None
          and "X is defined in file.py line 10" in scoped.result.text)
    check("scoped turn has nested children (the Read tool call/result)",
          len(scoped.children) >= 1)
    check("parent turn's own visible ids are just the scoped turn node "
          "(sidechain content is not flattened into the parent turn)",
          _visible_ids(turn) == {scoped.id})

    wire = chat_to_wire(chat)
    wire_turn = next(item for item in wire if item["type"] == "Turn")
    check("wire body carries a NativeSubagentTurn node with nested children",
          wire_turn["body"][0]["type"] == "NativeSubagentTurn"
          and len(wire_turn["body"][0]["children"]) >= 1)


def test_codex_sidechain_nests_identically() -> None:
    adapted, chat = _render(_codex_shaped_rows())
    check("codex: no typed drops", adapted.dropped == ())
    turn = next(item for item in chat.items if isinstance(item, Turn))
    check("codex: turn body is exactly one NativeSubagentTurn",
          len(turn.body) == 1 and isinstance(turn.body[0], ScopedTurn)
          and turn.body[0].type == "NativeSubagentTurn")
    check("codex: parent turn's own visible ids are just the scoped turn node",
          _visible_ids(turn) == {turn.body[0].id})


def test_unlinked_facts_render_flat_unchanged() -> None:
    """Regression: facts without subagent linkage keep rendering exactly
    as before this fix — flat, no parent_event_id, no scoped turn."""
    adapted, chat = _render(_unlinked_rows())
    check("no typed drops", adapted.dropped == ())
    check("no emitted event carries a parent_event_id",
          all(event["parent_event_id"] is None for event in adapted.events))
    check("no event is typed native_subagent_turn",
          all(event["type"] != "native_subagent_turn" for event in adapted.events))
    turn = next(item for item in chat.items if isinstance(item, Turn))
    check("body stays a flat Explanation (the pre-existing shape)",
          len(turn.body) == 1 and isinstance(turn.body[0], Explanation))
    tool_events = [event for event in adapted.events if event["type"] == "tool_interaction"]
    check("plain tool call/result still render as the pre-existing separate pair",
          [event["data"].get("status") for event in tool_events] == ["running", "complete"])


def test_admit_canonical_fact_stamps_real_parent_event_id() -> None:
    """chat_projection_ingestion.admit_canonical_fact must stop hardcoding
    parent_event_id=None: a fact whose payload carries parent_tool_use_id
    stores the deterministic scope reference; one without it stores None."""
    def digest(value: str) -> str:
        return hashlib.sha256(value.encode()).hexdigest()

    root_id = "proj-root-1"

    def wire(seq: int, payload_type: str, payload: dict, event_id: str) -> dict:
        return {
            "canonical_seq": seq, "fact_id": f"pf-{seq}", "source_event_id": event_id,
            "root_id": root_id, "sid": root_id, "source": "provider_stream",
            "source_stream_id": "run-1", "content_hash": digest(f"pf-content-{seq}"),
            "payload_type": payload_type, "payload": payload,
            "observed_at": f"2026-07-16T10:00:{seq:02d}Z", "source_timestamp": None,
            "turn_id": "pu1",
        }

    chat_projection_ingestion.admit_canonical_fact(
        wire(1, "tool_call",
             {"message_id": "pa1", "tool_use_id": "call-x", "tool": "Bash", "args": {}},
             "tool_use:call-x"),
        provider="claude",
    )
    chat_projection_ingestion.admit_canonical_fact(
        wire(2, "thinking", {"message_id": "pa1", "text": "unlinked"}, "event-unlinked"),
        provider="claude",
    )
    chat_projection_ingestion.admit_canonical_fact(
        wire(3, "thinking",
             {"message_id": "pa1", "text": "linked", "parent_tool_use_id": "call-x"},
             "event-with-parent"),
        provider="claude",
    )

    service, catalog = chat_projection_ingestion._instances()
    generation = catalog.root_generation(root_id)
    authority = service.register(
        provider="claude", session_id=root_id, root_id=root_id,
        root_generation=generation, store_kind="jsonl",
    )
    unlinked = service.read_projection(authority, event_id="event-unlinked")
    linked = service.read_projection(authority, event_id="event-with-parent")
    check("fact without parent_tool_use_id stores parent_event_id=None",
          unlinked is not None and unlinked.parent_event_id is None)
    check("fact with parent_tool_use_id stores the deterministic scope reference",
          linked is not None and linked.parent_event_id == "tool_use:call-x")


if __name__ == "__main__":
    try:
        test_claude_sidechain_nests_under_native_subagent_turn()
        test_codex_sidechain_nests_identically()
        test_unlinked_facts_render_flat_unchanged()
        test_admit_canonical_fact_stamps_real_parent_event_id()
    finally:
        chat_projection_ingestion.close()
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
    if _failures:
        print(f"{len(_failures)} test(s) FAILED")
        sys.exit(1)
    print("all native subagent turn nesting tests passed")
