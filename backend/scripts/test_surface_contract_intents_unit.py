"""Dedicated authoritative owner for `surface_contract/intents.py` (ADR 0006 §5).

The module is the closed command-plane contract: the SendTargetKind enum, the
SendTarget/IntentBase bases, twenty intent dataclasses grouped into three
disjoint intent unions (Chat/Provider/Session), and the TransportAck pair.
Importing the module executes every definition (so incidental line coverage
reads ~100% via the package __init__ transitive import — LESSON 49), but
nothing asserts the contract. This owner locks every invariant so a dropped
intent, a drifted union member, or a broken frozen/slots guarantee is caught.

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
    Approve,
    ArchiveSession,
    AssignProject,
    BeginLogin,
    CancelLogin,
    ChatIntent,
    CreateProject,
    CreateProvider,
    DeleteProject,
    DeleteProvider,
    DeleteQueued,
    DeleteRuntimeProfile,
    EditQueued,
    IntentAccepted,
    IntentBase,
    IntentRejected,
    MarkOpened,
    ProviderIntent,
    RefreshModels,
    RenameProject,
    RenameSession,
    Rearrange,
    RetryCredential,
    Rewind,
    SaveRuntimeProfile,
    SendMode,
    SendPrompt,
    SendTarget,
    SendTargetKind,
    SessionIntent,
    SetSelectors,
    Stop,
    SuspendProvider,
    TransportAck,
    UpdateProvider,
)
from backend.surface_contract.nodes import Attachment  # noqa: E402


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
# Per-subclass field-name + default + frozen + slots contract (class-level)
# --------------------------------------------------------------------------- #

# (cls, extra_field_names, defaults_dict)
_INTENT_SPECS: list[tuple[type, list[str], dict[str, object]]] = [
    (SendPrompt, ["text", "attachments", "send_mode", "target"], {}),
    (Stop, ["turn_id"], {}),
    (Approve, ["approval_ref", "decision", "scope"], {}),
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
    (ArchiveSession, ["archived"], {}),
    (RenameSession, ["title"], {}),
    (AssignProject, ["project_ref"], {}),
    (CreateProject, ["name"], {}),
    (RenameProject, ["project_ref", "name"], {}),
    (DeleteProject, ["project_ref"], {}),
    (Rearrange, ["ordering_patch"], {}),
    (MarkOpened, [], {}),
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


# --------------------------------------------------------------------------- #
# Representative instances: construction + immutability across the 3 planes
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


def test_mark_opened_has_no_extra_fields():
    m = MarkOpened(cv=1, intent_id="i", session_id="s")
    assert _field_names(MarkOpened) == ["cv", "intent_id", "session_id"]
    assert dataclasses.asdict(m) == {"cv": 1, "intent_id": "i", "session_id": "s"}


# --------------------------------------------------------------------------- #
# ChatIntent / ProviderIntent / SessionIntent unions
# --------------------------------------------------------------------------- #


def test_chat_intent_union_membership():
    assert set(typing.get_args(ChatIntent)) == {
        SendPrompt,
        Stop,
        Approve,
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
        Rearrange,
        MarkOpened,
    }


def test_three_intent_unions_are_pairwise_disjoint():
    chat = set(typing.get_args(ChatIntent))
    provider = set(typing.get_args(ProviderIntent))
    session = set(typing.get_args(SessionIntent))
    assert chat.isdisjoint(provider)
    assert chat.isdisjoint(session)
    assert provider.isdisjoint(session)


def test_all_intent_subclasses_extend_intentbase():
    union = (
        set(typing.get_args(ChatIntent))
        | set(typing.get_args(ProviderIntent))
        | set(typing.get_args(SessionIntent))
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
