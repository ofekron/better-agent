"""Dedicated authoritative owner for `surface_contract/descriptors.py`.

The module is the capability & display plane (ADR 0006 §6) plus the
provider-config read models (ADR 0007): four StrEnums (auth flow, config
state, login phase, form-field kind), fourteen frozen+slots dataclasses
spanning display/capability descriptors, catalog read models, login-flow
state, and the five control frames, and the five-member ProviderFrame union.
Importing the module executes every definition (so incidental line coverage
reads ~100%), but nothing asserts the contract. This owner locks every
invariant so a dropped enum member, a drifted union member, a lost
frozen/slots guarantee, or a changed field set is caught.

Run: ./scripts/run-backend-tests.sh -- --cov=backend.surface_contract.descriptors
    --cov-branch scripts/test_surface_contract_descriptors_unit.py
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

_test_home.isolate("bc-test-surface-contract-descriptors-unit-")

from backend.surface_contract import descriptors as descr  # noqa: E402
from backend.surface_contract.descriptors import (  # noqa: E402
    AuthFlow,
    Capabilities,
    CatalogModel,
    ConfigState,
    CredentialState,
    Display,
    FormField,
    FormFieldKind,
    InstallableCatalogChanged,
    InstallableDescriptor,
    LoginFlowFrame,
    LoginFlowState,
    LoginPhase,
    ModelCatalog,
    ModelCatalogChanged,
    ProviderDescriptor,
    ProviderFrame,
    ProviderUpsert,
    RuntimeProfile,
)
from backend.surface_contract.identity import ProviderId  # noqa: E402

# StrEnum name -> exact {member: value} mapping. Members ARE their value as str
# (StrEnum identity), asserted separately.
_ENUM_SPECS = [
    (
        AuthFlow,
        {
            "OAUTH_SUBSCRIPTION": "oauth_subscription",
            "API_KEY": "api_key",
            "NONE": "none",
        },
    ),
    (
        ConfigState,
        {
            "ACTIVE": "active",
            "SUSPENDED": "suspended",
            "CREDENTIAL_REQUIRED": "credential_required",
            "CREDENTIAL_FAILED": "credential_failed",
            "RETRYING": "retrying",
        },
    ),
    (
        LoginPhase,
        {
            "STARTING": "starting",
            "AWAITING_BROWSER": "awaiting_browser",
            "AWAITING_CODE": "awaiting_code",
            "POLLING": "polling",
            "COMPLETED": "completed",
            "FAILED": "failed",
            "CANCELLED": "cancelled",
        },
    ),
    (
        FormFieldKind,
        {
            "TEXT": "text",
            "PATH": "path",
            "SECRET": "secret",
            "ENUM": "enum",
            "BOOL": "bool",
        },
    ),
]

# dataclass -> (ordered field names, fields that carry a default)
_DC_SPECS = [
    (
        FormField,
        (
            "name",
            "kind",
            "label_key",
            "required",
            "choices",
            "default",
            "pattern",
            "max_length",
        ),
        frozenset({"required", "choices", "default", "pattern", "max_length"}),
    ),
    (Display, ("label", "icon_id", "config_copy_key"), frozenset()),
    (
        Capabilities,
        (
            "fork",
            "manager_mode",
            "rewind",
            "steering",
            "native_subagents",
            "reasoning_effort",
            "usage_reporting",
            "startup_monitoring",
        ),
        frozenset(),
    ),
    (
        ProviderDescriptor,
        (
            "provider_id",
            "display",
            "auth_flows",
            "capabilities",
            "orchestration_modes",
            "send_modes",
            "model_catalog_ref",
            "config_state",
        ),
        frozenset(),
    ),
    (
        InstallableDescriptor,
        ("kind", "display", "form_schema", "defaults", "auth_flows"),
        frozenset(),
    ),
    (
        CatalogModel,
        ("model", "runner", "reasoning_efforts", "retired"),
        frozenset({"retired"}),
    ),
    (ModelCatalog, ("provider_id", "models"), frozenset()),
    (
        RuntimeProfile,
        (
            "runtime_profile_id",
            "provider_id",
            "runner",
            "default_model",
            "default_reasoning_effort",
        ),
        frozenset(),
    ),
    (
        LoginFlowState,
        ("provider_id", "intent_id", "phase", "data"),
        frozenset({"data"}),
    ),
    (ProviderUpsert, ("cv", "descriptor", "intent_id"), frozenset({"intent_id"})),
    (InstallableCatalogChanged, ("cv",), frozenset()),
    (CredentialState, ("cv", "provider_id", "config_state"), frozenset()),
    (LoginFlowFrame, ("cv", "state"), frozenset()),
    (ModelCatalogChanged, ("cv", "provider_id"), frozenset()),
]


def _capabilities() -> Capabilities:
    return Capabilities(
        fork=True,
        manager_mode=True,
        rewind=True,
        steering=True,
        native_subagents=True,
        reasoning_effort=True,
        usage_reporting=True,
        startup_monitoring=False,
    )


def _display() -> Display:
    return Display(label="Claude", icon_id="claude", config_copy_key="provider.claude.label")


def _descriptor() -> ProviderDescriptor:
    return ProviderDescriptor(
        provider_id="claude",
        display=_display(),
        auth_flows=(AuthFlow.OAUTH_SUBSCRIPTION,),
        capabilities=_capabilities(),
        orchestration_modes=("native", "manager"),
        send_modes=("queue", "interrupt", "steer"),
        model_catalog_ref="catalog://claude",
        config_state=ConfigState.ACTIVE,
    )


def _catalog_model() -> CatalogModel:
    return CatalogModel(model="claude-sonnet-4-5", runner="native", reasoning_efforts=("low", "high"))


def _login_state() -> LoginFlowState:
    return LoginFlowState(provider_id="claude", intent_id="intent-1", phase=LoginPhase.STARTING)


# One valid instance per dataclass; frozen, so safe to share across tests.
_SAMPLE_INSTANCES = [
    FormField(name="api_key", kind=FormFieldKind.SECRET, label_key="field.api_key"),
    _display(),
    _capabilities(),
    _descriptor(),
    InstallableDescriptor(
        kind="claude",
        display=_display(),
        form_schema=(),
        defaults={},
        auth_flows=(AuthFlow.API_KEY,),
    ),
    _catalog_model(),
    ModelCatalog(provider_id="claude", models=(_catalog_model(),)),
    RuntimeProfile(
        runtime_profile_id="rp-1",
        provider_id="claude",
        runner="native",
        default_model="claude-sonnet-4-5",
        default_reasoning_effort=None,
    ),
    _login_state(),
    ProviderUpsert(cv=7, descriptor=_descriptor()),
    InstallableCatalogChanged(cv=7),
    CredentialState(cv=7, provider_id="claude", config_state=ConfigState.ACTIVE),
    LoginFlowFrame(cv=7, state=_login_state()),
    ModelCatalogChanged(cv=7, provider_id="claude"),
]


@pytest.mark.parametrize("cls,mapping", _ENUM_SPECS)
def test_enum_exact_membership_and_values(cls, mapping):
    assert {m.name: m.value for m in cls} == mapping
    assert set(cls) == {cls[name] for name in mapping}


@pytest.mark.parametrize("cls,mapping", _ENUM_SPECS)
def test_enums_are_strenum_str_identity(cls, mapping):
    # StrEnum members ARE their value as str, and value lookup returns the member.
    for name, value in mapping.items():
        member = cls[name]
        assert member == value
        assert cls(value) is member


@pytest.mark.parametrize("cls,fields,defaults", _DC_SPECS)
def test_dataclass_field_contract(cls, fields, defaults):
    actual = tuple(f.name for f in dataclasses.fields(cls))
    assert actual == fields

    defaulted = {
        f.name
        for f in dataclasses.fields(cls)
        if f.default is not dataclasses.MISSING or f.default_factory is not dataclasses.MISSING
    }
    assert defaulted == defaults


@pytest.mark.parametrize("cls,fields,defaults", _DC_SPECS)
def test_dataclasses_are_frozen_and_slots(cls, fields, defaults):
    assert cls.__dataclass_params__.frozen is True
    assert tuple(cls.__slots__) == fields


def test_provider_id_is_reused_from_identity_not_redeclared():
    # descriptors builds on the identity contract; it does not redefine ProviderId.
    from backend.surface_contract import identity

    assert descr.ProviderId is identity.ProviderId is ProviderId


def test_form_field_defaults_round_trip():
    field = FormField(name="api_key", kind=FormFieldKind.SECRET, label_key="field.api_key")
    # Five fields carry defaults; only name/kind/label_key are required.
    assert field.required is False
    assert field.choices == ()
    assert field.default is None
    assert field.pattern is None
    assert field.max_length is None


def test_catalog_model_retired_defaults_false():
    assert CatalogModel(model="m", runner="r", reasoning_efforts=()).retired is False
    assert CatalogModel(model="m", runner="r", reasoning_efforts=(), retired=True).retired is True


def test_login_flow_state_data_defaults_none():
    assert _login_state().data is None
    assert LoginFlowState(
        provider_id="claude", intent_id="i", phase=LoginPhase.POLLING, data={"url": "x"}
    ).data == {"url": "x"}


def test_provider_upsert_intent_id_defaults_none():
    assert ProviderUpsert(cv=1, descriptor=_descriptor()).intent_id is None
    assert ProviderUpsert(cv=1, descriptor=_descriptor(), intent_id="in-1").intent_id == "in-1"


def test_capabilities_are_all_bool_and_required():
    # All eight capability flags are required positional bools.
    assert all(
        typing.get_type_hints(Capabilities)[f.name] is bool
        for f in dataclasses.fields(Capabilities)
    )
    with pytest.raises(TypeError):
        Capabilities()  # type: ignore[call-arg]


def test_display_construction_and_equality():
    d = _display()
    assert d.label == "Claude"
    assert d == Display(label="Claude", icon_id="claude", config_copy_key="provider.claude.label")


def test_provider_descriptor_construction_round_trip():
    desc = _descriptor()
    assert desc.provider_id == "claude"
    assert desc.auth_flows == (AuthFlow.OAUTH_SUBSCRIPTION,)
    assert desc.send_modes == ("queue", "interrupt", "steer")
    assert desc.config_state is ConfigState.ACTIVE
    assert isinstance(desc.display, Display)
    assert isinstance(desc.capabilities, Capabilities)


def test_model_catalog_carries_models_tuple():
    cat = ModelCatalog(provider_id="claude", models=(_catalog_model(),))
    assert cat.models[0].model == "claude-sonnet-4-5"


def test_runtime_profile_default_reasoning_effort_allows_none():
    rp = RuntimeProfile(
        runtime_profile_id="rp",
        provider_id="claude",
        runner="native",
        default_model="m",
        default_reasoning_effort=None,
    )
    assert rp.default_reasoning_effort is None


def test_login_flow_frame_embeds_state():
    frame = LoginFlowFrame(cv=3, state=_login_state())
    assert frame.cv == 3
    assert frame.state.phase is LoginPhase.STARTING


def test_provider_frame_union_exact_membership():
    args = typing.get_args(ProviderFrame)
    origins = {typing.get_origin(a) or a for a in args}
    assert origins == {
        ProviderUpsert,
        InstallableCatalogChanged,
        CredentialState,
        LoginFlowFrame,
        ModelCatalogChanged,
    }


def test_provider_frame_members_are_distinct_types():
    args = typing.get_args(ProviderFrame)
    assert len(set(id(typing.get_origin(a) or a) for a in args)) == 5


@pytest.mark.parametrize("instance", _SAMPLE_INSTANCES)
def test_frozen_dataclasses_reject_mutation(instance):
    with pytest.raises(dataclasses.FrozenInstanceError):
        instance.cv = 999  # type: ignore[attr-defined]


@pytest.mark.parametrize("instance", _SAMPLE_INSTANCES)
def test_slots_dataclasses_have_no_instance_dict(instance):
    assert not hasattr(instance, "__dict__")
