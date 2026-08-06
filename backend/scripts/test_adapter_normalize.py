"""Golden-style tests for backend.adapters.normalize — pure, no I/O.

Run: PYTHONPATH=. python3 -m pytest backend/scripts/test_adapter_normalize.py -q
"""

from __future__ import annotations

from backend.adapters.normalize import (
    derive_link,
    enrich_typed_prompt_node,
    normalize_journal_row,
    pair_tool_results,
    resolve_parents,
    typed_prompt_node_id,
)
from backend.surface_contract.nodes import (
    ContentStatus,
    NodeKind,
    PromptOrigin,
    ResultKind,
    SendMode,
)

SURFACE = "surf-1"
TURN = "turn-1"


def _assistant_row(content, *, uuid="u1", seq=1, parent_tool_use_id=None):
    return {
        "type": "agent_message",
        "seq": seq,
        "ts": "2026-01-01T00:00:00+00:00",
        "data": {
            "type": "assistant",
            "uuid": uuid,
            "message": {"content": content},
            "parent_tool_use_id": parent_tool_use_id,
        },
    }


def _user_row(content, *, uuid="u2", seq=2):
    return {
        "type": "agent_message",
        "seq": seq,
        "ts": "2026-01-01T00:00:01+00:00",
        "data": {"type": "user", "uuid": uuid, "message": {"content": content}},
    }


def test_assistant_text_thinking_and_tool_use_result_pairing():
    row1 = _assistant_row(
        [
            {"type": "thinking", "thinking": "pondering"},
            {"type": "text", "text": "hello"},
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"cmd": "ls"}},
        ],
        uuid="u1",
        seq=1,
    )
    row2 = _user_row(
        [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}],
        uuid="u2",
        seq=2,
    )

    nodes1 = normalize_journal_row(row1, surface_id=SURFACE, turn_id=TURN)
    nodes2 = normalize_journal_row(row2, surface_id=SURFACE, turn_id=TURN)

    assert [n.kind for n in nodes1] == [NodeKind.THINKING, NodeKind.ASSISTANT_TEXT, NodeKind.TOOL_INTERACTION]
    assert nodes1[0].payload.text == "pondering"
    assert nodes1[0].payload.redacted is False
    assert nodes1[1].payload.text == "hello"
    assert nodes1[2].node_id == "tool:t1"
    assert nodes1[2].payload.tool_name == "Bash"
    assert nodes1[2].payload.args == {"cmd": "ls"}
    assert nodes1[2].payload.result is None
    assert nodes1[2].status == ContentStatus.STREAMING

    assert nodes2[0].kind == NodeKind.TOOL_INTERACTION
    assert nodes2[0].node_id == "tool:t1:result"
    assert nodes2[0].payload.result == {"output": "ok"}

    merged = pair_tool_results(nodes1 + nodes2)
    tool_nodes = [n for n in merged if n.node_id == "tool:t1"]
    assert len(tool_nodes) == 1
    tool_node = tool_nodes[0]
    assert tool_node.payload.tool_name == "Bash"
    assert tool_node.payload.args == {"cmd": "ls"}
    assert tool_node.payload.result == {"output": "ok"}
    assert tool_node.status == ContentStatus.COMPLETE
    assert not any(n.node_id == "tool:t1:result" for n in merged)


def test_node_id_stability_same_input_twice():
    row = _assistant_row([{"type": "text", "text": "hi"}], uuid="u9", seq=5)
    a = normalize_journal_row(row, surface_id=SURFACE, turn_id=TURN)
    b = normalize_journal_row(row, surface_id=SURFACE, turn_id=TURN)
    assert a == b


def test_multi_block_node_id_suffix():
    row = _assistant_row(
        [{"type": "text", "text": "one"}, {"type": "text", "text": "two"}],
        uuid="u5",
        seq=3,
    )
    nodes = normalize_journal_row(row, surface_id=SURFACE, turn_id=TURN)
    assert nodes[0].node_id == "u5:0"
    assert nodes[1].node_id == "u5:1"


def test_todo_write_tool_call_gets_todo_snapshot_view():
    row = _assistant_row(
        [{"type": "tool_use", "id": "t2", "name": "TodoWrite", "input": {"todos": []}}],
        uuid="u6",
        seq=4,
    )
    nodes = normalize_journal_row(row, surface_id=SURFACE, turn_id=TURN)
    assert nodes[0].kind == NodeKind.TOOL_INTERACTION
    assert nodes[0].payload.derived_view == "todo_snapshot"


def test_fallback_block_maps_to_model_change_provider_source():
    row = _assistant_row(
        [{"type": "fallback", "from": {"model": "opus"}, "to": {"model": "sonnet"}}],
        uuid="u7",
        seq=6,
    )
    nodes = normalize_journal_row(row, surface_id=SURFACE, turn_id=TURN)
    assert nodes[0].kind == NodeKind.MODEL_CHANGE
    assert nodes[0].payload.from_run_ref == "opus"
    assert nodes[0].payload.to_run_ref == "sonnet"
    from backend.surface_contract.nodes import ModelChangeSource
    assert nodes[0].payload.source == ModelChangeSource.PROVIDER


def test_unrecognized_block_type_maps_to_unknown_never_dropped():
    row = _assistant_row([{"type": "totally_new_block", "weird": True}], uuid="u8", seq=7)
    nodes = normalize_journal_row(row, surface_id=SURFACE, turn_id=TURN)
    assert len(nodes) == 1
    assert nodes[0].kind == NodeKind.UNKNOWN
    assert nodes[0].payload.label == "block.totally_new_block"
    assert nodes[0].payload.payload == {"type": "totally_new_block", "weird": True}


def test_unrecognized_codex_envelope_row_types_are_dropped():
    for mtype in ("response_item", "event_msg", "session_meta", "turn_context", "compacted", "thread.started"):
        row = {
            "type": "agent_message",
            "seq": 1,
            "ts": "2026-01-01T00:00:00+00:00",
            "data": {"type": mtype, "uuid": "x"},
        }
        assert normalize_journal_row(row, surface_id=SURFACE, turn_id=TURN) == []


def test_metadata_rows_are_dropped():
    for mtype in ("system", "queue-operation", "last-prompt", "attachment", "ai-title", "file-history-snapshot", "mode"):
        row = {
            "type": "agent_message",
            "seq": 1,
            "ts": "2026-01-01T00:00:00+00:00",
            "data": {"type": mtype, "uuid": "x"},
        }
        assert normalize_journal_row(row, surface_id=SURFACE, turn_id=TURN) == []


def test_diagnostic_row_for_unrecognized_mtype():
    row = {
        "type": "agent_message",
        "seq": 9,
        "ts": "2026-01-01T00:00:00+00:00",
        "data": {"type": "some_new_provider_row", "uuid": "d1"},
    }
    nodes = normalize_journal_row(row, surface_id=SURFACE, turn_id=TURN)
    assert nodes[0].kind == NodeKind.DIAGNOSTIC
    assert nodes[0].payload.text == "agent_message.some_new_provider_row"


def test_totally_unrecognized_row_type_maps_to_unknown():
    row = {"type": "some_future_row_type", "seq": 1, "ts": "2026-01-01T00:00:00+00:00", "data": {"x": 1}}
    nodes = normalize_journal_row(row, surface_id=SURFACE, turn_id=TURN)
    assert nodes[0].kind == NodeKind.UNKNOWN
    assert nodes[0].payload.payload == row


def test_worker_facts_map_to_worker_interaction():
    for fact_kind in ("worker_start", "worker_event", "worker_complete"):
        row = {
            "type": fact_kind,
            "seq": 1,
            "ts": "2026-01-01T00:00:00+00:00",
            "data": {"worker_id": "w1"},
            "uuid": f"{fact_kind}-uuid",
        }
        nodes = normalize_journal_row(row, surface_id=SURFACE, turn_id=TURN)
        assert nodes[0].kind == NodeKind.WORKER_INTERACTION
        assert nodes[0].payload.fact_kind == fact_kind
        assert nodes[0].payload.fact == {"worker_id": "w1"}


def test_compaction_lifecycle_notice():
    row = {
        "type": "agent_message",
        "seq": 1,
        "ts": "2026-01-01T00:00:00+00:00",
        "data": {"type": "lifecycle_notice", "uuid": "c1", "data": {"kind": "compacted", "summary": "folded"}},
    }
    nodes = normalize_journal_row(row, surface_id=SURFACE, turn_id=TURN)
    assert nodes[0].kind == NodeKind.COMPACTION
    assert nodes[0].payload.summary == "folded"


def test_generic_lifecycle_notice():
    row = {
        "type": "agent_message",
        "seq": 1,
        "ts": "2026-01-01T00:00:00+00:00",
        "data": {"type": "lifecycle_notice", "uuid": "l1", "data": {"kind": "retrying"}},
    }
    nodes = normalize_journal_row(row, surface_id=SURFACE, turn_id=TURN)
    assert nodes[0].kind == NodeKind.LIFECYCLE_NOTICE
    from backend.surface_contract.nodes import LifecycleNoticeKind
    assert nodes[0].payload.kind == LifecycleNoticeKind.RETRYING


def test_user_prompt_origin_mapping_queued_and_offline():
    row_queued = {
        "type": "agent_message",
        "seq": 1,
        "ts": "2026-01-01T00:00:00+00:00",
        "data": {"type": "user", "uuid": "p1", "origin": "queued", "message": {"content": "do it"}},
    }
    nodes = normalize_journal_row(row_queued, surface_id=SURFACE, turn_id=TURN)
    assert nodes[0].kind == NodeKind.TYPED_PROMPT
    assert nodes[0].payload.origin == PromptOrigin.QUEUED
    assert nodes[0].status == ContentStatus.QUEUED
    assert nodes[0].payload.text == "do it"

    row_offline = {
        "type": "agent_message",
        "seq": 2,
        "ts": "2026-01-01T00:00:01+00:00",
        "data": {"type": "user", "uuid": "p2", "origin": "offline_sync", "message": {"content": "hi"}},
    }
    nodes2 = normalize_journal_row(row_offline, surface_id=SURFACE, turn_id=TURN)
    assert nodes2[0].payload.origin == PromptOrigin.OFFLINE_SYNC


def test_resolve_parents_uses_parent_tool_use_id():
    parent_row = _assistant_row(
        [{"type": "tool_use", "id": "agentcall1", "name": "Agent", "input": {}}], uuid="pu1", seq=1
    )
    child_row = _assistant_row(
        [{"type": "text", "text": "sub-agent output"}],
        uuid="cu1",
        seq=2,
        parent_tool_use_id="agentcall1",
    )
    parent_nodes = normalize_journal_row(parent_row, surface_id=SURFACE, turn_id=TURN)
    child_nodes = normalize_journal_row(child_row, surface_id=SURFACE, turn_id=TURN)
    all_nodes = parent_nodes + child_nodes

    links = {}
    for n in parent_nodes:
        links[n.node_id] = derive_link(parent_row)
    for n in child_nodes:
        links[n.node_id] = derive_link(child_row)

    resolved = resolve_parents(all_nodes, links)
    child = next(n for n in resolved if n.node_id == "cu1")
    assert child.parent_id == "tool:agentcall1"


def test_result_row_maps_to_result_provider_node():
    row = {
        "type": "agent_message",
        "seq": 3,
        "ts": "2026-01-01T00:00:02+00:00",
        "data": {
            "type": "result",
            "uuid": "r1",
            "subtype": "success",
            "is_error": False,
            "result": "done",
        },
    }
    nodes = normalize_journal_row(row, surface_id=SURFACE, turn_id=TURN)
    assert len(nodes) == 1
    assert nodes[0].kind == NodeKind.RESULT
    assert nodes[0].node_id == "r1"
    assert nodes[0].status is None
    assert nodes[0].payload.result_kind == ResultKind.PROVIDER


def test_result_row_without_uuid_falls_back_to_seq_id():
    row = {
        "type": "agent_message",
        "seq": 4,
        "ts": "2026-01-01T00:00:03+00:00",
        "data": {"type": "result", "subtype": "success", "is_error": False},
    }
    nodes = normalize_journal_row(row, surface_id=SURFACE, turn_id=TURN)
    assert nodes[0].kind == NodeKind.RESULT
    assert nodes[0].node_id == "seq:4:result"


def test_typed_prompt_node_id_identity_and_none():
    assert typed_prompt_node_id("abc-123") == "abc-123"
    assert typed_prompt_node_id(None) is None
    assert typed_prompt_node_id("") is None


def test_derive_link_extracts_sidechain_fields():
    row = {
        "type": "agent_message",
        "seq": 1,
        "ts": "x",
        "data": {"parentUuid": "root1", "isSidechain": True, "parent_tool_use_id": "t9"},
    }
    link = derive_link(row)
    assert link.parent_uuid == "root1"
    assert link.is_sidechain is True
    assert link.parent_tool_use_id == "t9"


# ---------------------------------------------------------------------------
# prompt_meta row handling + chat_adapter join enrichment (Phase C).
# ---------------------------------------------------------------------------
def test_prompt_meta_row_normalizes_to_no_nodes():
    row = {
        "type": "prompt_meta",
        "seq": 1,
        "ts": "2026-01-01T00:00:00+00:00",
        "msg_id": "assistant-1",
        "data": {"msg_id": "user-1", "origin": "supervisor"},
    }
    assert normalize_journal_row(row, surface_id=SURFACE, turn_id=TURN) == []


def _typed_prompt_node(*, uuid="p1", origin=None, send_mode=None):
    data = {"type": "user", "uuid": uuid, "message": {"content": "hi"}}
    if origin is not None:
        data["origin"] = origin
    if send_mode is not None:
        data["send_mode"] = send_mode
    row = {"type": "agent_message", "seq": 1, "ts": "2026-01-01T00:00:00+00:00", "data": data}
    nodes = normalize_journal_row(row, surface_id=SURFACE, turn_id=TURN)
    assert nodes[0].kind == NodeKind.TYPED_PROMPT
    return row, nodes[0]


def test_enrich_typed_prompt_node_fills_origin_from_meta_when_row_silent():
    row, node = _typed_prompt_node()
    assert node.payload.origin == PromptOrigin.USER  # unenriched default

    enriched = enrich_typed_prompt_node(
        node, row_data=row["data"], meta={"origin": "supervisor"},
    )
    assert enriched.payload.origin == PromptOrigin.SUPERVISOR
    assert enriched.payload.send_mode == SendMode.QUEUE  # untouched: meta had no send_mode


def test_enrich_typed_prompt_node_row_origin_wins_over_meta():
    row, node = _typed_prompt_node(origin="ask")
    assert node.payload.origin == PromptOrigin.ASK

    enriched = enrich_typed_prompt_node(
        node, row_data=row["data"], meta={"origin": "supervisor"},
    )
    assert enriched.payload.origin == PromptOrigin.ASK  # row wins, meta ignored


def test_enrich_typed_prompt_node_no_meta_is_noop():
    row, node = _typed_prompt_node()
    enriched = enrich_typed_prompt_node(node, row_data=row["data"], meta=None)
    assert enriched is node  # identical origin/send_mode: no replace() churn


def test_enrich_typed_prompt_node_is_idempotent():
    row, node = _typed_prompt_node()
    once = enrich_typed_prompt_node(node, row_data=row["data"], meta={"origin": "supervisor"})
    twice = enrich_typed_prompt_node(once, row_data=row["data"], meta={"origin": "supervisor"})
    assert once == twice
    assert twice is once  # second call is a true no-op: values already match


def test_enrich_typed_prompt_node_ignores_non_typed_prompt_nodes():
    row = {
        "type": "agent_message", "seq": 1, "ts": "x",
        "data": {"type": "assistant", "uuid": "a1", "message": {"content": [{"type": "text", "text": "hi"}]}},
    }
    nodes = normalize_journal_row(row, surface_id=SURFACE, turn_id=TURN)
    assert nodes[0].kind == NodeKind.ASSISTANT_TEXT
    enriched = enrich_typed_prompt_node(nodes[0], row_data=row["data"], meta={"origin": "supervisor"})
    assert enriched is nodes[0]


if __name__ == "__main__":
    import sys

    failures = 0
    tests = [obj for name, obj in list(globals().items()) if name.startswith("test_") and callable(obj)]
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {t.__name__}: {e}")
    sys.exit(1 if failures else 0)
