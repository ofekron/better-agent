"""Dedicated authoritative owner for `surface_contract/identity.py` (ADR 0006 §0/§2).

The module is the closed identity/revision/read-result foundation of the Chat
Surface Contract family, reused verbatim by ADRs 0007-0009: ten str aliases,
CONTRACT_VERSION, seven frozen+slots dataclasses (one Generic), a typed
ProjectionResult union, a Focus StrEnum, a Subscription Protocol, and the Emit
callable alias. Importing the module executes every definition (so incidental
line coverage reads ~100%), but nothing asserts the contract. This owner locks
every invariant so a dropped alias, a drifted union member, a lost frozen/slots
guarantee, or a changed protocol method set is caught.

Run: ./scripts/run-backend-tests.sh -- --cov=backend.surface_contract.identity
    --cov-branch scripts/test_surface_contract_identity_unit.py
"""

from __future__ import annotations

import collections.abc
import dataclasses
import inspect
import sys
import typing
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(BACKEND.parent))

import _test_home  # noqa: E402

_test_home.isolate("bc-test-surface-contract-identity-unit-")

import backend.surface_contract.identity as ident  # noqa: E402
from backend.surface_contract.identity import (  # noqa: E402
    CONTRACT_VERSION,
    Emit,
    Focus,
    IntentId,
    NodeId,
    Ok,
    PageCursor,
    ProjectRef,
    ProjectionResult,
    ProviderId,
    Rebuilding,
    RunRef,
    SessionId,
    SessionSelectors,
    SidecarRef,
    SnapshotIdentity,
    StaleCursor,
    Subscription,
    SurfaceCursor,
    SurfaceId,
    TurnId,
    ApprovalRef,
)

# class, ordered field names, fields that carry a default
_DC_SPECS = [
    (SnapshotIdentity, ("incarnation", "render_rev", "hist_rev"), frozenset()),
    (SurfaceCursor, ("surface_id", "incarnation", "render_rev"), frozenset()),
    (PageCursor, ("surface_id", "snapshot", "token"), frozenset()),
    (
        SessionSelectors,
        (
            "provider_id",
            "runtime_profile_id",
            "model",
            "reasoning_effort",
            "orchestration_mode",
            "cwd",
        ),
        frozenset(),
    ),
    (Ok, ("value", "snapshot"), frozenset()),
    (Rebuilding, ("retry_after_ms",), frozenset({"retry_after_ms"})),
    (StaleCursor, (), frozenset()),
]

_STR_ALIASES = [
    "SessionId",
    "SurfaceId",
    "NodeId",
    "TurnId",
    "RunRef",
    "SidecarRef",
    "ApprovalRef",
    "ProviderId",
    "ProjectRef",
    "IntentId",
]


def _snap() -> SnapshotIdentity:
    return SnapshotIdentity(incarnation="inc-1", render_rev=3, hist_rev=5)


# One valid instance per dataclass; frozen, so safe to share across tests.
_SAMPLE_INSTANCES = [
    SnapshotIdentity(incarnation="inc-1", render_rev=3, hist_rev=5),
    SurfaceCursor(surface_id="surf", incarnation="inc-1", render_rev=3),
    PageCursor(surface_id="surf", snapshot=_snap(), token="tok"),
    SessionSelectors(None, None, None, None, None, None),
    Ok(value=0, snapshot=_snap()),
    Rebuilding(),
    StaleCursor(),
]


def test_contract_version_is_pinned_to_one():
    # Bumping the contract version is a breaking change for every surface family.
    assert CONTRACT_VERSION == 1


@pytest.mark.parametrize("name", _STR_ALIASES)
def test_str_aliases_are_str_identity(name):
    # Every identity alias IS str (not a subclass), so they interoperate freely.
    assert getattr(ident, name) is str


@pytest.mark.parametrize("cls,fields,defaults", _DC_SPECS)
def test_dataclass_field_contract(cls, fields, defaults):
    actual = tuple(f.name for f in dataclasses.fields(cls))
    assert actual == fields

    required = [f.name for f in dataclasses.fields(cls) if f.default is dataclasses.MISSING]
    assert set(required) == set(fields) - defaults


@pytest.mark.parametrize("cls,fields,defaults", _DC_SPECS)
def test_dataclasses_are_frozen_and_slots(cls, fields, defaults):
    assert cls.__dataclass_params__.frozen is True
    assert tuple(cls.__slots__) == fields


def test_session_selectors_fields_are_optional_typed_but_required_positional():
    hints = typing.get_type_hints(SessionSelectors)
    # All six selectors are typed `X | None` (nullable) yet carry NO default, so
    # the caller must name every selector explicitly (None is a real value).
    for field_name in ("provider_id", "runtime_profile_id", "model", "reasoning_effort", "orchestration_mode", "cwd"):
        assert typing.get_args(hints[field_name]) == (str, type(None))
    assert dataclasses.fields(SessionSelectors)  # exactly six, asserted above; required
    with pytest.raises(TypeError):
        SessionSelectors()


def test_ok_is_generic_over_single_typevar():
    params = Ok.__parameters__
    assert len(params) == 1
    parameterized = Ok[int]
    assert typing.get_origin(parameterized) is Ok


def test_rebuilding_retry_after_ms_defaults_to_none():
    assert Rebuilding().retry_after_ms is None
    assert Rebuilding(retry_after_ms=500).retry_after_ms == 500


def test_stale_cursor_takes_no_args():
    # The "cursor is stale, refetch" sentinel — no state.
    assert dataclasses.fields(StaleCursor) == ()
    StaleCursor()


def test_projection_result_union_membership():
    args = typing.get_args(ProjectionResult)
    origins = {typing.get_origin(a) or a for a in args}
    assert origins == {Ok, Rebuilding, StaleCursor}


def test_projection_result_members_are_distinct_types():
    args = typing.get_args(ProjectionResult)
    assert len(set(id(typing.get_origin(a) or a) for a in args)) == 3


def test_focus_enum_exact_membership_and_values():
    assert set(Focus) == {Focus.OPENED, Focus.WARM}
    assert Focus.OPENED.value == "opened"
    assert Focus.WARM.value == "warm"


def test_focus_is_strenum_str_identity():
    # StrEnum members ARE their value as str.
    assert Focus("opened") is Focus.OPENED
    assert Focus.OPENED == "opened"


def test_subscription_protocol_contract():
    # Subscription is a plain (non-runtime-checkable) Protocol: a structural
    # type whose sole member is close() -> None.
    assert typing.is_protocol(Subscription)
    assert set(typing.get_protocol_members(Subscription)) == {"close"}
    assert typing.get_type_hints(Subscription.close) == {"return": type(None)}
    assert list(inspect.signature(Subscription.close).parameters) == ["self"]
    assert not getattr(Subscription, "_is_runtime_protocol", False)


def test_emit_is_callable_from_object_to_none():
    # Emit = Callable[[object], None]: a single-object sink returning nothing.
    assert typing.get_origin(Emit) is collections.abc.Callable
    ga = typing.get_args(Emit)
    assert ga[-1] is type(None)  # returns None


def test_snapshot_identity_construction_and_immutability():
    snap = SnapshotIdentity(incarnation="inc-1", render_rev=3, hist_rev=5)
    assert snap.incarnation == "inc-1"
    assert snap.render_rev == 3
    assert snap.hist_rev == 5
    assert snap == SnapshotIdentity(incarnation="inc-1", render_rev=3, hist_rev=5)


def test_surface_cursor_construction():
    cur = SurfaceCursor(surface_id="surf", incarnation="inc-1", render_rev=3)
    assert cur.surface_id == "surf"
    assert cur.incarnation == "inc-1"
    assert cur.render_rev == 3


def test_page_cursor_embeds_snapshot_identity():
    snap = SnapshotIdentity(incarnation="inc-1", render_rev=3, hist_rev=5)
    page = PageCursor(surface_id="surf", snapshot=snap, token="tok")
    assert page.snapshot is snap
    assert page.token == "tok"


def test_ok_construction_carries_snapshot():
    snap = SnapshotIdentity(incarnation="inc-1", render_rev=3, hist_rev=5)
    ok = Ok(value={"k": 1}, snapshot=snap)
    assert ok.value == {"k": 1}
    assert ok.snapshot is snap


@pytest.mark.parametrize("instance", _SAMPLE_INSTANCES)
def test_frozen_dataclasses_reject_mutation(instance):
    # Frozen dataclasses raise on attribute assignment. StaleCursor has no
    # render_rev field; the assignment is still rejected before the field check.
    with pytest.raises(dataclasses.FrozenInstanceError):
        instance.render_rev = 999  # type: ignore[attr-defined]


@pytest.mark.parametrize("instance", _SAMPLE_INSTANCES)
def test_slots_dataclasses_have_no_instance_dict(instance):
    assert not hasattr(instance, "__dict__")
