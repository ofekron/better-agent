"""Single read-only façade over backend's persistent stores, for
backend/adapters/* surfaces. Adapters never import session_store /
config_store / project_store / runs_dir directly — only
`backend.adapters.store_access.store_access`. See
backend/scripts/test_adapter_boundaries.py for the enforced import
boundary and this module's per-file store allowlist.

CONSTRAINT: this backend bare-imports its modules (`import session_store`),
while backend/adapters/* imports dotted (`backend.session_store`) per the
adapters boundary. `_resolve` below performs a bare-import-then-alias trick
so a dotted import of a store module always resolves to whatever module
object the process already has under its bare name (in-memory
caches/locks/indexes included) instead of executing the module a second
time under the dotted key — the same problem
`backend/adapters/__init__.py` solves for the shared event_bus/
event_journal/paths/scheme_migrations infra it canonicalizes.

DELIBERATELY KEPT DYNAMIC (not folded into `backend/adapters/__init__.py`'s
static alias list, even though main.py's composition root could now be
extended to do that): store_access is exercised standalone, without ever
going through app boot or backend/adapters/__init__.py's package-import
side effect, by backend/scripts/test_store_access.py and
backend/scripts/test_surface_adapters.py — both import
`backend.adapters.store_access` directly and assert its reads observe the
SAME bare-imported store singleton the test itself seeded through the
store's public write API. A static `from backend import session_store`
here would tie that invariant to import order (whichever module first
imports `backend.session_store` wins the singleton), reintroducing exactly
the ordering hazard `backend/adapters/__init__.py` was written to remove.
Self-contained dynamic resolution keeps store_access correct regardless of
which module imports it first — main.py, a test, or anything else.

This dynamic route is closed everywhere else in backend/adapters/* (see
backend/scripts/test_adapter_boundaries.py's dynamic-import-evasion check)
and is itself statically verifiable here: `_resolve` only ever accepts a
name from `_ALLOWED_STORE_NAMES` (enforced at runtime below), and every
call site below passes a string literal, so the boundary test can extract
those literals via AST and check them against STORE_ACCESS_ALLOWLIST
exactly like a normal import — closing the "AST checker can't see
importlib" gap for this one sanctioned exception.
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Iterable
from dataclasses import dataclass

from backend.surface_contract.nodes import (
    DELEGATE_CHOICE_REF_PREFIX,
    TOOL_APPROVAL_REF_PREFIX,
    USER_INPUT_REF_PREFIX,
    WORKER_APPROVAL_REF_PREFIX,
)

_CAPABILITY_PREFIX = "supports_"

# Mirrors backend/scripts/test_adapter_boundaries.py's STORE_ACCESS_ALLOWLIST
# (bare names, not the dotted "backend.<name>" form that list uses) — fail
# closed at runtime even if the boundary test never ran, matching this
# project's "no fallback on ambiguity" rule.
_ALLOWED_STORE_NAMES = frozenset(
    {
        "session_store", "config_store", "project_store", "runs_dir",
        "worker_store", "llm_call_log", "session_organization_store",
        "tool_approval", "pending_approvals", "session_bridge",
        "user_input_store",
        # ADR 0011 (System & Host Surface) additions — read-only sources
        # for backend/adapters/system_adapter.py. Each is either a
        # top-level legacy module holding durable/in-memory state
        # (extension_store, harness_profile_store, startup_tasks,
        # installation_profile, node_store, node_link) or a deterministic
        # pure-projection module over that state (harness_profile_resolver
        # resolves a stored profile's effective disabled_builtin_* lists
        # the SAME way the legacy REST route does — reused, not
        # reimplemented; node_provider_credential_sync projects
        # `stores.node_provider_credential_grants` records per node).
        "extension_store", "harness_profile_store", "harness_profile_resolver",
        "marketplace_bridge", "schedule_store", "startup_tasks",
        "installation_profile", "node_store", "node_link",
        "node_provider_credential_sync",
        # ADR 0005 (background-work migration) — startup_tasks no longer owns
        # a registry itself; host-startup-task records are now a filtered
        # read of the shared background_work_registry (see
        # list_host_startup_tasks below). "startup_tasks" stays sanctioned
        # too: it's still the single source of truth for the owner_id
        # startup items report under (startup_tasks.OWNER).
        "background_work",
        # Package E (ADR 0009) addition — stateless process-tree inspection
        # for RunsSurfaceAdapter.run_detail()'s on-demand enrichment (see
        # inspect_process_tree above).
        "process_inspect",
        # Closure 3 (ADR 0007 RuntimeProfile v2 parity) — the last-used-
        # model/reasoning-effort prefill store, read-only here (writes stay
        # in runtime_profiles_api.py's record_last_model/
        # record_last_reasoning_effort, outside the adapters boundary).
        "user_prefs",
    }
)

# worker_store/pending_approvals/schedule_store are the sanctioned stores
# that aren't a top-level backend/*.py module — they live under the
# `stores` subpackage — so their bare literal names (matching every other
# entry's convention and STORE_ACCESS_ALLOWLIST's rsplit-derived short
# name) need an explicit import-path override rather than importing
# themselves literally.
_STORE_IMPORT_PATHS = {
    "worker_store": "stores.worker_store",
    "pending_approvals": "stores.pending_approvals",
    "schedule_store": "stores.schedule_store",
}


def _resolve(name: str):
    if name not in _ALLOWED_STORE_NAMES:
        raise AssertionError(
            f"store_access._resolve: {name!r} is not a sanctioned store module "
            f"(allowed: {sorted(_ALLOWED_STORE_NAMES)})",
        )
    dotted = f"backend.{name}"
    mod = sys.modules.get(dotted)
    if mod is not None:
        return mod
    import_path = _STORE_IMPORT_PATHS.get(name, name)
    mod = sys.modules.get(import_path) or importlib.import_module(import_path)
    sys.modules[dotted] = mod
    return mod


@dataclass(frozen=True, slots=True)
class AttentionMarkerDetailRecord:
    extension_id: str
    tag: str
    color: str | None
    tooltip: str | None
    sound: bool
    sound_setting: str | None


@dataclass(frozen=True, slots=True)
class SessionRecord:
    id: str
    title: str
    cwd: str
    archived: bool
    opened_at: str
    created_at: str
    updated_at: str
    provider_id: str | None
    kind: str
    runtime_profile_id: str | None
    model: str | None
    reasoning_effort: str | None
    orchestration_mode: str | None
    attention_markers: tuple[str, ...]
    attention_marker_details: tuple[AttentionMarkerDetailRecord, ...]


@dataclass(frozen=True, slots=True)
class SessionTreeNodeRecord:
    id: str
    parent_id: str | None
    kind: str  # "root" | "fork"
    title: str
    fork_closed: bool


@dataclass(frozen=True, slots=True)
class ProjectRecord:
    path: str
    node_id: str
    name: str
    git_remote: str | None
    created_at: str
    last_used: str


@dataclass(frozen=True, slots=True)
class ProviderRecord:
    id: str
    name: str
    kind: str
    models: tuple[str, ...]
    capabilities: dict[str, bool]
    suspended: bool
    mode: str


@dataclass(frozen=True, slots=True)
class RuntimeProfileRecord:
    id: str
    provider_id: str
    runner: str
    name: str
    default_model: str
    default_reasoning_effort: str
    created_at: str
    updated_at: str
    deleted_at: str | None


@dataclass(frozen=True, slots=True)
class DeletedProviderRecord:
    """Display-only graveyard entry (config_store.py's own module
    docstring: "{id, name, nickname, kind, deleted_at}... Credentials are
    wiped at burial time — the graveyard never holds secrets") —
    tombstoned runtime profiles resolve their provider's display name
    through this, never through `list_provider_records()` (live only)."""

    id: str
    name: str
    nickname: str
    kind: str
    deleted_at: str | None


@dataclass(frozen=True, slots=True)
class RunRecord:
    run_id: str
    session_id: str
    turn_id: str | None
    success: bool | None
    error: str | None
    started_at: float
    last_heartbeat_at: float | None


@dataclass(frozen=True, slots=True)
class LlmCallRecord:
    """One durable `llm_call_log.jsonl` entry — a single provider API call,
    additive/non-cumulative token usage (unlike session/turn-journal
    `token_usage`, which is context-window-cumulative and never safe to
    sum; see `backend/adapters/usage_analytics.py`). Narrowed to only the
    fields UsageAnalytics aggregation needs — `prompt_preview`/`error`/
    `metadata` stay out of the adapters layer entirely."""
    id: str
    timestamp: str
    provider_id: str | None
    model: str | None
    trace_id: str | None
    token_usage: dict[str, int | float]


@dataclass(frozen=True, slots=True)
class SessionFolderRecord:
    id: str
    project_id: str
    parent_folder_id: str | None
    name: str
    order: int


@dataclass(frozen=True, slots=True)
class SessionTagRecord:
    id: str
    project_id: str | None
    name: str
    color: str


@dataclass(frozen=True, slots=True)
class SessionOrganizationRecord:
    """One session's folder/tag membership — `tag_assignments` is
    `(tag_id, source)` pairs, mirroring `SessionSummary.tag_refs`
    (`backend/surface_contract/session_surface.py`)."""

    folder_id: str | None
    tag_assignments: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class PendingInteractionRecord:
    """One pending legacy approval/choice record, normalized across the 3
    source mechanisms (tool-use approval, worker-creation approval,
    delegation-picker choice) into the one shape
    `ChatSurfaceAdapter` maps onto a `UserInteraction`
    (`backend/surface_contract/nodes.py`). `interaction_ref` is already
    namespace-prefixed (see that module's `*_REF_PREFIX` constants) so the
    resolve-routing side (`backend/surface_commands.py`) can dispatch on it
    without a second lookup."""

    interaction_ref: str
    kind: str  # UserInteractionKind value
    request: dict[str, object]


@dataclass(frozen=True, slots=True)
class WorkerRecord:
    agent_session_id: str
    name: str | None
    role_key: str | None
    cwd: str
    orchestration_mode: str
    node_id: str
    created_at: str
    last_active: str
    delegation_count: int
    token_usage: dict
    tags: tuple[str, ...]


def _attention_markers(raw: dict) -> tuple[str, ...]:
    """Real attention-marker tags for one session — `raw["markers"]` is the
    session summary projection's `{extension_id: marker}` map
    (`session_store._markers_for_session`, persisted to
    `attention_markers.json` by `session_manager.set_marker`/`clear_marker`).
    The stable per-marker identifier is its `tag` (see
    `file_ref_resolver.detect_markers`) — colors/tooltips are cosmetic and
    may drift, the tag doesn't."""
    markers = raw.get("markers")
    if not isinstance(markers, dict):
        return ()
    tags = {
        str(marker["tag"])
        for marker in markers.values()
        if isinstance(marker, dict) and marker.get("tag")
    }
    return tuple(sorted(tags))


def _attention_marker_details(raw: dict) -> tuple[AttentionMarkerDetailRecord, ...]:
    """Full per-extension marker shape (color/tooltip/sound alongside the
    stable `tag` — see `_attention_markers` above for the tag-only
    companion), read from the SAME `raw["markers"]` map so the two never
    diverge. `session_manager.set_marker`'s `marker` payload is
    extension-supplied and free-form beyond `tag`; `color`/`tooltip` are
    honestly `None` when an extension omits them rather than guessed."""
    markers = raw.get("markers")
    if not isinstance(markers, dict):
        return ()
    out = [
        AttentionMarkerDetailRecord(
            extension_id=str(extension_id),
            tag=str(marker.get("tag") or ""),
            color=marker.get("color"),
            tooltip=marker.get("tooltip"),
            sound=bool(marker.get("sound", False)),
            sound_setting=marker.get("sound_setting"),
        )
        for extension_id, marker in markers.items()
        if isinstance(marker, dict) and marker.get("tag")
    ]
    out.sort(key=lambda m: m.extension_id)
    return tuple(out)


def _session_record(raw: dict) -> SessionRecord:
    return SessionRecord(
        id=str(raw.get("id", "")),
        title=str(raw.get("name", "")),
        cwd=str(raw.get("cwd", "")),
        archived=bool(raw.get("archived", False)),
        opened_at=str(raw.get("last_opened_at", "")),
        created_at=str(raw.get("created_at", "")),
        updated_at=str(raw.get("updated_at", "")),
        provider_id=raw.get("provider_id"),
        kind=str(raw.get("kind", "user")),
        # `runtime_profile_id` is absent from session_store's list-summary
        # shape (only the single-session get_session() read carries it) —
        # honestly None rather than guessed when the caller only has the
        # summary form.
        runtime_profile_id=raw.get("runtime_profile_id"),
        model=str(raw.get("model") or "") or None,
        reasoning_effort=str(raw.get("reasoning_effort") or "") or None,
        orchestration_mode=str(raw.get("orchestration_mode") or "") or None,
        attention_markers=_attention_markers(raw),
        attention_marker_details=_attention_marker_details(raw),
    )


def _walk_tree_records(node: dict) -> list["SessionTreeNodeRecord"]:
    out: list[SessionTreeNodeRecord] = []
    for fork in node.get("forks") or []:
        if not isinstance(fork, dict) or not fork.get("id"):
            continue
        out.append(SessionTreeNodeRecord(
            id=str(fork["id"]),
            parent_id=str(fork.get("parent_session_id") or "") or None,
            kind="fork",
            title=str(fork.get("name") or ""),
            fork_closed=bool(fork.get("fork_closed", False)),
        ))
        out.extend(_walk_tree_records(fork))
    return out


def _project_record(raw: dict) -> ProjectRecord:
    return ProjectRecord(
        path=str(raw.get("path", "")),
        node_id=str(raw.get("node_id", "primary")),
        name=str(raw.get("name", "")),
        git_remote=raw.get("git_remote"),
        created_at=str(raw.get("created_at", "")),
        last_used=str(raw.get("last_used", "")),
    )


def _provider_record(raw: dict) -> ProviderRecord:
    capabilities = {
        k: v for k, v in raw.items()
        if k.startswith(_CAPABILITY_PREFIX) and isinstance(v, bool)
    }
    return ProviderRecord(
        id=str(raw.get("id", "")),
        name=str(raw.get("name", "")),
        kind=str(raw.get("kind", "claude")),
        models=tuple(raw.get("custom_models") or ()),
        capabilities=capabilities,
        suspended=bool(raw.get("suspended", False)),
        mode=str(raw.get("mode", "subscription")),
    )


def _runtime_profile_record(raw: dict) -> RuntimeProfileRecord:
    return RuntimeProfileRecord(
        id=str(raw.get("id", "")),
        provider_id=str(raw.get("provider_id", "")),
        runner=str(raw.get("runner", "")),
        name=str(raw.get("name", "")),
        default_model=str(raw.get("default_model", "")),
        default_reasoning_effort=str(raw.get("default_reasoning_effort", "")),
        created_at=str(raw.get("created_at", "")),
        updated_at=str(raw.get("updated_at", "")),
        deleted_at=raw.get("deleted_at"),
    )


def _turn_id_for_run_dir(run_dir) -> str | None:
    """The run's owning turn id.

    `runner.py`'s `turn_dir(run_dir, turn_id)` writes `turns/<turn_id>/
    {start,complete}.json` and its own module docstring states "each run
    serves exactly one turn" — so the single child directory name under
    `turns/` IS the turn id, authoritatively, with no heuristic linkage
    needed. `None` before the runner has created the turn directory yet
    (a brief window right after admission) or for a legacy run that
    predates this convention. On the rare case of more than one turn
    subdirectory (e.g. an admission retry that re-spawned under the same
    run_id), the most recently modified one is taken as the run's current
    turn — still read off disk, never guessed.
    """
    turns_root = run_dir / "turns"
    try:
        children = [c for c in turns_root.iterdir() if c.is_dir()]
    except OSError:
        return None
    if not children:
        return None
    if len(children) == 1:
        return children[0].name
    try:
        latest = max(children, key=lambda c: c.stat().st_mtime)
    except OSError:
        return None
    return latest.name


def _last_heartbeat_at_for_run_dir(run_dir) -> float | None:
    """mtime of `runs/<run_id>/runner_alive`, the sentinel the runner
    refreshes every ~5s for its whole process lifetime and removes on
    clean exit (`runs_dir.runner_alive_path`, `runner.py`) — the only
    on-disk heartbeat signal reachable from `store_access`. `None` once
    the runner has exited (sentinel removed) or before its first tick."""
    runs_dir = _resolve("runs_dir")
    alive_path = runs_dir.runner_alive_path(run_dir)
    try:
        return alive_path.stat().st_mtime
    except OSError:
        return None


def _run_record(session_id: str, run_dir) -> RunRecord:
    runs_dir = _resolve("runs_dir")
    payload = runs_dir.read_best_complete(run_dir)
    success = (
        bool(payload.get("success")) if isinstance(payload, dict) and "success" in payload else None
    )
    error = payload.get("error") if isinstance(payload, dict) else None
    try:
        started_at = run_dir.stat().st_mtime
    except OSError:
        started_at = 0.0
    return RunRecord(
        run_id=run_dir.name,
        session_id=session_id,
        turn_id=_turn_id_for_run_dir(run_dir),
        success=success,
        error=error,
        started_at=started_at,
        last_heartbeat_at=_last_heartbeat_at_for_run_dir(run_dir),
    )


def _llm_call_record(raw: dict) -> LlmCallRecord:
    usage = raw.get("token_usage")
    return LlmCallRecord(
        id=str(raw.get("id", "")),
        timestamp=str(raw.get("timestamp", "")),
        provider_id=raw.get("provider_id"),
        model=raw.get("model"),
        trace_id=raw.get("trace_id"),
        token_usage=dict(usage) if isinstance(usage, dict) else {},
    )


def _folder_record(raw: dict) -> SessionFolderRecord:
    return SessionFolderRecord(
        id=str(raw.get("id", "")),
        project_id=str(raw.get("project_id", "")),
        parent_folder_id=raw.get("parent_folder_id"),
        name=str(raw.get("name", "")),
        order=int(raw.get("order") or 0),
    )


def _tag_record(raw: dict) -> SessionTagRecord:
    return SessionTagRecord(
        id=str(raw.get("id", "")),
        project_id=raw.get("project_id"),
        name=str(raw.get("name", "")),
        color=str(raw.get("color", "")),
    )


def _session_organization_record(org: dict) -> SessionOrganizationRecord:
    return SessionOrganizationRecord(
        folder_id=org["folder_id"],
        tag_assignments=tuple(
            (str(a["tag_id"]), str(a["source"])) for a in org["tag_assignments"]
        ),
    )


# ---------------------------------------------------------------------------
# `list_pending_interactions`/`list_pending_interactions_for_sessions` shared
# per-record converters — one construction site per mechanism, used by both
# the single-id and bulk read paths so they can never drift.
# ---------------------------------------------------------------------------

def _tool_approval_record(rec) -> PendingInteractionRecord:
    return PendingInteractionRecord(
        interaction_ref=f"{TOOL_APPROVAL_REF_PREFIX}{rec.approval_id}",
        kind="approval",
        request={
            "subject": "tool", "tool_name": rec.tool_name,
            "summary": dict(rec.summary or {}), "risk_scope": rec.provider_kind,
        },
    )


def _worker_approval_record(rec: dict) -> PendingInteractionRecord:
    return PendingInteractionRecord(
        interaction_ref=f"{WORKER_APPROVAL_REF_PREFIX}{rec['delegation_id']}",
        kind="approval",
        request={
            "subject": "delegation",
            "cwd": rec.get("cwd", ""),
            "justification": rec.get("justification", ""),
            "proposed_description": rec.get("proposed_description", ""),
            "risk_scope": rec.get("proposed_orchestration_mode", ""),
        },
    )


def _delegate_choice_record(rec: dict) -> PendingInteractionRecord:
    return PendingInteractionRecord(
        interaction_ref=f"{DELEGATE_CHOICE_REF_PREFIX}{rec['delegation_id']}",
        kind="choice",
        request={
            "candidates": list(rec.get("proposed_ids") or ()),
            "prompt_preview": rec.get("prompt", ""),
        },
    )


def _user_input_record(rec: dict, user_input_store) -> PendingInteractionRecord:
    """The 4th legacy mechanism (`user_input_api.py`'s free-form
    input/approval/memory-proposal asks, `user_input_store`) — the one
    remaining `UserInteractionKind.INPUT` slot, unlike the other 3 which
    are all `approval`/`choice`. `interaction_request_dict` is the single
    shared builder the live fact producers (`user_input_api.py`) also use,
    so cold hydration and the live `UserInteractionUpsert` a still-open tab
    receives render the same `request` shape."""
    return PendingInteractionRecord(
        interaction_ref=f"{USER_INPUT_REF_PREFIX}{rec['request_id']}",
        kind="input",
        request=user_input_store.interaction_request_dict(rec),
    )


# ---------------------------------------------------------------------------
# ADR 0011 (System & Host Surface) record types.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExtensionRecord:
    """One installed extension's catalog + config-bundle facts, sourced
    from `extension_store.list_extensions()` (record) and
    `extension_store.extension_config(extension_id)` (config bundle) —
    both read-only, non-reconciling, no side effects."""

    extension_id: str
    name: str
    enabled: bool
    version: str
    source_type: str
    config: dict[str, object]


@dataclass(frozen=True, slots=True)
class HarnessProfileRecord:
    harness_profile_id: str
    name: str
    is_default: bool
    disabled_builtin_extensions: tuple[str, ...]
    disabled_builtin_tools: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ExtensionUiHookRecord:
    """One quick-button or page hook, normalized across
    `extension_store.ui_hooks()`'s two lists — `kind` distinguishes them
    (`quick_button` | `page`), never a separate record type per kind
    (both are `ExtensionUiModule`-shaped, ADR 0011 §3)."""

    extension_id: str
    kind: str  # "quick_button" | "page"
    label: str
    icon: str | None
    placements: tuple[str, ...]
    action: dict[str, object] | None
    badge: dict[str, object] | None


@dataclass(frozen=True, slots=True)
class ExtensionFrontendModuleRecord:
    extension_id: str
    slot: str
    module_id: str
    label: str
    module_url: str
    payments: bool
    marketplace_auth: bool


@dataclass(frozen=True, slots=True)
class ScheduleRecord:
    schedule_id: str
    app_session_id: str
    prompt: str
    kind: str
    fire_at: str | None
    interval_seconds: int | None
    last_fired_at: str | None


@dataclass(frozen=True, slots=True)
class HostStartupTaskRecord:
    id: str
    label: str
    state: str
    started_at: str
    finished_at: str | None
    error: str | None


@dataclass(frozen=True, slots=True)
class InstallationCapabilityRecord:
    capability_id: str
    enabled: bool
    provisioned: bool
    active: bool
    restart_required: bool
    self_provisionable: bool
    in_app_restart_supported: bool


@dataclass(frozen=True, slots=True)
class NodeRecord:
    node_id: str
    role: str
    address: str
    cwd_roots: tuple[str, ...]
    state: str
    last_seen: float | None
    connected_at: float | None
    version_status: str
    app_commit_sha: str | None
    app_dirty: bool | None
    primary_commit_sha: str | None
    primary_dirty: bool | None


@dataclass(frozen=True, slots=True)
class NodeProviderCredentialRecord:
    node_id: str
    provider_id: str
    status: str
    authorized_at: str | None
    updated_at: str | None
    failure_code: str | None


@dataclass(frozen=True, slots=True)
class NodeRegistrationRecord:
    node_id: str
    address: str
    cwd_roots: tuple[str, ...]
    fingerprint: str
    status: str  # "pending" | "approved" | "denied"


def _extension_record(raw: dict, config: dict) -> ExtensionRecord:
    manifest = raw.get("manifest") or {}
    source = raw.get("source") or {}
    return ExtensionRecord(
        extension_id=str(manifest.get("id") or ""),
        name=str(manifest.get("name") or manifest.get("id") or ""),
        enabled=bool(raw.get("enabled", False)),
        version=str(manifest.get("version") or ""),
        source_type=str(source.get("type") or ""),
        config=config,
    )


def _schedule_record(raw: dict) -> ScheduleRecord:
    return ScheduleRecord(
        schedule_id=str(raw.get("id", "")),
        app_session_id=str(raw.get("app_session_id", "")),
        prompt=str(raw.get("prompt", "")),
        kind=str(raw.get("kind", "")),
        fire_at=raw.get("fire_at"),
        interval_seconds=raw.get("interval_seconds"),
        last_fired_at=raw.get("last_fired_at"),
    )


# ADR 0005: startup work is reported into the shared background_work
# registry (status: pending/running/succeeded/failed/cancelled/unknown), but
# HostStartupTaskState (system_surface.py §7) only ever modeled three states
# — running/done/failed — because the legacy registry never had a fourth.
# pending collapses onto running (a startup step is always registered the
# instant it starts, so a caller never observes pending); cancelled/unknown
# collapse onto failed (both are "this didn't finish successfully", the
# closest honest fit in a three-state enum that predates them).
_BACKGROUND_WORK_STATUS_TO_STARTUP_STATE = {
    "pending": "running",
    "running": "running",
    "succeeded": "done",
    "failed": "failed",
    "cancelled": "failed",
    "unknown": "failed",
}


def _startup_task_record(raw: dict) -> HostStartupTaskRecord:
    return HostStartupTaskRecord(
        id=str(raw.get("id", "")),
        label=str(raw.get("label", "")),
        state=_BACKGROUND_WORK_STATUS_TO_STARTUP_STATE.get(
            str(raw.get("status", "")), "running",
        ),
        started_at=str(raw.get("started_at", "")),
        finished_at=raw.get("finished_at"),
        error=raw.get("error"),
    )


def _node_record(raw: dict) -> NodeRecord:
    return NodeRecord(
        node_id=str(raw.get("id") or raw.get("node_id") or ""),
        role=str(raw.get("role", "")),
        address=str(raw.get("address", "")),
        cwd_roots=tuple(raw.get("cwd_roots") or ()),
        state=str(raw.get("state", "unknown")),
        last_seen=raw.get("last_seen"),
        connected_at=raw.get("connected_at"),
        version_status=str(raw.get("version_status", "unknown")),
        app_commit_sha=raw.get("app_commit_sha") or None,
        app_dirty=raw.get("app_dirty"),
        primary_commit_sha=raw.get("primary_commit_sha") or None,
        primary_dirty=raw.get("primary_dirty"),
    )


def _node_provider_credential_record(raw: dict) -> NodeProviderCredentialRecord:
    return NodeProviderCredentialRecord(
        node_id=str(raw.get("node_id", "")),
        provider_id=str(raw.get("provider_id", "")),
        status=str(raw.get("status", "")),
        authorized_at=raw.get("authorized_at"),
        updated_at=raw.get("updated_at"),
        failure_code=raw.get("failure_code"),
    )


def _node_registration_record(raw: dict) -> NodeRegistrationRecord:
    return NodeRegistrationRecord(
        node_id=str(raw.get("node_id", "")),
        address=str(raw.get("address", "")),
        cwd_roots=tuple(raw.get("cwd_roots") or ()),
        fingerprint=str(raw.get("fingerprint", "")),
        status=str(raw.get("status", "pending")),
    )


def _worker_record(raw: dict) -> WorkerRecord:
    return WorkerRecord(
        agent_session_id=str(raw.get("agent_session_id", "")),
        name=raw.get("name"),
        role_key=raw.get("role_key"),
        cwd=str(raw.get("cwd", "")),
        orchestration_mode=str(raw.get("orchestration_mode", "")),
        node_id=str(raw.get("node_id") or "primary"),
        created_at=str(raw.get("created_at", "")),
        last_active=str(raw.get("last_active", "")),
        delegation_count=int(raw.get("delegation_count", 0) or 0),
        token_usage=dict(raw.get("token_usage") or {}),
        tags=tuple(raw.get("tags") or ()),
    )


class StoreAccess:
    """Narrow, typed, read-only methods the surface adapters need. No store
    internals (dicts, module objects, mutation methods) leak out."""

    def list_session_records(self) -> tuple[SessionRecord, ...]:
        session_store = _resolve("session_store")
        return tuple(_session_record(s) for s in session_store.list_sessions())

    def get_session_record(self, sid: str) -> SessionRecord | None:
        session_store = _resolve("session_store")
        raw = session_store.get_session(sid)
        return _session_record(raw) if raw is not None else None

    def list_provider_records(self) -> tuple[ProviderRecord, ...]:
        config_store = _resolve("config_store")
        providers = config_store.list_providers().get("providers", [])
        return tuple(_provider_record(p) for p in providers)

    def list_runtime_profiles(self, *, include_deleted: bool = False) -> tuple[RuntimeProfileRecord, ...]:
        """`include_deleted=True` (closure 3, ADR 0007 parity with the
        legacy `runtime_profiles_api._snapshot()`'s own `include_deleted=
        True` call) surfaces tombstoned profiles too, for old-session
        display — default False, unchanged for every pre-existing caller
        (e.g. `provider_adapter.model_catalog()`'s runner lookup, which
        must never pick a deleted profile's runner)."""
        config_store = _resolve("config_store")
        return tuple(
            _runtime_profile_record(p)
            for p in config_store.list_runtime_profiles(include_deleted=include_deleted)
        )

    def get_default_runtime_profile_id(self) -> str | None:
        config_store = _resolve("config_store")
        return config_store.get_default_runtime_profile_id()

    def list_deleted_providers(self) -> tuple[DeletedProviderRecord, ...]:
        config_store = _resolve("config_store")
        return tuple(
            DeletedProviderRecord(
                id=str(r.get("id", "")),
                name=str(r.get("name", "")),
                nickname=str(r.get("nickname", "")),
                kind=str(r.get("kind", "")),
                deleted_at=r.get("deleted_at"),
            )
            for r in config_store.list_deleted_providers()
        )

    def get_last_models(self) -> dict[str, str]:
        user_prefs = _resolve("user_prefs")
        return dict(user_prefs.get_last_models())

    def get_last_reasoning_efforts(self) -> dict[str, str]:
        user_prefs = _resolve("user_prefs")
        return dict(user_prefs.get_last_reasoning_efforts())

    def list_run_records(self) -> tuple[RunRecord, ...]:
        """Every run dir for every session that has ever run, not just each
        session's latest.

        `runs_dir.run_dirs_by_app_session` collapses to ONE (most-recently-
        written) run dir per `app_session_id` — a discovery index, not a
        run history. Used here only to find WHICH sessions have any run at
        all (and, as a side effect, to warm/rebuild its ledger cache); the
        full per-session history then comes from
        `runs_dir.cached_run_dirs_for_app_session`, the same "all run dirs
        for one session" API `orchestrator.py`'s ask-reattach path already
        uses, riding the now-warm cache from the discovery pass above."""
        runs_dir = _resolve("runs_dir")
        root = runs_dir.runs_root()
        session_ids = runs_dir.run_dirs_by_app_session(root).keys()
        return tuple(
            _run_record(session_id, run_dir)
            for session_id in session_ids
            for run_dir in runs_dir.cached_run_dirs_for_app_session(root, session_id)
        )

    def list_run_records_for_session(self, session_id: str) -> tuple[RunRecord, ...]:
        """Every run dir for exactly ONE session — the session-scoped
        sibling of `list_run_records()`.

        `list_run_records()` first calls `runs_dir.run_dirs_by_app_session`
        to discover WHICH sessions have any run at all — an O(every
        session that has ever run) walk of the run-state ledger, correct
        for a genuine "list every run in the backend" caller (a paginated
        runs listing) but wasteful when the caller already knows the one
        session_id it cares about: it pays for every OTHER session's rows
        just to throw them away in a Python-side filter.

        Goes straight to `runs_dir.cached_run_dirs_for_app_session`, which
        filters at the sqlite layer (`WHERE app_session_id IN (...)`) to
        just this session's own run dirs — O(this session's runs), not
        O(every run dir the backend has ever created). Any caller that
        already knows the session_id (a lifecycle-fact handler reacting to
        one session's turn, a worker-sidecar lookup for one
        agent_session_id) MUST use this instead of filtering
        `list_run_records()` — the full scan's cost grows with the whole
        backend's run history, not with what the caller actually needs."""
        if not session_id:
            return ()
        runs_dir = _resolve("runs_dir")
        root = runs_dir.runs_root()
        return tuple(
            _run_record(session_id, run_dir)
            for run_dir in runs_dir.cached_run_dirs_for_app_session(root, session_id)
        )

    def get_latest_run_record(self, session_id: str) -> RunRecord | None:
        """Most-recently-started run for `session_id`, or None. The shared
        best-effort run<->session linkage heuristic (no direct run<->turn
        index exists) — used by both RunsSurfaceAdapter's lifecycle-fact
        handler and ChatSurfaceAdapter's worker-sidecar success/error
        lookup, so the heuristic has exactly one owner."""
        records = self.list_run_records_for_session(session_id)
        if not records:
            return None
        return max(records, key=lambda r: r.started_at)

    def inspect_process_tree(self, pid: int | None, run_id: str | None) -> tuple[dict, ...]:
        """One process-tree row per pid descending from `pid` — read-only
        pass-through to `process_inspect.inspect_process_tree`, the EXACT
        helper `session_detail_api.py`'s legacy run-details route uses (one
        implementation, not a second). Live OS state (forks `ps`/`pgrep`),
        never persisted — `RunsSurfaceAdapter.run_detail()` is this
        method's only caller, calling it on-demand only, never on a live
        push path. `process_inspect` holds no module-level mutable state
        (pure functions over caller-supplied pids), so this is exercised
        through the same `_resolve` singleton-canonicalization plumbing as
        every other store here purely for consistency with the one
        sanctioned door adapters use to reach backend/*.py modules, not
        because it needs the singleton guarantee."""
        process_inspect = _resolve("process_inspect")
        return tuple(process_inspect.inspect_process_tree(pid, run_id))

    def get_session_tree_records(
        self, session_id: str,
    ) -> tuple[SessionTreeNodeRecord, ...] | None:
        """The full root+fork tree containing `session_id` (root or fork
        id both resolve to the same tree), real data straight off
        `session_store.get_root_tree` — the same nested `forks` array the
        legacy fork/close_fork/reopen_fork routes
        (`session_detail_api.py`) mutate. `None` when no root tree
        contains `session_id` at all."""
        session_store = _resolve("session_store")
        root = session_store.get_root_tree(session_id)
        if root is None or not root.get("id"):
            return None
        root_node = SessionTreeNodeRecord(
            id=str(root["id"]),
            parent_id=None,
            kind="root",
            title=str(root.get("name") or ""),
            fork_closed=bool(root.get("fork_closed", False)),
        )
        return (root_node, *_walk_tree_records(root))

    def project_session_counts(self) -> dict[str, int]:
        """`{project_path: session_count}` — the single real aggregation
        source `projects_api.get_projects` and
        `SessionSurfaceAdapter.projects` both read, so the legacy REST
        surface and the v2 surface can never disagree (see
        `project_store.session_counts_by_cwd`)."""
        project_store = _resolve("project_store")
        return dict(project_store.session_counts_by_cwd())

    def list_projects(self) -> tuple[ProjectRecord, ...]:
        project_store = _resolve("project_store")
        return tuple(_project_record(p) for p in project_store.list_projects())

    def list_folders(self, project_ref: str | None) -> tuple[SessionFolderRecord, ...]:
        """Folder catalog, optionally narrowed to one project (ADR 0008's
        bounded `list_folders(project_ref?)` read) — `snapshot(project_id)`'s
        own filtering, the same one the legacy `GET /api/session-organization`
        route already relies on."""
        session_organization_store = _resolve("session_organization_store")
        snap = session_organization_store.snapshot(project_ref)
        return tuple(_folder_record(f) for f in snap["folders"])

    def list_tags(self, project_ref: str | None) -> tuple[SessionTagRecord, ...]:
        """Tag catalog, optionally narrowed to one project plus every
        global (`project_id is None`) tag — same `snapshot(project_id)`
        semantics as `list_folders` above."""
        session_organization_store = _resolve("session_organization_store")
        snap = session_organization_store.snapshot(project_ref)
        return tuple(_tag_record(t) for t in snap["tags"])

    def get_session_organization(self, session_id: str) -> SessionOrganizationRecord:
        session_organization_store = _resolve("session_organization_store")
        org = session_organization_store.organization_for_session(session_id)
        return _session_organization_record(org)

    def get_session_organizations(
        self, session_ids: Iterable[str],
    ) -> dict[str, SessionOrganizationRecord]:
        """Bulk counterpart of `get_session_organization` — ANY caller
        resolving organization for more than one session at a time (a page,
        a filter pass) MUST use this instead of looping the single-id call:
        the underlying store deep-copies its full folders/tags/assignments
        state per call, so N single-id calls cost N full-store copies where
        this costs exactly one. See `session_organization_store.
        organizations_for_sessions`'s docstring."""
        session_organization_store = _resolve("session_organization_store")
        orgs = session_organization_store.organizations_for_sessions(session_ids)
        return {sid: _session_organization_record(org) for sid, org in orgs.items()}

    def get_provider_credential_status(self, provider_id: str) -> str:
        config_store = _resolve("config_store")
        return config_store.provider_credential_status(provider_id)

    def get_worker_record(self, agent_session_id: str) -> WorkerRecord | None:
        worker_store = _resolve("worker_store")
        raw = worker_store.get_worker("", agent_session_id)
        return _worker_record(raw) if raw is not None else None

    def list_llm_call_records(self) -> tuple[LlmCallRecord, ...]:
        """Every durable `llm_call_log.jsonl` entry, oldest first — the
        source `backend/adapters/usage_analytics.py` aggregates into
        `UsageRow`s (ADR 0009's `UsageAnalytics`)."""
        llm_call_log = _resolve("llm_call_log")
        return tuple(_llm_call_record(c) for c in llm_call_log.iter_calls())

    def list_pending_interactions(self, session_id: str) -> tuple[PendingInteractionRecord, ...]:
        """Currently-pending UserInteractions across all 4 legacy
        mechanisms for one session — the cold-hydration source
        `ChatSurfaceAdapter.open_session`/`_open_session_fast` populate
        `CompactSessionSnapshot.interactions` from (mirrors how `runs` is
        meant to be sourced). Pending-only, like `tool_approval.registry`/
        `pending_approvals.list_pending`/`user_input_store.pending_for_session`
        themselves — history isn't modeled by any of the 4 legacy stores,
        so there is nothing to page. Single-id convenience; a caller
        resolving more than one session at once MUST use
        `list_pending_interactions_for_sessions` instead (one pass per
        store, not one per session — see that method's docstring)."""
        tool_approval = _resolve("tool_approval")
        pending_approvals = _resolve("pending_approvals")
        session_bridge = _resolve("session_bridge")
        user_input_store = _resolve("user_input_store")
        return (
            *(_tool_approval_record(r) for r in tool_approval.registry.list_for_session(session_id)),
            *(
                _worker_approval_record(r) for r in pending_approvals.list_pending()
                if r.get("app_session_id") == session_id
            ),
            *(_delegate_choice_record(r) for r in session_bridge.list_pending_for_caller(session_id)),
            *(
                _user_input_record(r, user_input_store)
                for r in user_input_store.pending_for_session(session_id)
            ),
        )

    def list_pending_interactions_for_sessions(
        self, session_ids: Iterable[str],
    ) -> dict[str, tuple[PendingInteractionRecord, ...]]:
        """Bulk counterpart of `list_pending_interactions` — ONE pass over
        each of the 4 legacy pending-interaction stores regardless of how
        many session ids are requested, instead of one pass per store PER
        session (`list_pending_interactions` in a loop). Any caller
        resolving pending interactions for more than one session at a time
        (a list page) MUST use this instead."""
        session_ids = list(session_ids)
        tool_approval = _resolve("tool_approval")
        pending_approvals = _resolve("pending_approvals")
        session_bridge = _resolve("session_bridge")
        user_input_store = _resolve("user_input_store")

        by_tool_approval = tool_approval.registry.grouped_by_session()
        by_worker_approval: dict[str, list[dict]] = {}
        for rec in pending_approvals.list_pending():
            sid = rec.get("app_session_id")
            if isinstance(sid, str) and sid:
                by_worker_approval.setdefault(sid, []).append(rec)
        by_delegate_choice = session_bridge.pending_by_caller()
        by_user_input = user_input_store.pending_for_sessions(session_ids)

        return {
            sid: (
                *(_tool_approval_record(r) for r in by_tool_approval.get(sid, [])),
                *(_worker_approval_record(r) for r in by_worker_approval.get(sid, [])),
                *(_delegate_choice_record(r) for r in by_delegate_choice.get(sid, [])),
                *(
                    _user_input_record(r, user_input_store)
                    for r in by_user_input.get(sid, [])
                ),
            )
            for sid in session_ids
        }


    # ---- ADR 0011 (System & Host Surface) reads ----------------------

    def list_extension_records(self) -> tuple[ExtensionRecord, ...]:
        """Installed-extension catalog + config bundle, non-reconciling
        (`list_extensions()`, not `list_extensions_with_reconciliation()`
        — reconciliation is a side-effecting write, out of scope for a
        read-only façade)."""
        extension_store = _resolve("extension_store")
        out = []
        for raw in extension_store.list_extensions(include_hidden=True):
            manifest = raw.get("manifest") or {}
            extension_id = str(manifest.get("id") or "")
            if not extension_id:
                continue
            try:
                config = extension_store.extension_config(extension_id)
            except extension_store.ExtensionError:
                config = {}
            out.append(_extension_record(raw, config))
        return tuple(out)

    def get_extension_record(self, extension_id: str) -> ExtensionRecord | None:
        return next(
            (r for r in self.list_extension_records() if r.extension_id == extension_id), None,
        )

    def extension_updates_available_ids(self) -> frozenset[str]:
        """Extension ids with a known-available update, from the
        disposable cached update-scan projection
        (`extension_store.cached_extension_updates()`) — never triggers a
        fresh network scan (`check_extension_updates(refresh=True)`),
        which is a side-effecting call this read-only façade must not
        make."""
        extension_store = _resolve("extension_store")
        cached = extension_store.cached_extension_updates()
        if not cached:
            return frozenset()
        return frozenset(str(x) for x in (cached.get("available") or ()))

    def list_extension_ui_hooks(self) -> tuple[ExtensionUiHookRecord, ...]:
        extension_store = _resolve("extension_store")
        hooks = extension_store.ui_hooks()
        out = [
            ExtensionUiHookRecord(
                extension_id=str(item.get("extension_id", "")),
                kind="quick_button",
                label=str(item.get("label", "")),
                icon=item.get("icon"),
                placements=tuple(item.get("placements") or ()),
                action=item.get("action") or None,
                badge=None,
            )
            for item in hooks.get("quick_buttons") or ()
        ]
        out.extend(
            ExtensionUiHookRecord(
                extension_id=str(item.get("extension_id", "")),
                kind="page",
                label=str(item.get("label", "")),
                icon=item.get("icon"),
                placements=(),
                action=item.get("open") or None,
                # `badge` is a poll-endpoint descriptor in legacy, not a
                # live count (see system_adapter.py's honest-gap note for
                # ExtensionUiModule.badge_count) — passed through raw for
                # the adapter to decide, never fabricated into a number.
                badge=item.get("badge") or None,
            )
            for item in hooks.get("pages") or ()
        )
        return tuple(out)

    def list_extension_frontend_modules(self) -> tuple[ExtensionFrontendModuleRecord, ...]:
        extension_store = _resolve("extension_store")
        out = []
        for entry in extension_store.frontend_entrypoints():
            extension_id = str(entry.get("extension_id", ""))
            payments = bool(entry.get("payments"))
            marketplace_auth = bool(entry.get("marketplace_auth"))
            for item in entry.get("frontend_modules") or ():
                out.append(
                    ExtensionFrontendModuleRecord(
                        extension_id=extension_id,
                        slot=str(item.get("slot", "")),
                        module_id=str(item.get("id", "")),
                        label=str(item.get("label", "")),
                        module_url=str(item.get("module_url", "")),
                        payments=payments,
                        marketplace_auth=marketplace_auth,
                    )
                )
        return tuple(out)

    def list_harness_profile_records(self) -> tuple[HarnessProfileRecord, ...]:
        """The synthesized `default` profile plus every named profile,
        each resolved through `harness_profile_resolver.resolve_profile`
        — the SAME resolution legacy `harness_profiles_api._profile_response`
        uses — so `disabled_builtin_*` reflects the true effective (base-
        chain-resolved) lists, not just this profile's own override
        delta."""
        harness_profile_store = _resolve("harness_profile_store")
        harness_profile_resolver = _resolve("harness_profile_resolver")
        out = [self._resolved_harness_profile(
            harness_profile_store.DEFAULT_PROFILE_ID, "Default", is_default=True,
        )]
        for stored in harness_profile_store.list_profiles():
            profile_id = str(stored.get("id", ""))
            if not profile_id:
                continue
            out.append(self._resolved_harness_profile(
                profile_id, str(stored.get("name") or profile_id), is_default=False,
            ))
        return tuple(out)

    def _resolved_harness_profile(
        self, profile_id: str, name: str, *, is_default: bool,
    ) -> HarnessProfileRecord:
        harness_profile_resolver = _resolve("harness_profile_resolver")
        resolved = harness_profile_resolver.resolve_profile(profile_id)
        return HarnessProfileRecord(
            harness_profile_id=profile_id,
            name=name,
            is_default=is_default,
            disabled_builtin_extensions=tuple(
                resolved["disabled_builtin_extensions"]["resolved"],
            ),
            disabled_builtin_tools=tuple(resolved["disabled_builtin_tools"]["resolved"]),
        )

    def marketplace_bridge_snapshot(self) -> dict:
        """Raw `MarketplaceBridge.snapshot()` dict (revision,
        connection_state, revocation_pending, paired_devices, intents) —
        kept as the raw legacy shape rather than a `*Record` dataclass
        since `system_adapter.py` maps it 1:1 onto two distinct ADR 0011
        §5 resources (`MarketplaceBridgeState`/`MarketplaceIntent`), and
        an intermediate dataclass would just be relabeled dict access."""
        marketplace_bridge = _resolve("marketplace_bridge")
        return marketplace_bridge.bridge.snapshot()

    def list_schedule_records(self, app_session_id: str | None) -> tuple[ScheduleRecord, ...]:
        schedule_store = _resolve("schedule_store")
        raws = (
            schedule_store.list_for_session(app_session_id)
            if app_session_id is not None
            else schedule_store.list_all()
        )
        return tuple(_schedule_record(r) for r in raws)

    def list_host_startup_tasks(self) -> tuple[HostStartupTaskRecord, ...]:
        """Startup work is one owner among many in the shared
        `background_work_registry` (ADR 0005) — filter its snapshot down to
        `startup_tasks.OWNER` (the single source of truth for which owner_id
        startup steps report under) so this feed keeps showing only startup
        work, not provider installs or node-credential syncs that share the
        same registry."""
        startup_tasks = _resolve("startup_tasks")
        background_work = _resolve("background_work")
        prefix = f"{background_work.OWNER_CORE}:{startup_tasks.OWNER}:"
        items = background_work.background_work_registry.snapshot()["items"]
        return tuple(
            _startup_task_record(item) for item in items if item["id"].startswith(prefix)
        )

    def list_installation_capability_records(self) -> tuple[InstallationCapabilityRecord, ...]:
        installation_profile = _resolve("installation_profile")
        state = installation_profile.capabilities()
        capabilities = state.get("capabilities") if isinstance(state, dict) else None
        if not isinstance(capabilities, dict):
            return ()
        return tuple(
            InstallationCapabilityRecord(
                capability_id=str(capability_id),
                enabled=bool(detail.get("enabled")),
                provisioned=bool(detail.get("provisioned")),
                active=bool(detail.get("active")),
                restart_required=bool(detail.get("restart_required")),
                self_provisionable=bool(detail.get("self_provisionable")),
                in_app_restart_supported=bool(detail.get("in_app_restart_supported")),
            )
            for capability_id, detail in capabilities.items()
            if isinstance(detail, dict)
        )

    def list_node_records(self) -> tuple[NodeRecord, ...]:
        node_store = _resolve("node_store")
        node_provider_credential_sync = _resolve("node_provider_credential_sync")
        snapshot = node_store.snapshot()
        projected = node_provider_credential_sync.project_node_snapshots(snapshot)
        return tuple(_node_record(r) for r in projected)

    def node_provider_credential_records(
        self, node_id: str,
    ) -> tuple[NodeProviderCredentialRecord, ...]:
        node_provider_credential_sync = _resolve("node_provider_credential_sync")
        return tuple(
            _node_provider_credential_record(r)
            for r in node_provider_credential_sync.snapshot(node_id)
        )

    def list_pending_node_registrations(self) -> tuple[NodeRegistrationRecord, ...]:
        """Pending-only (see `system_adapter.py`'s honest-gap note): the
        legacy `node_link.public_pending_nodes*` read path retains only
        PENDING registration requests — resolved/cancelled ones are not
        list-queryable, they surface solely via the live
        `node_registration_upsert` push (`node_link._emit_registration`)."""
        node_link = _resolve("node_link")
        cached = node_link.public_pending_nodes_cached()
        raws = cached if cached is not None else node_link.public_pending_nodes()
        return tuple(_node_registration_record(r) for r in raws)


store_access = StoreAccess()
