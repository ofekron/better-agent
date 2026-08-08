"""Versioned transport for the Chat Surface Contract (ADR 0006 §2/§4/§5).

The composition root (`backend/main.py`) is the only importer, per the
boundary enforced by `backend/scripts/test_adapter_boundaries.py`. Auth
mirrors the existing REST/WS surfaces exactly: REST routes ride the global
`/api/*` `auth_gate` middleware (no per-route dependency needed — this
router mounts under `app` like every other REST router), and the WS route
duplicates the session-cookie/bearer-token gate `ws_chat.websocket_chat`
uses, since a WS upgrade is not covered by the HTTP middleware.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

import auth
import browser_trust
import file_delivery
import perf
import providers_api
import runtime_profiles_api
from hot_path_executor import surface_read_path
from provider_validation import is_loopback_request
from session_detail_api import resolve_session_image_path
from backend.adapters.serialize import to_wire
from backend.surface_contract.adapter import BetterAgentAdapter
from backend.surface_contract.chat_surface import ChatSurface
from backend.surface_contract.identity import (
    CONTRACT_VERSION,
    Focus,
    Ok,
    PageCursor,
    Rebuilding,
    SnapshotIdentity,
    StaleCursor,
    SurfaceCursor,
)
from backend.surface_contract.intents import (
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
    CreateTag,
    DeleteFolder,
    DeleteProject,
    DeleteProvider,
    DeleteQueued,
    DeleteRuntimeProfile,
    DeleteTag,
    EditQueued,
    FolderDeleteMode,
    InputResponse,
    IntentRejected,
    MarkOpened,
    MoveFolder,
    ProviderIntent,
    RecolorTag,
    RefreshModels,
    RenameFolder,
    RenameProject,
    RenameSession,
    RenameTag,
    ResolveInteraction,
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
from backend.surface_contract.intents import (
    CreateSchedule,
    DecideMarketplaceIntent,
    DeleteHarnessProfile,
    DeleteSchedule,
    DisableExtension,
    EnableExtension,
    InstallExtension,
    MarketplaceDecision,
    NodeRegistrationDecisionValue,
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
from backend.surface_contract.nodes import ApprovalDecision, Attachment
from backend.surface_contract.provider_config_surface import ProviderConfigSurface
from backend.surface_contract.runs_surface import RunsSurface
from backend.surface_contract.session_surface import SessionSearchFilters, SessionSurface
from backend.surface_contract.system_surface import (
    ExtensionCatalogUpsert,
    ExtensionConfigUpsert,
    ExtensionSignal,
    ExtensionToast,
    ExtensionUiChanged,
    HarnessDefaultChanged,
    HarnessProfileUpsert,
    HostStartupTaskCleared,
    HostStartupTaskUpsert,
    InstallationCapabilityChanged,
    MachineNodeUpsert,
    MarketplaceBridgeStateChanged,
    MarketplaceIntentUpsert,
    NodeProviderCredentialUpsert,
    NodeRegistrationUpsert,
    NodeRemoved,
    ScheduleRemoved,
    ScheduleUpsert,
    SystemSurface,
)
from backend.ws_outbox import WebSocketOutbox

logger = logging.getLogger(__name__)

# No router-level prefix: the WebSocket route (/ws/v2/surface) sits outside
# the /api/v2/surface REST prefix, same split as ws_chat.router (/ws/chat
# alongside main.py's /api/* routers) — a shared prefix would wrongly nest it
# under /api/v2/surface/ws/v2/surface.
router = APIRouter()
_REST_PREFIX = "/api/v2/surface"

chat: ChatSurface | None = None
sessions: SessionSurface | None = None
providers: ProviderConfigSurface | None = None
runs: RunsSurface | None = None
system: SystemSurface | None = None


class ProviderCommandPort:
    """Thin async wrappers routing ADR 0007 `ProviderIntent` submissions to
    `providers_api.py`'s/`runtime_profiles_api.py`'s existing mutation
    functions — the single source of truth for provider mutation logic
    (validation, config_store writes, D1 fact emission; see those two
    modules' `_*` core functions). This module (`backend/adapter_api.py`)
    is exempt from `backend/adapters/*`'s import boundary (see
    `backend/scripts/test_adapter_boundaries.py`'s
    `_external_adapters_import_violations`), so it is the sanctioned
    composition-layer home for a port that needs to call config_store-
    touching functions `provider_adapter.py` itself may not import —
    mirrors `backend/surface_commands.py`'s `ChatCommandPort` split, just
    composed here instead of a dedicated port module (see
    `ProviderConfigSurfaceAdapter`'s module docstring for why).

    Every method catches `HTTPException`/`ValueError` from the underlying
    mutation function and turns it into an async `IntentRejected` pushed
    over the SAME `/ws/v2/surface` connection via `self._surface.
    reject_intent` (`ProviderConfigSurfaceAdapter.submit()` schedules these
    fire-and-forget — acks are projection facts, not a synchronous result —
    ADR 0006 §5 — exactly like `ChatSurfaceAdapter.submit()` does for chat
    intents; unlike chat, providers has no domain-model "failure node" to
    surface the rejection through instead, so the transport's own
    `IntentRejected` vocabulary is reused directly as that compensating
    fact, one ack mechanism, two delivery times)."""

    def __init__(self, surface: ProviderConfigSurface) -> None:
        self._surface = surface

    def _reject(self, intent_id: str, kind: str, exc: Exception) -> None:
        if isinstance(exc, HTTPException):
            code = str(exc.status_code)
            message = str(exc.detail)
        else:
            code = "invalid_config"
            message = str(exc)
        logger.info("%s intent %s rejected: %s %s", kind, intent_id, code, message)
        self._surface.reject_intent(intent_id, code, message)

    async def create_provider(self, intent_id: str, kind: str, config: dict) -> None:
        body = dict(config)
        body["kind"] = kind
        try:
            await providers_api._create_provider(body)
        except (HTTPException, ValueError) as exc:
            self._reject(intent_id, "create_provider", exc)

    async def update_provider(self, intent_id: str, provider_id: str, config_patch: dict) -> None:
        patch = dict(config_patch)
        # UpdateProvider carries no dedicated concurrency-token fields (see
        # providers_api._patch_provider's docstring) — only place a caller
        # could supply them is inside this same dict; None/None (skip the
        # optimistic-concurrency check) if absent, a mode config_store
        # already supports natively.
        expected_generation = patch.pop("expected_generation", None)
        expected_revision = patch.pop("expected_revision", None)
        try:
            await providers_api._patch_provider(
                provider_id, patch,
                expected_generation=expected_generation,
                expected_revision=expected_revision,
            )
        except (HTTPException, ValueError) as exc:
            self._reject(intent_id, "update_provider", exc)

    async def delete_provider(self, intent_id: str, provider_id: str) -> None:
        try:
            await providers_api._delete_provider(
                provider_id, expected_generation=None, expected_revision=None,
            )
        except HTTPException as exc:
            self._reject(intent_id, "delete_provider", exc)

    async def suspend_provider(self, intent_id: str, provider_id: str, suspended: bool) -> None:
        try:
            await providers_api._set_provider_suspended(
                provider_id, suspended, expected_generation=None, expected_revision=None,
            )
        except HTTPException as exc:
            self._reject(intent_id, "suspend_provider", exc)

    async def retry_credential(self, intent_id: str, provider_id: str) -> None:
        try:
            await providers_api._retry_provider_credential(provider_id)
        except HTTPException as exc:
            self._reject(intent_id, "retry_credential", exc)

    async def begin_login(self, intent_id: str, provider_id: str, flow: str) -> None:
        # Loopback-gated once already, at the WS transport layer, before
        # this coroutine was ever scheduled — see `_handle_intent` below,
        # which is why the `_unchecked` core (no request/websocket object
        # left in scope here to re-check against) is the right call.
        try:
            await providers_api._login_provider_unchecked(provider_id, intent_id=intent_id)
        except HTTPException as exc:
            self._reject(intent_id, "begin_login", exc)

    async def cancel_login(self, intent_id: str, provider_id: str) -> None:
        try:
            await providers_api._cancel_provider_login_unchecked(provider_id, intent_id=intent_id)
        except HTTPException as exc:
            self._reject(intent_id, "cancel_login", exc)

    async def refresh_models(self, intent_id: str, provider_id: str) -> None:
        try:
            await providers_api._refresh_provider_models_by_id(provider_id)
        except HTTPException as exc:
            self._reject(intent_id, "refresh_models", exc)

    async def save_runtime_profile(self, intent_id: str, profile: dict) -> None:
        body = dict(profile)
        profile_id = body.pop("runtime_profile_id", None)
        try:
            if profile_id:
                await runtime_profiles_api._patch_runtime_profile(str(profile_id), body)
            else:
                await runtime_profiles_api._create_runtime_profile(body)
        except (HTTPException, ValueError) as exc:
            self._reject(intent_id, "save_runtime_profile", exc)

    async def delete_runtime_profile(self, intent_id: str, runtime_profile_id: str) -> None:
        try:
            await runtime_profiles_api._delete_runtime_profile(runtime_profile_id)
        except HTTPException as exc:
            self._reject(intent_id, "delete_runtime_profile", exc)


def configure(
    adapter: BetterAgentAdapter | None = None,
    *,
    chat: ChatSurface | None = None,
    sessions: SessionSurface | None = None,
    providers: ProviderConfigSurface | None = None,
    runs: RunsSurface | None = None,
    system: SystemSurface | None = None,
) -> None:
    """Wire the module-level surface singletons the routes below dispatch
    to. `configure(composed_adapter)` sets all four at once (the
    composition-root call in `backend/main.py`); the keyword form
    (`configure(chat=...)`) sets individual surfaces directly and stays
    valid standalone — pre-existing callers that only ever wired `chat`
    keep working unchanged.

    D4: also injects a `ProviderCommandPort` onto the provider surface's
    `_command_port` attribute — the same duck-typed injection idiom
    `backend/adapters/__init__.py`'s `build_adapter` already uses for the
    chat surface's `_command_port` (see `ChatSurfaceAdapter.submit()`),
    performed here instead since `backend/adapters/__init__.py` composes
    surfaces before this module's port implementation can exist (that
    factory is inside the adapters import boundary; this port is not)."""
    if adapter is not None:
        chat = chat if chat is not None else adapter.chat
        sessions = sessions if sessions is not None else adapter.sessions
        providers = providers if providers is not None else adapter.providers
        runs = runs if runs is not None else adapter.runs
        system = system if system is not None else adapter.system
    if providers is not None:
        providers._command_port = ProviderCommandPort(providers)
    globals()["chat"] = chat
    globals()["sessions"] = sessions
    globals()["providers"] = providers
    globals()["runs"] = runs
    globals()["system"] = system


# ---- id validation (fail closed: no traversal, no separators, bounded) ----

_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")


def _validate_id(value: str, *, field: str) -> str:
    if not value or ".." in value or not _ID_RE.fullmatch(value):
        raise HTTPException(status_code=400, detail=f"invalid {field}")
    return value


# ---- opaque older-page cursor (round-trips PageCursor through a URL) ------

def _encode_cursor(cursor: PageCursor) -> str:
    raw = json.dumps(
        {
            "surface_id": cursor.surface_id,
            "token": cursor.token,
            "incarnation": cursor.snapshot.incarnation,
            "render_rev": cursor.snapshot.render_rev,
            "hist_rev": cursor.snapshot.hist_rev,
        },
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_cursor(token: str) -> PageCursor:
    try:
        raw = base64.urlsafe_b64decode(token.encode()).decode()
        data = json.loads(raw)
        return PageCursor(
            surface_id=str(data["surface_id"]),
            snapshot=SnapshotIdentity(
                incarnation=str(data["incarnation"]),
                render_rev=int(data["render_rev"]),
                hist_rev=int(data["hist_rev"]),
            ),
            token=str(data["token"]),
        )
    except Exception:
        raise HTTPException(status_code=400, detail="malformed cursor")


_CURSOR_FIELDS = ("older_cursor", "next_cursor")


def _serialize_with_cursor(value: object) -> object:
    """`to_wire` plus: any `PageCursor | None` field named in
    `_CURSOR_FIELDS` becomes the opaque string `?cursor=` expects,
    instead of a nested object."""
    body = to_wire(value)
    if isinstance(body, dict):
        for field in _CURSOR_FIELDS:
            if field not in body:
                continue
            cursor = getattr(value, field, None)
            body[field] = _encode_cursor(cursor) if cursor is not None else None
    return body


# ---- ProjectionResult -> stable envelope (typed states, never a 500) ------

def _result_body(result: object) -> dict:
    if isinstance(result, Ok):
        body = _serialize_with_cursor(result.value)
        if not isinstance(body, dict):
            body = {"value": body}
        body["kind"] = "ok"
        body["snapshot_identity"] = to_wire(result.snapshot)
        return body
    if isinstance(result, Rebuilding):
        return {"kind": "rebuilding", "retry_after_ms": result.retry_after_ms}
    if isinstance(result, StaleCursor):
        return {"kind": "stale_cursor"}
    raise AssertionError(f"unhandled ProjectionResult variant: {result!r}")


# ---- plain (non-ProjectionResult) surface reads -> same envelope shape ----
# ProviderConfigSurface methods return bare values (ADR 0007 has no
# rebuilding/stale-cursor state to project) — wrap them the same way
# `_result_body`'s Ok branch does, so every /api/v2/surface/* response
# shares one envelope shape.

def _envelope(value: object) -> dict:
    body = to_wire(value)
    if not isinstance(body, dict):
        body = {"value": body}
    body["kind"] = "ok"
    return body


def _require_chat() -> ChatSurface:
    if chat is None:
        raise HTTPException(status_code=503, detail="surface adapter not wired")
    return chat


def _require_sessions() -> SessionSurface:
    if sessions is None:
        raise HTTPException(status_code=503, detail="surface adapter not wired")
    return sessions


def _require_providers() -> ProviderConfigSurface:
    if providers is None:
        raise HTTPException(status_code=503, detail="surface adapter not wired")
    return providers


def _require_runs() -> RunsSurface:
    if runs is None:
        raise HTTPException(status_code=503, detail="surface adapter not wired")
    return runs


def _require_system() -> SystemSurface:
    if system is None:
        raise HTTPException(status_code=503, detail="surface adapter not wired")
    return system


# ---- REST read plane -------------------------------------------------

@router.get(f"{_REST_PREFIX}/sessions/{{session_id}}/snapshot")
async def get_snapshot(session_id: str) -> JSONResponse:
    session_id = _validate_id(session_id, field="session_id")
    # Chat reads stay ON the loop: chat_index shares one sqlite
    # connection per surface with the loop-side turn fold, and the
    # compact fast paths are O(window) by design.
    result = _require_chat().open_session(session_id)
    return JSONResponse(_result_body(result))


@router.get(f"{_REST_PREFIX}/sessions/{{session_id}}/nodes/{{node_id}}/children")
async def get_children(session_id: str, node_id: str, at_render_rev: int) -> JSONResponse:
    session_id = _validate_id(session_id, field="session_id")
    node_id = _validate_id(node_id, field="node_id")
    result = _require_chat().children(session_id, node_id, at_render_rev)
    return JSONResponse(_result_body(result))


@router.get(f"{_REST_PREFIX}/sessions/{{session_id}}/older")
async def get_older(session_id: str, cursor: str) -> JSONResponse:
    session_id = _validate_id(session_id, field="session_id")
    page_cursor = _decode_cursor(cursor)
    if page_cursor.surface_id != session_id:
        raise HTTPException(status_code=400, detail="cursor session mismatch")
    result = _require_chat().older(page_cursor)
    return JSONResponse(_result_body(result))


@router.get(f"{_REST_PREFIX}/sessions/{{session_id}}/search")
async def get_search(session_id: str, q: str = "") -> JSONResponse:
    session_id = _validate_id(session_id, field="session_id")
    result = _require_chat().search(session_id, q)
    return JSONResponse(_result_body(result))


@router.get(f"{_REST_PREFIX}/sessions/{{session_id}}/attachments/{{ref}}")
async def get_attachment(session_id: str, ref: str) -> FileResponse:
    """Streams one `Attachment.ref`'s bytes, confined to `session_id`'s own
    blob directory — the SAME storage helper (and fail-closed traversal
    confinement) `session_detail_api.get_session_image` uses, so a ref
    from a different session or a malformed ref both 4xx exactly like that
    route does; no separate resolve/validation path to drift out of sync."""
    session_id = _validate_id(session_id, field="session_id")
    ref = _validate_id(ref, field="ref")
    path = resolve_session_image_path(session_id, ref)
    if not await file_delivery.host.exists(path):
        raise HTTPException(status_code=404, detail="attachment not found")
    return FileResponse(path)


# ---- REST read plane: session/project/provider/runs surfaces -------------

@router.get(f"{_REST_PREFIX}/sessions")
async def list_sessions(
    cursor: str | None = None,
    q: str | None = None,
    folder_ref: str | None = None,
    tag_ref: list[str] = Query(default=[]),
    tag_match: str = "all",
) -> JSONResponse:
    page_cursor = _decode_cursor(cursor) if cursor else None
    # One query path (ADR 0008) — folder_ref/tag_ref/tag_match ride the
    # same `/sessions` read `q` already uses, never a parallel filtered-
    # list route.
    filters = None
    if folder_ref or tag_ref:
        filters = SessionSearchFilters(
            folder_ref=folder_ref, tag_refs=tuple(tag_ref), tag_match=tag_match,
        )
    surface = _require_sessions()
    result = await surface_read_path.run("surface.read.list_sessions", surface.list_sessions, page_cursor, q, filters)
    return JSONResponse(_result_body(result))


@router.get(f"{_REST_PREFIX}/projects")
async def list_projects() -> JSONResponse:
    surface = _require_sessions()
    result = await surface_read_path.run("surface.read.projects", surface.projects)
    return JSONResponse(_result_body(result))


@router.get(f"{_REST_PREFIX}/folders")
async def list_folders(project_ref: str | None = None) -> JSONResponse:
    surface = _require_sessions()
    result = await surface_read_path.run("surface.read.list_folders", surface.list_folders, project_ref)
    return JSONResponse(_result_body(result))


@router.get(f"{_REST_PREFIX}/tags")
async def list_tags(project_ref: str | None = None) -> JSONResponse:
    surface = _require_sessions()
    result = await surface_read_path.run("surface.read.list_tags", surface.list_tags, project_ref)
    return JSONResponse(_result_body(result))


@router.get(f"{_REST_PREFIX}/providers")
async def list_providers() -> JSONResponse:
    return JSONResponse(_envelope(_require_providers().list_providers()))


@router.get(f"{_REST_PREFIX}/providers/installable")
async def list_installable_providers() -> JSONResponse:
    """D2: the backend-owned catalog of provider kinds that CAN be
    configured (ADR 0007's `InstallableDescriptor` list) — replaces the
    frontend's static `InstallableProviderKind` union + template list."""
    return JSONResponse(_envelope(_require_providers().installable_catalog()))


@router.get(f"{_REST_PREFIX}/providers/{{provider_id}}/models")
async def get_provider_models(provider_id: str) -> JSONResponse:
    provider_id = _validate_id(provider_id, field="provider_id")
    surface = _require_providers()
    catalog = await surface_read_path.run("surface.read.model_catalog", surface.model_catalog, provider_id)
    return JSONResponse(_envelope(catalog))


@router.get(f"{_REST_PREFIX}/runtime-profiles")
async def list_runtime_profiles() -> JSONResponse:
    surface = _require_providers()
    profiles = await surface_read_path.run("surface.read.runtime_profiles", surface.runtime_profiles)
    return JSONResponse(_envelope(profiles))


@router.get(f"{_REST_PREFIX}/runs")
async def list_runs(session_id: str | None = None, cursor: str | None = None) -> JSONResponse:
    if session_id is not None:
        session_id = _validate_id(session_id, field="session_id")
    page_cursor = _decode_cursor(cursor) if cursor else None
    runs = _require_runs()
    result = await asyncio.to_thread(runs.list_runs, session_id, page_cursor)
    return JSONResponse(_result_body(result))


@router.get(f"{_REST_PREFIX}/runs/{{run_id}}")
async def get_run_detail(run_id: str) -> JSONResponse:
    run_id = _validate_id(run_id, field="run_id")
    runs = _require_runs()
    result = await asyncio.to_thread(runs.run_detail, run_id)
    return JSONResponse(_result_body(result))


# ---- REST read plane: system surface (ADR 0011) ---------------------------

@router.get(f"{_REST_PREFIX}/extension-config/{{extension_id}}")
async def get_extension_config(extension_id: str) -> JSONResponse:
    extension_id = _validate_id(extension_id, field="extension_id")
    surface = _require_system()
    result = await surface_read_path.run("surface.read.extension_config", surface.extension_config, extension_id)
    return JSONResponse(_result_body(result))


@router.get(f"{_REST_PREFIX}/extension-config")
async def list_extension_configs(cursor: str | None = None) -> JSONResponse:
    page_cursor = _decode_cursor(cursor) if cursor else None
    surface = _require_system()
    result = await surface_read_path.run("surface.read.list_extension_configs", surface.list_extension_configs, page_cursor)
    return JSONResponse(_result_body(result))


@router.get(f"{_REST_PREFIX}/harness-profiles")
async def list_harness_profiles(cursor: str | None = None) -> JSONResponse:
    page_cursor = _decode_cursor(cursor) if cursor else None
    surface = _require_system()
    result = await surface_read_path.run("surface.read.list_harness_profiles", surface.list_harness_profiles, page_cursor)
    return JSONResponse(_result_body(result))


@router.get(f"{_REST_PREFIX}/extension-ui")
async def list_extension_ui(cursor: str | None = None) -> JSONResponse:
    page_cursor = _decode_cursor(cursor) if cursor else None
    surface = _require_system()
    result = await surface_read_path.run("surface.read.list_extension_ui", surface.list_extension_ui, page_cursor)
    return JSONResponse(_result_body(result))


@router.get(f"{_REST_PREFIX}/extension-catalog")
async def list_extension_catalog(cursor: str | None = None) -> JSONResponse:
    page_cursor = _decode_cursor(cursor) if cursor else None
    surface = _require_system()
    result = await surface_read_path.run("surface.read.list_extension_catalog", surface.list_extension_catalog, page_cursor)
    return JSONResponse(_result_body(result))


@router.get(f"{_REST_PREFIX}/marketplace-bridge")
async def get_marketplace_bridge() -> JSONResponse:
    surface = _require_system()
    result = await surface_read_path.run("surface.read.marketplace_bridge_state", surface.marketplace_bridge_state)
    return JSONResponse(_result_body(result))


@router.get(f"{_REST_PREFIX}/marketplace-intents")
async def list_marketplace_intents(cursor: str | None = None) -> JSONResponse:
    page_cursor = _decode_cursor(cursor) if cursor else None
    surface = _require_system()
    result = await surface_read_path.run("surface.read.list_marketplace_intents", surface.list_marketplace_intents, page_cursor)
    return JSONResponse(_result_body(result))


@router.get(f"{_REST_PREFIX}/schedules")
async def list_schedules(session_id: str | None = None, cursor: str | None = None) -> JSONResponse:
    if session_id is not None:
        session_id = _validate_id(session_id, field="session_id")
    page_cursor = _decode_cursor(cursor) if cursor else None
    surface = _require_system()
    result = await surface_read_path.run("surface.read.list_schedules", surface.list_schedules, session_id, page_cursor)
    return JSONResponse(_result_body(result))


@router.get(f"{_REST_PREFIX}/host-startup-tasks")
async def get_host_startup_tasks() -> JSONResponse:
    surface = _require_system()
    result = await surface_read_path.run("surface.read.host_startup_tasks", surface.host_startup_tasks)
    return JSONResponse(_result_body(result))


@router.get(f"{_REST_PREFIX}/installation-capabilities")
async def list_installation_capabilities() -> JSONResponse:
    surface = _require_system()
    result = await surface_read_path.run("surface.read.list_installation_capabilities", surface.list_installation_capabilities)
    return JSONResponse(_result_body(result))


@router.get(f"{_REST_PREFIX}/machines")
async def list_machines(cursor: str | None = None) -> JSONResponse:
    page_cursor = _decode_cursor(cursor) if cursor else None
    surface = _require_system()
    result = await surface_read_path.run("surface.read.list_machines", surface.list_machines, page_cursor)
    return JSONResponse(_result_body(result))


@router.get(f"{_REST_PREFIX}/machines/{{node_id}}/provider-credentials")
async def get_node_provider_credentials(node_id: str) -> JSONResponse:
    node_id = _validate_id(node_id, field="node_id")
    surface = _require_system()
    result = await surface_read_path.run("surface.read.node_provider_credentials", surface.node_provider_credentials, node_id)
    return JSONResponse(_result_body(result))


@router.get(f"{_REST_PREFIX}/node-registrations")
async def list_node_registrations(cursor: str | None = None) -> JSONResponse:
    page_cursor = _decode_cursor(cursor) if cursor else None
    surface = _require_system()
    result = await surface_read_path.run("surface.read.list_node_registrations", surface.list_node_registrations, page_cursor)
    return JSONResponse(_result_body(result))


# ---- intent wire parsing (every surface's submit() routes a well-formed,
# currently-supported intent to its wired command port — see
# `backend/main.py`'s `configure(...)` call for the production wiring;
# `unsupported_contract_phase` is each surface's own not-wired fallback,
# not the default outcome) ---------------------------------------------

def _intent_base(data: dict) -> dict:
    return {
        "cv": int(data.get("cv", CONTRACT_VERSION)),
        "intent_id": str(data["intent_id"]),
        "session_id": data.get("session_id"),
    }


def _parse_send_prompt(data: dict, base: dict) -> SendPrompt:
    target_data = data.get("target") or {}
    target = SendTarget(
        kind=SendTargetKind(target_data.get("kind", SendTargetKind.CURRENT.value)),
        fork_node_id=target_data.get("fork_node_id"),
    )
    attachments = tuple(
        Attachment(name=str(a["name"]), media_type=str(a["media_type"]), ref=str(a["ref"]))
        for a in data.get("attachments") or ()
    )
    return SendPrompt(
        **base,
        text=str(data.get("text", "")),
        attachments=attachments,
        send_mode=SendMode(data.get("send_mode", SendMode.QUEUE.value)),
        target=target,
    )


def _parse_resolve_interaction(data: dict, base: dict) -> ResolveInteraction:
    # "interaction_kind", NOT "kind": this transport's dispatch selector
    # ("resolve") already owns the top-level "kind" key — same rename
    # rationale as ProviderIntent's "provider_kind" (see
    # `_PROVIDER_INTENT_PARSERS`'s "create_provider" entry below). Selects
    # which member of `InteractionResponse` (intents.py) the wire
    # `response` dict parses into.
    interaction_kind = str(data.get("interaction_kind", ""))
    response_data = data.get("response") or {}
    if interaction_kind == "approval":
        response = ApprovalResponse(decision=ApprovalDecision(response_data.get("decision", "")))
    elif interaction_kind == "choice":
        response = ChoiceResponse(picked_ref=response_data.get("picked_ref"))
    elif interaction_kind == "input":
        response = InputResponse(response=dict(response_data.get("response") or {}))
    else:
        raise ValueError(f"unknown interaction_kind {interaction_kind!r}")
    return ResolveInteraction(
        **base, interaction_ref=str(data.get("interaction_ref", "")), response=response,
    )


_INTENT_PARSERS = {
    "send_prompt": _parse_send_prompt,
    "stop": lambda data, base: Stop(**base, turn_id=str(data.get("turn_id", ""))),
    "resolve": _parse_resolve_interaction,
    "edit_queued": lambda data, base: EditQueued(
        **base, node_id=str(data.get("node_id", "")), text=str(data.get("text", "")),
    ),
    "delete_queued": lambda data, base: DeleteQueued(**base, node_id=str(data.get("node_id", ""))),
    "set_selectors": lambda data, base: SetSelectors(
        **base,
        runtime_profile_id=data.get("runtime_profile_id"),
        model=data.get("model"),
        reasoning_effort=data.get("reasoning_effort"),
        permission=data.get("permission"),
        harness_profile_id=data.get("harness_profile_id"),
        orchestration_mode=data.get("orchestration_mode"),
    ),
    "rewind": lambda data, base: Rewind(**base, node_id=str(data.get("node_id", ""))),
}


def _parse_intent(data: dict) -> ChatIntent:
    kind = data.get("kind")
    parser = _INTENT_PARSERS.get(kind)
    if parser is None:
        raise ValueError(f"unknown intent kind {kind!r}")
    return parser(data, _intent_base(data))


# ADR 0008's SessionIntent parser table — same shape as `_INTENT_PARSERS`
# above, kept as a separate table (rather than merged) so this file's two
# intent surfaces can each grow independently without one table's edits
# colliding with the other's. "unarchive_session" and "archive_session"
# both parse to the same `ArchiveSession` dataclass (`archived` carries the
# direction).
_SESSION_INTENT_PARSERS = {
    "archive_session": lambda data, base: ArchiveSession(**base, archived=True),
    "unarchive_session": lambda data, base: ArchiveSession(**base, archived=False),
    "rename_session": lambda data, base: RenameSession(**base, title=str(data.get("title", ""))),
    "assign_project": lambda data, base: AssignProject(**base, project_ref=data.get("project_ref")),
    "create_project": lambda data, base: CreateProject(
        **base, name=str(data.get("name", "")), path=str(data.get("path", "")),
    ),
    "rename_project": lambda data, base: RenameProject(
        **base, project_ref=str(data.get("project_ref", "")), name=str(data.get("name", "")),
    ),
    "delete_project": lambda data, base: DeleteProject(
        **base, project_ref=str(data.get("project_ref", "")),
    ),
    "mark_opened": lambda data, base: MarkOpened(**base),
    "create_folder": lambda data, base: CreateFolder(
        **base,
        project_ref=str(data.get("project_ref", "")),
        name=str(data.get("name", "")),
        parent_folder_ref=data.get("parent_folder_ref"),
    ),
    "rename_folder": lambda data, base: RenameFolder(
        **base, folder_ref=str(data.get("folder_ref", "")), name=str(data.get("name", "")),
    ),
    "move_folder": lambda data, base: MoveFolder(
        **base,
        folder_ref=str(data.get("folder_ref", "")),
        parent_folder_ref=data.get("parent_folder_ref"),
    ),
    "delete_folder": lambda data, base: DeleteFolder(
        **base,
        folder_ref=str(data.get("folder_ref", "")),
        mode=FolderDeleteMode(data.get("mode", FolderDeleteMode.UNASSIGN.value)),
    ),
    "create_tag": lambda data, base: CreateTag(
        **base,
        name=str(data.get("name", "")),
        project_ref=data.get("project_ref"),
        color=data.get("color"),
    ),
    "rename_tag": lambda data, base: RenameTag(
        **base, tag_ref=str(data.get("tag_ref", "")), name=str(data.get("name", "")),
    ),
    "recolor_tag": lambda data, base: RecolorTag(
        **base, tag_ref=str(data.get("tag_ref", "")), color=str(data.get("color", "")),
    ),
    "delete_tag": lambda data, base: DeleteTag(**base, tag_ref=str(data.get("tag_ref", ""))),
    "assign_folder": lambda data, base: AssignFolder(**base, folder_ref=data.get("folder_ref")),
    "assign_tags": lambda data, base: AssignTags(
        **base,
        source=str(data.get("source") or "manual"),
        add_tag_refs=(
            tuple(data["add_tag_refs"]) if data.get("add_tag_refs") is not None else None
        ),
        remove_tag_refs=(
            tuple(data["remove_tag_refs"]) if data.get("remove_tag_refs") is not None else None
        ),
        sync_tag_refs=(
            tuple(data["sync_tag_refs"]) if data.get("sync_tag_refs") is not None else None
        ),
    ),
}


def _parse_session_intent(data: dict) -> SessionIntent:
    kind = data.get("kind")
    parser = _SESSION_INTENT_PARSERS.get(kind)
    if parser is None:
        raise ValueError(f"unknown session intent kind {kind!r}")
    return parser(data, _intent_base(data))


# ADR 0007's ProviderIntent parser table — same shape as `_INTENT_PARSERS`/
# `_SESSION_INTENT_PARSERS` above, kept as its own table so all three
# intent surfaces grow independently without one's edits colliding with
# another's (see this file's module docstring / the caller's task note on
# additive-only edits here). "suspend_provider" and "resume_provider" both
# parse to the same `SuspendProvider` dataclass (`suspended` carries the
# direction), matching ADR 0007's `suspend_provider {…} | resume_provider
# {…}` wire vocabulary exactly. "activate_runtime_profile" has no entry —
# no ADR 0007 intent covers it (see `runtime_profiles_api.py`'s
# `activate_runtime_profile` route for the still-legacy-only gap).
_PROVIDER_INTENT_PARSERS = {
    # Wire key "provider_kind", NOT "kind": `CreateProvider.kind` (the
    # ADR-prescribed dataclass field for the provider's own kind, e.g.
    # "claude") would otherwise collide with this transport's own
    # dispatch-selector key ("kind" == the intent's wire kind, e.g.
    # "create_provider" — see `_handle_intent`/`_parse_provider_intent`
    # below, same top-level "kind" every other intent table dispatches
    # on). No ADR 0007 text mandates an exact JSON key name for this
    # field, only the dataclass shape, so renaming the WIRE key (not the
    # Python field) is the correct, non-breaking fix.
    "create_provider": lambda data, base: CreateProvider(
        **base, kind=str(data.get("provider_kind", "")), config=dict(data.get("config") or {}),
    ),
    "update_provider": lambda data, base: UpdateProvider(
        **base,
        provider_id=str(data.get("provider_id", "")),
        config_patch=dict(data.get("config_patch") or {}),
    ),
    "delete_provider": lambda data, base: DeleteProvider(
        **base, provider_id=str(data.get("provider_id", "")),
    ),
    "suspend_provider": lambda data, base: SuspendProvider(
        **base, provider_id=str(data.get("provider_id", "")), suspended=True,
    ),
    "resume_provider": lambda data, base: SuspendProvider(
        **base, provider_id=str(data.get("provider_id", "")), suspended=False,
    ),
    "retry_credential": lambda data, base: RetryCredential(
        **base, provider_id=str(data.get("provider_id", "")),
    ),
    "begin_login": lambda data, base: BeginLogin(
        **base, provider_id=str(data.get("provider_id", "")), flow=str(data.get("flow", "")),
    ),
    "cancel_login": lambda data, base: CancelLogin(
        **base, provider_id=str(data.get("provider_id", "")),
    ),
    "refresh_models": lambda data, base: RefreshModels(
        **base, provider_id=str(data.get("provider_id", "")),
    ),
    "save_runtime_profile": lambda data, base: SaveRuntimeProfile(
        **base, profile=dict(data.get("profile") or {}),
    ),
    "delete_runtime_profile": lambda data, base: DeleteRuntimeProfile(
        **base, runtime_profile_id=str(data.get("runtime_profile_id", "")),
    ),
}

# `begin_login`/`cancel_login` spawn a server-side OAuth browser flow or
# kill one — the same desktop/loopback-only gate the legacy REST routes
# enforce (`providers_api.py`'s `login_provider`/`cancel_provider_login`)
# must hold for the v2 intent path too. Checked HERE, once, before
# `submit()` is ever called — `ProviderConfigSurfaceAdapter.submit()` has
# no request/websocket object in scope (it only receives the parsed
# intent), so this is the one place in the v2 stack that still has one.
_LOOPBACK_GATED_PROVIDER_INTENTS = frozenset({"begin_login", "cancel_login"})


def _parse_provider_intent(data: dict) -> ProviderIntent:
    kind = data.get("kind")
    parser = _PROVIDER_INTENT_PARSERS.get(kind)
    if parser is None:
        raise ValueError(f"unknown provider intent kind {kind!r}")
    return parser(data, _intent_base(data))


# ADR 0011's SystemIntent parser table — same shape as the three tables
# above, kept as its own table for the same reason (independent growth,
# no cross-table edit collisions). "install_extension" doesn't carry a
# known `extension_id` up front for repo/artifact installs (the id is
# derived from the install itself) — the wire field is passed through
# unvalidated as part of `source` and `system_commands.py`'s port ignores
# it for those two paths (marketplace-metadata installs are the only
# source kind with a caller-known id ahead of time).
_SYSTEM_INTENT_PARSERS = {
    "update_extension_config": lambda data, base: UpdateExtensionConfig(
        **base,
        extension_id=str(data.get("extension_id", "")),
        section=str(data.get("section", "")),
        patch=dict(data.get("patch") or {}),
    ),
    "save_harness_profile": lambda data, base: SaveHarnessProfile(
        **base,
        harness_profile_id=data.get("harness_profile_id") or None,
        config=dict(data.get("config") or {}),
        revision=data.get("revision") or None,
        writes=tuple(data.get("writes") or ()),
    ),
    "delete_harness_profile": lambda data, base: DeleteHarnessProfile(
        **base,
        harness_profile_id=str(data.get("harness_profile_id", "")),
        revision=data.get("revision") or None,
    ),
    "set_default_harness_profile": lambda data, base: SetDefaultHarnessProfile(
        **base, harness_profile_id=str(data.get("harness_profile_id", "")),
    ),
    "install_extension": lambda data, base: InstallExtension(
        **base,
        extension_id=str(data.get("extension_id", "")),
        source=dict(data.get("source") or {}),
    ),
    "update_extension": lambda data, base: UpdateExtension(
        **base, extension_id=str(data.get("extension_id", "")),
    ),
    "uninstall_extension": lambda data, base: UninstallExtension(
        **base, extension_id=str(data.get("extension_id", "")),
    ),
    "enable_extension": lambda data, base: EnableExtension(
        **base, extension_id=str(data.get("extension_id", "")),
    ),
    "disable_extension": lambda data, base: DisableExtension(
        **base, extension_id=str(data.get("extension_id", "")),
    ),
    "decide_marketplace_intent": lambda data, base: DecideMarketplaceIntent(
        **base,
        marketplace_intent_id=str(data.get("intent_id_ref", "")),
        decision=MarketplaceDecision(data.get("decision", MarketplaceDecision.REJECT.value)),
    ),
    "revoke_marketplace_device": lambda data, base: RevokeMarketplaceDevice(
        **base, device_ref=str(data.get("device_ref", "")),
    ),
    "create_schedule": lambda data, base: CreateSchedule(
        **base,
        target_session_id=str(data.get("target_session_id", "")),
        prompt=str(data.get("prompt", "")),
        cadence=dict(data.get("cadence") or {}),
    ),
    "delete_schedule": lambda data, base: DeleteSchedule(
        **base, schedule_id=str(data.get("schedule_id", "")),
    ),
    "set_installation_capability": lambda data, base: SetInstallationCapability(
        **base,
        capability_id=str(data.get("capability_id", "")),
        enabled=bool(data.get("enabled", False)),
        confirm_cancels_extension_work=bool(data.get("confirm_cancels_extension_work", False)),
    ),
    "remove_node": lambda data, base: RemoveNode(**base, node_id=str(data.get("node_id", ""))),
    "sync_node_providers": lambda data, base: SyncNodeProviders(
        **base,
        node_id=str(data.get("node_id", "")),
        include_secrets=bool(data.get("include_secrets", False)),
        provider_ids=tuple(data.get("provider_ids") or ()),
    ),
    "resolve_node_registration": lambda data, base: ResolveNodeRegistration(
        **base,
        node_id=str(data.get("node_id", "")),
        decision=NodeRegistrationDecisionValue(
            data.get("decision", NodeRegistrationDecisionValue.DENIED.value),
        ),
    ),
}


def _parse_system_intent(data: dict) -> SystemIntent:
    kind = data.get("kind")
    parser = _SYSTEM_INTENT_PARSERS.get(kind)
    if parser is None:
        raise ValueError(f"unknown system intent kind {kind!r}")
    return parser(data, _intent_base(data))


def _frame_type_name(obj: object) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", type(obj).__name__).lower()


async def _handle_intent(websocket: WebSocket, data: dict) -> None:
    kind = data.get("kind")
    try:
        if kind in _SESSION_INTENT_PARSERS:
            intent: ChatIntent | SessionIntent | ProviderIntent | SystemIntent = _parse_session_intent(data)
            surface = _require_sessions()
        elif kind in _PROVIDER_INTENT_PARSERS:
            if kind in _LOOPBACK_GATED_PROVIDER_INTENTS and not is_loopback_request(websocket):
                raise HTTPException(
                    status_code=403,
                    detail="OAuth login is available only from a loopback session.",
                )
            intent = _parse_provider_intent(data)
            surface = _require_providers()
        elif kind in _SYSTEM_INTENT_PARSERS:
            intent = _parse_system_intent(data)
            surface = _require_system()
        else:
            intent = _parse_intent(data)
            surface = _require_chat()
    except HTTPException as exc:
        ack: TransportAck = IntentRejected(
            intent_id=str(data.get("intent_id", "")),
            code="forbidden",
            message=str(exc.detail),
        )
    except Exception:
        ack = IntentRejected(
            intent_id=str(data.get("intent_id", "")),
            code="malformed_intent",
            message="intent payload could not be parsed",
        )
    else:
        ack = surface.submit(intent)
    body = to_wire(ack)
    body["type"] = _frame_type_name(ack)
    await websocket.send_json(body)


# ---- WebSocket live plane --------------------------------------------

async def _authenticate(websocket: WebSocket) -> bool:
    """Duplicates `ws_chat.websocket_chat`'s gate: session cookie first,
    bearer-token query-param fallback for native clients. Accept before any
    close so the client sees a real 1008 close frame, not a 403 handshake
    failure (see ws_chat.py for the rationale)."""
    if not browser_trust.validate_websocket(websocket):
        await websocket.close(code=1008)
        return False
    await websocket.accept()
    user = websocket.session.get("user")
    if not user:
        tok = websocket.query_params.get("token")
        tok_user = auth.verify_token(tok) if tok else None
        if tok_user:
            user = tok_user
    if not user:
        await websocket.close(code=1008)
        return False
    return True


def _parse_surface_cursors(raw: dict) -> tuple[tuple[SurfaceCursor, ...], Focus]:
    surfaces = raw.get("surfaces")
    if not isinstance(surfaces, list) or not surfaces:
        raise ValueError("surfaces must be a non-empty list")
    cursors = tuple(
        SurfaceCursor(
            surface_id=_validate_id(str(s["surface_id"]), field="surface_id"),
            incarnation=str(s["incarnation"]),
            render_rev=int(s["render_rev"]),
        )
        for s in surfaces
    )
    focus = Focus(raw.get("focus", Focus.OPENED.value))
    return cursors, focus


class _SystemFeedView:
    """One `SystemSurface`-scoped feed name's `subscribe(emit)` — filters
    the ONE shared `system` adapter's frame stream down to the frame
    types this feed name owns, so `ws_surface`'s existing 1-feed-name==1-
    `subscribe()`-call reconciliation loop (below, unmodified) stays
    correct even though 12 distinct feed names all resolve to the SAME
    underlying `SystemSurfaceAdapter` instance (ADR 0011's twelve system
    feeds, vs. sessions/providers/runs' 1:1 feed-name-to-surface-object
    mapping). Calling `system.subscribe(emit)` once per feed name would
    instead register the connection's `emit` closure N times on one
    `BusBoundProjection`, delivering every frame N-fold — this view is
    what keeps "one feed name, one subscription" true regardless of how
    many feed names share one surface object."""

    def __init__(self, surface: SystemSurface, frame_types: tuple[type, ...]) -> None:
        self._surface = surface
        self._frame_types = frame_types

    def subscribe(self, emit):
        def _filtered(frame: object) -> None:
            if isinstance(frame, self._frame_types):
                emit(frame)

        return self._surface.subscribe(_filtered)


_SYSTEM_FEED_FRAME_TYPES: dict[str, tuple[type, ...]] = {
    "extension_notices": (ExtensionToast, ExtensionSignal),
    "extension_config": (ExtensionConfigUpsert,),
    "harness_profiles": (HarnessProfileUpsert, HarnessDefaultChanged),
    "extension_ui": (ExtensionUiChanged,),
    "extension_catalog": (ExtensionCatalogUpsert,),
    "marketplace_bridge": (MarketplaceBridgeStateChanged,),
    "marketplace_intents": (MarketplaceIntentUpsert,),
    "schedules": (ScheduleUpsert, ScheduleRemoved),
    "host_startup_tasks": (HostStartupTaskUpsert, HostStartupTaskCleared),
    "installation_capabilities": (InstallationCapabilityChanged,),
    "machines": (MachineNodeUpsert, NodeRemoved, NodeProviderCredentialUpsert),
    "node_registrations": (NodeRegistrationUpsert,),
}


_FEED_SURFACES = ("sessions", "providers", "runs", *_SYSTEM_FEED_FRAME_TYPES)


def _feed_surface(name: str):
    if name in ("sessions", "providers", "runs"):
        return {"sessions": sessions, "providers": providers, "runs": runs}[name]
    if system is None:
        return None
    return _SystemFeedView(system, _SYSTEM_FEED_FRAME_TYPES[name])


@router.websocket("/ws/v2/surface")
async def ws_surface(websocket: WebSocket) -> None:
    if not await _authenticate(websocket):
        return

    loop = asyncio.get_running_loop()
    # Bounded outbox / slow-consumer-disconnect (backend/ws_outbox.py,
    # shared with ws_chat.py's /ws/chat) — protects the process from
    # unbounded memory growth when a client can't drain a monster-turn's
    # broadcast fan-out fast enough. `on_close` is a no-op: a slow-
    # consumer disconnect closes `websocket` itself, which unblocks the
    # `receive_json()` loop below with WebSocketDisconnect and runs the
    # SAME subscription/feed cleanup any other disconnect reason does.
    outbox = WebSocketOutbox(websocket, on_close=lambda: asyncio.sleep(0))
    subscription = None
    # Non-cursor live feeds (SessionSurface/ProviderConfigSurface/
    # RunsSurface `subscribe(emit)` — no per-surface cursor, unlike the
    # chat surface's `subscribe(cursors, focus, emit)`), keyed by feed
    # name so re-sending `{"feeds": [...]}` can add/remove individual
    # feeds without disturbing the chat `subscription` above.
    feed_subscriptions: dict[str, object] = {}

    def emit(frame: object) -> None:
        frame_type = _frame_type_name(frame)
        body = to_wire(frame)
        body["type"] = frame_type
        # Perf-instrumented (additive only): counts every frame this
        # connection's live subscription hands off to the outbox, split by
        # frame type — lets a live-content investigation confirm frames
        # actually reached the WS transport layer (vs. never having been
        # broadcast by the adapter in the first place).
        perf.record_count(f"adapter_api.ws_surface.emit_handoff.{frame_type}")
        # `emit` may fire from a different thread than this connection's
        # event loop (chat_adapter's broadcast runs under the event-bus
        # dispatch context) — call_soon_threadsafe marshals the actual
        # (possibly backpressure-waiting) send onto the right loop as a
        # fire-and-forget task; `emit` itself must stay synchronous.
        loop.call_soon_threadsafe(lambda: asyncio.ensure_future(outbox.send(body)))

    try:
        while True:
            raw = await websocket.receive_json()
            if not isinstance(raw, dict):
                continue
            if "intent" in raw:
                intent_data = raw.get("intent")
                if isinstance(intent_data, dict):
                    await _handle_intent(websocket, intent_data)
                continue
            if "surfaces" in raw:
                try:
                    cursors, focus = _parse_surface_cursors(raw)
                except Exception:
                    await websocket.close(code=1003)
                    return
                if subscription is not None:
                    subscription.close()
                subscription = _require_chat().subscribe(cursors, focus, emit)
                continue
            if "feeds" in raw:
                requested = raw.get("feeds")
                if not isinstance(requested, list):
                    await websocket.close(code=1003)
                    return
                requested_set = {f for f in requested if f in _FEED_SURFACES}
                for name in list(feed_subscriptions):
                    if name not in requested_set:
                        feed_subscriptions.pop(name).close()
                for name in requested_set - feed_subscriptions.keys():
                    surface = _feed_surface(name)
                    if surface is not None:
                        feed_subscriptions[name] = surface.subscribe(emit)
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("ws/v2/surface: connection handler failed")
    finally:
        await outbox.close()
        await outbox.wait_closed()
        if subscription is not None:
            subscription.close()
        for feed_subscription in feed_subscriptions.values():
            feed_subscription.close()
