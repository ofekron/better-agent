"""Concrete SystemSurface implementation (ADR 0011 Part 1).

Reads exclusively through `backend.adapters.store_access.store_access`;
live deltas are bound to the `*.fire.*` facts this pass adds at each
owning legacy mutation/broadcast site (extension_api.py,
extension_ui_manager.py, harness_profiles_api.py, marketplace_bridge.py,
scheduler.py, startup_tasks.py, mobile_desktop_api.py, node_store.py,
node_provider_credential_sync.py, node_link.py) — same "facts not
commands" posture `runs_adapter.py` already uses for `lifecycle.turn_*`:
a fact means "something changed, go re-derive the current truth from
store_access", never a payload this adapter trusts as the new state.

Every field this adapter cannot honestly derive from `StoreAccess`'s
current surface is called out inline as a `# gap:` comment (CLAUDE.md:
"never invent"); the caller's report enumerates them."""

from __future__ import annotations

import logging

from backend.adapters.projection import BusBoundProjection
from backend.adapters.store_access import store_access
from backend.event_bus import BusEvent
from backend.surface_contract.identity import (
    Emit,
    NodeId,
    Ok,
    PageCursor,
    ProjectionResult,
    Rebuilding,
    SessionId,
    SnapshotIdentity,
    StaleCursor,
    Subscription,
)
from backend.surface_contract.intents import (
    CreateSchedule,
    DecideMarketplaceIntent,
    DeleteHarnessProfile,
    DeleteSchedule,
    DisableExtension,
    EnableExtension,
    InstallExtension,
    RemoveNode,
    ResolveNodeRegistration,
    RevokeMarketplaceDevice,
    SaveHarnessProfile,
    SetDefaultHarnessProfile,
    SetInstallationCapability,
    SyncNodeProviders,
    SystemIntent,
    UninstallExtension,
    UpdateExtension,
    UpdateExtensionConfig,
)
from backend.surface_contract.intents import IntentAccepted, IntentRejected, TransportAck
from backend.surface_contract.system_surface import (
    ExtensionCatalogEntry,
    ExtensionCatalogPage,
    ExtensionConfigDescriptor,
    ExtensionConfigPage,
    ExtensionConfigSections,
    ExtensionConfigUpsert,
    ExtensionCatalogUpsert,
    ExtensionUiDisplay,
    ExtensionUiModule,
    ExtensionUiModuleKind,
    ExtensionUiChanged,
    ExtensionUiPage,
    HarnessProfileDescriptor,
    HarnessProfilePage,
    HarnessProfileUpsert,
    HookAction,
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
    ScheduleSummary,
    SchedulePage,
    ScheduleRemoved,
    ScheduleUpsert,
    SystemSurface,
)

logger = logging.getLogger(__name__)

_PAGE_SIZE = 50


def _decode_offset_cursor(
    cursor: PageCursor | None, snapshot: SnapshotIdentity,
) -> int | StaleCursor:
    """Shared offset-token cursor decode, mirrors
    `runs_adapter._decode_offset_cursor` — one incarnation-check + one
    `int(token)` parse, factored once for every paged read below rather
    than duplicated per method."""
    if cursor is None:
        return 0
    if cursor.snapshot.incarnation != snapshot.incarnation:
        return StaleCursor()
    try:
        return int(cursor.token)
    except ValueError:
        return StaleCursor()


def _page(items: list, offset: int, surface_id: str, snapshot: SnapshotIdentity):
    page = items[offset : offset + _PAGE_SIZE]
    next_cursor = None
    if offset + _PAGE_SIZE < len(items):
        next_cursor = PageCursor(surface_id=surface_id, snapshot=snapshot, token=str(offset + _PAGE_SIZE))
    return page, next_cursor


class _SubscriptionImpl:
    def __init__(self, close_fn) -> None:
        self._close_fn = close_fn

    def close(self) -> None:
        self._close_fn()


def _extension_config_descriptor(record, *, cv: int) -> ExtensionConfigDescriptor:
    config = record.config
    sections = ExtensionConfigSections(
        settings=config.get("settings") or None,
        instructions={"instructions": config.get("user_instructions")}
        if config.get("user_instructions")
        else None,
        ui_settings=config.get("ui") or None,
        # gap: `extension_config()`'s `internal_llm_tasks` is the task-key
        # list an extension declares, not the {task: assignment} map a
        # section named "internal_llm" implies — the real assignment map
        # (`config_store.get_internal_llm_assignments()`) is global, not
        # extension-scoped, so it is intentionally not folded in here to
        # avoid misrepresenting per-extension ownership.
        internal_llm={"tasks": config.get("internal_llm_tasks")}
        if config.get("internal_llm_tasks")
        else None,
        permissions=config.get("permissions") or None,
        mcp={"servers": config.get("mcp")} if config.get("mcp") else None,
        skills={"skills": config.get("skills")} if config.get("skills") else None,
    )
    return ExtensionConfigDescriptor(extension_id=record.extension_id, cv=cv, sections=sections)


def _extension_catalog_entry(record, *, update_available: bool, cv: int) -> ExtensionCatalogEntry:
    return ExtensionCatalogEntry(
        extension_id=record.extension_id,
        cv=cv,
        display=record.name,
        installed_version=record.version,
        # gap: the cached update-scan projection only tracks WHICH
        # extension ids have an update, not the available version string
        # (that requires the fresh, side-effecting network scan
        # `check_extension_updates(refresh=True)` performs — out of scope
        # for a read-only façade). Honestly None rather than guessed.
        available_version=None,
        update_available=update_available,
        enabled=record.enabled,
        source=record.source_type,
    )


def _hook_action(raw: dict | None) -> HookAction | None:
    if not raw:
        return None
    return HookAction(
        kind=str(raw.get("kind", "")),
        path=raw.get("path"),
        endpoint=raw.get("endpoint"),
        path_template=raw.get("path_template"),
        id_field=raw.get("id_field"),
        include_cwd=raw.get("include_cwd"),
        module_url=raw.get("module_url") or raw.get("module"),
    )


def _harness_descriptor(record, *, cv: int) -> HarnessProfileDescriptor:
    return HarnessProfileDescriptor(
        harness_profile_id=record.harness_profile_id,
        cv=cv,
        display=record.name,
        is_default=record.is_default,
        disabled_builtin_extensions=record.disabled_builtin_extensions,
        disabled_builtin_tools=record.disabled_builtin_tools,
        config_schema=None,
    )


def _marketplace_extension_ref(raw: dict | None) -> MarketplaceExtensionRef | None:
    if not isinstance(raw, dict) or not raw:
        return None
    return MarketplaceExtensionRef(
        id=str(raw.get("id", "")),
        name=raw.get("name"),
        version=raw.get("version"),
        publisher=raw.get("publisher"),
        permission_delta=raw.get("permission_delta"),
    )


def _marketplace_intent(raw: dict, *, cv: int) -> MarketplaceIntent:
    return MarketplaceIntent(
        intent_id=str(raw.get("intent_id", "")),
        cv=cv,
        action=str(raw.get("action", "")),
        status=str(raw.get("status", "")),
        extension=_marketplace_extension_ref(raw.get("extension")),
        account_label=raw.get("account_label"),
        site_label=raw.get("site_label"),
        device_label=raw.get("device_label"),
        error=raw.get("error"),
    )


def _marketplace_bridge_state(raw: dict, *, cv: int) -> MarketplaceBridgeState:
    return MarketplaceBridgeState(
        cv=cv,
        connection_state=MarketplaceConnectionState(raw.get("connection_state", "unpaired")),
        revocation_pending=bool(raw.get("revocation_pending", False)),
        paired_devices=tuple(
            PairedDevice(device_ref=str(d.get("device_id", "")), label=str(d.get("label", "")))
            for d in raw.get("paired_devices") or ()
        ),
    )


def _cadence(kind: str, interval_seconds: int | None) -> str:
    if kind == "recurring" and interval_seconds is not None:
        return f"interval:{interval_seconds}"
    return kind or "once"


def _schedule_summary(record, *, cv: int) -> ScheduleSummary:
    return ScheduleSummary(
        schedule_id=record.schedule_id,
        cv=cv,
        session_id=record.app_session_id,
        prompt_preview=record.prompt[:200],
        cadence=_cadence(record.kind, record.interval_seconds),
        next_run_at=record.fire_at,
        last_run_at=record.last_fired_at,
        # No disabled/paused state exists in the legacy schedule store
        # (create/list/delete only — see system_commands.py's module
        # docstring) — every schedule that still exists is, structurally,
        # enabled; this is a real fact about the domain, not a fabricated
        # default.
        enabled=True,
    )


def _startup_task(record, *, cv: int) -> HostStartupTask:
    return HostStartupTask(
        id=record.id,
        cv=cv,
        label=record.label,
        state=HostStartupTaskState(record.state),
        started_at=record.started_at,
        finished_at=record.finished_at,
    )


_CAPABILITY_DISPLAY = {"mobile": "Mobile", "integrations": "Integrations"}


def _installation_capability(record, *, cv: int) -> InstallationCapability:
    return InstallationCapability(
        capability_id=record.capability_id,
        cv=cv,
        enabled=record.enabled,
        # gap: installation_capabilities.py carries no copy/display string
        # of its own — the small closed TOGGLEABLE set (mobile/
        # integrations) is given a minimal static label here rather than
        # left as the raw id, falling back to the id itself for any
        # future capability this map doesn't yet know about.
        display=_CAPABILITY_DISPLAY.get(record.capability_id, record.capability_id),
        provisioned=record.provisioned,
        active=record.active,
        restart_required=record.restart_required,
        self_provisionable=record.self_provisionable,
        in_app_restart_supported=record.in_app_restart_supported,
    )


def _node_descriptor(record, *, cv: int) -> NodeDescriptor:
    return NodeDescriptor(
        node_id=record.node_id,
        cv=cv,
        role=NodeRole(record.role) if record.role in ("primary", "worker_node") else NodeRole.WORKER_NODE,
        address=record.address,
        cwd_roots=record.cwd_roots,
        state=NodeConnState(record.state) if record.state in ("connected", "disconnected", "unknown") else NodeConnState.UNKNOWN,
        last_seen=record.last_seen,
        connected_at=record.connected_at,
        version_status=(
            NodeVersionStatus(record.version_status)
            if record.version_status in ("ok", "mismatch", "unknown")
            else NodeVersionStatus.UNKNOWN
        ),
        app_commit_sha=record.app_commit_sha,
        app_dirty=record.app_dirty,
        primary_commit_sha=record.primary_commit_sha,
        primary_dirty=record.primary_dirty,
    )


_CREDENTIAL_STATUS_MAP = {
    "pending": NodeProviderCredentialState.PENDING,
    "synced": NodeProviderCredentialState.SYNCED,
    "failed": NodeProviderCredentialState.FAILED,
}


def _node_provider_credential(record, *, cv: int) -> NodeProviderCredentialStatus:
    return NodeProviderCredentialStatus(
        node_id=record.node_id,
        provider_id=record.provider_id,
        cv=cv,
        status=_CREDENTIAL_STATUS_MAP.get(record.status, NodeProviderCredentialState.PENDING),
        authorized_at=record.authorized_at,
        updated_at=record.updated_at,
        failure_code=record.failure_code,
    )


def _pending_registration(record, *, cv: int) -> NodeRegistrationRequest:
    return NodeRegistrationRequest(
        node_id=record.node_id,
        cv=cv,
        request=NodeRegistrationRequestPayload(
            address=record.address, cwd_roots=record.cwd_roots, fingerprint=record.fingerprint,
        ),
        state=NodeRegistrationState.PENDING,
        response=None,
    )


class SystemSurfaceAdapter(SystemSurface):
    def __init__(self) -> None:
        self._projection = BusBoundProjection()
        self._command_port = None
        self._startup_epoch = 0
        # Adapter-local cache of a pending registration's request fields,
        # populated on `node_registration_requested` — the ONLY point the
        # legacy fact payload carries `address`/`cwd_roots`/`fingerprint`;
        # by the time `node_registration_resolved` fires the record is no
        # longer pending-queryable (see store_access.py's
        # list_pending_node_registrations docstring), so this is the one
        # honest way to reconstruct the full resource on resolution.
        self._registration_cache: dict[str, NodeRegistrationRequestPayload] = {}
        self._known_schedule_ids: dict[str | None, set[str]] = {}

    def bind(self) -> None:
        self._projection.bind([
            # "extension.mutated" is the EXISTING fact `extension_api.py`'s
            # `_broadcast_extension_changed` already fires (via
            # `extension_ui_manager.manager.publish_fact`, itself a thin
            # `bus.publish_threadsafe` wrapper — `thread_loop_host.py:171-
            # 195`) for every extension config/catalog/UI mutation — no
            # new fact-emission edit needed at that chokepoint; this
            # adapter taps the one that already exists rather than
            # forking a parallel one. Unconditional (not topic-filtered):
            # every mutation re-derives config+catalog+UI+harness-default
            # freshly from store_access, trading a little extra re-
            # broadcast traffic for zero new legacy edits.
            ("extension.mutated", self._on_extension_mutated),
            ("harness.fire.profile_changed", self._on_harness_profile_changed),
            ("marketplace.fire.changed", self._on_marketplace_changed),
            ("schedule.fire.changed", self._on_schedule_changed),
            ("startup_tasks.fire.changed", self._on_startup_task_changed),
            ("installation.fire.capability_changed", self._on_installation_capability_changed),
            ("machines.fire.node_changed", self._on_node_changed),
            ("machines.fire.credential_changed", self._on_credential_changed),
            ("machines.fire.registration_requested", self._on_registration_requested),
            ("machines.fire.registration_resolved", self._on_registration_resolved),
        ])

    # ---- §2 extension config + harness profiles ---------------------------

    def extension_config(self, extension_id: str) -> ProjectionResult[ExtensionConfigDescriptor]:
        snapshot = self._projection.snapshot()
        record = store_access.get_extension_record(extension_id)
        if record is None:
            return Rebuilding(retry_after_ms=None)
        return Ok(_extension_config_descriptor(record, cv=snapshot.render_rev), snapshot)

    def list_extension_configs(self, cursor: PageCursor | None) -> ProjectionResult[ExtensionConfigPage]:
        snapshot = self._projection.snapshot()
        offset = _decode_offset_cursor(cursor, snapshot)
        if isinstance(offset, StaleCursor):
            return offset
        records = list(store_access.list_extension_records())
        page, next_cursor = _page(records, offset, "system:extension_config", snapshot)
        descriptors = tuple(_extension_config_descriptor(r, cv=snapshot.render_rev) for r in page)
        return Ok(ExtensionConfigPage(descriptors=descriptors, next_cursor=next_cursor), snapshot)

    def list_harness_profiles(self, cursor: PageCursor | None) -> ProjectionResult[HarnessProfilePage]:
        snapshot = self._projection.snapshot()
        offset = _decode_offset_cursor(cursor, snapshot)
        if isinstance(offset, StaleCursor):
            return offset
        records = list(store_access.list_harness_profile_records())
        page, next_cursor = _page(records, offset, "system:harness_profiles", snapshot)
        profiles = tuple(_harness_descriptor(r, cv=snapshot.render_rev) for r in page)
        return Ok(HarnessProfilePage(profiles=profiles, next_cursor=next_cursor), snapshot)

    # ---- §3 extension UI modules -------------------------------------------

    def list_extension_ui(self, cursor: PageCursor | None) -> ProjectionResult[ExtensionUiPage]:
        snapshot = self._projection.snapshot()
        offset = _decode_offset_cursor(cursor, snapshot)
        if isinstance(offset, StaleCursor):
            return offset
        modules: list[ExtensionUiModule] = []
        for hook in store_access.list_extension_ui_hooks():
            kind = (
                ExtensionUiModuleKind.QUICK_BUTTON
                if hook.kind == "quick_button"
                else ExtensionUiModuleKind.PAGE
            )
            modules.append(ExtensionUiModule(
                extension_id=hook.extension_id,
                cv=snapshot.render_rev,
                kind=kind,
                display=ExtensionUiDisplay(label=hook.label, icon=hook.icon),
                action=_hook_action(hook.action),
                placements=hook.placements,
                # gap: `page.badge` is a legacy client-poll-endpoint
                # descriptor (`{endpoint, ...}`), not a backend-computed
                # live count — computing the real count server-side is
                # out of scope for this pass; honestly None rather than a
                # fabricated number (see ADR 0011 §3's own note that this
                # push is meant to REPLACE that poll, which Part 1 does
                # not yet complete).
                badge_count=None,
            ))
        for module in store_access.list_extension_frontend_modules():
            modules.append(ExtensionUiModule(
                extension_id=module.extension_id,
                cv=snapshot.render_rev,
                kind=ExtensionUiModuleKind.FRONTEND_MODULE,
                display=ExtensionUiDisplay(label=module.label, icon=None),
                slot=module.slot,
                module_url=module.module_url,
                payments=module.payments,
                marketplace_auth=module.marketplace_auth,
            ))
        page, next_cursor = _page(modules, offset, "system:extension_ui", snapshot)
        return Ok(ExtensionUiPage(modules=tuple(page), next_cursor=next_cursor), snapshot)

    # ---- §4 extension catalog ----------------------------------------------

    def list_extension_catalog(self, cursor: PageCursor | None) -> ProjectionResult[ExtensionCatalogPage]:
        snapshot = self._projection.snapshot()
        offset = _decode_offset_cursor(cursor, snapshot)
        if isinstance(offset, StaleCursor):
            return offset
        updating = store_access.extension_updates_available_ids()
        records = list(store_access.list_extension_records())
        page, next_cursor = _page(records, offset, "system:extension_catalog", snapshot)
        entries = tuple(
            _extension_catalog_entry(
                r, update_available=r.extension_id in updating, cv=snapshot.render_rev,
            )
            for r in page
        )
        return Ok(ExtensionCatalogPage(entries=entries, next_cursor=next_cursor), snapshot)

    # ---- §5 marketplace bridge ----------------------------------------------

    def marketplace_bridge_state(self) -> ProjectionResult[MarketplaceBridgeState]:
        snapshot = self._projection.snapshot()
        raw = store_access.marketplace_bridge_snapshot()
        return Ok(_marketplace_bridge_state(raw, cv=snapshot.render_rev), snapshot)

    def list_marketplace_intents(self, cursor: PageCursor | None) -> ProjectionResult[MarketplaceIntentPage]:
        snapshot = self._projection.snapshot()
        offset = _decode_offset_cursor(cursor, snapshot)
        if isinstance(offset, StaleCursor):
            return offset
        raw = store_access.marketplace_bridge_snapshot()
        intents = list(raw.get("intents") or ())
        page, next_cursor = _page(intents, offset, "system:marketplace_intents", snapshot)
        mapped = tuple(_marketplace_intent(i, cv=snapshot.render_rev) for i in page)
        return Ok(MarketplaceIntentPage(intents=mapped, next_cursor=next_cursor), snapshot)

    # ---- §6 schedules ---------------------------------------------------------

    def list_schedules(
        self, session_id: SessionId | None, cursor: PageCursor | None
    ) -> ProjectionResult[SchedulePage]:
        snapshot = self._projection.snapshot()
        offset = _decode_offset_cursor(cursor, snapshot)
        if isinstance(offset, StaleCursor):
            return offset
        records = list(store_access.list_schedule_records(session_id))
        page, next_cursor = _page(records, offset, "system:schedules", snapshot)
        schedules = tuple(_schedule_summary(r, cv=snapshot.render_rev) for r in page)
        return Ok(SchedulePage(schedules=schedules, next_cursor=next_cursor), snapshot)

    # ---- §7 host startup tasks ---------------------------------------------

    def host_startup_tasks(self) -> ProjectionResult[HostStartupTaskSnapshot]:
        snapshot = self._projection.snapshot()
        records = store_access.list_host_startup_tasks()
        tasks = tuple(_startup_task(r, cv=snapshot.render_rev) for r in records)
        return Ok(HostStartupTaskSnapshot(tasks=tasks, epoch=self._startup_epoch), snapshot)

    # ---- §8 installation capabilities ---------------------------------------

    def list_installation_capabilities(self) -> ProjectionResult[tuple[InstallationCapability, ...]]:
        snapshot = self._projection.snapshot()
        records = store_access.list_installation_capability_records()
        return Ok(
            tuple(_installation_capability(r, cv=snapshot.render_rev) for r in records), snapshot,
        )

    # ---- §9 machine/node topology -------------------------------------------

    def list_machines(self, cursor: PageCursor | None) -> ProjectionResult[MachinePage]:
        snapshot = self._projection.snapshot()
        offset = _decode_offset_cursor(cursor, snapshot)
        if isinstance(offset, StaleCursor):
            return offset
        records = list(store_access.list_node_records())
        page, next_cursor = _page(records, offset, "system:machines", snapshot)
        nodes = tuple(_node_descriptor(r, cv=snapshot.render_rev) for r in page)
        return Ok(MachinePage(nodes=nodes, next_cursor=next_cursor), snapshot)

    def node_provider_credentials(
        self, node_id: NodeId
    ) -> ProjectionResult[tuple[NodeProviderCredentialStatus, ...]]:
        snapshot = self._projection.snapshot()
        records = store_access.node_provider_credential_records(node_id)
        return Ok(
            tuple(_node_provider_credential(r, cv=snapshot.render_rev) for r in records), snapshot,
        )

    def list_node_registrations(self, cursor: PageCursor | None) -> ProjectionResult[NodeRegistrationPage]:
        snapshot = self._projection.snapshot()
        offset = _decode_offset_cursor(cursor, snapshot)
        if isinstance(offset, StaleCursor):
            return offset
        # gap: pending-only (see store_access.list_pending_node_registrations
        # docstring) — resolved/cancelled requests are not list-queryable
        # in the legacy store, so this cold-hydration page only ever shows
        # PENDING entries; a resolved entry surfaces exclusively via the
        # live `node_registration_upsert` push.
        records = list(store_access.list_pending_node_registrations())
        page, next_cursor = _page(records, offset, "system:node_registrations", snapshot)
        requests = tuple(_pending_registration(r, cv=snapshot.render_rev) for r in page)
        return Ok(NodeRegistrationPage(requests=requests, next_cursor=next_cursor), snapshot)

    # ---- live plane -----------------------------------------------------------

    def subscribe(self, emit: Emit) -> Subscription:
        self._projection.register(emit)
        return _SubscriptionImpl(lambda: self._projection.unregister(emit))

    # ---- command plane ----------------------------------------------------

    def submit(self, intent: SystemIntent) -> TransportAck:
        port = self._command_port
        if port is None:
            return IntentRejected(
                intent_id=intent.intent_id, code="unsupported_contract_phase",
                message="system command port is not wired",
            )
        if isinstance(intent, UpdateExtensionConfig):
            coro = port.update_extension_config(intent.extension_id, intent.section, intent.patch)
        elif isinstance(intent, SaveHarnessProfile):
            coro = port.save_harness_profile(
                intent.harness_profile_id, intent.config, intent.revision, intent.writes,
            )
        elif isinstance(intent, DeleteHarnessProfile):
            coro = port.delete_harness_profile(intent.harness_profile_id, intent.revision)
        elif isinstance(intent, SetDefaultHarnessProfile):
            coro = port.set_default_harness_profile(intent.harness_profile_id)
        elif isinstance(intent, InstallExtension):
            coro = port.install_extension(intent.extension_id, intent.source)
        elif isinstance(intent, UpdateExtension):
            coro = port.update_extension(intent.extension_id)
        elif isinstance(intent, UninstallExtension):
            coro = port.uninstall_extension(intent.extension_id)
        elif isinstance(intent, EnableExtension):
            coro = port.enable_extension(intent.extension_id)
        elif isinstance(intent, DisableExtension):
            coro = port.disable_extension(intent.extension_id)
        elif isinstance(intent, DecideMarketplaceIntent):
            coro = port.decide_marketplace_intent(intent.marketplace_intent_id, intent.decision.value)
        elif isinstance(intent, RevokeMarketplaceDevice):
            coro = port.revoke_marketplace_device(intent.device_ref)
        elif isinstance(intent, CreateSchedule):
            coro = port.create_schedule(intent.target_session_id, intent.prompt, intent.cadence)
        elif isinstance(intent, DeleteSchedule):
            coro = port.delete_schedule(intent.schedule_id)
        elif isinstance(intent, SetInstallationCapability):
            coro = port.set_installation_capability(
                intent.capability_id, intent.enabled, intent.confirm_cancels_extension_work,
            )
        elif isinstance(intent, RemoveNode):
            coro = port.remove_node(intent.node_id)
        elif isinstance(intent, SyncNodeProviders):
            coro = port.sync_node_providers(
                intent.node_id, intent.include_secrets, intent.provider_ids,
            )
        elif isinstance(intent, ResolveNodeRegistration):
            coro = port.resolve_node_registration(intent.node_id, intent.decision.value)
        else:
            return IntentRejected(
                intent_id=intent.intent_id, code="unsupported", message="intent not supported",
            )
        import asyncio

        asyncio.get_running_loop().create_task(self._run_command(coro, intent))
        return IntentAccepted(intent_id=intent.intent_id)

    def reject_intent(self, intent_id: str, code: str, message: str) -> None:
        """Push an async `IntentRejected` over the live plane (ADR 0006 §5
        "acks are projection facts") — mirrors `SessionSurfaceAdapter.
        reject_intent` exactly: `submit()` always admits synchronously and
        only discovers a business-logic failure (e.g. a stale-revision
        harness-profile write) once its fire-and-forget coroutine actually
        runs."""
        self._projection.broadcast(IntentRejected(intent_id=intent_id, code=code, message=message))

    async def _run_command(self, coro, intent: SystemIntent) -> None:
        """Await a fire-and-forget `SystemCommandPort` coroutine and turn
        its outcome into a live-plane fact — mirrors `SessionSurfaceAdapter.
        _run_command` exactly (see that method's docstring for the full
        rationale). Previously this task's result was discarded entirely
        (every `SystemCommandPort` rejection — e.g. a stale harness-profile
        revision — was silently swallowed); every system intent now gets
        the same async-reject behavior `SessionSurfaceAdapter`/
        `ProviderConfigSurfaceAdapter` already have.

        A `SaveHarnessProfile` CREATE (`harness_profile_id=None`) that
        produced a server-generated id (`CommandResult.ref`) additionally
        gets an intent_id-stamped `HarnessProfileUpsert` forced out for
        exactly that profile, so `HarnessSettingsEditor.tsx`'s create flow
        (no synchronous RPC result) can correlate on the live frame instead
        — same idiom as `SessionSurfaceAdapter`'s `CreateFolder`/
        `CreateTag`."""
        try:
            result = await coro
        except Exception:
            logger.exception(
                "system intent %s (%s) raised", type(intent).__name__, intent.intent_id,
            )
            self.reject_intent(intent.intent_id, "internal_error", "the command raised")
            return
        if not result.accepted:
            logger.info(
                "system intent %s (%s) rejected: %s %s",
                type(intent).__name__, intent.intent_id, result.code, result.message,
            )
            self.reject_intent(intent.intent_id, result.code, result.message)
            return
        if not result.ref or not isinstance(intent, SaveHarnessProfile) or intent.harness_profile_id:
            return
        records = store_access.list_harness_profile_records()
        record = next((r for r in records if r.harness_profile_id == result.ref), None)
        if record is None:
            return
        cv = self._projection.bump_render()
        self._projection.broadcast(HarnessProfileUpsert(
            cv=cv, profile=_harness_descriptor(record, cv=cv), intent_id=intent.intent_id,
        ))

    # ---- internals: live fact handlers -------------------------------------

    async def _on_extension_mutated(self, event: BusEvent) -> None:
        """One extension mutated (install/uninstall/enable/config/
        catalog/UI-toggle — `_broadcast_extension_changed`'s callers span
        all of §2/§3/§4) — re-derive the four things that can honestly
        have moved: this extension's `ExtensionConfigDescriptor`, its
        `ExtensionCatalogEntry`, the whole `ExtensionUiModule` catalog
        (cheap change-only signal, mirrors ADR 0007's
        `InstallableCatalogChanged{cv}` no-payload idiom), and the
        `default` `HarnessProfileDescriptor` (its `disabled_builtin_*`
        can move via `extension.harness.default.disabled_builtin_*`,
        which also routes through this same chokepoint)."""
        extension_id = str(event.payload.get("extension_id") or "")
        if extension_id:
            record = store_access.get_extension_record(extension_id)
            if record is not None:
                cv = self._projection.bump_render()
                self._projection.broadcast(ExtensionConfigUpsert(
                    cv=cv, extension_id=extension_id, section="",
                ))
                updating = store_access.extension_updates_available_ids()
                cv = self._projection.bump_render()
                self._projection.broadcast(ExtensionCatalogUpsert(
                    cv=cv,
                    entry=_extension_catalog_entry(
                        record, update_available=record.extension_id in updating, cv=cv,
                    ),
                ))
        cv = self._projection.bump_render()
        self._projection.broadcast(ExtensionUiChanged(cv=cv))
        default_record = next(
            (r for r in store_access.list_harness_profile_records() if r.is_default), None,
        )
        if default_record is not None:
            cv = self._projection.bump_render()
            self._projection.broadcast(HarnessProfileUpsert(
                cv=cv, profile=_harness_descriptor(default_record, cv=cv),
            ))

    async def _on_harness_profile_changed(self, event: BusEvent) -> None:
        profile_id = str(event.payload.get("profile_id") or "")
        records = store_access.list_harness_profile_records()
        record = next((r for r in records if r.harness_profile_id == profile_id), None)
        if record is None:
            return
        cv = self._projection.bump_render()
        self._projection.broadcast(HarnessProfileUpsert(cv=cv, profile=_harness_descriptor(record, cv=cv)))

    async def _on_marketplace_changed(self, event: BusEvent) -> None:
        raw = store_access.marketplace_bridge_snapshot()
        cv = self._projection.bump_render()
        self._projection.broadcast(MarketplaceBridgeStateChanged(
            cv=cv, state=_marketplace_bridge_state(raw, cv=cv),
        ))
        for intent_raw in raw.get("intents") or ():
            cv = self._projection.bump_render()
            self._projection.broadcast(MarketplaceIntentUpsert(
                cv=cv, intent=_marketplace_intent(intent_raw, cv=cv),
            ))

    async def _on_schedule_changed(self, event: BusEvent) -> None:
        app_session_id = event.payload.get("app_session_id")
        records = store_access.list_schedule_records(app_session_id if isinstance(app_session_id, str) else None)
        current_ids = {r.schedule_id for r in records}
        prior_ids = self._known_schedule_ids.get(app_session_id, set())
        for record in records:
            cv = self._projection.bump_render()
            self._projection.broadcast(ScheduleUpsert(cv=cv, schedule=_schedule_summary(record, cv=cv)))
        for removed_id in prior_ids - current_ids:
            cv = self._projection.bump_render()
            self._projection.broadcast(ScheduleRemoved(cv=cv, schedule_id=removed_id))
        self._known_schedule_ids[app_session_id] = current_ids

    async def _on_startup_task_changed(self, event: BusEvent) -> None:
        if event.payload.get("cleared"):
            self._startup_epoch += 1
            cv = self._projection.bump_render()
            self._projection.broadcast(HostStartupTaskCleared(cv=cv, epoch=self._startup_epoch))
            return
        task_id = str((event.payload.get("task") or {}).get("id") or "")
        records = store_access.list_host_startup_tasks()
        record = next((r for r in records if r.id == task_id), None)
        if record is None:
            return
        cv = self._projection.bump_render()
        self._projection.broadcast(HostStartupTaskUpsert(cv=cv, task=_startup_task(record, cv=cv)))

    async def _on_installation_capability_changed(self, event: BusEvent) -> None:
        capability_id = str(event.payload.get("capability_id") or "")
        records = store_access.list_installation_capability_records()
        record = next((r for r in records if r.capability_id == capability_id), None)
        if record is None:
            return
        cv = self._projection.bump_render()
        self._projection.broadcast(InstallationCapabilityChanged(
            cv=cv, capability=_installation_capability(record, cv=cv),
        ))

    async def _on_node_changed(self, event: BusEvent) -> None:
        node_id = str(event.payload.get("node_id") or "")
        records = store_access.list_node_records()
        record = next((r for r in records if r.node_id == node_id), None)
        cv = self._projection.bump_render()
        if record is None:
            self._projection.broadcast(NodeRemoved(cv=cv, node_id=node_id))
            return
        self._projection.broadcast(MachineNodeUpsert(cv=cv, node=_node_descriptor(record, cv=cv)))

    async def _on_credential_changed(self, event: BusEvent) -> None:
        node_id = str(event.payload.get("node_id") or "")
        for record in store_access.node_provider_credential_records(node_id):
            cv = self._projection.bump_render()
            self._projection.broadcast(NodeProviderCredentialUpsert(
                cv=cv, status=_node_provider_credential(record, cv=cv),
            ))

    async def _on_registration_requested(self, event: BusEvent) -> None:
        node_id = str(event.payload.get("node_id") or "")
        if not node_id:
            return
        payload = NodeRegistrationRequestPayload(
            address=str(event.payload.get("address") or ""),
            cwd_roots=tuple(event.payload.get("cwd_roots") or ()),
            fingerprint=str(event.payload.get("fingerprint") or ""),
        )
        self._registration_cache[node_id] = payload
        cv = self._projection.bump_render()
        self._projection.broadcast(NodeRegistrationUpsert(
            cv=cv, node_id=node_id,
            request=NodeRegistrationRequest(
                node_id=node_id, cv=cv, request=payload,
                state=NodeRegistrationState.PENDING, response=None,
            ),
        ))

    async def _on_registration_resolved(self, event: BusEvent) -> None:
        node_id = str(event.payload.get("node_id") or "")
        status = str(event.payload.get("status") or "")
        if not node_id:
            return
        # gap: `node_registration_resolved`'s legacy fact payload carries
        # only {node_id, status} — the full request fields (address/
        # cwd_roots/fingerprint) are reconstructed from this adapter's own
        # `_registration_cache`, populated on the earlier `_requested`
        # fact; a resolution observed with no matching prior `_requested`
        # fact in THIS process's lifetime (e.g. adapter restarted mid-
        # flight) honestly reports empty request fields rather than
        # guessing.
        payload = self._registration_cache.pop(node_id, NodeRegistrationRequestPayload(
            address="", cwd_roots=(), fingerprint="",
        ))
        decision = (
            NodeRegistrationDecision.APPROVED
            if status == "approved"
            else NodeRegistrationDecision.DENIED
        )
        cv = self._projection.bump_render()
        self._projection.broadcast(NodeRegistrationUpsert(
            cv=cv, node_id=node_id,
            request=NodeRegistrationRequest(
                node_id=node_id, cv=cv, request=payload,
                state=NodeRegistrationState.RESOLVED,
                response=NodeRegistrationResponse(decision=decision),
            ),
        ))
