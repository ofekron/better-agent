"""Dedicated authoritative owner for `surface_contract/intents.py` (ADR 0006 §5).

The module is the closed command-plane contract: the SendTargetKind enum, the
SendTarget/IntentBase bases, fifty-one intent dataclasses grouped into four
disjoint intent unions (Chat/Provider/Session/System), the UserInteraction
response union (ApprovalResponse/ChoiceResponse/InputResponse), and the
TransportAck pair. Importing the module executes every definition (so
incidental line coverage reads ~100% via the package __init__ transitive
import — LESSON 49), but nothing asserts the contract. This owner locks every
invariant so a dropped intent, a drifted union member, or a broken
frozen/slots guarantee is caught.

Run: ./scripts/run-backend-tests.sh -- --cov=backend.surface_contract.intents
    --cov-branch scripts/test_surface_contract_intents_unit.py
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

_test_home.isolate("bc-test-surface-contract-intents-unit-")

from backend.surface_contract.intents import (  # noqa: E402
    ApprovalResponse,
    ArchiveSession,
    AssignFolder,
    AssignProject,
    AssignTags,
    BeginLogin,
    CancelLogin,
    ChatIntent,
    ChoiceResponse,
    CreateFolder,
    CreateProject,
    CreateProvider,
    CreateSchedule,
    CreateTag,
    DecideMarketplaceIntent,
    DeleteFolder,
    DeleteHarnessProfile,
    DeleteProject,
    DeleteProvider,
    DeleteQueued,
    DeleteRuntimeProfile,
    DeleteSchedule,
    DeleteTag,
    DisableExtension,
    EditQueued,
    EnableExtension,
    FolderDeleteMode,
    InputResponse,
    InstallExtension,
    IntentAccepted,
    IntentBase,
    IntentRejected,
    InteractionResponse,
    MarketplaceDecision,
    MarkOpened,
    MoveFolder,
    NodeRegistrationDecisionValue,
    ProviderIntent,
    RecolorTag,
    RefreshModels,
    RemoveNode,
    RenameFolder,
    RenameProject,
    RenameSession,
    RenameTag,
    ResolveInteraction,
    ResolveNodeRegistration,
    RetryCredential,
    RevokeMarketplaceDevice,
    Rewind,
    SaveHarnessProfile,
    SaveRuntimeProfile,
    SendMode,
    SendPrompt,
    SendTarget,
    SendTargetKind,
    SessionIntent,
    SetDefaultHarnessProfile,
    SetInstallationCapability,
    SetSelectors,
    Stop,
    SuspendProvider,
    SyncNodeProviders,
    SystemIntent,
    TransportAck,
    UninstallExtension,
    UpdateExtension,
    UpdateExtensionConfig,
    UpdateProvider,
)
from backend.surface_contract.nodes import ApprovalDecision, Attachment  # noqa: E402


def _field_names(cls: type) -> list[str]:
    return [f.name for f in dataclasses.fields(cls)]


def _defaults(cls: type) -> dict[str, object]:
    return {
        f.name: f.default
        for f in dataclasses.fields(cls)
        if f.default is not dataclasses.MISSING
    }


# --------------------------------------------------------------------------- #
# SendTargetKind enum
# --------------------------------------------------------------------------- #


def test_send_target_kind_exact_membership():
    assert {m.name for m in SendTargetKind} == {"CURRENT", "FORK", "NEW_SESSION"}


def test_send_target_kind_values_and_str_identity():
    assert SendTargetKind.CURRENT.value == "current"
    assert SendTargetKind.FORK.value == "fork"
    assert SendTargetKind.NEW_SESSION.value == "new_session"
    assert SendTargetKind.CURRENT == "current"
    assert str(SendTargetKind.FORK) == "fork"


# --------------------------------------------------------------------------- #
# SendTarget
# --------------------------------------------------------------------------- #


def test_send_target_fields_and_defaults():
    assert _field_names(SendTarget) == ["kind", "fork_node_id"]
    assert _defaults(SendTarget) == {"fork_node_id": None}


def test_send_target_requires_kind():
    with pytest.raises(TypeError):
        SendTarget()  # type: ignore[call-arg]


def test_send_target_frozen_and_immutable():
    t = SendTarget(kind=SendTargetKind.FORK, fork_node_id="node-7")
    assert t.kind is SendTargetKind.FORK
    assert t.fork_node_id == "node-7"
    assert SendTarget.__dataclass_params__.frozen is True
    with pytest.raises(dataclasses.FrozenInstanceError):
        t.kind = SendTargetKind.CURRENT  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# IntentBase
# --------------------------------------------------------------------------- #


def test_intent_base_fields():
    assert _field_names(IntentBase) == ["cv", "intent_id", "session_id"]
    assert _defaults(IntentBase) == {}


def test_intent_base_frozen_and_slots():
    assert IntentBase.__dataclass_params__.frozen is True
    assert "__slots__" in IntentBase.__dict__


# --------------------------------------------------------------------------- #
# resolve_interaction's typed response union
# --------------------------------------------------------------------------- #


def test_approval_response_fields_frozen_slots():
    assert _field_names(ApprovalResponse) == ["decision"]
    assert ApprovalResponse.__dataclass_params__.frozen is True
    assert "__slots__" in ApprovalResponse.__dict__
    r = ApprovalResponse(decision=ApprovalDecision.APPROVE)
    assert r.decision is ApprovalDecision.APPROVE
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.decision = ApprovalDecision.DENY  # type: ignore[misc]


def test_choice_response_fields_and_none_allowed():
    assert _field_names(ChoiceResponse) == ["picked_ref"]
    assert _defaults(ChoiceResponse) == {}
    c = ChoiceResponse(picked_ref=None)
    assert c.picked_ref is None


def test_input_response_fields():
    assert _field_names(InputResponse) == ["response"]
    r = InputResponse(response={"answer": "yes"})
    assert r.response == {"answer": "yes"}


def test_interaction_response_union_membership():
    assert set(typing.get_args(InteractionResponse)) == {
        ApprovalResponse,
        ChoiceResponse,
        InputResponse,
    }


def test_resolve_interaction_construction_and_immutable():
    ri = ResolveInteraction(
        cv=1,
        intent_id="i",
        session_id="s",
        interaction_ref="tool_approval:abc",
        response=ApprovalResponse(decision=ApprovalDecision.DENY),
    )
    assert ri.interaction_ref == "tool_approval:abc"
    assert isinstance(ri.response, ApprovalResponse)
    with pytest.raises(dataclasses.FrozenInstanceError):
        ri.interaction_ref = "x"  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# ADR 0011 System-plane enums
# --------------------------------------------------------------------------- #


def test_folder_delete_mode_exact_membership_and_values():
    assert {m.name for m in FolderDeleteMode} == {"UNASSIGN", "DELETE_SESSIONS"}
    assert FolderDeleteMode.UNASSIGN.value == "unassign"
    assert FolderDeleteMode.DELETE_SESSIONS.value == "delete_sessions"


def test_marketplace_decision_exact_membership_and_values():
    assert {m.name for m in MarketplaceDecision} == {"APPROVE", "REJECT"}
    assert MarketplaceDecision.APPROVE.value == "approve"
    assert MarketplaceDecision.REJECT.value == "reject"


def test_node_registration_decision_value_exact_membership_and_values():
    assert {m.name for m in NodeRegistrationDecisionValue} == {"APPROVED", "DENIED"}
    assert NodeRegistrationDecisionValue.APPROVED.value == "approved"
    assert NodeRegistrationDecisionValue.DENIED.value == "denied"


# --------------------------------------------------------------------------- #
# Per-subclass field-name + default + frozen + slots contract (class-level)
# --------------------------------------------------------------------------- #

# (cls, extra_field_names, defaults_dict)
_INTENT_SPECS: list[tuple[type, list[str], dict[str, object]]] = [
    # ---- Chat plane ----
    (SendPrompt, ["text", "attachments", "send_mode", "target"], {}),
    (Stop, ["turn_id"], {}),
    (ResolveInteraction, ["interaction_ref", "response"], {}),
    (EditQueued, ["node_id", "text"], {}),
    (DeleteQueued, ["node_id"], {}),
    (
        SetSelectors,
        ["runtime_profile_id", "model", "reasoning_effort", "permission", "harness_profile_id", "orchestration_mode"],
        {
            "runtime_profile_id": None,
            "model": None,
            "reasoning_effort": None,
            "permission": None,
            "harness_profile_id": None,
            "orchestration_mode": None,
        },
    ),
    (Rewind, ["node_id"], {}),
    # ---- Provider plane ----
    (CreateProvider, ["kind", "config"], {}),
    (UpdateProvider, ["provider_id", "config_patch"], {}),
    (DeleteProvider, ["provider_id"], {}),
    (SuspendProvider, ["provider_id", "suspended"], {}),
    (RetryCredential, ["provider_id"], {}),
    (BeginLogin, ["provider_id", "flow"], {}),
    (CancelLogin, ["provider_id"], {}),
    (RefreshModels, ["provider_id"], {}),
    (SaveRuntimeProfile, ["profile"], {}),
    (DeleteRuntimeProfile, ["runtime_profile_id"], {}),
    # ---- Session plane ----
    (ArchiveSession, ["archived"], {}),
    (RenameSession, ["title"], {}),
    (AssignProject, ["project_ref"], {}),
    (CreateProject, ["name", "path"], {}),
    (RenameProject, ["project_ref", "name"], {}),
    (DeleteProject, ["project_ref"], {}),
    (MarkOpened, [], {}),
    (CreateFolder, ["project_ref", "name", "parent_folder_ref"], {"parent_folder_ref": None}),
    (RenameFolder, ["folder_ref", "name"], {}),
    (MoveFolder, ["folder_ref", "parent_folder_ref"], {}),
    (DeleteFolder, ["folder_ref", "mode"], {}),
    (CreateTag, ["name", "project_ref", "color"], {"project_ref": None, "color": None}),
    (RenameTag, ["tag_ref", "name"], {}),
    (RecolorTag, ["tag_ref", "color"], {}),
    (DeleteTag, ["tag_ref"], {}),
    (AssignFolder, ["folder_ref"], {}),
    (
        AssignTags,
        ["source", "add_tag_refs", "remove_tag_refs", "sync_tag_refs"],
        {
            "source": "manual",
            "add_tag_refs": None,
            "remove_tag_refs": None,
            "sync_tag_refs": None,
        },
    ),
    # ---- System plane (ADR 0011) ----
    (UpdateExtensionConfig, ["extension_id", "section", "patch"], {}),
    (
        SaveHarnessProfile,
        ["harness_profile_id", "config", "revision", "writes"],
        {"revision": None, "writes": ()},
    ),
    (DeleteHarnessProfile, ["harness_profile_id", "revision"], {"revision": None}),
    (SetDefaultHarnessProfile, ["harness_profile_id"], {}),
    (InstallExtension, ["extension_id", "source"], {}),
    (UpdateExtension, ["extension_id"], {}),
    (UninstallExtension, ["extension_id"], {}),
    (EnableExtension, ["extension_id"], {}),
    (DisableExtension, ["extension_id"], {}),
    (DecideMarketplaceIntent, ["marketplace_intent_id", "decision"], {}),
    (RevokeMarketplaceDevice, ["device_ref"], {}),
    (CreateSchedule, ["target_session_id", "prompt", "cadence"], {}),
    (DeleteSchedule, ["schedule_id"], {}),
    (
        SetInstallationCapability,
        ["capability_id", "enabled", "confirm_cancels_extension_work"],
        {"confirm_cancels_extension_work": False},
    ),
    (RemoveNode, ["node_id"], {}),
    (
        SyncNodeProviders,
        ["node_id", "include_secrets", "provider_ids"],
        {"include_secrets": False, "provider_ids": ()},
    ),
    (ResolveNodeRegistration, ["node_id", "decision"], {}),
]


@pytest.mark.parametrize("cls,extra,defaults", _INTENT_SPECS)
def test_subclass_extends_intentbase(cls, extra, defaults):
    assert issubclass(cls, IntentBase)


@pytest.mark.parametrize("cls,extra,defaults", _INTENT_SPECS)
def test_subclass_field_names(cls, extra, defaults):
    assert _field_names(cls) == ["cv", "intent_id", "session_id"] + extra


@pytest.mark.parametrize("cls,extra,defaults", _INTENT_SPECS)
def test_subclass_defaults(cls, extra, defaults):
    assert _defaults(cls) == defaults


@pytest.mark.parametrize("cls,extra,defaults", _INTENT_SPECS)
def test_subclass_frozen(cls, extra, defaults):
    assert cls.__dataclass_params__.frozen is True


@pytest.mark.parametrize("cls,extra,defaults", _INTENT_SPECS)
def test_subclass_slots(cls, extra, defaults):
    assert "__slots__" in cls.__dict__


@pytest.mark.parametrize("cls,extra,defaults", _INTENT_SPECS)
def test_subclass_requires_base_args(cls, extra, defaults):
    """No-arg construction must fail: cv/intent_id/session_id are required."""
    with pytest.raises(TypeError):
        cls()  # type: ignore[call-arg]


def test_intent_specs_cover_every_union_member_exactly_once():
    spec_classes = {cls for cls, _, _ in _INTENT_SPECS}
    assert len(spec_classes) == len(_INTENT_SPECS)
    union_classes = (
        set(typing.get_args(ChatIntent))
        | set(typing.get_args(ProviderIntent))
        | set(typing.get_args(SessionIntent))
        | set(typing.get_args(SystemIntent))
    )
    assert spec_classes == union_classes


# --------------------------------------------------------------------------- #
# Representative instances: construction + immutability across the 4 planes
# --------------------------------------------------------------------------- #


def test_send_prompt_construction_and_immutable():
    sp = SendPrompt(
        cv=3,
        intent_id="i",
        session_id=None,  # new_session target permits None per ADR 0006 §5
        text="hello",
        attachments=(Attachment(name="f", media_type="text/plain", ref="x"),),
        send_mode=SendMode.QUEUE,
        target=SendTarget(kind=SendTargetKind.NEW_SESSION),
    )
    assert sp.cv == 3
    assert sp.session_id is None
    assert sp.target.kind is SendTargetKind.NEW_SESSION
    with pytest.raises(dataclasses.FrozenInstanceError):
        sp.text = "x"  # type: ignore[misc]


def test_set_selectors_all_none_defaults():
    s = SetSelectors(cv=1, intent_id="i", session_id="s")
    assert s.model is None
    assert s.permission is None
    assert s.orchestration_mode is None
    assert s.runtime_profile_id is None


def test_provider_plane_construction_and_immutable():
    p = CreateProvider(cv=1, intent_id="i", session_id=None, kind="claude", config={"x": 1})
    assert p.kind == "claude"
    assert p.config == {"x": 1}
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.kind = "codex"  # type: ignore[misc]


def test_session_plane_construction():
    s = ArchiveSession(cv=1, intent_id="i", session_id="s1", archived=True)
    assert s.archived is True
    assert s.session_id == "s1"


def test_create_project_requires_path():
    p = CreateProject(cv=1, intent_id="i", session_id=None, name="proj", path="/tmp/proj")
    assert p.name == "proj"
    assert p.path == "/tmp/proj"
    with pytest.raises(TypeError):
        CreateProject(cv=1, intent_id="i", session_id=None, name="proj")  # type: ignore[call-arg]


def test_folder_and_tag_plane_construction():
    f = CreateFolder(cv=1, intent_id="i", session_id=None, project_ref="/p", name="Work")
    assert f.parent_folder_ref is None
    t = AssignTags(cv=1, intent_id="i", session_id="s1", add_tag_refs=("t1", "t2"))
    assert t.source == "manual"
    assert t.add_tag_refs == ("t1", "t2")
    assert t.remove_tag_refs is None
    assert t.sync_tag_refs is None


def test_system_plane_construction():
    sh = SaveHarnessProfile(
        cv=1, intent_id="i", session_id=None, harness_profile_id=None, config={"x": 1}
    )
    assert sh.revision is None
    assert sh.writes == ()
    d = DecideMarketplaceIntent(
        cv=1,
        intent_id="i",
        session_id=None,
        marketplace_intent_id="m1",
        decision=MarketplaceDecision.APPROVE,
    )
    assert d.decision is MarketplaceDecision.APPROVE


def test_mark_opened_has_no_extra_fields():
    m = MarkOpened(cv=1, intent_id="i", session_id="s")
    assert _field_names(MarkOpened) == ["cv", "intent_id", "session_id"]
    assert dataclasses.asdict(m) == {"cv": 1, "intent_id": "i", "session_id": "s"}


# --------------------------------------------------------------------------- #
# ChatIntent / ProviderIntent / SessionIntent / SystemIntent unions
# --------------------------------------------------------------------------- #


def test_chat_intent_union_membership():
    assert set(typing.get_args(ChatIntent)) == {
        SendPrompt,
        Stop,
        ResolveInteraction,
        EditQueued,
        DeleteQueued,
        SetSelectors,
        Rewind,
    }


def test_provider_intent_union_membership():
    assert set(typing.get_args(ProviderIntent)) == {
        CreateProvider,
        UpdateProvider,
        DeleteProvider,
        SuspendProvider,
        RetryCredential,
        BeginLogin,
        CancelLogin,
        RefreshModels,
        SaveRuntimeProfile,
        DeleteRuntimeProfile,
    }


def test_session_intent_union_membership():
    assert set(typing.get_args(SessionIntent)) == {
        ArchiveSession,
        RenameSession,
        AssignProject,
        CreateProject,
        RenameProject,
        DeleteProject,
        MarkOpened,
        CreateFolder,
        RenameFolder,
        MoveFolder,
        DeleteFolder,
        CreateTag,
        RenameTag,
        RecolorTag,
        DeleteTag,
        AssignFolder,
        AssignTags,
    }


def test_system_intent_union_membership():
    assert set(typing.get_args(SystemIntent)) == {
        UpdateExtensionConfig,
        SaveHarnessProfile,
        DeleteHarnessProfile,
        SetDefaultHarnessProfile,
        InstallExtension,
        UpdateExtension,
        UninstallExtension,
        EnableExtension,
        DisableExtension,
        DecideMarketplaceIntent,
        RevokeMarketplaceDevice,
        CreateSchedule,
        DeleteSchedule,
        SetInstallationCapability,
        RemoveNode,
        SyncNodeProviders,
        ResolveNodeRegistration,
    }


def test_four_intent_unions_are_pairwise_disjoint():
    chat = set(typing.get_args(ChatIntent))
    provider = set(typing.get_args(ProviderIntent))
    session = set(typing.get_args(SessionIntent))
    system = set(typing.get_args(SystemIntent))
    assert chat.isdisjoint(provider)
    assert chat.isdisjoint(session)
    assert chat.isdisjoint(system)
    assert provider.isdisjoint(session)
    assert provider.isdisjoint(system)
    assert session.isdisjoint(system)


def test_all_intent_subclasses_extend_intentbase():
    union = (
        set(typing.get_args(ChatIntent))
        | set(typing.get_args(ProviderIntent))
        | set(typing.get_args(SessionIntent))
        | set(typing.get_args(SystemIntent))
    )
    for cls in union:
        assert issubclass(cls, IntentBase), cls


# --------------------------------------------------------------------------- #
# TransportAck
# --------------------------------------------------------------------------- #


def test_transport_ack_union_membership():
    assert set(typing.get_args(TransportAck)) == {IntentAccepted, IntentRejected}


def test_intent_accepted_fields_frozen_immutable():
    assert _field_names(IntentAccepted) == ["intent_id"]
    assert IntentAccepted.__dataclass_params__.frozen is True
    assert "__slots__" in IntentAccepted.__dict__
    ack = IntentAccepted(intent_id="i1")
    with pytest.raises(dataclasses.FrozenInstanceError):
        ack.intent_id = "x"  # type: ignore[misc]


def test_intent_rejected_fields_and_required():
    assert _field_names(IntentRejected) == ["intent_id", "code", "message"]
    assert _defaults(IntentRejected) == {}
    with pytest.raises(TypeError):
        IntentRejected(intent_id="i1")  # type: ignore[call-arg]


def test_transport_ack_members_are_not_intents():
    """Acks are projection facts echoing intent_id — they do NOT extend IntentBase."""
    assert not issubclass(IntentAccepted, IntentBase)
    assert not issubclass(IntentRejected, IntentBase)
