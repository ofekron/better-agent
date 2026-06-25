"""Regression tests for Codex native metadata filtering.

Run with:
    cd backend && .venv/bin/python scripts/test_codex_metadata_filtering.py
"""

from __future__ import annotations

import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-codex-metadata-")

from codex_native import CodexRolloutNormalizer  # noqa: E402


PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _normalize(event: dict) -> list[dict]:
    return CodexRolloutNormalizer(namespace="metadata-test").normalize_line(
        json.dumps(event)
    )


def _normalize_many(events: list[dict]) -> list[dict]:
    normalizer = CodexRolloutNormalizer(namespace="metadata-test")
    rows: list[dict] = []
    for event in events:
        rows.extend(normalizer.normalize_line(json.dumps(event)))
    return rows


def _assistant_text(event: dict) -> str:
    content = (event.get("message") or {}).get("content")
    if not isinstance(content, list) or not content:
        return ""
    block = content[0]
    if not isinstance(block, dict):
        return ""
    return str(block.get("text") or "")


def _tool_result(event: dict) -> dict:
    content = (event.get("message") or {}).get("content")
    if not isinstance(content, list) or not content:
        return {}
    block = content[0]
    return block if isinstance(block, dict) and block.get("type") == "tool_result" else {}


def _spawn_agent_call(call_id: str, task_name: str) -> dict:
    return {
        "type": "response_item",
        "payload": {
            "type": "function_call",
            "name": "spawn_agent",
            "namespace": "collaboration",
            "call_id": call_id,
            "arguments": json.dumps({"task_name": task_name}),
        },
    }


def _spawn_agent_output(call_id: str, task_name: str) -> dict:
    return {
        "type": "response_item",
        "payload": {
            "type": "function_call_output",
            "call_id": call_id,
            "output": json.dumps({"task_name": task_name}),
        },
    }


def _subagent_message(agent_path: str, header: str, payload: str = "") -> dict:
    return {
        "type": "response_item",
        "payload": {
            "type": "agent_message",
            "author": agent_path,
            "recipient": "/root",
            "content": [{
                "type": "input_text",
                "text": (
                    f"Message Type: {header}\n"
                    "Task name: /root\n"
                    f"Sender: {agent_path}\n"
                    f"Payload:\n{payload}"
                ),
            }],
        },
    }


def test_thread_settings_applied_is_filtered() -> bool:
    rows = _normalize({
        "type": "event_msg",
        "payload": {
            "type": "thread_settings_applied",
            "thread_settings": {
                "model": "gpt-5.5",
                "cwd": "/Users/ofekron/better-claude",
                "approval_policy": "never",
            },
        },
    })
    if rows:
        print(f"  expected no render rows, got {rows!r}")
        return False
    return True


def test_world_state_is_filtered() -> bool:
    rows = _normalize({
        "type": "world_state",
        "payload": {
            "type": "world_state",
            "full": False,
            "state": {"agents_md": {"text": "rules"}},
        },
    })
    if rows:
        print(f"  expected no render rows, got {rows!r}")
        return False
    return True


def test_unknown_native_event_still_renders_debug_card() -> bool:
    rows = _normalize({
        "type": "event_msg",
        "payload": {"type": "new_visible_event", "value": 1},
    })
    if len(rows) != 1:
        print(f"  expected one debug render row, got {rows!r}")
        return False
    text = _assistant_text(rows[0])
    if "Codex native event_msg.new_visible_event" not in text:
        print(f"  expected native debug text, got {text!r}")
        return False
    return True


def test_subagent_final_answer_is_tool_result() -> bool:
    rows = _normalize_many([
        _spawn_agent_call("call_agent", "/root/trace_ui_mcp"),
        _spawn_agent_output("call_agent", "/root/trace_ui_mcp"),
        _subagent_message(
            "/root/trace_ui_mcp",
            "FINAL_ANSWER",
            "## Executive summary\n\n- traced UI MCP",
        ),
    ])
    results = [_tool_result(row) for row in rows]
    final_results = [
        result for result in results
        if result.get("tool_use_id") == "call_agent"
        and "traced UI MCP" in str(result.get("content") or "")
    ]
    if len(final_results) != 1:
        print(f"  expected one Agent tool_result with final answer, got {rows!r}")
        return False
    if any(_assistant_text(row).startswith("Codex native ") for row in rows):
        print(f"  expected no native debug card, got {rows!r}")
        return False
    return True


def test_final_answer_phase_is_stamped() -> bool:
    rows = _normalize_many([
        {
            "type": "event_msg",
            "payload": {
                "type": "agent_message",
                "message": "just commentary",
                "phase": "commentary",
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "agent_message",
                "message": "the final answer",
                "phase": "final_answer",
            },
        },
    ])
    commentary = [r for r in rows if _assistant_text(r) == "just commentary"]
    final = [r for r in rows if _assistant_text(r) == "the final answer"]
    if len(commentary) != 1 or commentary[0].get("final_answer"):
        print(f"  commentary must not carry final mark, got {rows!r}")
        return False
    if len(final) != 1 or final[0].get("final_answer") is not True:
        print(f"  final_answer phase must stamp the event, got {rows!r}")
        return False
    if final[0].get("final_answer_origin"):
        print(f"  main-agent final must have no origin, got {final[0]!r}")
        return False
    return True


def test_response_item_final_echo_is_stamped_when_not_deduped() -> bool:
    rows = _normalize({
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "assistant",
            "phase": "final_answer",
            "content": [{"type": "output_text", "text": "finalized only"}],
        },
    })
    if len(rows) != 1 or rows[0].get("final_answer") is not True:
        print(f"  expected stamped finalized echo, got {rows!r}")
        return False
    return True


def test_unmapped_subagent_final_answer_is_stamped_with_origin() -> bool:
    rows = _normalize_many([
        _subagent_message(
            "/root/trace_ui_mcp",
            "FINAL_ANSWER",
            "child conclusion",
        ),
    ])
    finals = [r for r in rows if r.get("final_answer") is True]
    if len(finals) != 1:
        print(f"  expected one stamped subagent final, got {rows!r}")
        return False
    if finals[0].get("final_answer_origin") != "/root/trace_ui_mcp":
        print(f"  expected author origin, got {finals[0]!r}")
        return False
    return True


def test_subagent_encrypted_message_is_not_raw_rendered() -> bool:
    rows = _normalize_many([
        _spawn_agent_call("call_agent", "/root/trace_ui_mcp"),
        _spawn_agent_output("call_agent", "/root/trace_ui_mcp"),
        {
            "type": "response_item",
            "payload": {
                "type": "agent_message",
                "author": "/root/trace_ui_mcp",
                "recipient": "/root",
                "content": [
                    {
                        "type": "input_text",
                        "text": (
                            "Message Type: MESSAGE\n"
                            "Task name: /root\n"
                            "Sender: /root/trace_ui_mcp\n"
                            "Payload:\n"
                        ),
                    },
                    {"type": "encrypted_content", "encrypted_content": "secret"},
                ],
            },
        },
    ])
    native_cards = [
        row for row in rows
        if _assistant_text(row).startswith("Codex native response_item.agent_message")
    ]
    if native_cards:
        print(f"  expected encrypted-only agent message to emit no native card, got {rows!r}")
        return False
    return True


def test_subagent_activity_is_lifecycle_notice() -> bool:
    rows = _normalize({
        "type": "event_msg",
        "payload": {
            "type": "sub_agent_activity",
            "agent_path": "/root/trace_ui_mcp",
            "agent_thread_id": "child-thread-id",
            "kind": "started",
            "event_id": "call_agent",
        },
    })
    if len(rows) != 1 or rows[0].get("type") != "lifecycle_notice":
        print(f"  expected one lifecycle notice, got {rows!r}")
        return False
    data = rows[0].get("data") or {}
    if data.get("kind") != "sub_agent_activity" or "trace_ui_mcp" not in data.get("message", ""):
        print(f"  unexpected lifecycle data: {data!r}")
        return False
    return (
        rows[0].get("codex_subagent_id") == "child-thread-id"
        and rows[0].get("parent_tool_use_id") == "call_agent"
    )


def test_non_spawn_subagent_activity_cannot_create_source() -> bool:
    cases = [
        {
            "type": "event_msg",
            "payload": {
                "type": "sub_agent_activity",
                "agent_thread_id": "child-thread-id",
                "agent_path": "/root/trace_ui_mcp",
                "kind": "interacted",
                "event_id": "call_followup",
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "sub_agent_activity",
                "agent_thread_id": "child-thread-id",
                "agent_path": "/root/trace_ui_mcp",
                "kind": "started",
                "event_id": "call_agent",
                "parent_tool_use_id": "conflicting_call",
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "sub_agent_activity",
                "agent_thread_id": "",
                "agent_path": "/root/trace_ui_mcp",
                "kind": "started",
                "event_id": "call_agent",
            },
        },
    ]
    for case in cases:
        rows = _normalize(case)
        if len(rows) != 1 or rows[0].get("codex_subagent_id"):
            print(f"  unsafe subagent source from {case!r}: {rows!r}")
            return False
    return True


def test_empty_inter_agent_metadata_is_filtered() -> bool:
    rows = _normalize({
        "type": "inter_agent_communication_metadata",
        "payload": {"trigger_turn": False},
    })
    if rows:
        print(f"  expected empty metadata to be filtered, got {rows!r}")
        return False
    return True


def test_rich_inter_agent_metadata_still_renders_debug_card() -> bool:
    rows = _normalize({
        "type": "inter_agent_communication_metadata",
        "payload": {"trigger_turn": False, "summary": "visible"},
    })
    if len(rows) != 1:
        print(f"  expected rich metadata fallback row, got {rows!r}")
        return False
    if "Codex native inter_agent_communication_metadata" not in _assistant_text(rows[0]):
        print(f"  expected native debug text, got {rows!r}")
        return False
    return True


def test_duplicate_subagent_path_does_not_cross_attach() -> bool:
    rows = _normalize_many([
        _spawn_agent_call("call_a", "/root/trace_ui_mcp"),
        _spawn_agent_output("call_a", "/root/trace_ui_mcp"),
        _spawn_agent_call("call_b", "/root/trace_ui_mcp"),
        _spawn_agent_output("call_b", "/root/trace_ui_mcp"),
        _subagent_message("/root/trace_ui_mcp", "FINAL_ANSWER", "ambiguous"),
    ])
    ambiguous_results = [
        _tool_result(row) for row in rows
        if _tool_result(row).get("content") == "ambiguous"
    ]
    if ambiguous_results:
        print(f"  ambiguous final answer must not attach to either Agent call: {rows!r}")
        return False
    if not any("ambiguous" in _assistant_text(row) for row in rows):
        print(f"  expected readable fallback text for ambiguous final answer, got {rows!r}")
        return False
    return True


TESTS = [
    ("thread_settings_applied is filtered", test_thread_settings_applied_is_filtered),
    ("world_state is filtered", test_world_state_is_filtered),
    ("subagent final answer is tool result", test_subagent_final_answer_is_tool_result),
    ("final_answer phase is stamped", test_final_answer_phase_is_stamped),
    (
        "response_item final echo stamped when not deduped",
        test_response_item_final_echo_is_stamped_when_not_deduped,
    ),
    (
        "unmapped subagent final stamped with origin",
        test_unmapped_subagent_final_answer_is_stamped_with_origin,
    ),
    ("subagent encrypted message is not raw rendered", test_subagent_encrypted_message_is_not_raw_rendered),
    ("subagent activity is lifecycle notice", test_subagent_activity_is_lifecycle_notice),
    ("non-spawn subagent activity cannot create source", test_non_spawn_subagent_activity_cannot_create_source),
    ("empty inter-agent metadata is filtered", test_empty_inter_agent_metadata_is_filtered),
    ("rich inter-agent metadata still renders debug card", test_rich_inter_agent_metadata_still_renders_debug_card),
    ("duplicate subagent path does not cross attach", test_duplicate_subagent_path_does_not_cross_attach),
    ("unknown native event still renders debug card", test_unknown_native_event_still_renders_debug_card),
]


def main() -> int:
    failed = False
    for name, fn in TESTS:
        try:
            ok = fn()
        except Exception as exc:
            ok = False
            print(f"  exception: {exc!r}")
        print(f"{PASS if ok else FAIL} {name}")
        failed = failed or not ok
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
