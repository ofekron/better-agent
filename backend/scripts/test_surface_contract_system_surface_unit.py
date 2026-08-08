"""Dedicated authoritative owner for the SystemSurface ABC contract.

Locks ``system_surface.SystemSurface`` (ADR 0011) — 15 abstract methods —
plus the module's own dataclass/enum family and the ``SystemFrame`` union.
The sibling ``test_surface_contract_abc_surfaces_unit.py`` locks the other
four surface seams and references SystemSurface only as a
``BetterAgentAdapter`` composition member; behavior is covered by
``test_adapter_system.py``. This owner locks the SHAPE: the EXACT
abstract-method set, each method's signature (param names/order,
per-parameter default status, return declared), that a concrete subclass
instantiates and a partial one cannot, every dataclass's field order and
default set, frozen+slots guarantees, exact enum memberships, and exact
``SystemFrame`` union membership — so a dropped method, a renamed
parameter, a lost frozen/slots guarantee, or a drifted union member is
caught.

Run: ./scripts/run-backend-tests.sh --
    --cov=backend.surface_contract.system_surface
    --cov-branch scripts/test_surface_contract_system_surface_unit.py
"""

from __future__ import annotations

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

_test_home.isolate("bc-test-surface-contract-system-surface-unit-")

from backend.surface_contract.system_surface import (  # noqa: E402
    ExtensionCatalogEntry,
    ExtensionCatalogPage,
    ExtensionCatalogUpsert,
    ExtensionConfigDescriptor,
    ExtensionConfigPage,
    ExtensionConfigSections,
    ExtensionConfigUpsert,
    ExtensionSignal,
    ExtensionToast,
    ExtensionToastLevel,
    ExtensionUiChanged,
    ExtensionUiDisplay,
    ExtensionUiModule,
    ExtensionUiModuleKind,
    ExtensionUiPage,
    HarnessDefaultChanged,
    HarnessProfileDescriptor,
    HarnessProfilePage,
    HarnessProfileUpsert,
    HookAction,
    HookActionKind,
    HostStartupTask,
    HostStartupTaskCleared,
    HostStartupTaskSnapshot,
    HostStartupTaskState,
    HostStartupTaskUpsert,
    InstallationCapability,
    InstallationCapabilityChanged,
    MachineNodeUpsert,
    MachinePage,
    MarketplaceBridgeState,
    MarketplaceBridgeStateChanged,
    MarketplaceConnectionState,
    MarketplaceExtensionRef,
    MarketplaceIntent,
    MarketplaceIntentPage,
    MarketplaceIntentUpsert,
    NodeConnState,
    NodeDescriptor,
    NodeProviderCredentialState,
    NodeProviderCredentialStatus,
    NodeProviderCredentialUpsert,
    NodeRegistrationDecision,
    NodeRegistrationPage,
    NodeRegistrationRequest,
    NodeRegistrationRequestPayload,
    NodeRegistrationResponse,
    NodeRegistrationState,
    NodeRegistrationUpsert,
    NodeRemoved,
    NodeRole,
    NodeVersionStatus,
    PairedDevice,
    ScheduleRemoved,
    SchedulePage,
    ScheduleSummary,
    ScheduleUpsert,
    SystemFrame,
    SystemSurface,
)

# --------------------------------------------------------------------------- #
# StrEnum specs — name -> {member: value}.
# --------------------------------------------------------------------------- #
_ENUM_SPECS = [
    (
        ExtensionToastLevel,
        {"INFO": "info", "WARNING": "warning", "ERROR": "error"},
    ),
    (
        ExtensionUiModuleKind,
        {
            "QUICK_BUTTON": "quick_button",
            "PAGE": "page",
            "FRONTEND_MODULE": "frontend_module",
        },
    ),
    (
        HookActionKind,
        {"NAVIGATE": "navigate", "ENSURE": "ensure", "MODULE": "module"},
    ),
    (
        MarketplaceConnectionState,
        {
            "UNPAIRED": "unpaired",
            "CONNECTING": "connecting",
            "CONNECTED": "connected",
            "OFFLINE": "offline",
        },
    ),
    (
        HostStartupTaskState,
        {"RUNNING": "running", "DONE": "done", "FAILED": "failed"},
    ),
    (
        NodeRole,
        {"PRIMARY": "primary", "WORKER_NODE": "worker_node"},
    ),
    (
        NodeConnState,
        {
            "CONNECTED": "connected",
            "DISCONNECTED": "disconnected",
            "UNKNOWN": "unknown",
        },
    ),
    (
        NodeVersionStatus,
        {"OK": "ok", "MISMATCH": "mismatch", "UNKNOWN": "unknown"},
    ),
    (
        NodeProviderCredentialState,
        {"PENDING": "pending", "SYNCED": "synced", "FAILED": "failed"},
    ),
    (
        NodeRegistrationState,
        {"PENDING": "pending", "RESOLVED": "resolved", "CANCELLED": "cancelled"},
    ),
    (
        NodeRegistrationDecision,
        {"APPROVED": "approved", "DENIED": "denied"},
    ),
]


# --------------------------------------------------------------------------- #
# Dataclass specs — (class, ordered field names, fields that carry a default).
# --------------------------------------------------------------------------- #
_DC_SPECS = [
    # §1 extension notices
    (
        ExtensionToast,
        ("extension_id", "level", "message", "session_id"),
        frozenset({"session_id"}),
    ),
    (ExtensionSignal, ("extension_id", "event_name", "data"), frozenset()),
    # §2 extension config + harness profiles
    (
        ExtensionConfigSections,
        (
            "settings",
            "instructions",
            "ui_settings",
            "internal_llm",
            "permissions",
            "mcp",
            "skills",
        ),
        frozenset(
            {
                "settings",
                "instructions",
                "ui_settings",
                "internal_llm",
                "permissions",
                "mcp",
                "skills",
            }
        ),
    ),
    (ExtensionConfigDescriptor, ("extension_id", "cv", "sections"), frozenset()),
    (ExtensionConfigPage, ("descriptors", "next_cursor"), frozenset()),
    (ExtensionConfigUpsert, ("cv", "extension_id", "section"), frozenset()),
    (
        HarnessProfileDescriptor,
        (
            "harness_profile_id",
            "cv",
            "display",
            "is_default",
            "disabled_builtin_extensions",
            "disabled_builtin_tools",
            "config_schema",
        ),
        frozenset({"config_schema"}),
    ),
    (HarnessProfilePage, ("profiles", "next_cursor"), frozenset()),
    (HarnessProfileUpsert, ("cv", "profile", "intent_id"), frozenset({"intent_id"})),
    (HarnessDefaultChanged, ("cv", "harness_profile_id"), frozenset()),
    # §3 extension UI modules
    (
        HookAction,
        (
            "kind",
            "path",
            "endpoint",
            "path_template",
            "id_field",
            "include_cwd",
            "module_url",
        ),
        frozenset(
            {"path", "endpoint", "path_template", "id_field", "include_cwd", "module_url"}
        ),
    ),
    (ExtensionUiDisplay, ("label", "icon"), frozenset({"icon"})),
    (
        ExtensionUiModule,
        (
            "extension_id",
            "cv",
            "kind",
            "display",
            "action",
            "placements",
            "badge_count",
            "slot",
            "module_url",
            "payments",
            "marketplace_auth",
        ),
        frozenset(
            {
                "action",
                "placements",
                "badge_count",
                "slot",
                "module_url",
                "payments",
                "marketplace_auth",
            }
        ),
    ),
    (ExtensionUiPage, ("modules", "next_cursor"), frozenset()),
    (ExtensionUiChanged, ("cv",), frozenset()),
    # §4 extension catalog
    (
        ExtensionCatalogEntry,
        (
            "extension_id",
            "cv",
            "display",
            "installed_version",
            "available_version",
            "update_available",
            "enabled",
            "source",
        ),
        frozenset(),
    ),
    (ExtensionCatalogPage, ("entries", "next_cursor"), frozenset()),
    (ExtensionCatalogUpsert, ("cv", "entry"), frozenset()),
    # §5 marketplace bridge
    (PairedDevice, ("device_ref", "label"), frozenset()),
    (
        MarketplaceBridgeState,
        ("cv", "connection_state", "revocation_pending", "paired_devices"),
        frozenset(),
    ),
    (MarketplaceBridgeStateChanged, ("cv", "state"), frozenset()),
    (
        MarketplaceExtensionRef,
        ("id", "name", "version", "publisher", "permission_delta"),
        frozenset({"name", "version", "publisher", "permission_delta"}),
    ),
    (
        MarketplaceIntent,
        (
            "intent_id",
            "cv",
            "action",
            "status",
            "extension",
            "account_label",
            "site_label",
            "device_label",
            "error",
        ),
        frozenset(
            {"extension", "account_label", "site_label", "device_label", "error"}
        ),
    ),
    (MarketplaceIntentPage, ("intents", "next_cursor"), frozenset()),
    (MarketplaceIntentUpsert, ("cv", "intent"), frozenset()),
    # §6 schedules
    (
        ScheduleSummary,
        (
            "schedule_id",
            "cv",
            "session_id",
            "prompt_preview",
            "cadence",
            "next_run_at",
            "last_run_at",
            "enabled",
        ),
        frozenset(),
    ),
    (SchedulePage, ("schedules", "next_cursor"), frozenset()),
    (ScheduleUpsert, ("cv", "schedule"), frozenset()),
    (ScheduleRemoved, ("cv", "schedule_id"), frozenset()),
    # §7 host startup tasks
    (
        HostStartupTask,
        ("id", "cv", "label", "state", "started_at", "finished_at"),
        frozenset({"finished_at"}),
    ),
    (HostStartupTaskSnapshot, ("tasks", "epoch"), frozenset()),
    (HostStartupTaskUpsert, ("cv", "task"), frozenset()),
    (HostStartupTaskCleared, ("cv", "epoch"), frozenset()),
    # §8 installation capabilities
    (
        InstallationCapability,
        (
            "capability_id",
            "cv",
            "enabled",
            "display",
            "provisioned",
            "active",
            "restart_required",
            "self_provisionable",
            "in_app_restart_supported",
        ),
        frozenset(
            {
                "provisioned",
                "active",
                "restart_required",
                "self_provisionable",
                "in_app_restart_supported",
            }
        ),
    ),
    (InstallationCapabilityChanged, ("cv", "capability"), frozenset()),
    # §9 machine/node topology + credential sync + node registration
    (
        NodeDescriptor,
        (
            "node_id",
            "cv",
            "role",
            "address",
            "cwd_roots",
            "state",
            "last_seen",
            "connected_at",
            "version_status",
            "app_commit_sha",
            "app_dirty",
            "primary_commit_sha",
            "primary_dirty",
        ),
        frozenset(
            {"app_commit_sha", "app_dirty", "primary_commit_sha", "primary_dirty"}
        ),
    ),
    (MachinePage, ("nodes", "next_cursor"), frozenset()),
    (MachineNodeUpsert, ("cv", "node"), frozenset()),
    (NodeRemoved, ("cv", "node_id"), frozenset()),
    (
        NodeProviderCredentialStatus,
        (
            "node_id",
            "provider_id",
            "cv",
            "status",
            "authorized_at",
            "updated_at",
            "failure_code",
        ),
        frozenset({"failure_code"}),
    ),
    (NodeProviderCredentialUpsert, ("cv", "status"), frozenset()),
    (
        NodeRegistrationRequestPayload,
        ("address", "cwd_roots", "fingerprint"),
        frozenset(),
    ),
    (NodeRegistrationResponse, ("decision",), frozenset()),
    (
        NodeRegistrationRequest,
        ("node_id", "cv", "request", "state", "response"),
        frozenset({"response"}),
    ),
    (NodeRegistrationPage, ("requests", "next_cursor"), frozenset()),
    (NodeRegistrationUpsert, ("cv", "node_id", "request"), frozenset()),
]


# --------------------------------------------------------------------------- #
# ABC spec — exact abstract-method name set, {method: param names},
# {method: frozenset of param names that carry a default}. Param names
# EXCLUDE ``self``; every SystemSurface method param is required.
# --------------------------------------------------------------------------- #
_ABC_METHODS = frozenset(
    {
        "extension_config",
        "list_extension_configs",
        "list_harness_profiles",
        "list_extension_ui",
        "list_extension_catalog",
        "marketplace_bridge_state",
        "list_marketplace_intents",
        "list_schedules",
        "host_startup_tasks",
        "list_installation_capabilities",
        "list_machines",
        "node_provider_credentials",
        "list_node_registrations",
        "subscribe",
        "submit",
    }
)

_ABC_PARAMS = {
    "extension_config": ("extension_id",),
    "list_extension_configs": ("cursor",),
    "list_harness_profiles": ("cursor",),
    "list_extension_ui": ("cursor",),
    "list_extension_catalog": ("cursor",),
    "marketplace_bridge_state": (),
    "list_marketplace_intents": ("cursor",),
    "list_schedules": ("session_id", "cursor"),
    "host_startup_tasks": (),
    "list_installation_capabilities": (),
    "list_machines": ("cursor",),
    "node_provider_credentials": ("node_id",),
    "list_node_registrations": ("cursor",),
    "subscribe": ("emit",),
    "submit": ("intent",),
}


def _concrete_subclass(abc_cls: type, *, drop: frozenset[str] = frozenset()) -> type:
    """Build a concrete subclass implementing every abstract method except ``drop``."""

    implemented = abc_cls.__abstractmethods__ - drop
    body = {name: lambda self, *args, **kwargs: None for name in implemented}
    return type("Concrete", (abc_cls,), body)


# --------------------------------------------------------------------------- #
# Enum contract.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("cls,mapping", _ENUM_SPECS)
def test_enum_exact_membership_and_values(cls, mapping):
    assert {m.name: m.value for m in cls} == mapping
    assert set(cls) == {cls[name] for name in mapping}


@pytest.mark.parametrize("cls,mapping", _ENUM_SPECS)
def test_enums_are_strenum_str_identity(cls, mapping):
    # StrEnum members ARE their value as str; value lookup returns the member.
    for name, value in mapping.items():
        member = cls[name]
        assert member == value
        assert cls(value) is member


# --------------------------------------------------------------------------- #
# Dataclass contract.
# --------------------------------------------------------------------------- #
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


def test_extension_toast_session_id_defaults_none():
    toast = ExtensionToast(extension_id="e1", level=ExtensionToastLevel.INFO, message="m")
    assert toast.session_id is None


def test_extension_config_sections_default_to_all_none():
    # `None` means "nothing declared" — never an empty dict standing in.
    sections = ExtensionConfigSections()
    for f in dataclasses.fields(ExtensionConfigSections):
        assert getattr(sections, f.name) is None


def test_harness_profile_upsert_intent_id_defaults_none():
    profile = object()
    assert HarnessProfileUpsert(cv=1, profile=profile).intent_id is None
    assert HarnessProfileUpsert(cv=1, profile=profile, intent_id="i").intent_id == "i"


def test_hook_action_only_kind_is_required():
    action = HookAction(kind="navigate")
    for f in dataclasses.fields(HookAction):
        if f.name != "kind":
            assert getattr(action, f.name) is None


def test_extension_ui_module_optional_defaults():
    module = ExtensionUiModule(
        extension_id="e1",
        cv=1,
        kind=ExtensionUiModuleKind.PAGE,
        display=ExtensionUiDisplay(label="l"),
    )
    assert module.action is None
    assert module.placements == ()
    assert module.badge_count is None
    assert module.slot is None
    assert module.module_url is None
    assert module.payments is None
    assert module.marketplace_auth is None


def test_marketplace_extension_ref_only_id_is_required():
    ref = MarketplaceExtensionRef(id="x")
    for f in dataclasses.fields(MarketplaceExtensionRef):
        if f.name != "id":
            assert getattr(ref, f.name) is None


def test_host_startup_task_finished_at_defaults_none():
    task = HostStartupTask(
        id="t1", cv=1, label="l", state=HostStartupTaskState.RUNNING, started_at="now"
    )
    assert task.finished_at is None


def test_installation_capability_state_flags_default_false():
    cap = InstallationCapability(capability_id="c1", cv=1, enabled=True, display="d")
    assert cap.provisioned is False
    assert cap.active is False
    assert cap.restart_required is False
    assert cap.self_provisionable is False
    assert cap.in_app_restart_supported is False


def test_node_descriptor_version_fields_default_none():
    node = _sample_node_descriptor()
    assert node.app_commit_sha is None
    assert node.app_dirty is None
    assert node.primary_commit_sha is None
    assert node.primary_dirty is None


def test_node_registration_request_response_defaults_none():
    request = NodeRegistrationRequest(
        node_id="n1",
        cv=1,
        request=NodeRegistrationRequestPayload(address="a", cwd_roots=(), fingerprint="f"),
        state=NodeRegistrationState.PENDING,
    )
    assert request.response is None


# --------------------------------------------------------------------------- #
# Frozen/slots proof across one valid instance per dataclass.
# --------------------------------------------------------------------------- #
def _sample_node_descriptor():
    return NodeDescriptor(
        node_id="n1",
        cv=1,
        role=NodeRole.PRIMARY,
        address="127.0.0.1:1",
        cwd_roots=("/w",),
        state=NodeConnState.CONNECTED,
        last_seen=0.0,
        connected_at=0.0,
        version_status=NodeVersionStatus.OK,
    )


def _sample_instances():
    sections = ExtensionConfigSections(settings={"k": "v"})
    config_descriptor = ExtensionConfigDescriptor(extension_id="e1", cv=1, sections=sections)
    harness_profile = HarnessProfileDescriptor(
        harness_profile_id="h1",
        cv=1,
        display="d",
        is_default=True,
        disabled_builtin_extensions=("x",),
        disabled_builtin_tools=(),
    )
    hook_action = HookAction(kind=HookActionKind.NAVIGATE, path="/p")
    ui_module = ExtensionUiModule(
        extension_id="e1",
        cv=1,
        kind=ExtensionUiModuleKind.QUICK_BUTTON,
        display=ExtensionUiDisplay(label="l", icon="i"),
        action=hook_action,
    )
    catalog_entry = ExtensionCatalogEntry(
        extension_id="e1",
        cv=1,
        display="d",
        installed_version="1.0",
        available_version=None,
        update_available=False,
        enabled=True,
        source="builtin",
    )
    paired_device = PairedDevice(device_ref="d1", label="phone")
    bridge_state = MarketplaceBridgeState(
        cv=1,
        connection_state=MarketplaceConnectionState.CONNECTED,
        revocation_pending=False,
        paired_devices=(paired_device,),
    )
    marketplace_ref = MarketplaceExtensionRef(id="x", name="n")
    marketplace_intent = MarketplaceIntent(
        intent_id="i1", cv=1, action="install", status="pending", extension=marketplace_ref
    )
    schedule = ScheduleSummary(
        schedule_id="sc1",
        cv=1,
        session_id="s1",
        prompt_preview="p",
        cadence="daily",
        next_run_at=None,
        last_run_at=None,
        enabled=True,
    )
    startup_task = HostStartupTask(
        id="t1", cv=1, label="l", state=HostStartupTaskState.DONE, started_at="t0", finished_at="t1"
    )
    capability = InstallationCapability(capability_id="c1", cv=1, enabled=True, display="d")
    node = _sample_node_descriptor()
    credential_status = NodeProviderCredentialStatus(
        node_id="n1",
        provider_id="claude",
        cv=1,
        status=NodeProviderCredentialState.SYNCED,
        authorized_at=None,
        updated_at=None,
    )
    registration_payload = NodeRegistrationRequestPayload(
        address="a", cwd_roots=("/w",), fingerprint="f"
    )
    registration = NodeRegistrationRequest(
        node_id="n1",
        cv=1,
        request=registration_payload,
        state=NodeRegistrationState.RESOLVED,
        response=NodeRegistrationResponse(decision=NodeRegistrationDecision.APPROVED),
    )
    return [
        ExtensionToast(extension_id="e1", level=ExtensionToastLevel.WARNING, message="m"),
        ExtensionSignal(extension_id="e1", event_name="ev", data={}),
        sections,
        config_descriptor,
        ExtensionConfigPage(descriptors=(config_descriptor,), next_cursor=None),
        ExtensionConfigUpsert(cv=1, extension_id="e1", section="settings"),
        harness_profile,
        HarnessProfilePage(profiles=(harness_profile,), next_cursor=None),
        HarnessProfileUpsert(cv=1, profile=harness_profile),
        HarnessDefaultChanged(cv=1, harness_profile_id="h1"),
        hook_action,
        ExtensionUiDisplay(label="l"),
        ui_module,
        ExtensionUiPage(modules=(ui_module,), next_cursor=None),
        ExtensionUiChanged(cv=1),
        catalog_entry,
        ExtensionCatalogPage(entries=(catalog_entry,), next_cursor=None),
        ExtensionCatalogUpsert(cv=1, entry=catalog_entry),
        paired_device,
        bridge_state,
        MarketplaceBridgeStateChanged(cv=1, state=bridge_state),
        marketplace_ref,
        marketplace_intent,
        MarketplaceIntentPage(intents=(marketplace_intent,), next_cursor=None),
        MarketplaceIntentUpsert(cv=1, intent=marketplace_intent),
        schedule,
        SchedulePage(schedules=(schedule,), next_cursor=None),
        ScheduleUpsert(cv=1, schedule=schedule),
        ScheduleRemoved(cv=1, schedule_id="sc1"),
        startup_task,
        HostStartupTaskSnapshot(tasks=(startup_task,), epoch=1),
        HostStartupTaskUpsert(cv=1, task=startup_task),
        HostStartupTaskCleared(cv=1, epoch=1),
        capability,
        InstallationCapabilityChanged(cv=1, capability=capability),
        node,
        MachinePage(nodes=(node,), next_cursor=None),
        MachineNodeUpsert(cv=1, node=node),
        NodeRemoved(cv=1, node_id="n1"),
        credential_status,
        NodeProviderCredentialUpsert(cv=1, status=credential_status),
        registration_payload,
        NodeRegistrationResponse(decision=NodeRegistrationDecision.DENIED),
        registration,
        NodeRegistrationPage(requests=(registration,), next_cursor=None),
        NodeRegistrationUpsert(cv=1, node_id="n1", request=registration),
    ]


def test_sample_instances_cover_every_dataclass_spec():
    covered = {type(instance) for instance in _sample_instances()}
    assert covered == {cls for cls, _, _ in _DC_SPECS}


@pytest.mark.parametrize("instance", _sample_instances())
def test_frozen_dataclasses_reject_mutation(instance):
    with pytest.raises(dataclasses.FrozenInstanceError):
        instance.cv = 999  # type: ignore[attr-defined]


@pytest.mark.parametrize("instance", _sample_instances())
def test_slots_dataclasses_have_no_instance_dict(instance):
    assert not hasattr(instance, "__dict__")


# --------------------------------------------------------------------------- #
# ABC contract: abstract-method set, abstractmethod flags, signatures.
# --------------------------------------------------------------------------- #
def test_abc_is_abstract_with_exact_method_set():
    from abc import ABC

    assert issubclass(SystemSurface, ABC)
    assert SystemSurface.__abstractmethods__ == _ABC_METHODS


def test_each_declared_method_is_abstractmethod():
    for name in _ABC_METHODS:
        fn = inspect.getattr_static(SystemSurface, name)
        assert getattr(fn, "__isabstractmethod__", False) is True


def test_abc_method_signatures():
    assert set(_ABC_PARAMS) == _ABC_METHODS
    for name, expected_params in _ABC_PARAMS.items():
        sig = inspect.signature(inspect.getattr_static(SystemSurface, name))
        actual_params = tuple(p for p in sig.parameters if p != "self")
        assert actual_params == expected_params
        # No SystemSurface method param carries a default.
        for pname in actual_params:
            assert sig.parameters[pname].default is inspect.Parameter.empty
        # Every surface method declares a return type.
        assert sig.return_annotation is not inspect.Signature.empty


def test_concrete_subclass_instantiates():
    # Implementing every abstract method lifts the abstractness gate.
    _concrete_subclass(SystemSurface)()


@pytest.mark.parametrize("missing", sorted(_ABC_METHODS))
def test_partial_subclass_rejects_instantiation(missing):
    # Dropping exactly one method keeps the gate; the missing name is reported.
    partial = _concrete_subclass(SystemSurface, drop=frozenset({missing}))
    with pytest.raises(TypeError) as exc_info:
        partial()
    assert missing in str(exc_info.value)


def test_abc_itself_is_not_instantiable():
    with pytest.raises(TypeError):
        SystemSurface()  # type: ignore[abstract]


# --------------------------------------------------------------------------- #
# Union contract.
# --------------------------------------------------------------------------- #
_SYSTEM_FRAME_MEMBERS = {
    ExtensionToast,
    ExtensionSignal,
    ExtensionConfigUpsert,
    HarnessProfileUpsert,
    HarnessDefaultChanged,
    ExtensionUiChanged,
    ExtensionCatalogUpsert,
    MarketplaceBridgeStateChanged,
    MarketplaceIntentUpsert,
    ScheduleUpsert,
    ScheduleRemoved,
    HostStartupTaskUpsert,
    HostStartupTaskCleared,
    InstallationCapabilityChanged,
    MachineNodeUpsert,
    NodeRemoved,
    NodeProviderCredentialUpsert,
    NodeRegistrationUpsert,
}


def test_system_frame_union_exact_membership_and_distinct():
    args = typing.get_args(SystemFrame)
    origins = {typing.get_origin(a) or a for a in args}
    assert origins == _SYSTEM_FRAME_MEMBERS
    # Pairwise-distinct member types.
    assert len({id(typing.get_origin(a) or a) for a in args}) == len(_SYSTEM_FRAME_MEMBERS)


def test_system_frame_members_are_locked_frozen_slots_dataclasses():
    # Every frame the surface can push is covered by the dataclass specs above.
    locked = {cls for cls, _, _ in _DC_SPECS}
    assert _SYSTEM_FRAME_MEMBERS <= locked
