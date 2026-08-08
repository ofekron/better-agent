"""Dedicated authoritative owner for the surface-contract ABC family.

Locks the four abstract surface seams (ADRs 0006-0009) and the composed
adapter:

* ``chat_surface.ChatSurface``      (ADR 0006) — 8 abstract methods
* ``runs_surface.RunsSurface``      (ADR 0009) — 4 abstract methods
* ``session_surface.SessionSurface``(ADR 0008) — 7 abstract methods
* ``provider_config_surface.ProviderConfigSurface`` (ADR 0007) — 6 abstract methods
* ``adapter.BetterAgentAdapter``    — composition of five surfaces (chat,
  providers, sessions, runs, system); this file locks the ABC shape of the
  first four above plus the adapter's own field/slot contract, including its
  ``system`` seam (``system_surface.SystemSurface``, ADR 0011) as a
  composition member — that surface's own ABC shape is locked by
  ``test_surface_contract_system_surface_unit.py`` and behaviorally covered
  by ``test_adapter_system.py``.

Importing the package executes every definition (so incidental line coverage
reads ~100% via the package ``__init__`` transitive import), but nothing
asserts the contract. This owner locks: each ABC's EXACT abstract-method set,
each method's signature (param names/order, per-parameter default status,
return declared), that a concrete subclass instantiates and a partial one
cannot, plus the dataclass/enum/union invariants the surfaces carry — so a
dropped method, a renamed parameter, a lost frozen/slots guarantee, or a
drifted union member is caught.

Run: ./scripts/run-backend-tests.sh --
    --cov=backend.surface_contract.chat_surface
    --cov=backend.surface_contract.runs_surface
    --cov=backend.surface_contract.session_surface
    --cov=backend.surface_contract.provider_config_surface
    --cov=backend.surface_contract.adapter
    --cov-branch scripts/test_surface_contract_abc_surfaces_unit.py
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

_test_home.isolate("bc-test-surface-contract-abc-surfaces-unit-")

from backend.surface_contract import adapter as adapter_mod  # noqa: E402
from backend.surface_contract import chat_surface as chat_mod  # noqa: E402
from backend.surface_contract import runs_surface as runs_mod  # noqa: E402
from backend.surface_contract import session_surface as session_mod  # noqa: E402
from backend.surface_contract.adapter import BetterAgentAdapter  # noqa: E402
from backend.surface_contract.chat_surface import (  # noqa: E402
    ChatSurface,
    CompactSessionSnapshot,
    CompactTurn,
    OlderPage,
    SearchMatch,
)
from backend.surface_contract.identity import CONTRACT_VERSION, SessionSelectors  # noqa: E402
from backend.surface_contract.provider_config_surface import (  # noqa: E402
    ProviderConfigSurface,
)
from backend.surface_contract.runs_surface import (  # noqa: E402
    DetailValueKind,
    RunDetail,
    RunDetailEntry,
    RunKind,
    RunPage,
    RunPhase,
    RunSummary,
    RunSummaryUpsert,
    RunsFrame,
    RunsSurface,
    StartupProgress,
    UsageMeasureState,
    UsagePage,
    UsageRow,
)
from backend.surface_contract.session_surface import (  # noqa: E402
    AttentionMarkerDetail,
    FolderRemoved,
    FolderUpsert,
    Project,
    ProjectRemoved,
    ProjectUpsert,
    SessionFolder,
    SessionFrame,
    SessionPage,
    SessionRemoved,
    SessionRollupState,
    SessionSearchFilters,
    SessionSummary,
    SessionSummaryUpsert,
    SessionSurface,
    SessionTag,
    SessionTreeChanged,
    SessionTreeNode,
    TagAssignment,
    TagRemoved,
    TagUpsert,
)
from backend.surface_contract.system_surface import SystemSurface  # noqa: E402

# --------------------------------------------------------------------------- #
# StrEnum specs — name -> {member: value}.
# --------------------------------------------------------------------------- #
_ENUM_SPECS = [
    (
        RunPhase,
        {
            "STARTING": "starting",
            "RUNNING": "running",
            "RECONNECTING": "reconnecting",
            "STALLED": "stalled",
            "COMPLETING": "completing",
            "COMPLETED": "completed",
            "FAILED": "failed",
            "UNKNOWN_AFTER_RECOVERY": "unknown_after_recovery",
        },
    ),
    (
        DetailValueKind,
        {
            "TEXT": "text",
            "DURATION": "duration",
            "COUNT": "count",
            "REF": "ref",
            "PROCESS_TREE": "process_tree",
        },
    ),
    (
        RunKind,
        {"MANAGER": "manager", "NATIVE": "native", "WORKER": "worker"},
    ),
    (
        UsageMeasureState,
        {"REPORTED": "reported", "UNREPORTED": "unreported"},
    ),
    (
        SessionRollupState,
        {
            "IDLE": "idle",
            "QUEUED": "queued",
            "RUNNING": "running",
            "AWAITING_APPROVAL": "awaiting_approval",
            "RECONNECTING": "reconnecting",
            "FAILED": "failed",
            "DETACHED": "detached",
        },
    ),
]


# --------------------------------------------------------------------------- #
# Dataclass specs — (class, ordered field names, fields that carry a default).
# --------------------------------------------------------------------------- #
_DC_SPECS = [
    # chat_surface
    (
        CompactTurn,
        ("turn", "prompt", "results", "manifest", "runtime_change"),
        frozenset({"runtime_change"}),
    ),
    (
        CompactSessionSnapshot,
        (
            "session_id",
            "surface_id",
            "instruction_widget",
            "turns",
            "live_turn_nodes",
            "runs",
            "older_cursor",
            "interactions",
        ),
        frozenset({"interactions"}),
    ),
    (OlderPage, ("turns", "runs", "older_cursor"), frozenset()),
    (SearchMatch, ("turn_id", "node_id", "path"), frozenset()),
    # runs_surface
    (StartupProgress, ("phase", "progress"), frozenset()),
    (
        RunSummary,
        (
            "run_id",
            "session_id",
            "turn_id",
            "provider_id",
            "runner",
            "phase",
            "started_at",
            "last_heartbeat_at",
            "startup",
            "kind",
            "target_message_id",
            "delegation_id",
            "pid",
            "stalled_at",
            "recovered_at_startup",
        ),
        frozenset(),
    ),
    (RunDetailEntry, ("key", "value_kind", "value"), frozenset()),
    (RunDetail, ("summary", "entries"), frozenset()),
    (
        UsageRow,
        ("provider_id", "model", "period", "state", "tokens", "turns", "cost"),
        frozenset(),
    ),
    (UsagePage, ("rows", "next_cursor"), frozenset()),
    (RunSummaryUpsert, ("cv", "summary"), frozenset()),
    (RunPage, ("runs", "next_cursor"), frozenset()),
    # session_surface
    (
        AttentionMarkerDetail,
        ("extension_id", "tag", "color", "tooltip", "sound", "sound_setting"),
        frozenset(),
    ),
    (TagAssignment, ("tag_ref", "source"), frozenset()),
    (
        SessionSummary,
        (
            "session_id",
            "title",
            "project_ref",
            "selectors",
            "state",
            "attention_markers",
            "opened_at",
            "last_activity_at",
            "archived",
            "folder_ref",
            "tag_refs",
            "unread_count",
            "has_error",
            "monitoring_state",
            "attention_marker_details",
            "pending_interactions",
        ),
        frozenset(),
    ),
    (SessionPage, ("sessions", "next_cursor"), frozenset()),
    (SessionTreeNode, ("surface_id", "parent_surface_id", "kind", "title", "state"), frozenset()),
    (Project, ("project_ref", "name", "order", "session_count"), frozenset()),
    (
        SessionFolder,
        ("folder_ref", "project_ref", "name", "parent_folder_ref", "order"),
        frozenset(),
    ),
    (SessionTag, ("tag_ref", "project_ref", "name", "color"), frozenset()),
    (
        SessionSearchFilters,
        ("folder_ref", "tag_refs", "tag_match"),
        frozenset({"folder_ref", "tag_refs", "tag_match"}),
    ),
    (SessionSummaryUpsert, ("cv", "summary", "intent_id"), frozenset({"intent_id"})),
    (SessionTreeChanged, ("cv", "session_id"), frozenset()),
    (ProjectUpsert, ("cv", "project", "intent_id"), frozenset({"intent_id"})),
    (SessionRemoved, ("cv", "session_id"), frozenset()),
    (ProjectRemoved, ("cv", "project_ref"), frozenset()),
    (FolderUpsert, ("cv", "folder", "intent_id"), frozenset({"intent_id"})),
    (FolderRemoved, ("cv", "folder_ref"), frozenset()),
    (TagUpsert, ("cv", "tag", "intent_id"), frozenset({"intent_id"})),
    (TagRemoved, ("cv", "tag_ref"), frozenset()),
]


# --------------------------------------------------------------------------- #
# ABC specs — (class, exact abstract-method name set, {method: param names},
# {method: frozenset of param names that carry a default}). Param names
# EXCLUDE ``self``; a param is required unless explicitly listed in the
# fourth element for its method. Every surface method is fully required
# except SessionSurface.list_sessions's `filters`.
# --------------------------------------------------------------------------- #
_ABC_SPECS = [
    (
        ChatSurface,
        frozenset(
            {
                "open_session",
                "children",
                "older",
                "search",
                "fetch_sidecar",
                "subscribe",
                "subscribe_control",
                "submit",
            }
        ),
        {
            "open_session": ("session_id",),
            "children": ("surface_id", "node_id", "at_render_rev"),
            "older": ("cursor",),
            "search": ("session_id", "query"),
            "fetch_sidecar": ("session_id", "sidecar_ref"),
            "subscribe": ("cursors", "focus", "emit"),
            "subscribe_control": ("emit",),
            "submit": ("intent",),
        },
        {},
    ),
    (
        RunsSurface,
        frozenset({"list_runs", "run_detail", "usage_analytics", "subscribe"}),
        {
            "list_runs": ("session_id", "cursor"),
            "run_detail": ("run_id",),
            "usage_analytics": ("cursor",),
            "subscribe": ("emit",),
        },
        {},
    ),
    (
        SessionSurface,
        frozenset(
            {
                "list_sessions",
                "session_tree",
                "projects",
                "list_folders",
                "list_tags",
                "subscribe",
                "submit",
            }
        ),
        {
            "list_sessions": ("cursor", "query", "filters"),
            "session_tree": ("session_id",),
            "projects": (),
            "list_folders": ("project_ref",),
            "list_tags": ("project_ref",),
            "subscribe": ("emit",),
            "submit": ("intent",),
        },
        {"list_sessions": frozenset({"filters"})},
    ),
    (
        ProviderConfigSurface,
        frozenset(
            {
                "list_providers",
                "installable_catalog",
                "model_catalog",
                "runtime_profiles",
                "subscribe",
                "submit",
            }
        ),
        {
            "list_providers": (),
            "installable_catalog": (),
            "model_catalog": ("provider_id",),
            "runtime_profiles": (),
            "subscribe": ("emit",),
            "submit": ("intent",),
        },
        {},
    ),
]


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


def test_compact_turn_runtime_change_defaults_none():
    # Constructing without runtime_change leaves it None; only the four core
    # node slots are required.
    turn = object()
    prompt = object()
    ct = CompactTurn(turn=turn, prompt=prompt, results=(), manifest=object())
    assert ct.runtime_change is None
    assert ct.turn is turn


def test_session_summary_upsert_intent_id_defaults_none():
    summary = object()
    assert SessionSummaryUpsert(cv=1, summary=summary).intent_id is None
    assert SessionSummaryUpsert(cv=1, summary=summary, intent_id="i").intent_id == "i"


def test_project_upsert_intent_id_defaults_none():
    project = object()
    assert ProjectUpsert(cv=1, project=project).intent_id is None
    assert ProjectUpsert(cv=1, project=project, intent_id="i").intent_id == "i"


def test_folder_upsert_intent_id_defaults_none():
    folder = object()
    assert FolderUpsert(cv=1, folder=folder).intent_id is None
    assert FolderUpsert(cv=1, folder=folder, intent_id="i").intent_id == "i"


def test_tag_upsert_intent_id_defaults_none():
    tag = object()
    assert TagUpsert(cv=1, tag=tag).intent_id is None
    assert TagUpsert(cv=1, tag=tag, intent_id="i").intent_id == "i"


def test_session_search_filters_defaults():
    filters = SessionSearchFilters()
    assert filters.folder_ref is None
    assert filters.tag_refs == ()
    assert filters.tag_match == "all"
    overridden = SessionSearchFilters(folder_ref="f1", tag_refs=("t1",), tag_match="any")
    assert overridden.folder_ref == "f1"
    assert overridden.tag_refs == ("t1",)
    assert overridden.tag_match == "any"


def test_compact_session_snapshot_interactions_defaults_empty():
    compact_turn = CompactTurn(turn=object(), prompt=object(), results=(), manifest=object())
    snapshot = CompactSessionSnapshot(
        session_id="s1",
        surface_id="sf1",
        instruction_widget=None,
        turns=(compact_turn,),
        live_turn_nodes=(),
        runs=(),
        older_cursor=None,
    )
    assert snapshot.interactions == ()


def test_startup_progress_allows_null_progress():
    sp = StartupProgress(phase="starting", progress=None)
    assert sp.progress is None
    assert StartupProgress(phase="running", progress=0.5).progress == 0.5


def test_run_summary_startup_allows_none():
    rs = RunSummary(
        run_id="r1",
        session_id="s1",
        turn_id=None,
        provider_id="claude",
        runner="native",
        phase=RunPhase.RUNNING,
        started_at=0.0,
        last_heartbeat_at=None,
        startup=None,
        kind=None,
        target_message_id=None,
        delegation_id=None,
        pid=None,
        stalled_at=None,
        recovered_at_startup=False,
    )
    assert rs.phase is RunPhase.RUNNING
    assert rs.startup is None
    assert rs.kind is None


def test_run_summary_upsert_carries_summary():
    upsert = RunSummaryUpsert(cv=3, summary=object())
    assert upsert.cv == 3


# --------------------------------------------------------------------------- #
# Frozen/slots proof across one valid instance per dataclass.
# --------------------------------------------------------------------------- #
def _sample_instances():
    compact_turn = CompactTurn(turn=object(), prompt=object(), results=(), manifest=object())
    snapshot = CompactSessionSnapshot(
        session_id="s1",
        surface_id="sf1",
        instruction_widget=None,
        turns=(compact_turn,),
        live_turn_nodes=(),
        runs=(),
        older_cursor=None,
        interactions=(),
    )
    startup = StartupProgress(phase="starting", progress=0.0)
    run_summary = RunSummary(
        run_id="r1",
        session_id="s1",
        turn_id=None,
        provider_id="claude",
        runner="native",
        phase=RunPhase.STARTING,
        started_at=0.0,
        last_heartbeat_at=None,
        startup=startup,
        kind=RunKind.NATIVE,
        target_message_id=None,
        delegation_id=None,
        pid=None,
        stalled_at=None,
        recovered_at_startup=False,
    )
    detail_entry = RunDetailEntry(key="k", value_kind=DetailValueKind.TEXT, value="v")
    run_detail = RunDetail(summary=run_summary, entries=(detail_entry,))
    usage_row = UsageRow(
        provider_id="claude",
        model="m",
        period="p",
        state=UsageMeasureState.REPORTED,
        tokens=10,
        turns=1,
        cost=0.0,
    )
    usage_page = UsagePage(rows=(usage_row,), next_cursor=None)
    run_page = RunPage(runs=(run_summary,), next_cursor=None)
    marker_detail = AttentionMarkerDetail(
        extension_id="ext1",
        tag="urgent",
        color=None,
        tooltip=None,
        sound=False,
        sound_setting=None,
    )
    tag_assignment = TagAssignment(tag_ref="tag1", source="manual")
    selectors = SessionSelectors(
        provider_id=None,
        runtime_profile_id=None,
        model=None,
        reasoning_effort=None,
        orchestration_mode=None,
        cwd=None,
    )
    session_summary = SessionSummary(
        session_id="s1",
        title="t",
        project_ref=None,
        selectors=selectors,
        state=SessionRollupState.IDLE,
        attention_markers=(),
        opened_at=None,
        last_activity_at=0.0,
        archived=False,
        folder_ref=None,
        tag_refs=(tag_assignment,),
        unread_count=0,
        has_error=False,
        monitoring_state="idle",
        attention_marker_details=(marker_detail,),
        pending_interactions=0,
    )
    project = Project(project_ref=object(), name="n", order=0, session_count=1)
    session_folder = SessionFolder(
        folder_ref="f1", project_ref="p1", name="folder", parent_folder_ref=None, order=0
    )
    session_tag = SessionTag(tag_ref="tag1", project_ref="p1", name="tag", color="#000000")
    search_filters = SessionSearchFilters()
    return [
        compact_turn,
        snapshot,
        OlderPage(turns=(compact_turn,), runs=(), older_cursor=None),
        SearchMatch(turn_id="t", node_id="n", path=("n",)),
        startup,
        run_summary,
        detail_entry,
        run_detail,
        usage_row,
        usage_page,
        RunSummaryUpsert(cv=1, summary=run_summary),
        run_page,
        marker_detail,
        tag_assignment,
        session_summary,
        SessionPage(sessions=(session_summary,), next_cursor=None),
        SessionTreeNode(
            surface_id="sf", parent_surface_id=None, kind="root", title=None, state=SessionRollupState.IDLE
        ),
        project,
        session_folder,
        session_tag,
        search_filters,
        SessionSummaryUpsert(cv=1, summary=session_summary),
        SessionTreeChanged(cv=1, session_id="s1"),
        ProjectUpsert(cv=1, project=project),
        SessionRemoved(cv=1, session_id="s1"),
        ProjectRemoved(cv=1, project_ref="p1"),
        FolderUpsert(cv=1, folder=session_folder),
        FolderRemoved(cv=1, folder_ref="f1"),
        TagUpsert(cv=1, tag=session_tag),
        TagRemoved(cv=1, tag_ref="tag1"),
    ]


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
@pytest.mark.parametrize("cls,methods,params,defaulted_params", _ABC_SPECS)
def test_abc_is_abstract_with_exact_method_set(cls, methods, params, defaulted_params):
    from abc import ABC

    assert issubclass(cls, ABC)
    assert cls.__abstractmethods__ == methods


@pytest.mark.parametrize("cls,methods,params,defaulted_params", _ABC_SPECS)
def test_each_declared_method_is_abstractmethod(cls, methods, params, defaulted_params):
    for name in methods:
        fn = inspect.getattr_static(cls, name)
        assert getattr(fn, "__isabstractmethod__", False) is True


@pytest.mark.parametrize("cls,methods,params,defaulted_params", _ABC_SPECS)
def test_abc_method_signatures(cls, methods, params, defaulted_params):
    for name, expected_params in params.items():
        sig = inspect.signature(inspect.getattr_static(cls, name))
        actual_params = tuple(p for p in sig.parameters if p != "self")
        assert actual_params == expected_params
        # Exactly the declared params for this method carry a default.
        expected_defaulted = defaulted_params.get(name, frozenset())
        for pname in actual_params:
            has_default = sig.parameters[pname].default is not inspect.Parameter.empty
            assert has_default == (pname in expected_defaulted)
        # Every surface method declares a return type.
        assert sig.return_annotation is not inspect.Signature.empty


@pytest.mark.parametrize("cls,methods,params,defaulted_params", _ABC_SPECS)
def test_concrete_subclass_instantiates(cls, methods, params, defaulted_params):
    # Implementing every abstract method lifts the abstractness gate.
    _concrete_subclass(cls)()


@pytest.mark.parametrize("cls,methods,params,defaulted_params", _ABC_SPECS)
def test_partial_subclass_rejects_instantiation(cls, methods, params, defaulted_params):
    # Dropping exactly one method keeps the gate; the missing name is reported.
    missing = next(iter(methods))
    partial = _concrete_subclass(cls, drop=frozenset({missing}))
    with pytest.raises(TypeError) as exc_info:
        partial()
    assert missing in str(exc_info.value)


@pytest.mark.parametrize("cls,methods,params,defaulted_params", _ABC_SPECS)
def test_abc_itself_is_not_instantiable(cls, methods, params, defaulted_params):
    with pytest.raises(TypeError):
        cls()  # type: ignore[abstract]


# --------------------------------------------------------------------------- #
# Union / alias contract.
# --------------------------------------------------------------------------- #
def test_session_frame_union_exact_membership_and_distinct():
    args = typing.get_args(SessionFrame)
    origins = {typing.get_origin(a) or a for a in args}
    assert origins == {
        SessionSummaryUpsert,
        SessionTreeChanged,
        ProjectUpsert,
        SessionRemoved,
        ProjectRemoved,
        FolderUpsert,
        FolderRemoved,
        TagUpsert,
        TagRemoved,
    }
    # Pairwise-distinct member types.
    assert len({id(typing.get_origin(a) or a) for a in args}) == 9


def test_runs_frame_is_run_summary_upsert_alias():
    # RunsFrame is an alias, not a new type — the runs surface pushes a single
    # frame shape.
    assert RunsFrame is RunSummaryUpsert


# --------------------------------------------------------------------------- #
# Adapter composition contract.
# --------------------------------------------------------------------------- #
def test_adapter_field_contract():
    fields = tuple(f.name for f in dataclasses.fields(BetterAgentAdapter))
    assert fields == ("chat", "providers", "sessions", "runs", "system", "contract_version")


def test_adapter_is_frozen_and_slots():
    assert BetterAgentAdapter.__dataclass_params__.frozen is True
    assert tuple(BetterAgentAdapter.__slots__) == (
        "chat",
        "providers",
        "sessions",
        "runs",
        "system",
        "contract_version",
    )


def test_adapter_contract_version_is_init_false_default():
    contract_field = {f.name: f for f in dataclasses.fields(BetterAgentAdapter)}["contract_version"]
    assert contract_field.init is False
    assert contract_field.default == CONTRACT_VERSION


def test_adapter_composes_the_five_surface_seams():
    adapter = BetterAgentAdapter(
        chat=ChatSurface,
        providers=ProviderConfigSurface,
        sessions=SessionSurface,
        runs=RunsSurface,
        system=SystemSurface,
    )
    assert adapter.chat is ChatSurface
    assert adapter.providers is ProviderConfigSurface
    assert adapter.sessions is SessionSurface
    assert adapter.runs is RunsSurface
    assert adapter.system is SystemSurface
    assert adapter.contract_version == CONTRACT_VERSION


def test_adapter_rejects_unknown_kwargs():
    with pytest.raises(TypeError):
        BetterAgentAdapter(  # type: ignore[call-arg]
            chat=ChatSurface,
            providers=ProviderConfigSurface,
            sessions=SessionSurface,
            runs=RunsSurface,
            system=SystemSurface,
            extra=None,
        )


# --------------------------------------------------------------------------- #
# Package re-export contract.
# --------------------------------------------------------------------------- #
def test_package_all_exports_resolve():
    import backend.surface_contract as pkg

    assert pkg.__all__ == [
        "CONTRACT_VERSION",
        "BetterAgentAdapter",
        "ChatSurface",
        "ProviderConfigSurface",
        "RunsSurface",
        "SessionSurface",
        "SystemSurface",
    ]
    for name in pkg.__all__:
        assert getattr(pkg, name) is not None


def test_exported_surfaces_are_abc_seams():
    from abc import ABC

    import backend.surface_contract as pkg

    for name in (
        "ChatSurface",
        "ProviderConfigSurface",
        "RunsSurface",
        "SessionSurface",
        "SystemSurface",
    ):
        assert issubclass(getattr(pkg, name), ABC)


def test_modules_are_distinct_runtime_objects():
    # The surface modules are separate, not aliased onto one another.
    assert chat_mod is not runs_mod is not session_mod
    assert adapter_mod is not chat_mod
