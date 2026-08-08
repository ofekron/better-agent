"""Dedicated authoritative owner for `surface_contract/nodes.py` (ADR 0006 §1).

The module is the closed, versioned serialization of the chat-panel.md
content-plane grammar: fourteen StrEnums, three structural frozensets, a
nineteen-member payload union, and the core Node/Run/UserInteraction/Sidecar
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
    ApprovalDecision,
    AssistantTextPayload,
    Attachment,
    ChildManifest,
    CompactionOrigin,
    CompactionPayload,
    ContentStatus,
    ContinuationSessionPayload,
    DELEGATE_CHOICE_REF_PREFIX,
    DiagnosticCode,
    DiagnosticPayload,
    FactPayload,
    FailurePayload,
    FailureResolution,
    FailureSeverity,
    HarnessChangePayload,
    InstructionWidgetPayload,
    interaction_fact_payload,
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
    SubAgentTurnPayload,
    TargetRef,
    ThinkingPayload,
    ToolInteractionPayload,
    TOOL_APPROVAL_REF_PREFIX,
    TypedPromptPayload,
    UnknownPayload,
    Usage,
    usage_from_raw,
    UserInteraction,
    UserInteractionKind,
    UserInteractionPayload,
    UserInteractionState,
    WORKER_APPROVAL_REF_PREFIX,
    WorkerInteractionPayload,
    USER_INPUT_REF_PREFIX,
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
        "ALTER": "alter",
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
    assert _members(UserInteractionKind) == {
        "APPROVAL": "approval",
        "CHOICE": "choice",
        "INPUT": "input",
    }
    assert _members(ApprovalDecision) == {
        "APPROVE": "approve",
        "DENY": "deny",
    }
    assert _members(FailureSeverity) == {
        "INFO": "info",
        "WARNING": "warning",
        "ERROR": "error",
    }
    assert _members(FailureResolution) == {
        "NONE": "none",
        "RETRY": "retry",
        "FIX_CREDENTIAL": "fix_credential",
        "CHOOSE_FALLBACK": "choose_fallback",
        "OPEN_SETTINGS": "open_settings",
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
        SubAgentTurnPayload,
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
        "interaction_ref",
        "ui_kind",
        "derived_view",
    ]
    _assert_frozen_slots(ToolInteractionPayload)
    d = _field_defaults(ToolInteractionPayload)
    assert d["result"] is None
    assert d["interaction_ref"] is None
    assert d["ui_kind"] is None
    assert d["derived_view"] is None


def test_steering_message_payload_fields_and_defaults():
    assert _field_names(SteeringMessagePayload) == ["text", "target", "attachments"]
    _assert_frozen_slots(SteeringMessagePayload)
    assert _field_defaults(SteeringMessagePayload)["attachments"] == ()


def test_subagent_turn_payload_fields_and_defaults():
    assert _field_names(SubAgentTurnPayload) == ["label", "created"]
    _assert_frozen_slots(SubAgentTurnPayload)
    assert _field_defaults(SubAgentTurnPayload)["created"] is False


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


def test_result_payload_fields_and_defaults():
    assert _field_names(ResultPayload) == ["result_kind", "text", "is_error"]
    _assert_frozen_slots(ResultPayload)
    d = _field_defaults(ResultPayload)
    assert d["text"] is None
    assert d["is_error"] is False


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
    assert _field_names(FailurePayload) == [
        "code",
        "text",
        "data",
        "severity",
        "retryable",
        "resolution",
    ]
    _assert_frozen_slots(FailurePayload)
    d = _field_defaults(FailurePayload)
    assert d["data"] is None
    assert d["severity"] == FailureSeverity.ERROR
    assert d["retryable"] is False
    assert d["resolution"] == FailureResolution.NONE


def test_diagnostic_payload_fields_and_defaults():
    assert _field_names(DiagnosticPayload) == ["severity", "code", "text", "data"]
    _assert_frozen_slots(DiagnosticPayload)
    assert _field_defaults(DiagnosticPayload)["data"] is None


def test_user_interaction_payload_fields():
    # Render anchor only — carries no state/kind of its own; resolves via
    # the UserInteraction resource keyed by interaction_ref.
    assert _field_names(UserInteractionPayload) == ["interaction_ref"]
    _assert_frozen_slots(UserInteractionPayload)
    assert _field_defaults(UserInteractionPayload) == {"interaction_ref": dataclasses.MISSING}


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
    assert _field_names(Attachment) == ["name", "media_type", "ref", "size"]
    _assert_frozen_slots(Attachment)
    # None when byte size is not cheaply derivable — never a guessed/zero
    # placeholder.
    assert _field_defaults(Attachment)["size"] is None


def test_target_ref_fields_and_defaults():
    assert _field_names(TargetRef) == ["session_id", "turn_id"]
    _assert_frozen_slots(TargetRef)
    # None when only the target session is known and no turn is resolvable
    # yet — never fabricated.
    assert _field_defaults(TargetRef)["turn_id"] is None


# ------------------------------------------------------ core node dataclasses


def test_child_manifest_fields():
    assert _field_names(ChildManifest) == [
        "renderable_child_count",
        "has_children",
        "started_ts",
        "ended_ts",
    ]
    _assert_frozen_slots(ChildManifest)
    # Group time span (earliest/latest member ts) — both None only when the
    # container has no children at all, never a guessed/zero ts.
    d = _field_defaults(ChildManifest)
    assert d["started_ts"] is None
    assert d["ended_ts"] is None


def test_usage_fields_and_defaults():
    assert _field_names(Usage) == [
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    ]
    _assert_frozen_slots(Usage)
    d = _field_defaults(Usage)
    assert d["cache_creation_input_tokens"] is None
    assert d["cache_read_input_tokens"] is None


def test_usage_from_raw_non_dict_is_none():
    assert usage_from_raw(None) is None
    assert usage_from_raw("not a dict") is None
    assert usage_from_raw(42) is None


def test_usage_from_raw_empty_dict_is_all_none():
    usage = usage_from_raw({})
    assert usage == Usage(
        input_tokens=None,
        output_tokens=None,
        total_tokens=None,
        cache_creation_input_tokens=None,
        cache_read_input_tokens=None,
    )


def test_usage_from_raw_derives_total_only_when_both_present():
    assert usage_from_raw({"input_tokens": 10}).total_tokens is None
    assert usage_from_raw({"output_tokens": 5}).total_tokens is None
    usage = usage_from_raw({"input_tokens": 10, "output_tokens": 5})
    assert usage.total_tokens == 15


def test_usage_from_raw_full_shape():
    usage = usage_from_raw(
        {
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_creation_input_tokens": 3,
            "cache_read_input_tokens": 7,
        }
    )
    assert usage == Usage(
        input_tokens=10,
        output_tokens=5,
        total_tokens=15,
        cache_creation_input_tokens=3,
        cache_read_input_tokens=7,
    )


def test_usage_from_raw_ignores_non_int_fields():
    usage = usage_from_raw(
        {
            "input_tokens": "10",
            "output_tokens": None,
            "cache_creation_input_tokens": "3",
            "cache_read_input_tokens": None,
        }
    )
    assert usage == Usage(
        input_tokens=None,
        output_tokens=None,
        total_tokens=None,
        cache_creation_input_tokens=None,
        cache_read_input_tokens=None,
    )


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
        "usage",
        "orchestration_mode",
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
        "usage",
        "orchestration_mode",
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


def test_user_interaction_resource_fields_and_defaults():
    # Session-scoped cross-cutting resource — the parent of every
    # UserInteraction kind (approval/choice/input); request/response are
    # kind-shaped dicts, not a per-kind dataclass union.
    assert _field_names(UserInteraction) == [
        "interaction_ref",
        "kind",
        "request",
        "state",
        "response",
    ]
    _assert_frozen_slots(UserInteraction)
    assert _field_defaults(UserInteraction)["response"] is None


def test_interaction_ref_prefixes():
    assert TOOL_APPROVAL_REF_PREFIX == "tool_approval:"
    assert WORKER_APPROVAL_REF_PREFIX == "worker_approval:"
    assert DELEGATE_CHOICE_REF_PREFIX == "delegate_choice:"
    assert USER_INPUT_REF_PREFIX == "user_input:"


def test_interaction_fact_payload_without_response():
    payload = interaction_fact_payload(
        "tool_approval:abc",
        UserInteractionKind.APPROVAL,
        {"subject": "run rm"},
    )
    assert payload == {
        "interaction_ref": "tool_approval:abc",
        "kind": "approval",
        "request": {"subject": "run rm"},
        "state": "pending",
    }
    assert "response" not in payload


def test_interaction_fact_payload_with_response():
    payload = interaction_fact_payload(
        "tool_approval:abc",
        UserInteractionKind.APPROVAL,
        {"subject": "run rm"},
        state=UserInteractionState.RESOLVED,
        response={"decision": ApprovalDecision.APPROVE.value},
    )
    assert payload == {
        "interaction_ref": "tool_approval:abc",
        "kind": "approval",
        "request": {"subject": "run rm"},
        "state": "resolved",
        "response": {"decision": "approve"},
    }


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
