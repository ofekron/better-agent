"""Golden-style tests for backend.adapters.normalize — pure, no I/O.

Run: PYTHONPATH=. python3 -m pytest backend/scripts/test_adapter_normalize.py -q
"""

from __future__ import annotations

import base64

from backend.adapters.normalize import (
    _DROPPED_CONTROL_ROW_TYPES,
    derive_link,
    enrich_typed_prompt_node,
    failure_payload_for_reason,
    is_canonical_prompt_row,
    normalize_journal_row,
    pair_tool_results,
    resolve_parents,
    turn_error_meta_node_id,
    typed_prompt_node_id,
    user_message_failed_node_id,
)
from backend.surface_contract.nodes import (
    ContentStatus,
    FailureResolution,
    FailureSeverity,
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


def test_backend_control_telemetry_row_types_are_dropped_not_unknown():
    """Defect: control/telemetry rows (turn dispatch bookkeeping, provider
    stream framing, WS delta plumbing) were falling through to the
    catch-all and rendering as bogus UNKNOWN nodes — 28 of them in one
    live-validation turn, and a 26-65% unknown-node ratio on real
    sessions (Gate-3 finding). None of these carry chat content; every
    one must normalize to zero nodes, same as prompt_meta."""
    for row_type in _DROPPED_CONTROL_ROW_TYPES:
        row = {
            "type": row_type, "seq": 1, "ts": "2026-01-01T00:00:00+00:00",
            "data": {"some": "payload"},
        }
        assert normalize_journal_row(row, surface_id=SURFACE, turn_id=TURN) == [], (
            f"{row_type!r} row produced a node instead of being dropped"
        )


def test_dropped_control_row_types_never_overlap_render_event_types():
    """Test-lock: the control-row exclusion set must stay disjoint from
    `event_shape.RENDER_EVENT_TYPES` (the write-path render allowlist),
    so it can never accidentally swallow a real render type. This is the
    single source of truth this list mirrors."""
    from event_shape import RENDER_EVENT_TYPES

    assert _DROPPED_CONTROL_ROW_TYPES.isdisjoint(RENDER_EVENT_TYPES)


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


def _user_message_failed_row(*, lifecycle_msg_id="lc-1", reason="orphaned_before_provider", error="boom", seq=5, msg_id=None):
    # Exact on-disk shape the wildcard `event_bus_subscribers.
    # _persist_to_event_journal` subscriber produces for a
    # `user_message_failed` BusEvent, per `event_ingester.py`'s `_emit`
    # entry construction (`{"seq","ts","sid","type","data","source",
    # +"msg_id" when set}`) — traced via
    # `_persist_to_event_journal` -> `EventJournalWriter._event_from_bus`
    # -> `_append_metadata_event` -> `event_ingester.ingest`.
    row = {
        "type": "user_message_failed",
        "seq": seq,
        "ts": "2026-01-01T00:00:00+00:00",
        "sid": SURFACE,
        "data": {"lifecycle_msg_id": lifecycle_msg_id, "reason": reason, "error": error},
        "source": "event_bus",
    }
    if msg_id is not None:
        row["msg_id"] = msg_id
    return row


def test_user_message_failed_row_maps_to_failure_node_with_reason_mapping():
    row = _user_message_failed_row(lifecycle_msg_id="lc-1", reason="orphaned_before_provider", error="boom")
    nodes = normalize_journal_row(row, surface_id=SURFACE, turn_id=TURN)
    assert len(nodes) == 1
    n = nodes[0]
    assert n.kind == NodeKind.FAILURE
    assert n.node_id == "failure:lc-1"
    assert n.turn_id == TURN
    assert n.surface_id == SURFACE
    assert n.payload.code == "recovery_unknown"
    assert n.payload.severity == FailureSeverity.ERROR
    assert n.payload.retryable is True
    assert n.payload.resolution == FailureResolution.RETRY
    assert n.payload.text == "boom"


def test_user_message_failed_row_unknown_reason_falls_back_verbatim():
    row = _user_message_failed_row(lifecycle_msg_id="lc-2", reason="some_new_reason", error=None)
    nodes = normalize_journal_row(row, surface_id=SURFACE, turn_id=TURN)
    n = nodes[0]
    assert n.payload.code == "some_new_reason"
    assert n.payload.severity == FailureSeverity.ERROR
    assert n.payload.retryable is False
    assert n.payload.resolution == FailureResolution.NONE
    assert n.payload.text == "user message failed: some_new_reason"


def test_user_message_failed_row_falls_back_to_row_msg_id_when_data_lacks_it():
    row = _user_message_failed_row(seq=6, msg_id="lc-from-row")
    del row["data"]["lifecycle_msg_id"]
    nodes = normalize_journal_row(row, surface_id=SURFACE, turn_id=TURN)
    assert nodes[0].node_id == "failure:lc-from-row"


def test_user_message_failed_row_with_no_lifecycle_msg_id_anywhere_produces_no_node():
    row = _user_message_failed_row(seq=7)
    del row["data"]["lifecycle_msg_id"]
    nodes = normalize_journal_row(row, surface_id=SURFACE, turn_id=TURN)
    assert nodes == []


def test_failure_payload_for_reason_matches_admission_rejected_table():
    for reason in ("interrupt_failed", "alter_interrupt_failed", "durable_admission_failed"):
        payload = failure_payload_for_reason(reason, None)
        assert payload.code == "admission_rejected", reason
        assert payload.severity == FailureSeverity.ERROR, reason
        assert payload.retryable is False, reason
        assert payload.resolution == FailureResolution.NONE, reason


def test_user_message_failed_node_id_is_deterministic():
    assert user_message_failed_node_id("abc") == "failure:abc"
    assert user_message_failed_node_id("abc") == user_message_failed_node_id("abc")


def _turn_error_meta_row(
    *, assistant_msg_id="asst-1", user_msg_id="user-1",
    error_text="cred missing", error_meta=None, seq=9,
):
    # Exact on-disk shape `turn_manager.py`'s `_publish_turn_error_meta`
    # produces (same wildcard-journal-subscriber path traced for
    # `_user_message_failed_row` above): `{"type": "turn_error_meta",
    # "data": {msg_id, error_text, error_meta}, "msg_id":
    # assistant_message_id, ...}` — `row["msg_id"]` is the JOURNAL
    # OWNERSHIP key (turn-owning assistant message id), `data["msg_id"]`
    # is the failed prompt's own id.
    return {
        "type": "turn_error_meta",
        "seq": seq,
        "ts": "2026-01-01T00:00:00+00:00",
        "sid": SURFACE,
        "data": {
            "msg_id": user_msg_id,
            "error_text": error_text,
            "error_meta": (
                error_meta if error_meta is not None else {
                    "kind": "provider_credential",
                    "provider_id": "anthropic",
                    "credential_status": "missing",
                }
            ),
        },
        "msg_id": assistant_msg_id,
        "source": "event_bus",
    }


def test_turn_error_meta_row_provider_credential_maps_to_failure_node():
    row = _turn_error_meta_row()
    nodes = normalize_journal_row(row, surface_id=SURFACE, turn_id=TURN)
    assert len(nodes) == 1
    n = nodes[0]
    assert n.kind == NodeKind.FAILURE
    assert n.node_id == "failure:err:asst-1"
    assert n.turn_id == TURN
    assert n.surface_id == SURFACE
    assert n.payload.code == "provider_credential"
    assert n.payload.text == "cred missing"
    assert n.payload.severity == FailureSeverity.ERROR
    assert n.payload.retryable is True
    assert n.payload.resolution == FailureResolution.FIX_CREDENTIAL
    assert n.payload.data == {"provider_id": "anthropic", "credential_status": "missing"}


def test_turn_error_meta_row_non_credential_kind_falls_back_to_defaults():
    row = _turn_error_meta_row(error_meta={"kind": "some_other_kind"}, error_text="boom")
    nodes = normalize_journal_row(row, surface_id=SURFACE, turn_id=TURN)
    n = nodes[0]
    assert n.payload.code == "some_other_kind"
    assert n.payload.text == "boom"
    assert n.payload.severity == FailureSeverity.ERROR
    assert n.payload.retryable is False
    assert n.payload.resolution == FailureResolution.NONE
    assert n.payload.data is None


def test_turn_error_meta_row_missing_kind_defaults_to_unknown():
    row = _turn_error_meta_row(error_meta={}, error_text="boom")
    nodes = normalize_journal_row(row, surface_id=SURFACE, turn_id=TURN)
    assert nodes[0].payload.code == "unknown"


def test_turn_error_meta_row_without_row_msg_id_produces_no_node():
    row = _turn_error_meta_row()
    del row["msg_id"]
    nodes = normalize_journal_row(row, surface_id=SURFACE, turn_id=TURN)
    assert nodes == []


def test_turn_error_meta_node_id_is_deterministic():
    assert turn_error_meta_node_id("asst-1") == "failure:err:asst-1"
    assert turn_error_meta_node_id("asst-1") == turn_error_meta_node_id("asst-1")


def test_turn_error_meta_provider_credential_data_is_closed_set_no_secrets():
    """`error_meta` only ever carries `{kind, provider_id,
    credential_status}` in production (`ProviderCredentialError.
    error_meta()`), but the FAILURE node's `data` must stay a closed
    set even if a future producer widens the dict — never pass through
    an unexpected (potentially secret-bearing) key verbatim."""
    row = _turn_error_meta_row(error_meta={
        "kind": "provider_credential",
        "provider_id": "anthropic",
        "credential_status": "missing",
        "api_key": "sk-should-never-appear",
    })
    nodes = normalize_journal_row(row, surface_id=SURFACE, turn_id=TURN)
    assert set(nodes[0].payload.data.keys()) == {"provider_id", "credential_status"}


def test_compaction_lifecycle_notice():
    row = {
        "type": "agent_message",
        "seq": 1,
        "ts": "2026-01-01T00:00:00+00:00",
        "data": {"type": "lifecycle_notice", "uuid": "c1", "data": {"kind": "compacted", "summary": "folded"}},
    }
    nodes = normalize_journal_row(row, surface_id=SURFACE, turn_id=TURN)
    assert len(nodes) == 1  # no replacement_history -> no synthesized children
    assert nodes[0].kind == NodeKind.COMPACTION
    assert nodes[0].payload.summary == "folded"


def test_compaction_lifecycle_notice_with_replacement_history_produces_children():
    """`codex_native._normalize_compacted_event`'s own
    `replacement_history` (`[{"role", "text"}, ...]`) is the ONLY place
    pre-compaction content survives in the journal — see
    `normalize._compaction_replay_children`'s docstring. Each entry
    becomes a child node parented under the compaction node so
    `derive.child_manifest`/the frontend's children-fetch can surface it."""
    row = {
        "type": "agent_message", "seq": 1, "ts": "2026-01-01T00:00:00+00:00",
        "data": {
            "type": "lifecycle_notice", "uuid": "c2",
            "data": {
                "kind": "compacted", "summary": "folded",
                "replacement_history": [
                    {"role": "user", "text": "please look at foo.py"},
                    {"role": "assistant", "text": "sure, looking now"},
                ],
            },
        },
    }
    nodes = normalize_journal_row(row, surface_id=SURFACE, turn_id=TURN)
    assert len(nodes) == 3
    compaction = nodes[0]
    assert compaction.kind == NodeKind.COMPACTION
    assert compaction.node_id == "c2"

    user_child = nodes[1]
    assert user_child.kind == NodeKind.TYPED_PROMPT
    assert user_child.payload.text == "please look at foo.py"
    assert user_child.payload.origin == PromptOrigin.USER
    assert user_child.parent_id == "c2"
    assert user_child.node_id == "c2:replayed:0"

    assistant_child = nodes[2]
    assert assistant_child.kind == NodeKind.ASSISTANT_TEXT
    assert assistant_child.payload.text == "sure, looking now"
    assert assistant_child.parent_id == "c2"
    assert assistant_child.node_id == "c2:replayed:1"


def test_compaction_replacement_history_skips_malformed_entries():
    row = {
        "type": "agent_message", "seq": 1, "ts": "x",
        "data": {
            "type": "lifecycle_notice", "uuid": "c3",
            "data": {
                "kind": "compacted",
                "replacement_history": [
                    {"role": "user", "text": ""},
                    {"role": "user"},
                    "not-a-dict",
                    {"role": "assistant", "text": "kept"},
                ],
            },
        },
    }
    nodes = normalize_journal_row(row, surface_id=SURFACE, turn_id=TURN)
    assert len(nodes) == 2  # compaction + the one well-formed entry
    assert nodes[1].payload.text == "kept"
    assert nodes[1].kind == NodeKind.ASSISTANT_TEXT


def test_compaction_resolve_parents_does_not_clobber_explicit_child_parenting():
    """Guards `resolve_parents`'s "explicit parent_id wins" rule: a
    compaction row that ALSO carries a resolvable `parentUuid` (as
    codex_native's own rows always do, pointing at the session root
    sentinel) must not have that row-level link overwrite the
    already-stamped child parent_id."""
    compaction_row = {
        "type": "agent_message", "seq": 1, "ts": "x",
        "data": {
            "type": "lifecycle_notice", "uuid": "c4", "parentUuid": "root-sentinel",
            "data": {
                "kind": "compacted",
                "replacement_history": [{"role": "user", "text": "hi"}],
            },
        },
    }
    # `root-sentinel` never appears as any real node's own id in this batch
    # (matches codex's synthetic root uuid never being a real node) —
    # resolve_parents must leave the compaction node's own parent_id
    # (None, pre-attach) and the child's (already "c4") both untouched.
    other_row = {
        "type": "agent_message", "seq": 2, "ts": "x",
        "data": {"type": "assistant", "uuid": "a1", "message": {"content": [{"type": "text", "text": "ok"}]}},
    }
    produced_compaction = normalize_journal_row(compaction_row, surface_id=SURFACE, turn_id=TURN)
    produced_other = normalize_journal_row(other_row, surface_id=SURFACE, turn_id=TURN)
    links = {}
    for n in produced_compaction:
        links[n.node_id] = derive_link(compaction_row)
    for n in produced_other:
        links[n.node_id] = derive_link(other_row)
    resolved = resolve_parents(produced_compaction + produced_other, links)
    by_id = {n.node_id: n for n in resolved}
    assert by_id["c4:replayed:0"].parent_id == "c4"


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


def test_parse_prompt_origin_dict_shaped_value_does_not_crash():
    """Real CLI rows carry `origin={"kind": "task-notification"}` /
    `{"kind": "peer", ...}` — an unguarded `value in <set>` raises
    TypeError: unhashable type: 'dict'. A non-str value must default to
    PromptOrigin.USER, never raise."""
    from backend.adapters.normalize import parse_prompt_origin

    assert parse_prompt_origin({"kind": "task-notification"}) == PromptOrigin.USER
    assert parse_prompt_origin({"kind": "peer", "peer_id": "x"}) == PromptOrigin.USER
    assert parse_prompt_origin(None) == PromptOrigin.USER
    assert parse_prompt_origin(["queued"]) == PromptOrigin.USER
    assert parse_prompt_origin(42) == PromptOrigin.USER


def test_parse_send_mode_dict_shaped_value_does_not_crash():
    from backend.adapters.normalize import parse_send_mode

    assert parse_send_mode({"kind": "task-notification"}) == SendMode.QUEUE
    assert parse_send_mode({"kind": "peer", "peer_id": "x"}) == SendMode.QUEUE
    assert parse_send_mode(None) == SendMode.QUEUE
    assert parse_send_mode(["queue"]) == SendMode.QUEUE
    assert parse_send_mode(42) == SendMode.QUEUE


def test_user_row_with_dict_shaped_origin_and_send_mode_normalizes_with_defaults():
    """End-to-end: normalize_journal_row must not crash on a real
    monster-session row shape (dict-valued origin/send_mode), and must
    fall back to prompt defaults instead."""
    row = {
        "type": "agent_message",
        "seq": 1,
        "ts": "2026-01-01T00:00:00+00:00",
        "data": {
            "type": "user", "uuid": "p3",
            "origin": {"kind": "task-notification"},
            "send_mode": {"kind": "peer", "peer_id": "abc"},
            "message": {"content": "hello"},
        },
    }
    nodes = normalize_journal_row(row, surface_id=SURFACE, turn_id=TURN)
    assert nodes[0].kind == NodeKind.TYPED_PROMPT
    assert nodes[0].payload.origin == PromptOrigin.USER
    assert nodes[0].payload.send_mode == SendMode.QUEUE


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
    assert nodes[0].payload.text == "done"
    assert nodes[0].payload.is_error is False


def test_result_row_carries_is_error_true():
    row = {
        "type": "agent_message",
        "seq": 5,
        "ts": "2026-01-01T00:00:04+00:00",
        "data": {
            "type": "result",
            "uuid": "r2",
            "subtype": "error",
            "is_error": True,
            "result": "boom",
        },
    }
    nodes = normalize_journal_row(row, surface_id=SURFACE, turn_id=TURN)
    assert nodes[0].payload.text == "boom"
    assert nodes[0].payload.is_error is True


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
    assert nodes[0].payload.text is None
    assert nodes[0].payload.is_error is False


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


def _typed_prompt_node(*, uuid="p1", origin=None, send_mode=None, content="hi"):
    data = {"type": "user", "uuid": uuid, "message": {"content": content}}
    if origin is not None:
        data["origin"] = origin
    if send_mode is not None:
        data["send_mode"] = send_mode
    row = {"type": "agent_message", "seq": 1, "ts": "2026-01-01T00:00:00+00:00", "data": data}
    nodes = normalize_journal_row(row, surface_id=SURFACE, turn_id=TURN)
    assert nodes[0].kind == NodeKind.TYPED_PROMPT
    return row, nodes[0]


def _image_block(raw: bytes, *, media_type="image/png"):
    return {
        "type": "image",
        "source": {
            "type": "base64", "media_type": media_type,
            "data": base64.b64encode(raw).decode(),
        },
    }


# ---------------------------------------------------------------------------
# _split_image_attachments — image blocks -> Attachment(ref="", size=...).
# ---------------------------------------------------------------------------
def test_user_row_image_block_splits_to_attachment_with_decoded_size():
    row, node = _typed_prompt_node(content=[
        {"type": "text", "text": "look at this"},
        _image_block(b"hello world"),
    ])
    assert node.payload.text == "look at this"
    assert len(node.payload.attachments) == 1
    att = node.payload.attachments[0]
    assert att.media_type == "image/png"
    assert att.ref == ""
    assert att.size == len(b"hello world")


def test_user_row_image_block_malformed_base64_size_is_none():
    row, node = _typed_prompt_node(content=[
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "not-base64!!"}},
    ])
    assert node.payload.attachments[0].size is None


def test_user_row_multiple_image_blocks_preserve_order_and_size():
    row, node = _typed_prompt_node(content=[
        _image_block(b"aa", media_type="image/png"),
        _image_block(b"bbbb", media_type="image/jpeg"),
    ])
    atts = node.payload.attachments
    assert [a.media_type for a in atts] == ["image/png", "image/jpeg"]
    assert [a.size for a in atts] == [2, 4]
    assert [a.ref for a in atts] == ["", ""]


# ---------------------------------------------------------------------------
# enrich_typed_prompt_node — image ref fill from prompt_meta.image_filenames.
# ---------------------------------------------------------------------------
def test_enrich_typed_prompt_node_fills_image_ref_from_meta_positionally():
    row, node = _typed_prompt_node(content=[_image_block(b"x"), _image_block(b"yy")])
    enriched = enrich_typed_prompt_node(
        node, row_data=row["data"], meta={"image_filenames": ["user-1_0.png", "user-1_1.png"]},
    )
    assert [a.ref for a in enriched.payload.attachments] == ["user-1_0.png", "user-1_1.png"]
    # size survives the ref fill untouched.
    assert [a.size for a in enriched.payload.attachments] == [1, 2]


def test_enrich_typed_prompt_node_image_ref_fill_partial_when_counts_mismatch():
    row, node = _typed_prompt_node(content=[_image_block(b"x"), _image_block(b"yy")])
    enriched = enrich_typed_prompt_node(
        node, row_data=row["data"], meta={"image_filenames": ["only-one.png"]},
    )
    refs = [a.ref for a in enriched.payload.attachments]
    assert refs == ["only-one.png", ""]


def test_enrich_typed_prompt_node_never_overwrites_existing_ref():
    row, node = _typed_prompt_node(content=[_image_block(b"x")])
    once = enrich_typed_prompt_node(
        node, row_data=row["data"], meta={"image_filenames": ["first.png"]},
    )
    twice = enrich_typed_prompt_node(
        once, row_data=row["data"], meta={"image_filenames": ["second.png"]},
    )
    assert twice.payload.attachments[0].ref == "first.png"


def test_enrich_typed_prompt_node_image_ref_fill_is_idempotent():
    row, node = _typed_prompt_node(content=[_image_block(b"x")])
    once = enrich_typed_prompt_node(
        node, row_data=row["data"], meta={"image_filenames": ["first.png"]},
    )
    twice = enrich_typed_prompt_node(
        once, row_data=row["data"], meta={"image_filenames": ["first.png"]},
    )
    assert twice is once  # true no-op: origin/send_mode/attachments already match


def test_enrich_typed_prompt_node_no_images_in_meta_is_noop_on_attachments():
    row, node = _typed_prompt_node(content=[_image_block(b"x")])
    enriched = enrich_typed_prompt_node(node, row_data=row["data"], meta={"origin": "supervisor"})
    assert enriched.payload.attachments[0].ref == ""


# ---------------------------------------------------------------------------
# Explicit typed-attachment size passthrough (data.attachments[i].size).
# ---------------------------------------------------------------------------
def test_user_row_explicit_attachment_carries_size_through():
    row = {
        "type": "agent_message", "seq": 1, "ts": "2026-01-01T00:00:00+00:00",
        "data": {
            "type": "user", "uuid": "p-explicit", "message": {"content": "hi"},
            "attachments": [{"name": "doc.txt", "media_type": "text/plain", "ref": "r1", "size": 42}],
        },
    }
    nodes = normalize_journal_row(row, surface_id=SURFACE, turn_id=TURN)
    assert nodes[0].payload.attachments[0].size == 42


def test_user_row_explicit_attachment_missing_size_defaults_to_none():
    row = {
        "type": "agent_message", "seq": 1, "ts": "2026-01-01T00:00:00+00:00",
        "data": {
            "type": "user", "uuid": "p-explicit-2", "message": {"content": "hi"},
            "attachments": [{"name": "doc.txt", "media_type": "text/plain", "ref": "r1"}],
        },
    }
    nodes = normalize_journal_row(row, surface_id=SURFACE, turn_id=TURN)
    assert nodes[0].payload.attachments[0].size is None


def test_user_row_dispatch_shaped_with_intent_id_round_trips():
    """Locks the wire round trip `turn_manager._publish_typed_prompt_
    journal`'s fix relies on: a row shaped EXACTLY like that method's own
    published `data` dict (with `intent_id` now stamped from the
    dispatch-time client_id/intent_id correlator) must produce a
    TYPED_PROMPT node whose `payload.intent_id` carries it through —
    covers BOTH ingress paths (v2 intent / legacy client_id), since both
    funnel into the SAME `user_msg["client_id"]` -> journal row field."""
    row = {
        "type": "agent_message", "seq": 1, "ts": "2026-01-01T00:00:00+00:00",
        "data": {
            "type": "user", "uuid": "prompt-dispatch-1",
            "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]},
            "origin": "user",
            "intent_id": "correlator-42",
        },
    }
    nodes = normalize_journal_row(row, surface_id=SURFACE, turn_id=TURN)
    assert nodes[0].kind == NodeKind.TYPED_PROMPT
    assert nodes[0].payload.intent_id == "correlator-42"


def test_user_row_without_intent_id_is_none():
    row = {
        "type": "agent_message", "seq": 1, "ts": "x",
        "data": {
            "type": "user", "uuid": "prompt-dispatch-2",
            "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        },
    }
    nodes = normalize_journal_row(row, surface_id=SURFACE, turn_id=TURN)
    assert nodes[0].payload.intent_id is None


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


# ---------------------------------------------------------------------------
# is_canonical_prompt_row — echo-dedup discriminator (P0 write-path fix).
# ---------------------------------------------------------------------------
def test_is_canonical_prompt_row_true_for_backend_authored_row():
    row = {
        "type": "agent_message", "seq": 1, "ts": "x",
        "data": {
            "type": "user", "uuid": "user-msg-1",
            "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]},
            "origin": "user",
        },
    }
    assert is_canonical_prompt_row(row) is True


def test_is_canonical_prompt_row_false_for_raw_provider_echo():
    """A raw CLI/SDK session-jsonl `type: "user"` line never carries
    `origin` — it is never backend-authored."""
    row = {
        "type": "agent_message", "seq": 2, "ts": "x",
        "data": {
            "type": "user", "uuid": "echo-uuid-1",
            "message": {"role": "user", "content": "hi"},
        },
    }
    assert is_canonical_prompt_row(row) is False


def test_is_canonical_prompt_row_false_for_non_user_rows():
    assistant_row = {
        "type": "agent_message", "seq": 3, "ts": "x",
        "data": {"type": "assistant", "uuid": "a1", "message": {"content": [{"type": "text", "text": "hi"}]}},
    }
    assert is_canonical_prompt_row(assistant_row) is False
    assert is_canonical_prompt_row({"type": "prompt_meta", "data": {"origin": "user"}}) is False
    assert is_canonical_prompt_row({"type": "agent_message", "data": None}) is False
    assert is_canonical_prompt_row({"type": "agent_message"}) is False


def test_is_canonical_prompt_row_false_when_origin_key_missing_even_if_empty_message():
    row = {"type": "agent_message", "data": {"type": "user", "uuid": "u1", "message": {}}}
    assert is_canonical_prompt_row(row) is False


# --------------------------------------------------------------------------- #
# `model_switched` — TOP-LEVEL row type (event_shape.RENDER_EVENT_TYPES
# sibling of agent_message), backend.main._record_model_switched_event.
# --------------------------------------------------------------------------- #
def test_model_switched_row_maps_to_model_change_user_source():
    row = {
        "type": "model_switched",
        "seq": 20,
        "ts": "2026-01-01T00:00:00+00:00",
        "data": {
            "uuid": "model-switch-abc",
            "model": "gpt-5",
            "provider_id": "openai",
            "previous_model": "gpt-4",
            "previous_provider_id": "openai",
            "changed": ["model"],
            "app_session_id": "sess-1",
            "msg_id": "a1",
        },
    }
    nodes = normalize_journal_row(row, surface_id=SURFACE, turn_id=TURN)
    assert len(nodes) == 1
    assert nodes[0].kind == NodeKind.MODEL_CHANGE
    assert nodes[0].node_id == "model-switch-abc"
    assert nodes[0].payload.from_run_ref == "gpt-4"
    assert nodes[0].payload.to_run_ref == "gpt-5"
    from backend.surface_contract.nodes import ModelChangeSource
    assert nodes[0].payload.source == ModelChangeSource.USER


def test_model_switched_row_without_uuid_falls_back_to_seq_id():
    row = {
        "type": "model_switched", "seq": 21, "ts": "x",
        "data": {"model": "sonnet", "previous_model": None},
    }
    nodes = normalize_journal_row(row, surface_id=SURFACE, turn_id=TURN)
    assert nodes[0].node_id == "seq:21:model_switched"
    assert nodes[0].payload.from_run_ref is None
    assert nodes[0].payload.to_run_ref == "sonnet"


# --------------------------------------------------------------------------- #
# `steer_prompt` — TOP-LEVEL row type, orchestrator.Coordinator.
# steer_active_turn's save_callback write. Maps to STEERING_MESSAGE
# (native SteeringMessageView / legacy SteerPromptEvent's "Steer" label).
# --------------------------------------------------------------------------- #
def test_steer_prompt_row_maps_to_steering_message():
    row = {
        "type": "steer_prompt",
        "seq": 22,
        "ts": "2026-01-01T00:00:01+00:00",
        "data": {
            "uuid": "steer-1",
            "prompt": "also check X",
            "timestamp": "2026-01-01T00:00:01+00:00",
            "client_id": "c1",
            "lifecycle_msg_id": "a1",
        },
    }
    nodes = normalize_journal_row(row, surface_id=SURFACE, turn_id=TURN)
    assert len(nodes) == 1
    assert nodes[0].kind == NodeKind.STEERING_MESSAGE
    assert nodes[0].node_id == "steer-1"
    assert nodes[0].payload.text == "also check X"
    assert nodes[0].payload.target == "a1"
    assert nodes[0].payload.attachments == ()


def test_steer_prompt_row_without_uuid_falls_back_to_seq_id():
    row = {
        "type": "steer_prompt", "seq": 23, "ts": "x",
        "data": {"prompt": "steer text", "lifecycle_msg_id": "a2"},
    }
    nodes = normalize_journal_row(row, surface_id=SURFACE, turn_id=TURN)
    assert nodes[0].node_id == "seq:23:steer_prompt"


def test_steer_prompt_row_with_images_produces_attachments():
    """`orchestrator.steer_active_turn`'s `_save_message_images` return
    value (`[{"filename", "media_type"}, ...]`) journaled inline on the
    steer_prompt row — self-contained, `ref` filled directly from
    `filename` (no separate join/placeholder needed, unlike TypedPrompt's
    two-fact join)."""
    row = {
        "type": "steer_prompt", "seq": 24, "ts": "x",
        "data": {
            "uuid": "steer-2",
            "prompt": "look at this",
            "lifecycle_msg_id": "a3",
            "images": [
                {"filename": "steer-2_0.png", "media_type": "image/png"},
                {"filename": "steer-2_1.jpg", "media_type": "image/jpeg"},
            ],
        },
    }
    nodes = normalize_journal_row(row, surface_id=SURFACE, turn_id=TURN)
    assert len(nodes) == 1
    attachments = nodes[0].payload.attachments
    assert len(attachments) == 2
    assert attachments[0].name == "steer-2_0.png"
    assert attachments[0].media_type == "image/png"
    assert attachments[0].ref == "steer-2_0.png"
    assert attachments[0].size is None
    assert attachments[1].name == "steer-2_1.jpg"
    assert attachments[1].ref == "steer-2_1.jpg"


def test_steer_prompt_row_images_malformed_entries_are_skipped():
    row = {
        "type": "steer_prompt", "seq": 25, "ts": "x",
        "data": {
            "uuid": "steer-3", "prompt": "x", "lifecycle_msg_id": "a4",
            "images": [{"media_type": "image/png"}, "not-a-dict", None, {"filename": 5}],
        },
    }
    nodes = normalize_journal_row(row, surface_id=SURFACE, turn_id=TURN)
    assert nodes[0].payload.attachments == ()


def test_steer_prompt_row_files_do_not_produce_attachments():
    """Files are intentionally NOT modeled as attachments — matches
    `TypedPromptPayload`'s own current scope (image-only)."""
    row = {
        "type": "steer_prompt", "seq": 26, "ts": "x",
        "data": {
            "uuid": "steer-4", "prompt": "x", "lifecycle_msg_id": "a5",
            "files": [{"name": "notes.txt", "media_type": "text/plain", "size": 12}],
        },
    }
    nodes = normalize_journal_row(row, surface_id=SURFACE, turn_id=TURN)
    assert nodes[0].payload.attachments == ()


# --------------------------------------------------------------------------- #
# `pr-link` — nested agent_message data.type, Claude CLI-native row (no
# uuid). Maps to FACT node kind="pr_link" for the native PrLinkChip.
# --------------------------------------------------------------------------- #
def test_pr_link_agent_message_maps_to_fact_node():
    row = {
        "type": "agent_message",
        "seq": 24,
        "ts": "2026-01-01T00:00:02+00:00",
        "data": {
            "type": "pr-link",
            "prUrl": "https://github.com/octo/repo/pull/42",
            "prNumber": 42,
            "prRepository": "octo/repo",
        },
    }
    nodes = normalize_journal_row(row, surface_id=SURFACE, turn_id=TURN)
    assert len(nodes) == 1
    assert nodes[0].kind == NodeKind.FACT
    assert nodes[0].node_id == "seq:24:pr_link"
    assert nodes[0].payload.kind == "pr_link"
    assert nodes[0].payload.data == {
        "prUrl": "https://github.com/octo/repo/pull/42",
        "prNumber": 42,
        "prRepository": "octo/repo",
    }


def test_pr_link_agent_message_missing_optional_fields_still_maps():
    row = {
        "type": "agent_message", "seq": 25, "ts": "x",
        "data": {"type": "pr-link", "prUrl": "https://x/y/pull/7"},
    }
    nodes = normalize_journal_row(row, surface_id=SURFACE, turn_id=TURN)
    assert nodes[0].payload.data == {
        "prUrl": "https://x/y/pull/7", "prNumber": None, "prRepository": None,
    }


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
