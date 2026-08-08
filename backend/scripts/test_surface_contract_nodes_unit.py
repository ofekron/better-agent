"""Dedicated authoritative owner for `surface_contract/nodes.py` (ADR 0006 §1).

The module is the closed, versioned serialization of the chat-panel.md
content-plane grammar: eleven StrEnums, three structural frozensets, an
eighteen-member payload union, and the core Node/Run/Approval/Sidecar
dataclasses. Importing the module executes every definition (so incidental
line coverage reads ~100%), but nothing asserts the contract. This owner
locks every invariant so a dropped enum member, a drifted frozenset, or a
broken union membership is caught.

Run: ./scripts/run-backend-tests.sh -- --cov=backend.surface_contract.nodes
    --cov-branch scripts/test_surface_contract_nodes_unit.py
"""

from __future__ import annotations

import dataclasses
import sys
import typing
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(BACKEND.parent))

import _test_home  # noqa: E402

_test_home.isolate("bc-test-surface-contract-nodes-unit-")

from backend.surface_contract.nodes import (  # noqa: E402
    Approval,
    ApprovalState,
    AssistantTextPayload,
    Attachment,
    ChildManifest,
    CompactionOrigin,
    CompactionPayload,
    ContentStatus,
    ContinuationSessionPayload,
    DiagnosticCode,
    DiagnosticPayload,
    FactPayload,
    FailurePayload,
    HarnessChangePayload,
    InstructionWidgetPayload,
    LifecycleNoticeKind,
    LifecycleNoticePayload,
    ModelChangePayload,
    ModelChangeSource,
    Node,
    NodeKind,
    NodePayload,
    PromptOrigin,
    ResultKind,
    ResultPayload,
    Run,
    RUNTIME_CHANGED_KINDS,
    SendMode,
    Sidecar,
    SteeringMessagePayload,
    STRUCTURAL_KINDS,
    SUBAGENT_TURN_KINDS,
    TargetRef,
    ThinkingPayload,
    ToolInteractionPayload,
    TypedPromptPayload,
    UnknownPayload,
    UserInteractionState,
    UserInteractionPayload,
    WorkerInteractionPayload,
)


def _members(enum_cls) -> dict[str, str]:
    """name -> value for every member, asserting value == str(member)."""
    out = {}
    for m in enum_cls:
        out[m.name] = m.value
        assert str(m) == m.value  # StrEnum identity: str(member) is the value
    return out


def _assert_frozen_slots(cls) -> None:
    assert cls.__dataclass_params__.frozen is True
    assert "__slots__" in cls.__dict__


def _field_defaults(cls) -> dict[str, object]:
    return {f.name: f.default for f in dataclasses.fields(cls)}


def _field_names(cls) -> list[str]:
    return [f.name for f in dataclasses.fields(cls)]


# ---------------------------------------------------------------- enums


def test_nodekind_membership_and_values():
    assert _members(NodeKind) == {
        "INSTRUCTION_WIDGET": "instruction_widget",
        "TURN": "turn",
        "TYPED_PROMPT": "typed_prompt",
        "EXPLANATION": "explanation",
        "ASSISTANT_TEXT": "assistant_text",
        "THINKING": "thinking",
        "TOOL_INTERACTION": "tool_interaction",
        "WORKER_INTERACTION": "worker_interaction",
        "STEERING_MESSAGE": "steering_message",
        "NATIVE_SUBAGENT_TURN": "native_subagent_turn",
        "WORKER_TURN": "worker_turn",
        "SUB_SESSION_TURN": "sub_session_turn",
        "SESSION_TURN": "session_turn",
        "MODEL_CHANGE": "model_change",
        "HARNESS_CHANGE": "harness_change",
        "RESULT": "result",
        "COMPACTION": "compaction",
        "CONTINUATION_SESSION": "continuation_session",
        "FAILURE": "failure",
        "DIAGNOSTIC": "diagnostic",
        "USER_INTERACTION": "user_interaction",
        "LIFECYCLE_NOTICE": "lifecycle_notice",
        "FACT": "fact",
        "UNKNOWN": "unknown",
    }


def test_content_status_membership_and_values():
    assert _members(ContentStatus) == {
        "QUEUED": "queued",
        "STREAMING": "streaming",
        "PARTIAL": "partial",
        "COMPLETE": "complete",
        "FAILED": "failed",
        "STOPPED": "stopped",
    }


def test_send_mode_membership_and_values():
    assert _members(SendMode) == {
        "QUEUE": "queue",
        "INTERRUPT": "interrupt",
        "STEER": "steer",
    }


def test_prompt_origin_membership_and_values():
    assert _members(PromptOrigin) == {
        "USER": "user",
        "QUEUED": "queued",
        "OFFLINE_SYNC": "offline_sync",
        "ASK": "ask",
        "SUPERVISOR": "supervisor",
    }


def test_small_enums_membership_and_values():
    assert _members(ResultKind) == {"PROVIDER": "provider", "DERIVED": "derived"}
    assert _members(DiagnosticCode) == {
        "EXECUTION_CONTINUATION": "execution_continuation",
        "OTHER": "other",
    }
    assert _members(ModelChangeSource) == {
        "USER": "user",
        "PROVIDER": "provider",
    }
    assert _members(CompactionOrigin) == {
        "NATIVE": "native",
        "BETTER_AGENT": "better_agent",
    }
    assert _members(UserInteractionState) == {
        "PENDING": "pending",
        "RESOLVED": "resolved",
        "CANCELLED": "cancelled",
    }
    assert _members(ApprovalState) == {
        "PENDING": "pending",
        "APPROVED": "approved",
        "DENIED": "denied",
    }


def test_lifecycle_notice_kind_membership_and_values():
    assert _members(LifecycleNoticeKind) == {
        "RETRYING": "retrying",
        "DETACHED": "detached",
        "RECOVERING": "recovering",
        "AUTO_RETRIED": "auto_retried",
        "RATE_LIMITED": "rate_limited",
    }


# ----------------------------------------------------- structural frozensets


def test_structural_kinds_exact_membership():
    assert isinstance(STRUCTURAL_KINDS, frozenset)
    assert STRUCTURAL_KINDS == frozenset(
        {
            NodeKind.TURN,
            NodeKind.EXPLANATION,
            NodeKind.NATIVE_SUBAGENT_TURN,
            NodeKind.WORKER_TURN,
            NodeKind.SUB_SESSION_TURN,
            NodeKind.SESSION_TURN,
            NodeKind.RESULT,
        }
    )


def test_subagent_turn_kinds_exact_membership_and_subset():
    assert isinstance(SUBAGENT_TURN_KINDS, frozenset)
    assert SUBAGENT_TURN_KINDS == frozenset(
        {
            NodeKind.NATIVE_SUBAGENT_TURN,
            NodeKind.WORKER_TURN,
            NodeKind.SUB_SESSION_TURN,
            NodeKind.SESSION_TURN,
        }
    )
    # The four sourcing modes are all structural container kinds.
    assert SUBAGENT_TURN_KINDS <= STRUCTURAL_KINDS


def test_runtime_changed_kinds_exact_membership_and_disjoint():
    assert isinstance(RUNTIME_CHANGED_KINDS, frozenset)
    assert RUNTIME_CHANGED_KINDS == frozenset(
        {NodeKind.MODEL_CHANGE, NodeKind.HARNESS_CHANGE}
    )
    # Boundary nodes are never structural containers nor subagent turns.
    assert RUNTIME_CHANGED_KINDS.isdisjoint(STRUCTURAL_KINDS)
    assert RUNTIME_CHANGED_KINDS.isdisjoint(SUBAGENT_TURN_KINDS)


def test_all_kind_group_members_are_nodekind_values():
    all_kinds = set(NodeKind)
    assert STRUCTURAL_KINDS <= all_kinds
    assert SUBAGENT_TURN_KINDS <= all_kinds
    assert RUNTIME_CHANGED_KINDS <= all_kinds


# --------------------------------------------------------- NodePayload union


def test_node_payload_union_membership():
    expected = {
        InstructionWidgetPayload,
        TypedPromptPayload,
        AssistantTextPayload,
        ThinkingPayload,
        ToolInteractionPayload,
        WorkerInteractionPayload,
        SteeringMessagePayload,
        ModelChangePayload,
        HarnessChangePayload,
        ResultPayload,
        CompactionPayload,
        ContinuationSessionPayload,
        FailurePayload,
        DiagnosticPayload,
        UserInteractionPayload,
        LifecycleNoticePayload,
        FactPayload,
        UnknownPayload,
        type(None),
    }
    assert set(typing.get_args(NodePayload)) == expected


# ------------------------------------------------- payload dataclass shapes


def test_typed_prompt_payload_fields_and_defaults():
    assert _field_names(TypedPromptPayload) == [
        "text",
        "attachments",
        "send_mode",
        "origin",
        "source_session_ref",
        "sent_text",
        "intent_id",
    ]
    _assert_frozen_slots(TypedPromptPayload)
    d = _field_defaults(TypedPromptPayload)
    assert d["attachments"] == ()
    assert d["send_mode"] == SendMode.QUEUE
    assert d["origin"] == PromptOrigin.USER
    assert d["source_session_ref"] is None
    assert d["sent_text"] is None
    assert d["intent_id"] is None


def test_instruction_widget_payload_fields_and_defaults():
    assert _field_names(InstructionWidgetPayload) == ["text", "action"]
    _assert_frozen_slots(InstructionWidgetPayload)
    assert _field_defaults(InstructionWidgetPayload)["action"] is None


def test_assistant_text_payload_fields():
    assert _field_names(AssistantTextPayload) == ["text"]
    _assert_frozen_slots(AssistantTextPayload)


def test_thinking_payload_fields_and_defaults():
    assert _field_names(ThinkingPayload) == ["text", "redacted"]
    _assert_frozen_slots(ThinkingPayload)
    assert _field_defaults(ThinkingPayload)["redacted"] is False


def test_tool_interaction_payload_fields_and_defaults():
    assert _field_names(ToolInteractionPayload) == [
        "tool_name",
        "args",
        "result",
        "approval_ref",
        "ui_kind",
        "derived_view",
    ]
    _assert_frozen_slots(ToolInteractionPayload)
    d = _field_defaults(ToolInteractionPayload)
    assert d["result"] is None
    assert d["approval_ref"] is None
    assert d["ui_kind"] is None
    assert d["derived_view"] is None


def test_steering_message_payload_fields():
    assert _field_names(SteeringMessagePayload) == ["text", "target"]
    _assert_frozen_slots(SteeringMessagePayload)


def test_model_change_payload_fields():
    assert _field_names(ModelChangePayload) == [
        "from_run_ref",
        "to_run_ref",
        "source",
    ]
    _assert_frozen_slots(ModelChangePayload)


def test_harness_change_payload_fields():
    assert _field_names(HarnessChangePayload) == [
        "from_harness_profile_id",
        "to_harness_profile_id",
    ]
    _assert_frozen_slots(HarnessChangePayload)


def test_worker_interaction_payload_fields():
    assert _field_names(WorkerInteractionPayload) == ["fact_kind", "fact"]
    _assert_frozen_slots(WorkerInteractionPayload)


def test_result_payload_fields():
    assert _field_names(ResultPayload) == ["result_kind"]
    _assert_frozen_slots(ResultPayload)


def test_compaction_payload_fields_and_defaults():
    assert _field_names(CompactionPayload) == ["origin", "summary", "replaced_node_ids"]
    _assert_frozen_slots(CompactionPayload)
    assert _field_defaults(CompactionPayload)["replaced_node_ids"] == ()


def test_continuation_session_payload_fields_and_defaults():
    assert _field_names(ContinuationSessionPayload) == [
        "execution_ref",
        "chain_depth",
        "summary",
    ]
    _assert_frozen_slots(ContinuationSessionPayload)
    assert _field_defaults(ContinuationSessionPayload)["summary"] is None


def test_failure_payload_fields_and_defaults():
    assert _field_names(FailurePayload) == ["code", "text", "data"]
    _assert_frozen_slots(FailurePayload)
    assert _field_defaults(FailurePayload)["data"] is None


def test_diagnostic_payload_fields_and_defaults():
    assert _field_names(DiagnosticPayload) == ["severity", "code", "text", "data"]
    _assert_frozen_slots(DiagnosticPayload)
    assert _field_defaults(DiagnosticPayload)["data"] is None


def test_user_interaction_payload_fields_and_defaults():
    assert _field_names(UserInteractionPayload) == ["kind", "request", "state", "response"]
    _assert_frozen_slots(UserInteractionPayload)
    d = _field_defaults(UserInteractionPayload)
    assert d["state"] == UserInteractionState.PENDING
    assert d["response"] is None


def test_lifecycle_notice_payload_fields_and_defaults():
    assert _field_names(LifecycleNoticePayload) == ["kind", "data"]
    _assert_frozen_slots(LifecycleNoticePayload)
    assert _field_defaults(LifecycleNoticePayload)["data"] is None


def test_fact_payload_fields():
    assert _field_names(FactPayload) == ["kind", "data"]
    _assert_frozen_slots(FactPayload)


def test_unknown_payload_fields():
    assert _field_names(UnknownPayload) == ["label", "payload"]
    _assert_frozen_slots(UnknownPayload)


def test_attachment_fields():
    assert _field_names(Attachment) == ["name", "media_type", "ref"]
    _assert_frozen_slots(Attachment)


def test_target_ref_fields():
    assert _field_names(TargetRef) == ["session_id", "turn_id"]
    _assert_frozen_slots(TargetRef)


# ------------------------------------------------------ core node dataclasses


def test_child_manifest_fields():
    assert _field_names(ChildManifest) == ["renderable_child_count", "has_children"]
    _assert_frozen_slots(ChildManifest)


def test_node_fields_and_defaults():
    assert _field_names(Node) == [
        "cv",
        "node_id",
        "parent_id",
        "turn_id",
        "surface_id",
        "kind",
        "ts",
        "seq",
        "status",
        "payload",
        "run_ref",
        "sidecar_ref",
        "target_ref",
        "child_manifest",
    ]
    _assert_frozen_slots(Node)
    d = _field_defaults(Node)
    for name in (
        "status",
        "payload",
        "run_ref",
        "sidecar_ref",
        "target_ref",
        "child_manifest",
    ):
        assert d[name] is None


def test_node_is_frozen_and_slotted():
    node = Node(
        cv=1,
        node_id="n1",
        parent_id=None,
        turn_id="t1",
        surface_id="s1",
        kind=NodeKind.TURN,
        ts=1.0,
        seq=1,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        node.cv = 2
    with pytest.raises(AttributeError):
        node.no_such_field = 1


def test_node_required_fields_enforced():
    with pytest.raises(TypeError):
        Node(cv=1, node_id="n1")  # missing required fields


def test_run_fields():
    assert _field_names(Run) == [
        "run_ref",
        "provider_id",
        "account_name",
        "model",
        "reasoning_effort",
        "runner",
    ]
    _assert_frozen_slots(Run)


def test_approval_fields():
    assert _field_names(Approval) == [
        "approval_ref",
        "subject",
        "summary",
        "risk_scope",
        "state",
    ]
    _assert_frozen_slots(Approval)


def test_sidecar_fields_and_default_factory():
    assert _field_names(Sidecar) == ["sidecar_ref", "panel_kind", "status", "payload"]
    _assert_frozen_slots(Sidecar)
    # default_factory yields an independent dict per instance.
    f = {fld.name: fld for fld in dataclasses.fields(Sidecar)}["payload"]
    assert f.default is dataclasses.MISSING
    assert callable(f.default_factory)
    sidecar = Sidecar(sidecar_ref="r", panel_kind="k", status="ok")
    assert sidecar.payload == {}


def test_sidecar_default_factory_is_independent():
    a = Sidecar(sidecar_ref="a", panel_kind="k", status="ok")
    b = Sidecar(sidecar_ref="b", panel_kind="k", status="ok")
    a.payload["x"] = 1
    assert b.payload == {}


def test_typed_prompt_payload_construct_and_freeze():
    p = TypedPromptPayload(text="hi")
    assert p.text == "hi"
    assert p.attachments == ()
    assert p.send_mode == SendMode.QUEUE
    assert p.origin == PromptOrigin.USER
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.text = "no"
