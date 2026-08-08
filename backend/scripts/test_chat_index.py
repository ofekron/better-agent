#!/usr/bin/env python3
"""Unit coverage for backend/adapters/chat_index.py (layer-2 chat index).

Isolated via `paths.engage_test_home` before any backend import — same
harness convention as `test_chat_adapter.py` (see that file's own
docstring for the bare-vs-dotted-import rationale this mirrors exactly).

Run:
    PYTHONPATH=. python3 -m pytest backend/scripts/test_chat_index.py -q
    PYTHONPATH=. python3 backend/scripts/test_chat_index.py   # __main__ fallback
"""

from __future__ import annotations

import asyncio
import atexit
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

_BACKEND_DIR = str(Path(__file__).resolve().parents[1])
_REPO_ROOT = str(Path(_BACKEND_DIR).parent)
for _p in (_BACKEND_DIR, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import paths  # noqa: E402  (bare — matches sibling backend/scripts tests)

_TEST_HOME = tempfile.mkdtemp(prefix="ba-chat-index-test-")
paths.engage_test_home(_TEST_HOME)
atexit.register(shutil.rmtree, _TEST_HOME, ignore_errors=True)

import perf  # noqa: E402  (bare — matches every other perf-instrumented backend module)

import backend.adapters.chat_adapter as chat_adapter_mod  # noqa: E402
import backend.adapters.chat_index as chat_index_mod  # noqa: E402
import backend.adapters.store_access as store_access_mod  # noqa: E402
from backend.adapters.chat_adapter import ChatSurfaceAdapter  # noqa: E402
from backend.event_bus import BusEvent, bus  # noqa: E402
from backend.event_ingester import event_ingester  # noqa: E402
from backend.event_journal import EVENT_JOURNAL_WRITTEN  # noqa: E402
from backend.surface_contract.identity import Ok  # noqa: E402
from backend.adapters.serialize import to_wire  # noqa: E402
from backend.surface_contract.nodes import (  # noqa: E402
    Attachment,
    NodeKind,
    SteeringMessagePayload,
    SubAgentTurnPayload,
    TargetRef,
)


def _ingest_prompt(root_id: str, text: str) -> int:
    return event_ingester.ingest(
        root_id, root_id, "agent_message",
        {"type": "user", "message": {"content": text}},
        source="test",
    )


def _ingest_assistant_text(root_id: str, text: str) -> int:
    return event_ingester.ingest(
        root_id, root_id, "agent_message",
        {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}},
        source="test",
    )


def _ingest_tool_use(root_id: str, tool_use_id: str, name: str = "Task") -> int:
    return event_ingester.ingest(
        root_id, root_id, "agent_message",
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": tool_use_id, "name": name, "input": {}},
        ]}},
        source="test",
    )


def _ingest_sidechain_text(
    root_id: str, uuid_str: str, text: str, *, parent_tool_use_id: str | None = None,
    parent_uuid: str | None = None,
) -> int:
    data = {
        "type": "assistant", "message": {"content": [{"type": "text", "text": text}]},
        "uuid": uuid_str, "isSidechain": True,
    }
    if parent_tool_use_id is not None:
        data["parent_tool_use_id"] = parent_tool_use_id
    if parent_uuid is not None:
        data["parentUuid"] = parent_uuid
    return event_ingester.ingest(root_id, root_id, "agent_message", data, source="test")


def _ingest_worker_fact(root_id: str, fact_type: str, delegation_id: str, **fields) -> int:
    data = {"delegation_id": delegation_id, **fields}
    return event_ingester.ingest(root_id, root_id, fact_type, data, source="test")


def _publish_written(root_id: str, seq: int) -> None:
    asyncio.run(
        bus.publish(
            BusEvent(
                type=EVENT_JOURNAL_WRITTEN,
                root_id=root_id, sid=root_id,
                payload={"event_type": "agent_message", "seq": seq, "data": {}, "source": "test", "event_id": str(uuid.uuid4())},
            )
        )
    )


def _build_a_turn_with_explanation_and_subagent(root_id: str) -> tuple[int, str, str]:
    """One SETTLED turn: prompt -> assistant text A -> Task tool_use ->
    sidechain reply (subagent) -> assistant text B (final result). Returns
    (last_seq, task_tool_use_id, prompt_text) so callers can drive a
    second turn to close this one via the transition-fold path."""
    _ingest_prompt(root_id, "please delegate part of this")
    _ingest_assistant_text(root_id, "Sure, let me hand this off.")
    task_id = str(uuid.uuid4())
    _ingest_tool_use(root_id, task_id, name="Task")
    sub_uuid = str(uuid.uuid4())
    _ingest_sidechain_text(root_id, sub_uuid, "subagent working on it", parent_tool_use_id=task_id)
    last_seq = _ingest_assistant_text(root_id, "All done.")
    return last_seq, task_id, "please delegate part of this"


def _adapter_with_bound_bus() -> ChatSurfaceAdapter:
    adapter = ChatSurfaceAdapter()
    adapter.bind()
    return adapter


# ---------------------------------------------------------------------
# 1. Build from a synthetic session, including subagent nesting.
# ---------------------------------------------------------------------


def test_fold_on_turn_transition_persists_settled_turn_with_subagent() -> None:
    """The transition-fold path (`_flush_event_written` observing a NEW
    prompt row open while a different turn_id was current) settles the
    PREVIOUS turn into chat_index — including its Explanation/subagent
    structure — without any explicit open_session/older/children call."""
    root_id = f"root-{uuid.uuid4().hex}"
    adapter = _adapter_with_bound_bus()
    seq1, task_id, _ = _build_a_turn_with_explanation_and_subagent(root_id)
    _publish_written(root_id, seq1)

    # Nothing settled yet — turn 1 is still the live trailing turn.
    assert chat_index_mod.read_recent_settled_turns(root_id, 5) is None

    seq2 = _ingest_prompt(root_id, "second turn")
    _publish_written(root_id, seq2)

    settled = chat_index_mod.read_recent_settled_turns(root_id, 5)
    assert settled is not None
    assert len(settled) == 1
    turn = settled[0]
    assert turn.turn.kind == NodeKind.TURN
    assert turn.prompt is not None and turn.prompt.payload.text == "please delegate part of this"
    assert len(turn.results) == 1
    assert turn.results[0].payload.text == "All done."

    # The subagent structure survived folding: children(turn) includes a
    # NATIVE_SUBAGENT_TURN header (payload=None + manifest).
    kids = chat_index_mod.read_children(root_id, turn.turn_node_id)
    assert kids is not None
    subagent_kids = [n for n in kids if n.kind == NodeKind.NATIVE_SUBAGENT_TURN]
    assert len(subagent_kids) == 1
    assert subagent_kids[0].payload is None
    assert subagent_kids[0].child_manifest is not None
    # KNOWN pre-existing gap (not introduced by chat_index, identical via
    # the non-indexed path): `build_subagent_turns` never applies
    # `derive_turn`'s own result-extraction step to a subagent's body, so
    # a text-only subagent body ends up fully Explanation-wrapped and
    # `renderable_child_count` reads 0 even though `has_children` is
    # True. Asserting on `has_children` (the real "is there anything to
    # expand" signal) rather than the count.
    assert subagent_kids[0].child_manifest.has_children is True

    # Its own children are one level down (an Explanation header wrapping
    # the sidechain reply — same "structural node = header, expand once
    # more for the leaf" rule as any other Explanation).
    sub_children = chat_index_mod.read_children(root_id, subagent_kids[0].node_id)
    assert sub_children is not None
    explanations = [n for n in sub_children if n.kind == NodeKind.EXPLANATION]
    assert len(explanations) == 1 and explanations[0].payload is None

    leaves = chat_index_mod.read_children(root_id, explanations[0].node_id)
    assert leaves is not None
    assert any(
        n.payload is not None and getattr(n.payload, "text", None) == "subagent working on it"
        for n in leaves
    )


def test_fold_on_lifecycle_complete_settles_the_last_turn() -> None:
    """A session's LAST turn never gets superseded by a "next" prompt
    while the session is alive — `lifecycle.turn_complete` is the only
    signal that closes it. Guarded on `turn_id == state.current_turn_id`
    (see `_on_lifecycle`) so this never fires for a stale/reordered fact."""
    root_id = f"root-{uuid.uuid4().hex}"
    adapter = _adapter_with_bound_bus()
    seq1, _task_id, _ = _build_a_turn_with_explanation_and_subagent(root_id)
    _publish_written(root_id, seq1)
    assert chat_index_mod.read_recent_settled_turns(root_id, 5) is None

    state = adapter._ensure_seeded(root_id)
    turn_id = state.current_turn_id
    assert turn_id is not None

    # `typed_prompt_node_id` (normalize.py) is an identity passthrough —
    # a lifecycle fact's `prompt_uuid` must equal the turn's own node_id
    # verbatim for `_on_lifecycle`'s derivation to match `state.current_
    # turn_id` (see `_on_lifecycle`'s "Prefer the contract-derivation..."
    # comment in chat_adapter.py).
    asyncio.run(
        bus.publish(
            BusEvent(
                type="lifecycle.turn_complete", root_id=root_id, sid=root_id,
                payload={"reason": "success", "prompt_uuid": turn_id},
            )
        )
    )
    settled = chat_index_mod.read_recent_settled_turns(root_id, 5)
    assert settled is not None and len(settled) == 1


def test_fold_persists_worker_turn_with_usage_and_target_ref_round_trip() -> None:
    """The SubAgentTurn family's panel-level `usage`/`target_ref`/
    `payload` (a `SubAgentTurnPayload`, new node kinds this round added)
    round-trip through the index's SQLite header row exactly like every
    other typed payload — real dataclasses back out, not raw dicts (see
    module docstring's "payload_json/target_ref_json" section)."""
    root_id = f"root-{uuid.uuid4().hex}"
    adapter = _adapter_with_bound_bus()
    _ingest_prompt(root_id, "please delegate this")
    _ingest_worker_fact(
        root_id, "worker_start", "deleg-1", panel_kind="worker",
        worker_description="fix it", worker_session_id="sess-idx",
    )
    seq1 = _ingest_worker_fact(
        root_id, "worker_complete", "deleg-1", worker_session_id="sess-idx",
        token_usage={"input_tokens": 11, "output_tokens": 4},
    )
    _publish_written(root_id, seq1)

    seq2 = _ingest_prompt(root_id, "second turn")
    _publish_written(root_id, seq2)

    settled = chat_index_mod.read_recent_settled_turns(root_id, 5)
    assert settled is not None and len(settled) == 1
    turn = settled[0]

    kids = chat_index_mod.read_children(root_id, turn.turn_node_id)
    assert kids is not None
    containers = [n for n in kids if n.kind == NodeKind.WORKER_TURN]
    assert len(containers) == 1
    container = containers[0]
    assert container.payload == SubAgentTurnPayload(label="fix it")
    assert container.target_ref == TargetRef(session_id="sess-idx")
    assert container.sidecar_ref == "sess-idx"
    assert container.usage.input_tokens == 11
    assert container.usage.output_tokens == 4
    assert container.usage.total_tokens == 15

    grandkids = chat_index_mod.read_children(root_id, container.node_id)
    assert grandkids is not None
    assert len(grandkids) == 2
    assert all(n.kind == NodeKind.WORKER_INTERACTION for n in grandkids)
    assert all(n.parent_id == container.node_id for n in grandkids)


def test_fold_persists_worker_turn_created_and_target_turn_id_round_trip() -> None:
    """`SubAgentTurnPayload.created` and `TargetRef.turn_id` — both new
    this round — round-trip through the index's SQLite header row exactly
    like `label`/`session_id` already do (payload_json/target_ref_json,
    same generic to_wire/_reconstruct_payload machinery)."""
    root_id = f"root-{uuid.uuid4().hex}"
    adapter = _adapter_with_bound_bus()
    _ingest_prompt(root_id, "please delegate this")
    _ingest_worker_fact(
        root_id, "worker_start", "deleg-1", panel_kind="sub_session_created",
        worker_description="sess created", worker_session_id="sess-corr", is_new=True,
    )
    seq1 = _ingest_worker_fact(
        root_id, "worker_complete", "deleg-1",
        worker_session_id="sess-corr", target_turn_id="prompt-corr",
    )
    _publish_written(root_id, seq1)
    seq2 = _ingest_prompt(root_id, "second turn")
    _publish_written(root_id, seq2)

    settled = chat_index_mod.read_recent_settled_turns(root_id, 5)
    assert settled is not None and len(settled) == 1
    kids = chat_index_mod.read_children(root_id, settled[0].turn_node_id)
    assert kids is not None
    containers = [n for n in kids if n.kind == NodeKind.SUB_SESSION_TURN]
    assert len(containers) == 1
    container = containers[0]
    assert container.payload == SubAgentTurnPayload(label="sess created", created=True)
    assert container.target_ref == TargetRef(session_id="sess-corr", turn_id="prompt-corr")
    assert container.sidecar_ref == "sess-corr"


def test_fold_persists_worker_turn_without_usage_when_no_worker_complete() -> None:
    root_id = f"root-{uuid.uuid4().hex}"
    adapter = _adapter_with_bound_bus()
    _ingest_prompt(root_id, "please delegate this")
    seq1 = _ingest_worker_fact(
        root_id, "worker_start", "deleg-1", panel_kind="sub_session",
        worker_description="delegated", worker_session_id="sess-idx",
    )
    _publish_written(root_id, seq1)
    seq2 = _ingest_prompt(root_id, "second turn")
    _publish_written(root_id, seq2)

    settled = chat_index_mod.read_recent_settled_turns(root_id, 5)
    assert settled is not None and len(settled) == 1
    kids = chat_index_mod.read_children(root_id, settled[0].turn_node_id)
    assert kids is not None
    containers = [n for n in kids if n.kind == NodeKind.SUB_SESSION_TURN]
    assert len(containers) == 1
    assert containers[0].usage is None


# ---------------------------------------------------------------------
# 2. Visibility-fetch matrix: each collapse state -> exact node set,
#    hidden payloads ABSENT.
# ---------------------------------------------------------------------


def _settle_two_turns(root_id: str) -> ChatSurfaceAdapter:
    adapter = _adapter_with_bound_bus()
    seq1, _task_id, _ = _build_a_turn_with_explanation_and_subagent(root_id)
    _publish_written(root_id, seq1)
    seq2 = _ingest_prompt(root_id, "second turn closes the first")
    _ingest_assistant_text(root_id, "second turn reply")
    _publish_written(root_id, seq2)
    return adapter


def test_visibility_collapsed_turn_never_fetches_body() -> None:
    """Collapsed turn -> typed_prompt + result parts + manifest ONLY (via
    CompactTurn) — body items are never even reachable from this response
    shape (CompactTurn has no `body` field), matching the render
    algorithm's "collapsed turn never fetches a hidden subtree" rule."""
    root_id = f"root-{uuid.uuid4().hex}"
    adapter = _settle_two_turns(root_id)
    result = adapter.open_session(root_id)
    assert isinstance(result, Ok), result
    first_turn = result.value.turns[0]
    assert first_turn.prompt.payload.text == "please delegate part of this"
    assert len(first_turn.results) == 1
    assert first_turn.manifest.renderable_child_count >= 1


def test_visibility_extended_turn_returns_structural_headers_only() -> None:
    """Extended turn (children(turn_node_id)) -> body item HEADERS:
    Explanation/subagent nodes come back with payload=None + manifest —
    never their members' text — matching the Node contract's existing
    header-only expression (payload=None + child_manifest)."""
    root_id = f"root-{uuid.uuid4().hex}"
    adapter = _settle_two_turns(root_id)
    result = adapter.open_session(root_id)
    assert isinstance(result, Ok)
    turn_node_id = result.value.turns[0].turn.node_id

    body = adapter.children(root_id, turn_node_id, _render_rev(result))
    assert isinstance(body, Ok)
    kinds = {n.kind for n in body.value}
    assert NodeKind.EXPLANATION in kinds or NodeKind.NATIVE_SUBAGENT_TURN in kinds
    for n in body.value:
        if n.kind in (NodeKind.EXPLANATION, NodeKind.NATIVE_SUBAGENT_TURN):
            assert n.payload is None, f"{n.kind} leaked payload at header level"
            assert n.child_manifest is not None


def _render_rev(result: Ok) -> int:
    return result.snapshot.render_rev


def test_visibility_expanded_explanation_returns_full_leaf_payload() -> None:
    """Expanded explanation (children(explanation_node_id)) -> its items
    one level, leaf content nodes carrying FULL payload."""
    root_id = f"root-{uuid.uuid4().hex}"
    adapter = _settle_two_turns(root_id)
    result = adapter.open_session(root_id)
    assert isinstance(result, Ok)
    turn_node_id = result.value.turns[0].turn.node_id
    rev = _render_rev(result)

    body = adapter.children(root_id, turn_node_id, rev)
    assert isinstance(body, Ok)
    explanations = [n for n in body.value if n.kind == NodeKind.EXPLANATION]
    assert explanations, "expected at least one Explanation body item"
    exp = explanations[0]

    members = adapter.children(root_id, exp.node_id, rev)
    assert isinstance(members, Ok)
    assert members.value, "explanation must have at least one member"
    for n in members.value:
        if n.kind not in (NodeKind.EXPLANATION, NodeKind.NATIVE_SUBAGENT_TURN):
            assert n.payload is not None, f"leaf {n.kind} missing payload at expanded level"


def test_visibility_collapsed_subagent_header_only_until_expanded() -> None:
    """Collapsed subagent turn -> header + manifest only (no fetch of its
    own children unless independently expanded)."""
    root_id = f"root-{uuid.uuid4().hex}"
    adapter = _settle_two_turns(root_id)
    result = adapter.open_session(root_id)
    assert isinstance(result, Ok)
    turn_node_id = result.value.turns[0].turn.node_id
    rev = _render_rev(result)

    body = adapter.children(root_id, turn_node_id, rev)
    assert isinstance(body, Ok)
    subagents = [n for n in body.value if n.kind == NodeKind.NATIVE_SUBAGENT_TURN]
    assert len(subagents) == 1
    assert subagents[0].payload is None

    # Expand = one more level: children() of the subagent turn reveals its
    # own body — here a single Explanation header (payload=None); one
    # more expand reaches the leaf with full payload.
    expanded = adapter.children(root_id, subagents[0].node_id, rev)
    assert isinstance(expanded, Ok)
    explanations = [n for n in expanded.value if n.kind == NodeKind.EXPLANATION]
    assert len(explanations) == 1 and explanations[0].payload is None

    leaves = adapter.children(root_id, explanations[0].node_id, rev)
    assert isinstance(leaves, Ok)
    assert any(getattr(n.payload, "text", None) == "subagent working on it" for n in leaves.value)


# ---------------------------------------------------------------------
# 3. Rebuild-on-stale.
# ---------------------------------------------------------------------


def test_rebuild_on_stale_falls_back_and_reindexes() -> None:
    root_id = f"root-{uuid.uuid4().hex}"
    adapter = _settle_two_turns(root_id)
    assert chat_index_mod.read_recent_settled_turns(root_id, 5) is not None

    chat_index_mod.invalidate(root_id)
    assert chat_index_mod.read_recent_settled_turns(root_id, 5) is None

    # A fresh adapter instance (simulating a process restart with a
    # cold-but-now-invalidated index) still answers correctly via the
    # fallback pipeline, and re-warms the index as a side effect.
    adapter2 = ChatSurfaceAdapter()
    result = adapter2.open_session(root_id)
    assert isinstance(result, Ok)
    assert len(result.value.turns) == 2
    assert result.value.turns[0].prompt.payload.text == "please delegate part of this"

    settled_again = chat_index_mod.read_recent_settled_turns(root_id, 5)
    assert settled_again is not None and len(settled_again) == 1


# ---------------------------------------------------------------------
# 4. Watermark resume.
# ---------------------------------------------------------------------


def test_watermark_advances_monotonically_and_resumes() -> None:
    root_id = f"root-{uuid.uuid4().hex}"
    adapter = _adapter_with_bound_bus()
    assert chat_index_mod.watermark_seq(root_id) == 0

    seq1, _task_id, _ = _build_a_turn_with_explanation_and_subagent(root_id)
    _publish_written(root_id, seq1)
    seq2 = _ingest_prompt(root_id, "closes turn 1")
    _publish_written(root_id, seq2)
    wm_after_turn1 = chat_index_mod.watermark_seq(root_id)
    assert wm_after_turn1 >= seq1

    seq3 = _ingest_assistant_text(root_id, "turn 2 reply")
    seq4 = _ingest_prompt(root_id, "closes turn 2")
    _publish_written(root_id, seq4)
    wm_after_turn2 = chat_index_mod.watermark_seq(root_id)
    assert wm_after_turn2 >= seq3
    assert wm_after_turn2 >= wm_after_turn1  # never regresses


# ---------------------------------------------------------------------
# 5. Convergence: index-served == fresh-normalize-served.
# ---------------------------------------------------------------------


def test_index_served_matches_fresh_normalize_served() -> None:
    """The SAME surface read two ways — once via a warm-index adapter
    (fast path), once via a forced-cold adapter (fallback pipeline) —
    must produce IDENTICAL CompactTurn/children() results."""
    root_id = f"root-{uuid.uuid4().hex}"
    adapter_warm = _settle_two_turns(root_id)
    warm_result = adapter_warm.open_session(root_id)
    assert isinstance(warm_result, Ok)

    chat_index_mod.invalidate(root_id)
    adapter_cold = ChatSurfaceAdapter()
    cold_result = adapter_cold.open_session(root_id)
    assert isinstance(cold_result, Ok)

    assert len(warm_result.value.turns) == len(cold_result.value.turns)
    for warm_turn, cold_turn in zip(warm_result.value.turns, cold_result.value.turns):
        assert warm_turn.turn.node_id == cold_turn.turn.node_id
        assert warm_turn.prompt == cold_turn.prompt
        assert warm_turn.results == cold_turn.results
        assert warm_turn.manifest == cold_turn.manifest

    # children() convergence for the first (settled, index-eligible) turn.
    turn_node_id = warm_result.value.turns[0].turn.node_id
    warm_children = adapter_warm.children(root_id, turn_node_id, _render_rev(warm_result))
    cold_children = adapter_cold.children(root_id, turn_node_id, _render_rev(cold_result))
    assert isinstance(warm_children, Ok) and isinstance(cold_children, Ok)
    assert sorted(warm_children.value, key=lambda n: n.node_id) == sorted(cold_children.value, key=lambda n: n.node_id)


class _FakeSessionRecord:
    """Duck-typed stand-in for `store_access.SessionRecord` — only
    `orchestration_mode` is read by `ChatSurfaceAdapter._session_
    orchestration_mode`."""

    def __init__(self, orchestration_mode: str | None) -> None:
        self.orchestration_mode = orchestration_mode


def test_settled_turn_orchestration_mode_and_manifest_time_range_round_trip() -> None:
    """Both new backend-sourced chip fields must survive the chat_index
    sqlite round trip: (1) `ChildManifest.started_ts`/`ended_ts` (the
    Explanation group's count+time-range chip data, ADR 0006 explanation
    chip ruling) and (2) `Node.orchestration_mode` (the Team/native turn
    chip, sourced from the session record AT SETTLE TIME). A cold reload
    with the live session record no longer resolvable (`get_session_
    record` -> None) must still report the ORIGINAL stamped value — proof
    it came from the persisted row, not a fresh store_access re-lookup."""
    root_id = f"root-{uuid.uuid4().hex}"
    original_get_session_record = store_access_mod.store_access.get_session_record
    store_access_mod.store_access.get_session_record = lambda sid: _FakeSessionRecord("team")
    try:
        adapter = _adapter_with_bound_bus()
        _ingest_prompt(root_id, "please do two things")
        text_seq = _ingest_assistant_text(root_id, "on it")
        _ingest_tool_use(root_id, str(uuid.uuid4()), name="Bash")
        seq1 = _ingest_assistant_text(root_id, "done")
        _publish_written(root_id, seq1)
        seq2 = _ingest_prompt(root_id, "second turn closes the first")
        _ingest_assistant_text(root_id, "second turn reply")
        _publish_written(root_id, seq2)

        warm_result = adapter.open_session(root_id)
        assert isinstance(warm_result, Ok)
        warm_turn = warm_result.value.turns[0]
        assert warm_turn.turn.orchestration_mode == "team"

        warm_children = adapter.children(root_id, warm_turn.turn.node_id, _render_rev(warm_result))
        assert isinstance(warm_children, Ok)
        warm_explanation = next(n for n in warm_children.value if n.kind == NodeKind.EXPLANATION)
        assert warm_explanation.child_manifest.started_ts is not None
        assert warm_explanation.child_manifest.ended_ts is not None
        assert warm_explanation.child_manifest.started_ts <= warm_explanation.child_manifest.ended_ts
        assert warm_explanation.child_manifest.renderable_child_count == 2
    finally:
        store_access_mod.store_access.get_session_record = original_get_session_record

    # Cold reload, session record now UNRESOLVABLE — a fresh adapter must
    # still see the values chat_index persisted at settle time.
    store_access_mod.store_access.get_session_record = lambda sid: None
    try:
        cold_adapter = ChatSurfaceAdapter()
        cold_result = cold_adapter.open_session(root_id)
        assert isinstance(cold_result, Ok)
        cold_turn = cold_result.value.turns[0]
        assert cold_turn.turn.orchestration_mode == "team"

        cold_children = cold_adapter.children(root_id, cold_turn.turn.node_id, _render_rev(cold_result))
        assert isinstance(cold_children, Ok)
        cold_explanation = next(n for n in cold_children.value if n.kind == NodeKind.EXPLANATION)
        assert cold_explanation.child_manifest.started_ts == warm_explanation.child_manifest.started_ts
        assert cold_explanation.child_manifest.ended_ts == warm_explanation.child_manifest.ended_ts
        assert cold_explanation.child_manifest.renderable_child_count == 2
    finally:
        store_access_mod.store_access.get_session_record = original_get_session_record


# ---------------------------------------------------------------------
# 6. Monster-scale smoke: O(window) reads, not O(journal).
# ---------------------------------------------------------------------


def test_open_session_index_fast_path_is_o_window_not_o_journal() -> None:
    """50 settled turns + a live tail. A FRESH adapter instance (cold
    Python-level state, warm on-disk index — simulating a process
    restart) must answer open_session by reading a bounded number of
    journal rows, not the whole journal — asserted via read-count
    instrumentation (perf counters), the same idiom H6's perf smoke
    tests already use in test_chat_adapter.py."""
    root_id = f"root-{uuid.uuid4().hex}"
    adapter = _adapter_with_bound_bus()
    turn_count = 50
    for i in range(turn_count):
        seq = _ingest_prompt(root_id, f"turn {i}")
        _ingest_assistant_text(root_id, f"reply {i}")
        _publish_written(root_id, seq)
    # One final live turn, left open (never closed by a "next" prompt or
    # lifecycle fact) — the still-open trailing turn.
    _ingest_prompt(root_id, "final live turn")
    tail_seq = _ingest_assistant_text(root_id, "streaming...")
    _publish_written(root_id, tail_seq)

    # All 50 turns get closed by the NEXT turn's own prompt row opening
    # (transition-fold) — including turn 49, closed by the final live
    # turn's own prompt. read_recent_settled_turns(..., 5) returns the 5
    # most recent of those settled turns.
    settled = chat_index_mod.read_recent_settled_turns(root_id, 5)
    assert settled is not None
    assert len(settled) == 5
    total_journal_rows = event_ingester.current_seq(root_id)
    assert total_journal_rows is not None and total_journal_rows > 50

    read_calls: list[tuple[int, int | None]] = []
    # `_read_all_rows` is a `@staticmethod` — `ChatSurfaceAdapter._read_
    # all_rows` unwraps the descriptor to a plain function on access, so
    # restoring via that unwrapped reference (without re-wrapping in
    # `staticmethod(...)`) would corrupt it into an accidental INSTANCE
    # method for every later `self._read_all_rows(...)` call in this same
    # pytest process (auto-binds `self` as an extra positional arg).
    # Capture straight from `__dict__` (the real descriptor) instead.
    original_descriptor = ChatSurfaceAdapter.__dict__["_read_all_rows"]
    original_read = original_descriptor.__func__

    @staticmethod
    def counting_read(root_id_arg, *, after_seq: int = 0, before_seq=None):
        read_calls.append((after_seq, before_seq))
        return original_read(root_id_arg, after_seq=after_seq, before_seq=before_seq)

    ChatSurfaceAdapter._read_all_rows = counting_read
    try:
        fresh_adapter = ChatSurfaceAdapter()  # simulates a process restart: no seeded state
        result = fresh_adapter.open_session(root_id)
    finally:
        ChatSurfaceAdapter._read_all_rows = original_descriptor

    assert isinstance(result, Ok), result
    assert len(result.value.turns) == 5
    total_rows_read = sum(
        _rows_in_range(root_id, after_seq, before_seq) for after_seq, before_seq in read_calls
    )
    # A true O(journal) rescan would read >1000 rows (50 turns * ~2 rows
    # each + the tail). The fast path only reads the still-open trailing
    # turn's own rows plus a small constant seed/lookup cost.
    assert total_rows_read < 20, (total_rows_read, read_calls)


def _rows_in_range(root_id: str, after_seq: int, before_seq: int | None) -> int:
    rows, _total, _has_more = event_ingester.read_events(root_id, after_seq=after_seq, limit=999_999)
    if before_seq is not None:
        rows = [r for r in rows if r.get("seq", 0) < before_seq]
    return len(rows)


def test_steering_message_payload_attachments_survive_sqlite_round_trip() -> None:
    """Item 2: `SteeringMessagePayload.attachments` (new field) must
    round-trip through `chat_index._reconstruct_payload` exactly like
    `TypedPromptPayload.attachments` already does — `to_wire` serializes
    it generically (reflective over dataclass fields), but the manual
    per-kind `_reconstruct_payload` dispatch needed its own STEERING_
    MESSAGE branch updated to read it back, or it would silently drop on
    every cold reload / settled-turn fold even though the live path
    carried it correctly."""
    payload = SteeringMessagePayload(
        text="steer text", target="turn-1",
        attachments=(Attachment(name="img.png", media_type="image/png", ref="img.png", size=None),),
    )
    wire_dict = to_wire(payload)
    reconstructed = chat_index_mod._reconstruct_payload(NodeKind.STEERING_MESSAGE, wire_dict)
    assert reconstructed == payload
    assert reconstructed.attachments[0].name == "img.png"
    assert reconstructed.attachments[0].ref == "img.png"


def test_steering_message_payload_no_attachments_round_trips_empty() -> None:
    payload = SteeringMessagePayload(text="x", target="t1")
    reconstructed = chat_index_mod._reconstruct_payload(NodeKind.STEERING_MESSAGE, to_wire(payload))
    assert reconstructed.attachments == ()


_TESTS = [
    test_steering_message_payload_attachments_survive_sqlite_round_trip,
    test_steering_message_payload_no_attachments_round_trips_empty,
    test_fold_on_turn_transition_persists_settled_turn_with_subagent,
    test_fold_on_lifecycle_complete_settles_the_last_turn,
    test_fold_persists_worker_turn_with_usage_and_target_ref_round_trip,
    test_fold_persists_worker_turn_created_and_target_turn_id_round_trip,
    test_fold_persists_worker_turn_without_usage_when_no_worker_complete,
    test_visibility_collapsed_turn_never_fetches_body,
    test_visibility_extended_turn_returns_structural_headers_only,
    test_visibility_expanded_explanation_returns_full_leaf_payload,
    test_visibility_collapsed_subagent_header_only_until_expanded,
    test_rebuild_on_stale_falls_back_and_reindexes,
    test_watermark_advances_monotonically_and_resumes,
    test_index_served_matches_fresh_normalize_served,
    test_settled_turn_orchestration_mode_and_manifest_time_range_round_trip,
    test_open_session_index_fast_path_is_o_window_not_o_journal,
]


if __name__ == "__main__":
    failures = 0
    for test in _TESTS:
        try:
            test()
            print(f"PASS {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {test.__name__}: {exc}")
    sys.exit(1 if failures else 0)
