"""Dedicated authoritative owner for `surface_contract/frames.py` (ADR 0006 §4;
ADRs 0007-0009 companion surfaces).

The module is a pure type-contract surface: two StrEnums, a `Usage` dataclass,
a `FrameBase`, nine surface-bound chat frames, two control-scope frames, and
two union aliases. This owner locks every contract property rather than merely
importing the names — most importantly the ADR 0006 §4/§5 split: surface-bound
frames subclass `FrameBase` and carry a snapshot identity, control-scope frames
(`SessionCreated`, `ResyncRequired`) do neither.

Run: ./scripts/run-backend-tests.sh -- --cov=surface_contract.frames --cov-branch
    scripts/test_surface_contract_frames_unit.py
"""

from __future__ import annotations

import dataclasses
import os
import sys
import typing
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(BACKEND.parent))

import _test_home  # noqa: E402

_test_home.isolate("bc-test-surface-contract-frames-unit-")

from backend.surface_contract.frames import (  # noqa: E402
    ApprovalUpsert,
    ChatFrame,
    ControlFrame,
    FrameBase,
    NodeStatus,
    NodeUpsert,
    Notice,
    ResyncRequired,
    RunUpsert,
    SessionCreated,
    SessionState,
    SidecarUpsert,
    TerminalReason,
    TextDelta,
    TurnLifecycle,
    TurnPhase,
    Usage,
)
from backend.surface_contract.identity import (  # noqa: E402
    SessionSelectors,
    SnapshotIdentity,
)
from backend.surface_contract.nodes import (  # noqa: E402
    Approval,
    ApprovalState,
    ContentStatus,
    Node,
    NodeKind,
    Run,
    Sidecar,
)


# ── fixtures ─────────────────────────────────────────────────────────────


def _snap() -> SnapshotIdentity:
    return SnapshotIdentity(incarnation="inc-1", render_rev=3, hist_rev=2)


def _usage() -> Usage:
    return Usage(input_tokens=10, output_tokens=20, total_tokens=30)


def _node() -> Node:
    return Node(
        cv=1,
        node_id="n1",
        parent_id=None,
        turn_id="t1",
        surface_id="s1",
        kind=NodeKind.TURN,
        ts=0.0,
        seq=0,
    )


def _run() -> Run:
    return Run(
        run_ref="r1",
        provider_id="claude",
        account_name=None,
        model="haiku",
        reasoning_effort=None,
        runner="native",
    )


def _approval() -> Approval:
    return Approval(
        approval_ref="a1",
        subject="run bash",
        summary="ls",
        risk_scope="bash",
        state=ApprovalState.PENDING,
    )


def _sidecar() -> Sidecar:
    return Sidecar(sidecar_ref="sc1", panel_kind="worker", status="running")


def _selectors() -> SessionSelectors:
    return SessionSelectors(
        provider_id="claude",
        runtime_profile_id=None,
        model=None,
        reasoning_effort=None,
        orchestration_mode=None,
        cwd=None,
    )


# ── TurnPhase enum ───────────────────────────────────────────────────────


def test_turn_phase_has_exactly_the_expected_members():
    assert {
        member.name for member in TurnPhase
    } == {
        "QUEUED",
        "STARTING",
        "RUNNING",
        "AWAITING_APPROVAL",
        "RECONNECTING",
        "STOPPING",
        "COMPLETED",
        "STOPPED",
        "FAILED",
    }


def test_turn_phase_values_match_the_wire_strings():
    assert TurnPhase.QUEUED.value == "queued"
    assert TurnPhase.AWAITING_APPROVAL.value == "awaiting_approval"
    assert TurnPhase.COMPLETED.value == "completed"
    assert TurnPhase.FAILED.value == "failed"


def test_turn_phase_is_a_str_enum():
    assert TurnPhase.RUNNING is not None
    assert TurnPhase.RUNNING == "running"
    assert isinstance(TurnPhase.RUNNING, str)


# ── TerminalReason enum ──────────────────────────────────────────────────


def test_terminal_reason_has_exactly_the_expected_members():
    assert {member.name for member in TerminalReason} == {
        "OK",
        "USER_STOPPED",
        "PROVIDER_ERROR",
        "UNKNOWN_AFTER_RECOVERY",
    }


def test_terminal_reason_values_match_the_wire_strings():
    assert TerminalReason.OK.value == "ok"
    assert TerminalReason.USER_STOPPED.value == "user_stopped"
    assert TerminalReason.PROVIDER_ERROR.value == "provider_error"
    assert TerminalReason.UNKNOWN_AFTER_RECOVERY.value == "unknown_after_recovery"


# ── Usage dataclass ──────────────────────────────────────────────────────


def test_usage_stores_all_three_token_counts():
    usage = Usage(input_tokens=7, output_tokens=11, total_tokens=18)
    assert usage.input_tokens == 7
    assert usage.output_tokens == 11
    assert usage.total_tokens == 18


def test_usage_accepts_all_none():
    usage = Usage(input_tokens=None, output_tokens=None, total_tokens=None)
    assert usage.input_tokens is None
    assert usage.output_tokens is None
    assert usage.total_tokens is None


# ── FrameBase ────────────────────────────────────────────────────────────


def test_frame_base_fields_are_cv_surface_id_snapshot():
    assert [f.name for f in dataclasses.fields(FrameBase)] == [
        "cv",
        "surface_id",
        "snapshot",
    ]


def test_frame_base_is_frozen():
    base = FrameBase(cv=1, surface_id="s1", snapshot=_snap())
    try:
        base.cv = 2  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        return
    raise AssertionError("FrameBase must be frozen")


def test_frame_base_is_slotted_rejecting_new_attributes():
    base = FrameBase(cv=1, surface_id="s1", snapshot=_snap())
    try:
        base.unexpected = 1  # type: ignore[attr-defined]
    except AttributeError:
        return
    raise AssertionError("FrameBase must use slots")


# ── surface-bound chat frames (all subclass FrameBase) ───────────────────

_SURFACE_FRAMES = [
    NodeUpsert,
    TextDelta,
    NodeStatus,
    TurnLifecycle,
    RunUpsert,
    ApprovalUpsert,
    SidecarUpsert,
    SessionState,
    Notice,
]


def test_every_surface_frame_subclasses_frame_base():
    for frame in _SURFACE_FRAMES:
        assert issubclass(frame, FrameBase), f"{frame.__name__} must be a FrameBase"


def test_node_upsert_carries_a_node():
    frame = NodeUpsert(cv=1, surface_id="s1", snapshot=_snap(), node=_node())
    assert frame.node.node_id == "n1"
    assert frame.cv == 1
    assert frame.surface_id == "s1"
    assert frame.snapshot.render_rev == 3


def test_text_delta_carries_appended_text():
    frame = TextDelta(cv=1, surface_id="s1", snapshot=_snap(), node_id="n1", appended_text="hi")
    assert frame.node_id == "n1"
    assert frame.appended_text == "hi"


def test_node_status_carries_a_content_status():
    frame = NodeStatus(cv=1, surface_id="s1", snapshot=_snap(), node_id="n1", status=ContentStatus.STREAMING)
    assert frame.status is ContentStatus.STREAMING


def test_turn_lifecycle_defaults_reason_and_usage_to_none():
    frame = TurnLifecycle(cv=1, surface_id="s1", snapshot=_snap(), turn_id="t1", phase=TurnPhase.RUNNING)
    assert frame.turn_id == "t1"
    assert frame.phase is TurnPhase.RUNNING
    assert frame.reason is None
    assert frame.usage is None


def test_turn_lifecycle_carries_terminal_reason_and_usage():
    frame = TurnLifecycle(
        cv=1,
        surface_id="s1",
        snapshot=_snap(),
        turn_id="t1",
        phase=TurnPhase.COMPLETED,
        reason=TerminalReason.OK,
        usage=_usage(),
    )
    assert frame.reason is TerminalReason.OK
    assert frame.usage.total_tokens == 30


def test_run_upsert_carries_a_run():
    frame = RunUpsert(cv=1, surface_id="s1", snapshot=_snap(), run=_run())
    assert frame.run.run_ref == "r1"


def test_approval_upsert_carries_an_approval():
    frame = ApprovalUpsert(cv=1, surface_id="s1", snapshot=_snap(), approval=_approval())
    assert frame.approval.state is ApprovalState.PENDING


def test_sidecar_upsert_carries_a_sidecar():
    frame = SidecarUpsert(cv=1, surface_id="s1", snapshot=_snap(), sidecar=_sidecar())
    assert frame.sidecar.panel_kind == "worker"


def test_session_state_defaults_all_optional_fields_to_none():
    frame = SessionState(cv=1, surface_id="s1", snapshot=_snap())
    assert frame.intent_id is None
    assert frame.title is None
    assert frame.markers is None
    assert frame.selectors is None


def test_session_state_carries_full_state():
    frame = SessionState(
        cv=1,
        surface_id="s1",
        snapshot=_snap(),
        intent_id="i1",
        title="t",
        markers=("a", "b"),
        selectors=_selectors(),
    )
    assert frame.intent_id == "i1"
    assert frame.title == "t"
    assert frame.markers == ("a", "b")
    assert frame.selectors.provider_id == "claude"


def test_notice_carries_scope_and_payload():
    frame = Notice(cv=1, surface_id="s1", snapshot=_snap(), scope="global", payload={"k": "v"})
    assert frame.scope == "global"
    assert frame.payload == {"k": "v"}


def test_surface_frames_are_frozen():
    frame = NodeUpsert(cv=1, surface_id="s1", snapshot=_snap(), node=_node())
    try:
        frame.cv = 9  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        return
    raise AssertionError("surface frames must be frozen")


# ── control-scope frames (ADR 0006 §4/§5 — NOT FrameBase) ────────────────


def test_session_created_does_not_subclass_frame_base():
    # Control-scope frames carry only cv (no snapshot identity) — they are not
    # bound to a surface subscription.
    assert not issubclass(SessionCreated, FrameBase)


def test_resync_required_does_not_subclass_frame_base():
    assert not issubclass(ResyncRequired, FrameBase)


def test_session_created_fields_and_values():
    frame = SessionCreated(cv=5, intent_id="i1", session_id="sess-1", surface_id="s1")
    assert frame.cv == 5
    assert frame.intent_id == "i1"
    assert frame.session_id == "sess-1"
    assert frame.surface_id == "s1"
    assert [f.name for f in dataclasses.fields(SessionCreated)] == [
        "cv",
        "intent_id",
        "session_id",
        "surface_id",
    ]


def test_resync_required_fields_and_values():
    frame = ResyncRequired(cv=7, surface_id="s1")
    assert frame.cv == 7
    assert frame.surface_id == "s1"
    assert [f.name for f in dataclasses.fields(ResyncRequired)] == ["cv", "surface_id"]


def test_control_frames_are_frozen():
    frame = ResyncRequired(cv=1, surface_id="s1")
    try:
        frame.cv = 2  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        return
    raise AssertionError("control frames must be frozen")


# ── union aliases ────────────────────────────────────────────────────────


def test_chat_frame_union_contains_exactly_the_surface_frames_plus_resync():
    expected = set(_SURFACE_FRAMES) | {ResyncRequired}
    assert set(typing.get_args(ChatFrame)) == expected


def test_chat_frame_union_excludes_control_session_created():
    # SessionCreated is control-only; it must never appear on a chat surface
    # subscription stream.
    assert SessionCreated not in typing.get_args(ChatFrame)


def test_control_frame_alias_is_session_created():
    assert ControlFrame is SessionCreated
