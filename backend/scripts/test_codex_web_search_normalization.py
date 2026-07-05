#!/usr/bin/env python3
"""Regression checks for Codex web-search event normalization."""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import threading
import time
from pathlib import Path

_BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_BACKEND))

from codex_normalize import (  # noqa: E402
    _attach_collab_parent_from_thread,
    _normalize_agent_message,
    _normalize_collab_agent_completed,
    _normalize_collab_agent_started,
    _normalize_native_payload,
    _normalize_response_item_event,
    _normalize_response_tool_call,
    _normalize_response_tool_result,
    _normalize_web_search_events,
    _normalize_web_search,
    _normalize_web_search_result,
    _remember_collab_receivers,
    _web_search_dedupe_key,
    _web_search_item_from_payload,
    _web_search_result_text,
)
from codex_native import (  # noqa: E402
    CodexRolloutNormalizer,
    CodexRolloutTailer,
    codex_subagent_delegation_id,
    codex_subagent_id_from_event,
    codex_subagent_ids_from_event,
    codex_subagent_sources_from_event,
    codex_subagent_rollout_start_byte,
)


def test_web_search_call_preserves_query_and_action() -> bool:
    item = {
        "id": "ws_1",
        "type": "web_search",
        "query": "embedding model leaderboard",
        "action": {"type": "search", "queries": ["embedding model leaderboard"]},
    }
    event = _normalize_web_search(item, "parent")
    block = event["message"]["content"][0]
    assert block["type"] == "tool_use"
    assert block["id"] == "ws_1"
    assert block["name"] == "WebSearch"
    assert block["input"]["query"] == "embedding model leaderboard"
    assert block["input"]["action"] == item["action"]
    assert _normalize_web_search_result(item, event["uuid"]) is None
    return True


def test_web_search_result_list_becomes_tool_result() -> bool:
    item = {
        "id": "ws_2",
        "results": [
            {
                "title": "Qwen3 Embedding",
                "url": "https://example.test/qwen",
                "snippet": "Embedding model family with retrieval benchmarks.",
            },
            {
                "title": "BGE M3",
                "link": "https://example.test/bge",
                "summary": "Multilingual retrieval model.",
            },
        ],
    }
    text = _web_search_result_text(item)
    assert "Qwen3 Embedding" in text
    assert "https://example.test/qwen" in text
    assert "Multilingual retrieval model." in text

    event = _normalize_web_search_result(item, "tool-use-uuid")
    assert event is not None
    block = event["message"]["content"][0]
    assert block["type"] == "tool_result"
    assert block["tool_use_id"] == "ws_2"
    assert block["content"] == text
    assert event["parentUuid"] == "tool-use-uuid"
    return True


def test_web_search_events_share_generated_id() -> bool:
    item = {
        "query": "embedding model leaderboard",
        "results": [
            {
                "title": "Qwen3 Embedding",
                "url": "https://example.test/qwen",
                "snippet": "Embedding model family with retrieval benchmarks.",
            },
        ],
    }
    events = _normalize_web_search_events(item, "parent")
    assert len(events) == 2

    tool_use = events[0]["message"]["content"][0]
    tool_result = events[1]["message"]["content"][0]
    assert tool_use["type"] == "tool_use"
    assert tool_result["type"] == "tool_result"
    assert tool_use["id"]
    assert tool_result["tool_use_id"] == tool_use["id"]
    assert events[1]["parentUuid"] == events[0]["uuid"]
    return True


def test_web_search_events_without_result_emit_only_call() -> bool:
    events = _normalize_web_search_events(
        {
            "id": "ws_empty",
            "query": "embedding model leaderboard",
            "action": {"type": "search"},
        },
        "parent",
    )
    assert len(events) == 1
    block = events[0]["message"]["content"][0]
    assert block["type"] == "tool_use"
    assert block["id"] == "ws_empty"
    return True


def test_response_item_function_call_normalizes_exec_command() -> bool:
    event, tool_use_id = _normalize_response_tool_call(
        {
            "type": "function_call",
            "name": "exec_command",
            "arguments": (
                '{"cmd":"rg -n \\"response_item\\" backend",'
                '"workdir":"/workspace/better-agent"}'
            ),
            "call_id": "call_exec",
        },
        "parent",
    )
    block = event["message"]["content"][0]
    assert tool_use_id == "call_exec"
    assert block["type"] == "tool_use"
    assert block["id"] == "call_exec"
    assert block["name"] == "Bash"
    assert block["input"]["cmd"] == 'rg -n "response_item" backend'
    assert block["input"]["command"] == 'rg -n "response_item" backend'
    return True


def test_response_item_function_call_maps_update_plan_to_todowrite() -> bool:
    import json as _json
    event, tool_use_id = _normalize_response_tool_call(
        {
            "type": "function_call",
            "name": "update_plan",
            "arguments": _json.dumps({
                "plan": [
                    {"step": "Inspect runner input contract", "status": "in_progress"},
                    {"step": "Create isolated A/B runner probe", "status": "pending"},
                ],
                "explanation": "Used immutable commit snapshots for the A/B.",
            }),
            "call_id": "call_plan",
        },
        "parent",
    )
    assert tool_use_id == "call_plan"
    block = event["message"]["content"][0]
    assert block["type"] == "tool_use"
    # call_id preserved so the function_call_output ("Plan updated") still pairs.
    assert block["id"] == "call_plan"
    # Mapped to TodoWrite so the Todos extension reconstructs it as current_todos.
    assert block["name"] == "TodoWrite"
    assert block["input"]["todos"] == [
        {"content": "Inspect runner input contract", "status": "in_progress", "activeForm": None},
        {"content": "Create isolated A/B runner probe", "status": "pending", "activeForm": None},
    ]
    # Explanation has no TodoWrite slot — dropped, not leaked into input.
    assert "explanation" not in block["input"]
    return True


def test_response_item_function_output_attaches_by_call_id() -> bool:
    event, tool_use_id = _normalize_response_tool_result(
        {
            "type": "function_call_output",
            "call_id": "call_exec",
            "output": "Chunk ID: 123\nOutput:\nmatched\n",
        },
        "tool-use-uuid",
    )
    assert tool_use_id == "call_exec"
    assert event["type"] == "user"
    block = event["message"]["content"][0]
    assert block["type"] == "tool_result"
    assert block["tool_use_id"] == "call_exec"
    assert "matched" in block["content"]
    assert event["parentUuid"] == "tool-use-uuid"
    return True


def test_custom_tool_call_preserves_apply_patch_input() -> bool:
    event = _normalize_response_item_event(
        {
            "type": "custom_tool_call",
            "status": "completed",
            "call_id": "call_patch",
            "name": "apply_patch",
            "input": "*** Begin Patch\n*** End Patch\n",
        },
        "parent",
    )
    assert event is not None
    block = event["message"]["content"][0]
    assert block["id"] == "call_patch"
    assert block["name"] == "apply_patch"
    assert block["input"]["value"].startswith("*** Begin Patch")
    return True


def test_response_item_assistant_message_becomes_text_event() -> bool:
    event = _normalize_response_item_event(
        {
            "type": "message",
            "id": "msg_1",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "done"}],
        },
        "parent",
    )
    assert event is not None
    block = event["message"]["content"][0]
    assert block["type"] == "text"
    assert block["text"] == "done"
    return True


def test_response_item_assistant_message_uuid_is_stable() -> bool:
    payload = {
        "type": "message",
        "id": "msg_stable",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "done"}],
    }
    first = _normalize_response_item_event(payload, "parent")
    second = _normalize_response_item_event(payload, "parent")
    assert first is not None
    assert second is not None
    assert first["uuid"] == second["uuid"]
    return True


def test_response_item_render_branches_keep_stable_uuids() -> bool:
    payloads = [
        {
            "type": "reasoning",
            "id": "reasoning_1",
            "summary": [{"type": "summary_text", "text": "checked parser"}],
        },
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "exec_command",
            "arguments": {"cmd": "pwd"},
        },
        {
            "type": "function_call_output",
            "call_id": "call_1",
            "output": "/tmp/project",
        },
        {
            "type": "web_search_call",
            "id": "search_1",
            "action": {"query": "Better Agent"},
        },
        {
            "type": "future_shape",
            "id": "future_1",
            "value": {"ok": True},
        },
    ]
    for payload in payloads:
        first = _normalize_response_item_event(payload, "parent")
        second = _normalize_response_item_event(payload, "parent")
        assert first is not None, payload
        assert second is not None, payload
        assert first["uuid"] == second["uuid"], payload
        assert first["parentUuid"] == second["parentUuid"], payload
    return True


def test_codex_rollout_item_replay_keeps_stable_uuids() -> bool:
    raw_events = [
        {
            "type": "item.started",
            "timestamp": "2026-01-01T00:00:00Z",
            "item": {
                "id": "cmd_1",
                "type": "command_execution",
                "command": "pwd",
            },
        },
        {
            "type": "item.completed",
            "timestamp": "2026-01-01T00:00:01Z",
            "item": {
                "id": "msg_1",
                "type": "agent_message",
                "text": "Progress update",
            },
        },
    ]
    first = CodexRolloutNormalizer(namespace="thread-1")
    second = CodexRolloutNormalizer(namespace="thread-1")
    first_rows = [row for raw in raw_events for row in first.normalize_event(raw)]
    second_rows = [row for raw in raw_events for row in second.normalize_event(raw)]
    assert [row["uuid"] for row in first_rows] == [row["uuid"] for row in second_rows]
    assert [row["parentUuid"] for row in first_rows] == [row["parentUuid"] for row in second_rows]
    return True


def test_codex_rollout_event_msg_replay_keeps_stable_parent_chain() -> bool:
    raw_events = [
        {
            "type": "event_msg",
            "timestamp": "2026-01-01T00:00:00Z",
            "payload": {"type": "agent_message", "message": "Progress update"},
        },
        {
            "type": "event_msg",
            "timestamp": "2026-01-01T00:00:01Z",
            "payload": {"type": "agent_reasoning", "message": "checking"},
        },
    ]
    first = CodexRolloutNormalizer(namespace="thread-1")
    second = CodexRolloutNormalizer(namespace="thread-1")
    first_rows = [row for raw in raw_events for row in first.normalize_event(raw)]
    second_rows = [row for raw in raw_events for row in second.normalize_event(raw)]
    assert [row["uuid"] for row in first_rows] == [row["uuid"] for row in second_rows]
    assert [row["parentUuid"] for row in first_rows] == [row["parentUuid"] for row in second_rows]
    return True


def test_response_item_user_message_without_subagent_notification_is_skipped() -> bool:
    event = _normalize_response_item_event(
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "prompt"}],
        },
        "parent",
    )
    assert event is None
    return True


def test_response_item_reasoning_summary_becomes_thinking() -> bool:
    event = _normalize_response_item_event(
        {
            "type": "reasoning",
            "summary": [{"type": "summary_text", "text": "checked parser"}],
        },
        "parent",
    )
    assert event is not None
    block = event["message"]["content"][0]
    assert block["type"] == "thinking"
    assert block["thinking"] == "checked parser"
    return True


def test_empty_encrypted_reasoning_is_skipped() -> bool:
    # Encrypted-only reasoning has no renderable content; it must not produce
    # a card (no placeholder text, no raw native-payload fallback either).
    event = _normalize_response_item_event(
        {
            "type": "reasoning",
            "summary": [],
            "encrypted_content": "opaque",
        },
        "parent",
    )
    assert event is None
    return True


def test_codex_rollout_keeps_bookkeeping_envelopes_visible() -> bool:
    normalizer = CodexRolloutNormalizer(namespace="thread")
    for payload_type in ("turn_diff",):
        rows = normalizer.normalize_event({
            "type": "event_msg",
            "payload": {"type": payload_type, "message": "noise"},
        })
        assert len(rows) == 1, payload_type
        block = rows[0]["message"]["content"][0]
        assert block["type"] == "text"
        assert f"Codex native event_msg.{payload_type}" in block["text"]
    user_rows = normalizer.normalize_event({
        "type": "event_msg",
        "payload": {"type": "user_message", "message": "duplicate prompt"},
    })
    # user_message echoes response_item.message and is owned by BC scaffolds.
    assert user_rows == []
    ctx_rows = normalizer.normalize_event({
        "type": "turn_context",
        "payload": {"turn_id": "turn-1", "cwd": "/tmp", "model": "gpt-5.5"},
    })
    # turn_context is operational metadata, never rendered.
    assert ctx_rows == []
    return True


def test_codex_rollout_digests_known_event_msg_primitives() -> bool:
    normalizer = CodexRolloutNormalizer(namespace="thread")
    cases = [
        (
            {
                "type": "patch_apply_end",
                "call_id": "call_patch",
                "stdout": "Success. Updated files.",
                "success": True,
            },
            "Success. Updated files.",
        ),
        ({"type": "agent_reasoning", "message": "checking"}, "checking"),
    ]
    for payload, expected in cases:
        rows = normalizer.normalize_event({"type": "event_msg", "payload": payload})
        assert len(rows) == 1
        block = rows[0]["message"]["content"][0]
        text = block.get("text") or block.get("content") or block.get("thinking")
        assert expected in text, (payload, text)
        if payload["type"] != "patch_apply_end":
            assert not str(text).startswith("Codex native event_msg."), text
    # token_count, task_complete, and task_started are operational metadata,
    # not rendered chat context.
    assert normalizer.normalize_event({
        "type": "event_msg",
        "payload": {"type": "task_complete", "usage": {"total_tokens": 42}},
    }) == []
    assert normalizer.normalize_event({
        "type": "event_msg",
        "payload": {"type": "token_count", "info": {"last_token_usage": {"total_tokens": 12}}},
    }) == []
    return True


def test_codex_rollout_context_compacted_renders_readable_notice() -> bool:
    normalizer = CodexRolloutNormalizer(namespace="thread")
    rows = normalizer.normalize_event({
        "type": "event_msg",
        "payload": {"type": "context_compacted"},
    })
    assert len(rows) == 1
    assert rows[0]["type"] == "lifecycle_notice"
    assert rows[0]["data"]["kind"] == "context_compacted"
    assert rows[0]["data"]["message"] == "Context compacted"
    assert "message" not in rows[0]
    return True


def test_codex_rollout_compacted_renders_replacement_history() -> bool:
    normalizer = CodexRolloutNormalizer(namespace="thread")
    rows = normalizer.normalize_event({
        "type": "compacted",
        "payload": {
            "replacement_history": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "original ask"}],
                },
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "compact summary"}],
                },
            ],
        },
    })
    assert len(rows) == 1
    assert rows[0]["type"] == "lifecycle_notice"
    assert rows[0]["data"]["kind"] == "compacted"
    assert rows[0]["data"]["replacement_history"] == [
        {"role": "user", "text": "original ask"},
        {"role": "assistant", "text": "compact summary"},
    ]
    return True


def test_codex_rollout_compacted_replaces_context_compacted_notice() -> bool:
    normalizer = CodexRolloutNormalizer(namespace="thread")
    notice_rows = normalizer.normalize_event({
        "type": "event_msg",
        "payload": {"type": "context_compacted"},
    })
    detail_rows = normalizer.normalize_event({
        "type": "compacted",
        "payload": {
            "replacement_history": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "original ask"}],
                },
            ],
        },
    })
    assert len(notice_rows) == 1
    assert len(detail_rows) == 1
    assert detail_rows[0]["uuid"] == notice_rows[0]["uuid"]
    assert detail_rows[0]["data"]["kind"] == "compacted"
    assert detail_rows[0]["data"]["replacement_history"] == [
        {"role": "user", "text": "original ask"},
    ]
    return True


def test_codex_rollout_turn_aborted_renders_readable_notice() -> bool:
    normalizer = CodexRolloutNormalizer(namespace="thread")
    rows = normalizer.normalize_event({
        "type": "event_msg",
        "payload": {
            "type": "turn_aborted",
            "turn_id": "019ed2bd-78f8-73f2-b63d-6499a5482626",
            "reason": "interrupted",
            "completed_at": 1781653400,
            "duration_ms": 1307799,
        },
    })
    assert len(rows) == 1
    assert rows[0]["type"] == "lifecycle_notice"
    assert rows[0]["data"]["kind"] == "turn_aborted"
    assert rows[0]["data"]["message"] == "Turn interrupted after 21m 48s"
    assert "message" not in rows[0]
    return True


def test_codex_rollout_mcp_tool_call_end_becomes_tool_pair() -> bool:
    normalizer = CodexRolloutNormalizer(namespace="thread")
    rows = normalizer.normalize_event({
        "type": "event_msg",
        "timestamp": "2026-06-14T08:36:50.902Z",
        "payload": {
            "type": "mcp_tool_call_end",
            "call_id": "call_mcp",
            "invocation": {
                "server": "node_repl",
                "tool": "js",
                "arguments": {
                    "code": "nodeRepl.write('ok')",
                    "title": "Check result",
                },
            },
            "duration": {"secs": 0, "nanos": 244747167},
            "result": {
                "Ok": {
                    "content": [{"type": "text", "text": "{\"ok\":true}"}],
                    "isError": False,
                },
            },
        },
    })
    assert len(rows) == 2
    tool_block = rows[0]["message"]["content"][0]
    result_block = rows[1]["message"]["content"][0]
    assert tool_block["type"] == "tool_use"
    assert tool_block["id"] == "call_mcp"
    assert tool_block["name"] == "mcp__node_repl__js"
    assert tool_block["input"]["title"] == "Check result"
    assert result_block["type"] == "tool_result"
    assert result_block["tool_use_id"] == "call_mcp"
    assert result_block["content"] == "{\"ok\":true}"
    assert rows[1]["parentUuid"] == rows[0]["uuid"]
    assert rows[1]["uuid"] != rows[0]["uuid"]
    assert "Codex native event_msg.mcp_tool_call_end" not in str(rows)
    return True


def _codex_tool_call_event(call_id: str = "call_mcp") -> dict:
    return {
        "type": "response_item",
        "payload": {
            "type": "function_call",
            "name": "lock_ops",
            "call_id": call_id,
            "arguments": json.dumps({"release": True}),
        },
    }


def _codex_mcp_tool_call_end_event(call_id: str = "call_mcp") -> dict:
    return {
        "type": "event_msg",
        "timestamp": "2026-06-14T08:36:50.902Z",
        "payload": {
            "type": "mcp_tool_call_end",
            "call_id": call_id,
            "invocation": {
                "server": "better_agent_coordination",
                "tool": "lock_ops",
                "arguments": {"release": True},
            },
            "result": {
                "Ok": {
                    "content": [{"type": "text", "text": "{\"success\":false}"}],
                    "isError": False,
                },
            },
        },
    }


def _codex_function_call_output_event(call_id: str = "call_mcp") -> dict:
    return {
        "type": "response_item",
        "payload": {
            "type": "function_call_output",
            "call_id": call_id,
            "output": "Wall time: 0.0056 seconds\nOutput:\n{\"success\":false}",
        },
    }


def _tool_result_rows(rows: list[dict]) -> list[dict]:
    return [
        row for row in rows
        if ((row.get("message") or {}).get("content") or [{}])[0].get("type") == "tool_result"
    ]


def test_codex_rollout_mcp_end_suppresses_response_output_duplicate() -> bool:
    normalizer = CodexRolloutNormalizer(namespace="thread")
    rows: list[dict] = []
    rows.extend(normalizer.normalize_event(_codex_tool_call_event()))
    rows.extend(normalizer.normalize_event(_codex_mcp_tool_call_end_event()))
    rows.extend(normalizer.normalize_event(_codex_function_call_output_event()))

    result_rows = _tool_result_rows(rows)
    assert len(result_rows) == 1
    block = result_rows[0]["message"]["content"][0]
    assert block["tool_use_id"] == "call_mcp"
    assert block["content"] == "{\"success\":false}"
    assert "Wall time" not in str(rows)
    return True


def test_codex_rollout_response_output_without_mcp_end_still_emits() -> bool:
    normalizer = CodexRolloutNormalizer(namespace="thread")
    rows: list[dict] = []
    rows.extend(normalizer.normalize_event(_codex_tool_call_event()))
    rows.extend(normalizer.normalize_event(_codex_function_call_output_event()))

    result_rows = _tool_result_rows(rows)
    assert len(result_rows) == 1
    block = result_rows[0]["message"]["content"][0]
    assert block["tool_use_id"] == "call_mcp"
    assert "Wall time" in block["content"]
    return True


def test_codex_rollout_distinct_tool_results_both_emit() -> bool:
    normalizer = CodexRolloutNormalizer(namespace="thread")
    rows: list[dict] = []
    rows.extend(normalizer.normalize_event(_codex_tool_call_event("call_one")))
    rows.extend(normalizer.normalize_event(_codex_mcp_tool_call_end_event("call_one")))
    rows.extend(normalizer.normalize_event(_codex_tool_call_event("call_two")))
    rows.extend(normalizer.normalize_event(_codex_mcp_tool_call_end_event("call_two")))

    ids = [
        row["message"]["content"][0]["tool_use_id"]
        for row in _tool_result_rows(rows)
    ]
    assert ids == ["call_one", "call_two"]
    return True


def test_codex_rollout_response_first_suppresses_late_mcp_end() -> bool:
    normalizer = CodexRolloutNormalizer(namespace="thread")
    rows: list[dict] = []
    rows.extend(normalizer.normalize_event(_codex_tool_call_event()))
    rows.extend(normalizer.normalize_event(_codex_function_call_output_event()))
    rows.extend(normalizer.normalize_event(_codex_mcp_tool_call_end_event()))

    result_rows = _tool_result_rows(rows)
    assert len(result_rows) == 1
    assert result_rows[0]["message"]["content"][0]["content"].startswith("Wall time")
    assert len([row for row in rows if ((row.get("message") or {}).get("content") or [{}])[0].get("type") == "tool_use"]) == 1
    return True


def test_event_msg_task_started_not_rendered() -> bool:
    normalizer = CodexRolloutNormalizer(namespace="thread")
    rows = normalizer.normalize_event({
        "type": "event_msg",
        "payload": {
            "type": "task_started",
            "turn_id": "019ed0ff-630a-7b30-b23b-6b7f2c88d05f",
            "model_context_window": 258400,
            "collaboration_mode_kind": "default",
        },
    })
    assert rows == [], f"task_started should not render: {rows}"
    return True


def test_codex_rollout_token_count_captures_context_window_and_fill() -> bool:
    # token_count is not a card; its model_context_window/last_token_usage is
    # surfaced on the normalizer so the caller can route it into the
    # context-window UI channel via the complete envelope.
    normalizer = CodexRolloutNormalizer(namespace="thread")
    normalizer.normalize_event({
        "type": "event_msg",
        "payload": {"type": "token_count", "info": {
            "model_context_window": 258400,
            "total_token_usage": {"total_tokens": 120000},
            "last_token_usage": {"total_tokens": 64000},
        }},
    })
    assert normalizer.context_window == 258400
    assert normalizer.context_tokens == 64000
    # last-seen wins
    normalizer.normalize_event({
        "type": "event_msg",
        "payload": {"type": "token_count", "info": {
            "model_context_window": 200000,
            "total_token_usage": {"total_tokens": 180000},
            "last_token_usage": {"total_tokens": 170000},
        }},
    })
    assert normalizer.context_window == 200000
    assert normalizer.context_tokens == 170000
    return True


def test_codex_rollout_tailer_emits_context_updates() -> bool:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "rollout.jsonl"
        path.write_text(
            json.dumps({
                "type": "event_msg",
                "payload": {"type": "token_count", "info": {
                    "model_context_window": 200000,
                    "last_token_usage": {"total_tokens": 180000},
                }},
            }) + "\n",
            encoding="utf-8",
        )
        updates: list[tuple[int | None, int | None]] = []
        rendered: list[dict] = []

        async def _go() -> None:
            tailer = CodexRolloutTailer(
                path=path,
                start_byte=0,
                namespace="thread",
                dispatch=lambda event: rendered.append(event),
                on_context_update=lambda window, tokens: updates.append((window, tokens)),
            )
            await tailer.drain_available()

        asyncio.run(_go())
    assert updates == [(200000, 180000)]
    assert rendered == []
    return True


def _rollout_agent_line(text: str) -> str:
    return json.dumps({
        "type": "event_msg",
        "payload": {"type": "agent_message", "message": text},
    }) + "\n"


def test_codex_rollout_tailer_keeps_partial_line_pending() -> bool:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "rollout.jsonl"
        first = _rollout_agent_line("complete")
        partial = json.dumps({
            "type": "event_msg",
            "payload": {"type": "agent_message", "message": "partial"},
        })
        path.write_text(first + partial, encoding="utf-8")
        rendered: list[dict] = []
        cursors: list[int] = []

        async def _go() -> CodexRolloutTailer:
            tailer = CodexRolloutTailer(
                path=path,
                start_byte=0,
                namespace="thread",
                dispatch=lambda event: rendered.append(event),
                on_cursor_advance=lambda cursor: cursors.append(cursor),
            )
            assert await tailer.drain_available() is True
            return tailer

        tailer = asyncio.run(_go())
        first_cursor = len(first.encode("utf-8"))
        assert tailer.processed_byte == first_cursor
        assert cursors == [first_cursor]
        assert [ev["message"]["content"][0]["text"] for ev in rendered] == ["complete"]

        with path.open("a", encoding="utf-8") as fh:
            fh.write("\n")

        async def _again() -> None:
            assert await tailer.drain_available() is True

        asyncio.run(_again())
        final_cursor = len((first + partial + "\n").encode("utf-8"))
        assert tailer.processed_byte == final_cursor
        assert cursors == [first_cursor, final_cursor]
        assert [ev["message"]["content"][0]["text"] for ev in rendered] == [
            "complete",
            "partial",
        ]
    return True


def test_codex_rollout_tailer_advances_cursor_after_dispatch() -> bool:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "rollout.jsonl"
        first = _rollout_agent_line("one")
        second = _rollout_agent_line("two")
        path.write_text(first + second, encoding="utf-8")
        order: list[tuple[str, int]] = []

        async def _go() -> None:
            tailer: CodexRolloutTailer | None = None

            def _dispatch(_event: dict) -> None:
                assert tailer is not None
                order.append(("dispatch", tailer.processed_byte))

            def _cursor(cursor: int) -> None:
                order.append(("cursor", cursor))

            tailer = CodexRolloutTailer(
                path=path,
                start_byte=0,
                namespace="thread",
                dispatch=_dispatch,
                on_cursor_advance=_cursor,
            )
            assert await tailer.drain_available() is True

        asyncio.run(_go())
        first_cursor = len(first.encode("utf-8"))
        second_cursor = len((first + second).encode("utf-8"))
        assert order == [
            ("dispatch", 0),
            ("cursor", first_cursor),
            ("dispatch", first_cursor),
            ("cursor", second_cursor),
        ]
    return True


class _BlockingOpenPath:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.release = threading.Event()

    def open(self, *args, **kwargs):
        self.release.wait(timeout=0.35)
        return self.path.open(*args, **kwargs)


def test_codex_rollout_tailer_file_read_does_not_block_loop() -> bool:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "rollout.jsonl"
        path.write_text(_rollout_agent_line("nonblocking"), encoding="utf-8")
        blocking_path = _BlockingOpenPath(path)
        rendered: list[dict] = []

        async def _go() -> float:
            tailer = CodexRolloutTailer(
                path=blocking_path,  # type: ignore[arg-type]
                start_byte=0,
                namespace="thread",
                dispatch=lambda event: rendered.append(event),
            )
            timer = threading.Timer(0.35, blocking_path.release.set)
            timer.start()
            task = asyncio.create_task(tailer.drain_available())
            started = time.perf_counter()
            try:
                await asyncio.sleep(0.05)
                elapsed = time.perf_counter() - started
            finally:
                blocking_path.release.set()
                timer.cancel()
            assert await asyncio.wait_for(task, timeout=2) is True
            return elapsed

        elapsed = asyncio.run(_go())
        assert elapsed < 0.22
        assert [ev["message"]["content"][0]["text"] for ev in rendered] == ["nonblocking"]
    return True


def test_codex_rollout_assistant_text_dedup_is_lossless() -> bool:
    # Codex streams every assistant utterance via event_msg.agent_message
    # (including intermediate commentary) and re-emits only the finalized
    # answer as response_item.message. The finalized copy that duplicates an
    # already-streamed utterance is dropped; a finalized message never streamed
    # still renders (lossless). Intermediates are never dropped.
    normalizer = CodexRolloutNormalizer(namespace="thread")

    intermediate = normalizer.normalize_event({
        "type": "event_msg",
        "timestamp": "2026-06-16T11:55:38.100Z",
        "payload": {"type": "agent_message", "message": "Looking into it."},
    })
    assert len(intermediate) == 1
    assert intermediate[0]["message"]["content"][0]["text"] == "Looking into it."

    streamed_final = normalizer.normalize_event({
        "type": "event_msg",
        "timestamp": "2026-06-16T11:55:38.270Z",
        "payload": {"type": "agent_message", "message": "Readable answer"},
    })
    assert len(streamed_final) == 1

    # Finalized echo of an already-streamed utterance -> dropped.
    echo_rows = normalizer.normalize_event({
        "type": "response_item",
        "timestamp": "2026-06-16T11:55:38.273Z",
        "payload": {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "Readable answer"}],
        },
    })
    assert echo_rows == []

    # Finalized message never streamed this turn -> still renders.
    unstreamed_rows = normalizer.normalize_event({
        "type": "response_item",
        "timestamp": "2026-06-16T11:55:38.500Z",
        "payload": {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "Only in response_item"}],
        },
    })
    assert len(unstreamed_rows) == 1
    assert unstreamed_rows[0]["message"]["content"][0]["text"] == "Only in response_item"
    return True


def test_codex_rollout_assistant_text_dedup_scopes_per_turn() -> bool:
    # Identical assistant text in different turns must both render; the dedup
    # set resets at each turn_context boundary.
    normalizer = CodexRolloutNormalizer(namespace="thread")
    for turn in (1, 2):
        normalizer.normalize_event({"type": "turn_context", "timestamp": f"2026-01-0{turn}T00:00:00.000Z", "payload": {"turn_id": f"t{turn}"}})
        rows = normalizer.normalize_event({
            "type": "event_msg",
            "timestamp": f"2026-01-0{turn}T00:00:01.000Z",
            "payload": {"type": "agent_message", "message": "Compact task completed"},
        })
        assert len(rows) == 1, turn
    return True


def test_codex_rollout_response_item_subagent_notification_passes_through() -> bool:
    # A response_item.message that yields a non-assistant event (subagent
    # notification) must NOT be dropped by the assistant-text dedup path.
    import json as _json
    notification = _json.dumps({"agent_path": "child_1", "status": "completed"})
    rows = CodexRolloutNormalizer(namespace="thread").normalize_event({
        "type": "response_item",
        "timestamp": "2026-06-16T11:55:39.000Z",
        "payload": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": f"<subagent_notification>{notification}</subagent_notification>"}],
        },
    })
    assert len(rows) == 1
    assert rows[0]["type"] == "user"
    assert rows[0]["timestamp"] == "2026-06-16T11:55:39.000Z"
    return True


def test_codex_rollout_subagent_notification_attaches_to_agent_tool() -> bool:
    import json as _json
    normalizer = CodexRolloutNormalizer(namespace="thread")
    tool_rows = normalizer.normalize_event({
        "type": "response_item",
        "timestamp": "2026-06-16T11:55:37.000Z",
        "payload": {
            "type": "function_call",
            "name": "spawn_agent",
            "call_id": "call_agent",
            "arguments": _json.dumps({
                "agent_type": "explorer",
                "message": "Find runner_codex.py",
            }),
        },
    })
    assert len(tool_rows) == 1
    agent_event_uuid = tool_rows[0]["uuid"]

    result_rows = normalizer.normalize_event({
        "type": "response_item",
        "timestamp": "2026-06-16T11:55:38.000Z",
        "payload": {
            "type": "function_call_output",
            "call_id": "call_agent",
            "output": _json.dumps({"agent_id": "agent-1", "nickname": "Turing"}),
        },
    })
    assert len(result_rows) == 1

    notification = _json.dumps({
        "agent_path": "agent-1",
        "status": {"completed": "Found backend/runner_codex.py"},
    })
    rows = normalizer.normalize_event({
        "type": "response_item",
        "timestamp": "2026-06-16T11:55:39.000Z",
        "payload": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": f"<subagent_notification>{notification}</subagent_notification>"}],
        },
    })
    assert len(rows) == 1
    block = rows[0]["message"]["content"][0]
    assert block["type"] == "tool_result"
    assert block["tool_use_id"] == "call_agent"
    assert rows[0]["parentUuid"] == agent_event_uuid
    return True


def test_codex_subagent_id_from_spawn_result() -> bool:
    normalizer = CodexRolloutNormalizer(namespace="thread")
    normalizer.normalize_event({
        "type": "response_item",
        "timestamp": "2026-06-16T11:55:37.000Z",
        "payload": {
            "type": "function_call",
            "name": "spawn_agent",
            "call_id": "call_agent",
            "arguments": "{\"message\":\"review\"}",
        },
    })
    rows = normalizer.normalize_event({
        "type": "response_item",
        "timestamp": "2026-06-16T11:55:38.000Z",
        "payload": {
            "type": "function_call_output",
            "call_id": "call_agent",
            "output": "{\"agent_id\":\"agent-1\",\"nickname\":\"Turing\"}",
        },
    })
    assert len(rows) == 1
    assert codex_subagent_id_from_event(rows[0]) == "agent-1"
    invalid_normalizer = CodexRolloutNormalizer(namespace="thread")
    invalid_normalizer.normalize_event({
        "type": "response_item",
        "timestamp": "2026-06-16T11:55:37.000Z",
        "payload": {
            "type": "function_call",
            "name": "spawn_agent",
            "call_id": "call_agent",
            "arguments": "{\"message\":\"review\"}",
        },
    })
    invalid_rows = invalid_normalizer.normalize_event({
        "type": "response_item",
        "timestamp": "2026-06-16T11:55:38.000Z",
        "payload": {
            "type": "function_call_output",
            "call_id": "call_agent",
            "output": "spawn failed",
        },
    })
    assert codex_subagent_id_from_event(invalid_rows[0]) is None
    return True


def test_codex_subagent_ids_from_plural_spawn_result() -> bool:
    normalizer = CodexRolloutNormalizer(namespace="thread")
    normalizer.normalize_event({
        "type": "response_item",
        "timestamp": "2026-06-16T11:55:37.000Z",
        "payload": {
            "type": "function_call",
            "name": "spawn_agents",
            "call_id": "call_agents",
            "arguments": "{\"messages\":[\"a\",\"b\"]}",
        },
    })
    rows = normalizer.normalize_event({
        "type": "response_item",
        "timestamp": "2026-06-16T11:55:38.000Z",
        "payload": {
            "type": "function_call_output",
            "call_id": "call_agents",
            "output": "{\"agent_ids\":[\"agent-1\",\"agent-2\"],\"agents\":[{\"agent_id\":\"agent-3\"}]}",
        },
    })
    assert len(rows) == 1
    assert codex_subagent_ids_from_event(rows[0]) == ["agent-1", "agent-2", "agent-3"]
    return True


def test_codex_subagent_ids_from_wait_agent_result() -> bool:
    normalizer = CodexRolloutNormalizer(namespace="thread")
    normalizer.normalize_event({
        "type": "response_item",
        "timestamp": "2026-06-21T13:46:14.985Z",
        "payload": {
            "type": "function_call",
            "name": "wait_agent",
            "call_id": "call_wait",
            "arguments": "{\"targets\":[\"agent-1\",\"agent-2\"],\"timeout_ms\":300000}",
        },
    })
    rows = normalizer.normalize_event({
        "type": "response_item",
        "timestamp": "2026-06-21T13:46:30.743Z",
        "payload": {
            "type": "function_call_output",
            "call_id": "call_wait",
            "output": (
                "{\"status\":{\"agent-1\":{\"completed\":\"done\"},"
                "\"agent-2\":{\"completed\":\"also done\"}},\"timed_out\":false}"
            ),
        },
    })
    assert len(rows) == 1
    assert codex_subagent_ids_from_event(rows[0]) == ["agent-1", "agent-2"]
    return True


def test_codex_subagent_sources_include_parent_tool_call() -> bool:
    normalizer = CodexRolloutNormalizer(namespace="thread")
    normalizer.normalize_event({
        "type": "response_item",
        "timestamp": "2026-06-21T13:46:14.985Z",
        "payload": {
            "type": "function_call",
            "name": "wait_agent",
            "call_id": "call_wait",
            "arguments": "{\"targets\":[\"agent-1\"],\"timeout_ms\":300000}",
        },
    })
    rows = normalizer.normalize_event({
        "type": "response_item",
        "timestamp": "2026-06-21T13:46:30.743Z",
        "payload": {
            "type": "function_call_output",
            "call_id": "call_wait",
            "output": "{\"status\":{\"agent-1\":{\"completed\":\"done\"}},\"timed_out\":false}",
        },
    })
    source = codex_subagent_sources_from_event(rows[0])[0]
    assert source["child_id"] == "agent-1"
    assert source["parent_tool_use_id"] == "call_wait"
    assert source["delegation_id"] == codex_subagent_delegation_id(
        "agent-1",
        parent_tool_use_id="call_wait",
    )
    return True


def test_codex_status_payload_without_wait_agent_is_not_subagent() -> bool:
    normalizer = CodexRolloutNormalizer(namespace="thread")
    normalizer.normalize_event({
        "type": "response_item",
        "timestamp": "2026-06-21T13:46:14.985Z",
        "payload": {
            "type": "function_call",
            "name": "some_status_tool",
            "call_id": "call_status",
            "arguments": "{}",
        },
    })
    rows = normalizer.normalize_event({
        "type": "response_item",
        "timestamp": "2026-06-21T13:46:30.743Z",
        "payload": {
            "type": "function_call_output",
            "call_id": "call_status",
            "output": "{\"status\":{\"agent-1\":{\"completed\":\"done\"}}}",
        },
    })
    assert len(rows) == 1
    assert codex_subagent_ids_from_event(rows[0]) == []
    return True


def test_codex_subagent_ids_from_notification_metadata() -> bool:
    notification = (
        "<subagent_notification>\n"
        "{\"agent_path\":\"agent-1\",\"status\":{\"completed\":\"done\"}}\n"
        "</subagent_notification>"
    )
    rows = CodexRolloutNormalizer(namespace="thread").normalize_event({
        "type": "response_item",
        "timestamp": "2026-06-21T13:46:30.747Z",
        "payload": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": notification}],
        },
    })
    assert len(rows) == 1
    assert codex_subagent_ids_from_event(rows[0]) == ["agent-1"]
    return True


def test_codex_subagent_rollout_start_skips_inherited_history(tmp_path: Path | None = None) -> bool:
    import tempfile
    import json as _json

    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "child.jsonl"
        with path.open("wb") as f:
            f.write((_json.dumps({
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "parent history"}],
                },
            }) + "\n").encode())
            f.write((_json.dumps({
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "child prompt"},
            }) + "\n").encode())
            expected = f.tell()
            f.write((_json.dumps({
                "type": "event_msg",
                "payload": {"type": "agent_message", "message": "child work"},
            }) + "\n").encode())
        assert codex_subagent_rollout_start_byte(path) == expected
    return True


def test_codex_rollout_stamps_real_rollout_timestamp() -> bool:
    # Every emitted event must carry the source rollout line's `timestamp`,
    # not `datetime.now()`. Verified at the single `_push` chokepoint across
    # a message, a tool call, and a todo_list (the path that bypasses _push).
    normalizer = CodexRolloutNormalizer(namespace="thread")
    msg = normalizer.normalize_event({
        "type": "response_item",
        "timestamp": "2026-06-16T11:55:38.273Z",
        "payload": {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "hi"}],
        },
    })[0]
    assert msg["timestamp"] == "2026-06-16T11:55:38.273Z"

    tool = normalizer.normalize_event({
        "type": "response_item",
        "timestamp": "2026-06-16T11:55:40.000Z",
        "payload": {"type": "function_call", "name": "exec_command", "call_id": "c1"},
    })[0]
    assert tool["timestamp"] == "2026-06-16T11:55:40.000Z"

    todo = normalizer.normalize_event({
        "type": "item.completed",
        "timestamp": "2026-06-16T11:55:41.000Z",
        "item": {"id": "item_0", "type": "todo_list", "items": [{"text": "step", "completed": False}]},
    })[0]
    assert todo["timestamp"] == "2026-06-16T11:55:41.000Z"
    return True


def test_response_item_tool_search_call_and_output() -> bool:
    call = _normalize_response_item_event(
        {
            "type": "tool_search_call",
            "call_id": "call_search",
            "arguments": {"query": "spawn_agent", "limit": 5},
        },
        "parent",
    )
    assert call is not None
    call_block = call["message"]["content"][0]
    assert call_block["type"] == "tool_use"
    assert call_block["id"] == "call_search"
    assert call_block["name"] == "tool_search_tool"
    assert call_block["input"]["query"] == "spawn_agent"

    result = _normalize_response_item_event(
        {
            "type": "tool_search_output",
            "call_id": "call_search",
            "tools": [{"type": "function", "name": "spawn_agent"}],
        },
        call["uuid"],
    )
    assert result is not None
    assert result["type"] == "user"
    result_block = result["message"]["content"][0]
    assert result_block["type"] == "tool_result"
    assert result_block["tool_use_id"] == "call_search"
    assert "spawn_agent" in result_block["content"]
    assert result["parentUuid"] == call["uuid"]
    return True


def test_codex_spawn_agent_call_becomes_agent_tool() -> bool:
    event, tool_use_id = _normalize_response_tool_call(
        {
            "type": "function_call",
            "name": "spawn_agent",
            "call_id": "call_agent",
            "arguments": {
                "agent_type": "explorer",
                "task": "Find Codex subagent event paths.",
            },
        },
        "parent",
    )
    block = event["message"]["content"][0]
    assert tool_use_id == "call_agent"
    assert block["type"] == "tool_use"
    assert block["id"] == "call_agent"
    assert block["name"] == "Agent"
    assert block["input"]["subagent_type"] == "explorer"
    assert block["input"]["description"] == "Find Codex subagent event paths."
    assert block["input"]["prompt"] == "Find Codex subagent event paths."
    return True


def test_codex_subagent_child_message_keeps_parent_tool_use_id() -> bool:
    event = _normalize_agent_message(
        {
            "id": "item_child_msg",
            "type": "agent_message",
            "text": "Subagent found the path.",
            "parentToolUseId": "call_agent",
        },
        "parent",
    )
    assert event["parent_tool_use_id"] == "call_agent"
    block = event["message"]["content"][0]
    assert block["type"] == "text"
    assert block["text"] == "Subagent found the path."
    return True


def test_codex_child_thread_maps_to_parent_tool_use_id() -> bool:
    item = _attach_collab_parent_from_thread(
        {
            "id": "item_child_msg",
            "type": "agent_message",
            "text": "Subagent found the path.",
            "threadId": "child-thread",
        },
        {"child-thread": "collab_1"},
    )
    event = _normalize_agent_message(item, "parent")
    assert event["parent_tool_use_id"] == "collab_1"
    return True


def test_codex_collab_update_can_seed_child_thread_parent() -> bool:
    parents: dict[str, str] = {}
    _remember_collab_receivers(
        {
            "id": "collab_1",
            "type": "collab_agent_tool_call",
            "receiverThreadIds": ["child-thread"],
        },
        parents,
    )
    item = _attach_collab_parent_from_thread(
        {
            "id": "item_child_msg",
            "type": "agent_message",
            "text": "Subagent found the path.",
            "threadId": "child-thread",
        },
        parents,
    )
    assert item["parentToolUseId"] == "collab_1"
    return True


def test_codex_response_child_message_keeps_parent_tool_use_id() -> bool:
    event = _normalize_response_item_event(
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "Nested response"}],
            "parent_call_id": "call_agent",
        },
        "parent",
    )
    assert event is not None
    assert event["parent_tool_use_id"] == "call_agent"
    block = event["message"]["content"][0]
    assert block["type"] == "text"
    assert block["text"] == "Nested response"
    return True


def test_codex_spawn_agent_output_becomes_tool_result() -> bool:
    call, tool_use_id = _normalize_response_tool_call(
        {
            "type": "function_call",
            "name": "multi_agent_v1.spawn_agent",
            "call_id": "call_agent",
            "arguments": {
                "agent_type": "explorer",
                "message": "Find Codex subagent event paths.",
            },
        },
        "parent",
    )
    assert tool_use_id == "call_agent"
    call_block = call["message"]["content"][0]
    assert call_block["name"] == "Agent"

    result, result_tool_use_id = _normalize_response_tool_result(
        {
            "type": "function_call_output",
            "call_id": "call_agent",
            "output": "agent-1: completed Found runner_codex.py",
        },
        call["uuid"],
    )
    assert result_tool_use_id == "call_agent"
    assert result["type"] == "user"
    result_block = result["message"]["content"][0]
    assert result_block["type"] == "tool_result"
    assert result_block["tool_use_id"] == "call_agent"
    assert "Found runner_codex.py" in result_block["content"]
    assert result["parentUuid"] == call["uuid"]
    return True


def test_codex_collab_agent_item_becomes_agent_tool_pair() -> bool:
    item = {
        "id": "collab_1",
        "type": "collab_agent_tool_call",
        "tool": "explorer",
        "status": "completed",
        "senderThreadId": "root-thread",
        "receiverThreadIds": ["child-thread"],
        "prompt": "Find Codex subagent event paths.",
        "model": "gpt-5.4-mini",
        "reasoningEffort": "medium",
        "agentsStates": {
            "child-thread": {
                "status": "completed",
                "message": "Found runner_codex.py",
            },
        },
    }
    tool_use = _normalize_collab_agent_started(item, "parent")
    tool_block = tool_use["message"]["content"][0]
    assert tool_block["type"] == "tool_use"
    assert tool_block["id"] == "collab_1"
    assert tool_block["name"] == "Agent"
    assert tool_block["input"]["subagent_type"] == "explorer"
    assert tool_block["input"]["prompt"] == "Find Codex subagent event paths."
    assert tool_block["input"]["model"] == "gpt-5.4-mini"
    assert tool_block["input"]["reasoning_effort"] == "medium"

    result = _normalize_collab_agent_completed(item, tool_use["uuid"])
    result_block = result["message"]["content"][0]
    assert result_block["type"] == "tool_result"
    assert result_block["tool_use_id"] == "collab_1"
    assert "child-thread: completed Found runner_codex.py" in result_block["content"]
    assert result["parentUuid"] == tool_use["uuid"]
    return True


def test_unknown_response_item_becomes_raw_native_event() -> bool:
    event = _normalize_response_item_event(
        {
            "type": "future_tool_shape",
            "call_id": "call_future",
            "payload": {"ok": True},
        },
        "parent",
    )
    assert event is not None
    block = event["message"]["content"][0]
    assert block["type"] == "text"
    assert "Codex native response_item.future_tool_shape" in block["text"]
    assert "call_future" in block["text"]
    return True


def test_event_msg_patch_apply_end_becomes_raw_native_event() -> bool:
    event = _normalize_native_payload(
        "event_msg",
        {
            "type": "patch_apply_end",
            "call_id": "call_patch",
            "stdout": "Success. Updated files.",
            "success": True,
        },
        "parent",
    )
    block = event["message"]["content"][0]
    assert block["type"] == "text"
    assert "Codex native event_msg.patch_apply_end" in block["text"]
    assert "Success. Updated files." in block["text"]
    return True


def test_web_search_event_msg_and_response_item_have_same_dedupe_key() -> bool:
    action = {
        "type": "search",
        "query": "nomic-ai CodeRankEmbed model card Hugging Face",
        "queries": [
            "nomic-ai CodeRankEmbed model card Hugging Face",
            "CodeRankEmbed arxiv code retrieval embedding",
        ],
    }
    event_msg_item = _web_search_item_from_payload(
        {
            "type": "web_search_end",
            "call_id": "ws_1",
            "query": "nomic-ai CodeRankEmbed model card Hugging Face",
            "action": action,
        },
    )
    response_item = _web_search_item_from_payload(
        {
            "type": "web_search_call",
            "status": "completed",
            "action": action,
        },
    )
    assert event_msg_item["id"] == "ws_1"
    assert response_item["id"]
    assert _web_search_dedupe_key(event_msg_item) == _web_search_dedupe_key(response_item)
    return True


def test_turn_context_not_rendered() -> bool:
    normalizer = CodexRolloutNormalizer(namespace="thread")
    rows = normalizer.normalize_event({
        "type": "turn_context",
        "payload": {
            "turn_id": 3,
            "cwd": "/repo",
            "current_date": "2026-06-16",
            "timezone": "Asia/Jerusalem",
            "model": "gpt-5.5",
            "effort": "medium",
            "approval_policy": "never",
            "sandbox_policy": {"type": "dangerFullAccess"},
        },
    })
    assert rows == [], f"turn_context should not render: {rows}"
    return True


TESTS = [
    (
        "turn context not rendered at all",
        test_turn_context_not_rendered,
    ),
    (
        "event_msg task_started is not rendered",
        test_event_msg_task_started_not_rendered,
    ),
    (
        "web search call preserves query/action and no-result stays absent",
        test_web_search_call_preserves_query_and_action,
    ),
    (
        "web search result payload becomes tool_result",
        test_web_search_result_list_becomes_tool_result,
    ),
    (
        "web search call/result events share generated id",
        test_web_search_events_share_generated_id,
    ),
    (
        "web search without result emits only tool call",
        test_web_search_events_without_result_emit_only_call,
    ),
    (
        "response_item function_call normalizes exec_command",
        test_response_item_function_call_normalizes_exec_command,
    ),
    (
        "response_item function_call maps update_plan to TodoWrite",
        test_response_item_function_call_maps_update_plan_to_todowrite,
    ),
    (
        "response_item function_call_output attaches by call_id",
        test_response_item_function_output_attaches_by_call_id,
    ),
    (
        "custom_tool_call preserves apply_patch input",
        test_custom_tool_call_preserves_apply_patch_input,
    ),
    (
        "response_item assistant message becomes text event",
        test_response_item_assistant_message_becomes_text_event,
    ),
    (
        "response_item assistant message uuid is stable",
        test_response_item_assistant_message_uuid_is_stable,
    ),
    (
        "response_item render branches keep stable uuids",
        test_response_item_render_branches_keep_stable_uuids,
    ),
    (
        "codex rollout item replay keeps stable uuids",
        test_codex_rollout_item_replay_keeps_stable_uuids,
    ),
    (
        "codex rollout event_msg replay keeps stable parent chain",
        test_codex_rollout_event_msg_replay_keeps_stable_parent_chain,
    ),
    (
        "response_item user message without subagent notification is skipped",
        test_response_item_user_message_without_subagent_notification_is_skipped,
    ),
    (
        "response_item reasoning summary becomes thinking",
        test_response_item_reasoning_summary_becomes_thinking,
    ),
    (
        "empty encrypted response_item reasoning is skipped",
        test_empty_encrypted_reasoning_is_skipped,
    ),
    (
        "codex rollout keeps bookkeeping envelopes visible",
        test_codex_rollout_keeps_bookkeeping_envelopes_visible,
    ),
    (
        "codex rollout digests known event_msg primitives",
        test_codex_rollout_digests_known_event_msg_primitives,
    ),
    (
        "codex rollout context_compacted renders readable notice",
        test_codex_rollout_context_compacted_renders_readable_notice,
    ),
    (
        "codex rollout compacted renders replacement history",
        test_codex_rollout_compacted_renders_replacement_history,
    ),
    (
        "codex rollout compacted replaces context_compacted notice",
        test_codex_rollout_compacted_replaces_context_compacted_notice,
    ),
    (
        "codex rollout turn_aborted renders readable notice",
        test_codex_rollout_turn_aborted_renders_readable_notice,
    ),
    (
        "codex rollout mcp_tool_call_end becomes tool pair",
        test_codex_rollout_mcp_tool_call_end_becomes_tool_pair,
    ),
    (
        "codex rollout mcp_tool_call_end suppresses duplicate response output",
        test_codex_rollout_mcp_end_suppresses_response_output_duplicate,
    ),
    (
        "codex rollout response output without mcp_tool_call_end still emits",
        test_codex_rollout_response_output_without_mcp_end_still_emits,
    ),
    (
        "codex rollout distinct tool results both emit",
        test_codex_rollout_distinct_tool_results_both_emit,
    ),
    (
        "codex rollout response-first duplicate keeps one tool pair",
        test_codex_rollout_response_first_suppresses_late_mcp_end,
    ),
    (
        "codex rollout token_count captures context window and fill",
        test_codex_rollout_token_count_captures_context_window_and_fill,
    ),
    (
        "codex rollout tailer emits context updates",
        test_codex_rollout_tailer_emits_context_updates,
    ),
    (
        "codex rollout tailer keeps partial line pending",
        test_codex_rollout_tailer_keeps_partial_line_pending,
    ),
    (
        "codex rollout tailer advances cursor after dispatch",
        test_codex_rollout_tailer_advances_cursor_after_dispatch,
    ),
    (
        "codex rollout tailer file read does not block loop",
        test_codex_rollout_tailer_file_read_does_not_block_loop,
    ),
    (
        "codex rollout assistant text dedup is lossless",
        test_codex_rollout_assistant_text_dedup_is_lossless,
    ),
    (
        "codex rollout assistant text dedup scopes per turn",
        test_codex_rollout_assistant_text_dedup_scopes_per_turn,
    ),
    (
        "codex rollout stamps real rollout timestamp",
        test_codex_rollout_stamps_real_rollout_timestamp,
    ),
    (
        "codex rollout response_item subagent notification passes through",
        test_codex_rollout_response_item_subagent_notification_passes_through,
    ),
    (
        "codex rollout subagent notification attaches to Agent tool",
        test_codex_rollout_subagent_notification_attaches_to_agent_tool,
    ),
    (
        "codex subagent id discovered from spawn result",
        test_codex_subagent_id_from_spawn_result,
    ),
    (
        "codex subagent ids discovered from plural spawn result",
        test_codex_subagent_ids_from_plural_spawn_result,
    ),
    (
        "codex subagent ids discovered from wait_agent result",
        test_codex_subagent_ids_from_wait_agent_result,
    ),
    (
        "codex subagent sources include parent tool call",
        test_codex_subagent_sources_include_parent_tool_call,
    ),
    (
        "codex status payload without wait_agent is not subagent",
        test_codex_status_payload_without_wait_agent_is_not_subagent,
    ),
    (
        "codex subagent ids discovered from notification metadata",
        test_codex_subagent_ids_from_notification_metadata,
    ),
    (
        "codex subagent rollout start skips inherited history",
        test_codex_subagent_rollout_start_skips_inherited_history,
    ),
    (
        "response_item tool_search call/output normalizes",
        test_response_item_tool_search_call_and_output,
    ),
    (
        "codex spawn_agent call becomes Agent tool",
        test_codex_spawn_agent_call_becomes_agent_tool,
    ),
    (
        "codex subagent child message keeps parent_tool_use_id",
        test_codex_subagent_child_message_keeps_parent_tool_use_id,
    ),
    (
        "codex child thread maps to parent_tool_use_id",
        test_codex_child_thread_maps_to_parent_tool_use_id,
    ),
    (
        "codex collab update can seed child thread parent",
        test_codex_collab_update_can_seed_child_thread_parent,
    ),
    (
        "codex response child message keeps parent_tool_use_id",
        test_codex_response_child_message_keeps_parent_tool_use_id,
    ),
    (
        "codex spawn_agent output becomes tool_result",
        test_codex_spawn_agent_output_becomes_tool_result,
    ),
    (
        "codex collab agent item becomes Agent tool pair",
        test_codex_collab_agent_item_becomes_agent_tool_pair,
    ),
    (
        "unknown response_item becomes raw native event",
        test_unknown_response_item_becomes_raw_native_event,
    ),
    (
        "event_msg patch_apply_end becomes raw native event",
        test_event_msg_patch_apply_end_becomes_raw_native_event,
    ),
    (
        "web_search_end and web_search_call dedupe by action",
        test_web_search_event_msg_and_response_item_have_same_dedupe_key,
    ),
]


def main() -> int:
    ok = True
    for name, fn in TESTS:
        try:
            fn()
            print(f"PASS {name}")
        except Exception as exc:
            ok = False
            print(f"FAIL {name}: {exc}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
