"""Better Agent — FastAPI backend with WebSocket streaming and REST APIs."""

import asyncio
from contextlib import asynccontextmanager
import faulthandler
import json
import logging
import logging.handlers
import re
import signal
import subprocess
import sys

# Build version: first 5 chars of git HEAD SHA.
try:
    _GIT_SHA = subprocess.check_output(
        ["git", "rev-parse", "--short=5", "HEAD"],
        stderr=subprocess.DEVNULL,
    ).decode().strip()
except Exception:
    _GIT_SHA = "dev"
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

# Assert an adequate fd limit before any backend module opens handles.
from fd_limits import raise_fd_limit
raise_fd_limit()

from env_compat import get_env
from local_machine_identity import initialize_primary_machine_id
from event_bus import BusEvent, bus as event_bus
import browser_trust
from paths import ba_home
from i18n import t
import perf
from bounded_async_executor import AdmissionOverloaded
from requirements_query_runner import (
    PROCESSOR_RESULT_TIMEOUT_SECONDS,
    REQUIREMENTS_PROCESSOR_EXECUTOR,
    REQUIREMENTS_SEARCH_EXECUTOR,
    run_requirements_processor_query,
    run_requirements_query,
    run_supervised_requirements_search,
)
from secret_redaction import install_access_log_redaction

initialize_primary_machine_id()
install_access_log_redaction()


def _streaming_assistant_message_id(session: dict) -> Optional[str]:
    messages = session.get("messages") if isinstance(session, dict) else None
    if not isinstance(messages, list):
        return None
    for msg in reversed(messages):
        if isinstance(msg, dict) and msg.get("role") == "assistant" and msg.get("isStreaming"):
            msg_id = msg.get("id")
            return msg_id if isinstance(msg_id, str) and msg_id else None
    return None


def _append_selector_change_anchor(session_id: str) -> Optional[str]:
    now = datetime.now(timezone.utc).isoformat()
    msg_id = f"model-switch-{uuid.uuid4()}"
    anchor = {
        "id": msg_id,
        "role": "assistant",
        "content": "",
        "events": [],
        "timestamp": now,
        "isStreaming": False,
        "completed_at": now,
        "source": "selector_change",
    }
    if session_manager.append_assistant_msg(session_id, anchor) is None:
        return None
    return msg_id


def _record_model_switched_event(
    session_id: str,
    before: dict,
    after: dict,
    updates: dict,
) -> None:
    keys = ("model", "provider_id", "reasoning_effort", "runner", "runtime_profile_id")
    changed = [
        key for key in keys
        if key in updates and before.get(key) != after.get(key)
    ]
    if not changed:
        return
    msg_id = _streaming_assistant_message_id(after)
    root_id = session_manager._root_id_for(session_id)
    if not root_id:
        return
    if not msg_id:
        if not after.get("messages"):
            return
        msg_id = _append_selector_change_anchor(session_id)
        if not msg_id:
            return

    provider = config_store.get_provider(after.get("provider_id"))
    previous_provider = config_store.get_provider(before.get("provider_id"))
    data = {
        "uuid": f"model-switch-{uuid.uuid4()}",
        "model": after.get("model"),
        "provider_id": after.get("provider_id"),
        "provider_name": (provider or {}).get("name"),
        "provider_nickname": (provider or {}).get("nickname"),
        "provider_kind": (provider or {}).get("kind"),
        "reasoning_effort": after.get("reasoning_effort"),
        "runner": after.get("runner"),
        "previous_model": before.get("model"),
        "previous_provider_id": before.get("provider_id"),
        "previous_provider_name": (previous_provider or {}).get("name"),
        "previous_provider_nickname": (previous_provider or {}).get("nickname"),
        "previous_provider_kind": (previous_provider or {}).get("kind"),
        "previous_reasoning_effort": before.get("reasoning_effort"),
        "previous_runner": before.get("runner"),
        "changed": changed,
        "app_session_id": session_id,
        "msg_id": msg_id,
    }
    event = {"type": "model_switched", "data": data}
    # Journal FIRST: events.jsonl is the durable source the render tree is
    # rebuilt from on reload, while `append_native_event` only mutates the
    # in-memory tree (session.json strips events). Writing the render tree
    # first and the journal second leaves the badge live-visible but
    # journal-absent if the process dies between them — so it vanishes on
    # reload. Journal-first fails closed: on publish failure the live tree
    # never gets an event it can't recover.
    from event_journal import publish_event_sync
    publish_event_sync(
        session_id=root_id,
        context_id=session_id,
        event_type="model_switched",
        data=data,
        source="selector_change",
        message_id=msg_id,
        timeout=30,
    )
    session_manager.append_native_event(session_id, msg_id, event)


from fastapi import FastAPI, HTTPException, Request
from browser_cors import BrowserTrustCORSMiddleware

import config_store
import user_prefs
import ui_selection
from runtime_profiles_api import (
    record_last_model as _record_last_model,
    record_last_reasoning_effort as _record_last_reasoning_effort,
)
from session_list_cache import _invalidate_session_list_user_prefs_cache

# Apply saved auth env vars at import time so any code path that still
# reads `os.environ` directly (e.g. `runner.py` jsonl-path resolution)
# sees the active provider's `CLAUDE_CONFIG_DIR`. Subprocess spawns now
# go through `Provider.build_env()` and don't depend on this — but
# in-process fallbacks still might.
#
config_store.apply_provider_config_env_vars()

from orchestrator import Coordinator
from session_manager import manager as session_manager
from session_manager import (
    IncompatibleOrchestrationMode,
    session_matches_project,
)
import runs_dir
import file_browser
import project_store
import project_mapping_store
import prompt_engineer
import project_config
import extension_store
import harness_field_writer
import harness_fields

# Log directory is intentionally captured at module-load. The
# "no module-load Path caching" rule (CLAUDE.md, A12) applies to
# STATE storage (sessions, traces) — observability
# is configured exactly once at process boot and the `FileHandler`
# below binds a single Path into logging's machinery regardless of
# how we resolve it here. Tests that need isolated logs must set
# `BETTER_CLAUDE_HOME` BEFORE importing `main`.
_log_dir = ba_home() / "logs"
_log_dir.mkdir(parents=True, exist_ok=True)

_LOG_FORMAT = logging.Formatter("%(asctime)s %(levelname)-5s %(name)s: %(message)s")

import queued_logging  # noqa: E402

_root_stream_handler = logging.StreamHandler()
_root_stream_handler.setFormatter(_LOG_FORMAT)
# Rotated, not unbounded: an ever-growing backend.log (500MB+ observed)
# makes every write — and therefore every queue-listener flush — slower
# over a long-running process's lifetime.
_root_file_handler = logging.handlers.RotatingFileHandler(
    _log_dir / "backend.log", maxBytes=50 * 1024 * 1024, backupCount=5, encoding="utf-8",
)
_root_file_handler.setFormatter(_LOG_FORMAT)
logging.getLogger().setLevel(logging.INFO)
queued_logging.install_queued_logging(logging.getLogger(), _root_stream_handler, _root_file_handler)
logger = logging.getLogger(__name__)


try:
    faulthandler.enable()
    faulthandler.register(signal.SIGUSR1, file=sys.stderr, all_threads=True, chain=False)
except Exception:
    logger.debug("faulthandler enable failed", exc_info=True)
frontend_logger = logging.getLogger("frontend")
frontend_logger.setLevel(logging.DEBUG)
frontend_logger.propagate = False
if not frontend_logger.handlers:
    _frontend_file_handler = logging.handlers.RotatingFileHandler(
        _log_dir / "frontend.log", maxBytes=20 * 1024 * 1024, backupCount=5, encoding="utf-8",
    )
    _frontend_file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-5s %(message)s"))
    queued_logging.install_queued_logging(frontend_logger, _frontend_file_handler)


@asynccontextmanager
async def app_lifespan(_app: FastAPI):
    """Application lifespan: run startup before serving, shutdown after.

    Startup and shutdown stay in separate blocks (not one try/finally) so a
    failure in startup cannot trigger the shutdown drain on resources startup
    never initialised. `app_lifecycle` is imported (and configured) further
    down this module; the name resolves at call time.
    """
    await app_lifecycle.on_startup()
    for _deferred in frontend_mount._deferred_startup_tasks:
        await _deferred()
    try:
        yield
    finally:
        await app_lifecycle.on_shutdown()


def create_api_app() -> FastAPI:
    """Create the FastAPI API app before frontend static files are mounted.

    Route registration in this module still happens via decorators on the
    module-level `app`; tests that need API-only import set
    `BETTER_CLAUDE_API_ONLY=1` before importing `main`.
    """
    return FastAPI(title="Better Agent", lifespan=app_lifespan)


app = create_api_app()

# CORS, auth_gate, SessionMiddleware, and ingest_command_received are
# registered AFTER `coordinator` is created (we need its internal_token
# in auth_gate). See the block below `coordinator = Coordinator()`.


@app.exception_handler(IncompatibleOrchestrationMode)
async def _incompatible_orchestration_mode_handler(_request, exc):
    """Layer-2 capability gate raised inside `session_manager.create`
    surfaces here as a 400 instead of a 500. Catches HTTP `POST
    /api/sessions` + Team Orchestration worker creation + any future route that mints
    a session through the manager. CLI / tests still see the raw
    exception (no FastAPI middleware in those paths)."""
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=400, content={"detail": str(exc)})


_COMMAND_METHODS = {"POST", "PATCH", "PUT", "DELETE"}
_COMMAND_JOURNAL_EXCLUDED_SUFFIXES = ("/draft",)


def _extract_command_sid(path: str) -> Optional[str]:
    """Return the session id this request mutates, or None if the path
    is not a per-session mutation route.

    Covers two prefixes the frontend actually uses for session-scoped
    state changes: `/api/sessions/<sid>/...` and `/api/file-editor/<sid>/...`.
    A bare `/api/sessions` (the create-session endpoint) returns None —
    the new session's existence is captured in `session_store`; the
    follow-up state-mutating calls each carry sid.
    """
    if path.endswith(_COMMAND_JOURNAL_EXCLUDED_SUFFIXES):
        return None
    parts = path.split("/")
    # ["", "api", <root>, <sid>, ...]
    if len(parts) >= 5 and parts[1] == "api" and parts[2] in {"sessions", "file-editor"}:
        sid = parts[3]
        if sid:
            return sid
    return None


@app.middleware("http")
async def perf_timing(request, call_next):
    # INVARIANT: this is the innermost middleware so the recorded
    # duration is handler-only (auth/session/CORS overhead is not
    # included). Route template is only populated on `request.scope`
    # AFTER routing inside `call_next`, so it's looked up post-call.
    import recovery_priority

    t0 = time.perf_counter()
    response_status: Optional[int] = None
    recovery_priority.interactive_request_started()
    try:
        response = await call_next(request)
        response_status = response.status_code
        return response
    except Exception:
        response_status = 500
        raise
    finally:
        recovery_priority.interactive_request_finished()
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        # Three populated-scope cases after routing:
        #   (a) FastAPI `APIRoute` → `scope["route"].path` is the template.
        #   (b) Starlette `Mount` (e.g. SPA static files) → `scope["route"]`
        #       is NOT set; `scope["endpoint"]` is the mounted app. Without
        #       this branch every static asset hit would bucket as
        #       `unmatched`, polluting the 404 signal.
        #   (c) No match → both None → `unmatched`.
        route = request.scope.get("route")
        template = getattr(route, "path", None)
        if template is None:
            endpoint = request.scope.get("endpoint")
            if endpoint is not None:
                template = f"mount:{type(endpoint).__name__}"
            else:
                template = "unmatched"
        metric_name = f"rest.{request.method}.{template}"
        perf.record(metric_name, elapsed_ms)
        if response_status is not None:
            perf.record_count(f"{metric_name}.status.{response_status}")


@app.middleware("http")
async def ingest_command_received(request, call_next):
    """Persist every inbound state-mutating REST request as a
    `command_received` event in the target session's events.jsonl,
    BEFORE the handler runs. This is the structural guardrail that
    makes the durable log a complete record of frontend → backend
    inputs (without it, only the downstream worker/manager effects
    appear; "user clicked rewind to seq=42" is invisible).

    Body re-injection pattern: Starlette consumes the request body
    via a single `receive` callable; reading it here exhausts the
    stream. We capture the bytes, ingest, then patch `_receive` so
    downstream Pydantic/`Body(...)` parsing sees the bytes again.
    """
    if request.method not in _COMMAND_METHODS:
        return await call_next(request)
    sid = _extract_command_sid(request.url.path)
    if not sid:
        return await call_next(request)
    body_bytes = await request.body()
    payload: Any
    if body_bytes:
        try:
            payload = json.loads(body_bytes)
        except Exception:
            payload = {"_raw": body_bytes.decode("utf-8", errors="replace")}
    else:
        payload = {}
    try:
        from event_journal import publish_event
        root_id = await asyncio.to_thread(session_manager._root_id_for, sid) or sid
        await publish_event(
            session_id=root_id,
            context_id=sid,
            event_type="command_received",
            data={
                "method": request.method,
                "path": request.url.path,
                "sid": sid,
                "payload": payload,
                "uuid": str(uuid.uuid4()),
            },
            source="rest",
        )
    except Exception:
        logger.exception(
            "command_received ingest failed sid=%s path=%s",
            sid, request.url.path,
        )

    async def receive():
        return {"type": "http.request", "body": body_bytes, "more_body": False}

    request._receive = receive
    return await call_next(request)

coordinator = Coordinator()
from scheduler import Scheduler
schedule_ticker = Scheduler(coordinator)
# A10 TOCTOU closure: wire the active-run gate at module-load time,
# BEFORE any HTTP route is mounted, so the very first PATCH that
# arrives can't skip the inside-the-lock recheck via the "gate not
# bound yet" path. Keeping this here (not inside on_startup) shrinks
# the gate-binding window to zero.
session_manager.bind_active_run_gate(coordinator.turn_manager.has_active_runs)
# Source of truth for the "Running…" indicator: coordinator walks
# `_run_state[sid]` and checks pid liveness per entry. session_manager
# only keeps the last-broadcast value per sid for WS dedup.
# Bound at module load (same window as the active-run gate) so the
# first request can't race a None check.
session_manager.bind_running_check(coordinator.turn_manager.is_running)
session_manager.bind_monitoring_check(coordinator.turn_manager.monitoring_state)
# Pin predicate for LRU root eviction: never evict a root the
# orchestrator still references (active turn / WS subscriber / live
# tailer). Bound at module load so an early load can't enforce the cap
# with the predicate still None (which fails closed → nothing evicted).
session_manager.bind_pin_predicate(coordinator.is_root_in_use)

# ============================================================================
# Auth — keychain-backed credentials, session-cookie gate.
# ----------------------------------------------------------------------------
# Middleware source-order below determines the wrapping (Starlette docs:
# last-added = outermost = runs first). Runtime order is:
#   CORS  →  SessionMiddleware  →  auth_gate  →  ingest_command_received
#         →  handler / router
# CORS outermost so OPTIONS preflight returns without auth-401ing it.
# SessionMiddleware before auth_gate so the latter can read the session.
# auth_gate before ingest so unauth requests can't write
# `command_received` events to disk.
# ============================================================================

import auth                                                       # noqa: E402
import auth_routes                                                # noqa: E402
from dynamic_session_middleware import DynamicSecretSessionMiddleware  # noqa: E402

import app_composition  # noqa: E402
app_composition.wire(
    app,
    coordinator=coordinator,
    git_sha=_GIT_SHA,
    session_lite=lambda sid: _session_lite(sid),
    publish_worker_fanout=lambda sid, **kw: _publish_worker_fanout_required(sid, **kw),
    invalidate_session_list_cache=_invalidate_session_list_user_prefs_cache,
    notify_projects_changed=lambda: _broadcast_projects_changed(),
    require_builtin_extension=lambda extension_id: _require_builtin_extension(extension_id),
    resolve_selector_updates=lambda sid, body: _resolve_selector_updates(sid, body),
    record_model_switched_event=_record_model_switched_event,
    record_last_model=_record_last_model,
    record_last_reasoning_effort=_record_last_reasoning_effort,
    broadcast_session_organization_changed=(
        lambda session_ids=None: _broadcast_session_organization_changed(session_ids)
    ),
    delete_session_tree=lambda sid: _delete_session_tree(sid),
    extension_daemons_ready=lambda: _EXTENSION_DAEMONS_READY,
    cold_recovery_integration_pending=lambda: recovery._cold_recovery_integration_pending(),
    frontend_dist=lambda: frontend_mount.frontend_dist_dir(),
)

# Imported directly beyond mounting it; the mounting lives in
# app_composition.
import internal_extension_api  # noqa: E402
# Re-exported for direct unit-test access; the implementations live in
# internal_extension_api now.
_encode_extension_call_body = internal_extension_api._encode_extension_call_body
_require_project_updates_internal_async = internal_extension_api.require_project_updates_internal_async
from session_listing_api import (  # noqa: E402
    broadcast_session_organization_changed as _broadcast_session_organization_changed,
)
# Imported for main.py's own remaining wiring: the recovery configure()
# call below and the SPA static mount at the end of this module.
import recovery  # noqa: E402
import frontend_mount  # noqa: E402
# Injected into wire() and called by the cascade-delete path; the
# implementations live in session_detail_api.
from session_detail_api import (  # noqa: E402
    _publish_worker_fanout_required,
    _resolve_selector_updates,
)
import credential_clone_api  # noqa: E402
import prompt_engineer_api  # noqa: E402


def _builtin_extension_enabled(extension_id: str) -> bool:
    return extension_store.is_builtin_feature_enabled(extension_id)


def _require_builtin_extension(extension_id: str) -> None:
    if not _builtin_extension_enabled(extension_id):
        raise HTTPException(status_code=404, detail="Extension is not installed")


# Auth routes reachable without credentials (you authenticate TO reach
# them). /api/auth/me is excluded — see auth_gate below.
_AUTH_PUBLIC_ROUTES = frozenset({
    "/api/auth/login",
    "/api/auth/setup",
    "/api/auth/needs_setup",
    # QR / refresh-token external access. qr_grant self-gates to
    # loopback-or-authed inside the handler (see auth_routes.py).
    "/api/auth/qr_grant",
    "/api/auth/qr_redeem",
    "/api/auth/refresh",
    # OTA bundle download for the Capacitor updater. The native HTTP GET
    # cannot carry our dynamic bearer header, so the handler validates a
    # `token` query param (same pattern as the WS endpoints) and fails
    # closed on an invalid/missing token.
    "/api/mobile/bundle/download",
})
_AUTH_PUBLIC_PREFIXES = (
    "/api/desktop/updates/",
    "/api/download/desktop/",
    # HTML preview files. The route self-gates via an HMAC-signed,
    # expiring, directory-scoped token minted by the authed
    # /api/file/preview-url endpoint (which this prefix does NOT match).
    # The preview iframe is an opaque origin that cannot send the
    # session cookie, so the token is the credential.
    "/api/file/preview/",
)
_AUTH_PUBLIC_ARTIFACT_ROUTES = frozenset({
    "/api/desktop/status",
})

# Extension UI bundles are static JS/CSS assets, served to the client the
# same way the SPA shell is. They are loaded by dynamic `import()` of a
# backend-served URL. On the Capacitor native shell the page origin
# (http://localhost / capacitor://localhost) differs from the API origin,
# so the import is cross-origin — and `import()` can neither carry the
# SameSite=Lax session cookie nor set an Authorization header (the bearer
# interceptor only wraps window.fetch, not the module loader). An auth
# requirement here therefore 401s the module request and surfaces in the
# WebView as "Failed to fetch dynamically imported module". Treat these
# static assets as public, exactly like the frontend shell.
_EXTENSION_FRONTEND_ASSET_RE = re.compile(r"^/api/extensions/[^/]+/frontend/")
_TASK_OUTPUT_PREVIEW_RE = re.compile(
    r"^/api/task-output/preview/[^/]+/[A-Za-z0-9_-]{1,64}/[a-f0-9]{12}$"
)


def _is_extension_frontend_asset(path: str) -> bool:
    return bool(_EXTENSION_FRONTEND_ASSET_RE.match(path))


def _is_task_output_preview_request(request: Request) -> bool:
    return request.method == "GET" and bool(_TASK_OUTPUT_PREVIEW_RE.fullmatch(request.url.path))


@app.middleware("http")
async def auth_gate(request, call_next):
    """Gate every /api/* request except the pre-auth auth routes
    (`_AUTH_PUBLIC_ROUTES`) and /api/internal/* (the latter uses the
    existing X-Internal-Token pattern that worker subprocesses already
    send — see main.py handlers using `Header(..., alias="X-Internal-Token")`).
    Note /api/auth/me IS gated — native clients authenticate to it via
    the bearer fallback, since their session cookie can't cross origins.

    `BETTER_CLAUDE_TEST_AUTH_BYPASS` is intentionally ignored; tests
    authenticate normally or use internal tokens."""
    from fastapi.responses import JSONResponse

    path = request.url.path
    if (
        path.startswith("/api/")
        and path not in _AUTH_PUBLIC_ARTIFACT_ROUTES
        and not _is_extension_frontend_asset(path)
        and not _is_task_output_preview_request(request)
    ):
        try:
            browser_trust.validate_http_request(request)
        except HTTPException as exc:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    # Pre-auth auth routes must stay reachable without credentials (you
    # log in to reach them). /api/auth/me is intentionally NOT here: it
    # requires auth like every other /api/ route, so native Capacitor
    # clients authenticate via the bearer fallback below. Otherwise the
    # SameSite=Lax session cookie can't cross origins from the WebView
    # (http://localhost) to the backend, /me 401s, and the just-logged-in
    # user bounces straight back to <Login /> with no error shown.
    if (
        path in _AUTH_PUBLIC_ROUTES
        or path in _AUTH_PUBLIC_ARTIFACT_ROUTES
        or any(path.startswith(prefix) for prefix in _AUTH_PUBLIC_PREFIXES)
        or _is_extension_frontend_asset(path)
        or _is_task_output_preview_request(request)
    ):
        return await call_next(request)
    if path.startswith("/api/internal/"):
        token = request.headers.get("X-Internal-Token")
        # Authn: accept the core/runner token OR a registered per-extension
        # token. Identity (which extension) is derived from the token by the
        # per-endpoint gates — never from a self-asserted X-Extension-Id.
        try:
            principal = await coordinator.resolve_principal_async(token)
        except AdmissionOverloaded:
            return JSONResponse(
                {"detail": "internal authentication is busy; retry shortly"},
                status_code=503,
                headers={"Retry-After": "1"},
            )
        if principal is None:
            from fastapi.responses import JSONResponse
            return JSONResponse({"detail": "invalid internal token"}, status_code=403)
        narrow_extension_routes = {
            "/api/internal/capabilities/invoke",
            "/api/internal/extension-call",
        }
        if principal[0] == "extension" and path not in narrow_extension_routes:
            try:
                allowed = await coordinator.extension_internal_loopback_allowed_async(
                    str(principal[1] or ""),
                )
            except AdmissionOverloaded:
                return JSONResponse(
                    {"detail": "internal authorization is busy; retry shortly"},
                    status_code=503,
                    headers={"Retry-After": "1"},
                )
            if not allowed:
                return JSONResponse(
                    {"detail": "internal route requires internal_loopback permission"},
                    status_code=403,
                )
        request.state.internal_principal = principal
        request.state.internal_token = token
        with coordinator.bind_principal(token, principal, allow_downstream=True):
            return await call_next(request)
    if not path.startswith("/api/"):
        # Frontend static files and any non-API path are public — the
        # frontend SPA handles redirecting to <Login /> when /api/auth/me
        # comes back 401.
        return await call_next(request)
    user = request.session.get("user") if "session" in request.scope else None
    if not user:
        # Fall back to Bearer-token auth for cross-origin native clients
        # (Capacitor WebView) where the session cookie can't make it
        # across origins. See auth.verify_token for the contract.
        auth_header = request.headers.get("authorization") or ""
        if auth_header.lower().startswith("bearer "):
            tok_user = auth.verify_token(auth_header.split(" ", 1)[1].strip())
            if tok_user:
                # Stash on request.state, NOT request.session — writing into
                # the session here would resurrect/extend a cleared or
                # absent session cookie for a request that only carried a
                # bearer token (SessionMiddleware re-issues Set-Cookie for
                # any non-empty session at response time). Downstream
                # identity reads use auth.identify_request() to see this.
                request.state.bearer_user = tok_user
                user = tok_user
    if not user:
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": "unauthenticated"}, status_code=401)
    return await call_next(request)


app.add_middleware(
    DynamicSecretSessionMiddleware,
    max_age=30 * 86400,  # 30 days; matches the documented cookie lifetime
    same_site="lax",
    https_only=False,    # LAN HTTP for now; flip when fronted by TLS
    session_cookie="better_agent_session",
)

app.add_middleware(
    BrowserTrustCORSMiddleware,
    allow_origins=[
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from installation_admission import InstallationAdmissionMiddleware  # noqa: E402
app.add_middleware(InstallationAdmissionMiddleware)

# SessionManager change events fan out as global session metadata WS frames.
from session_ws_broadcaster import SessionWSBroadcaster  # noqa: E402
ws_broadcaster = SessionWSBroadcaster(coordinator)
from event_bus_subscribers import bind_session_ws_broadcaster
bind_session_ws_broadcaster(ws_broadcaster)

from event_bus_subscribers import (
    bind_post_turn_hooks,
    bind_pre_turn_hooks,
    bind_worker_fanout_cleanup,
)
bind_worker_fanout_cleanup(coordinator.broadcast_workers_changed, coordinator.cancel_session)
bind_post_turn_hooks()
bind_pre_turn_hooks()
# Deleting a session closes its tab: the open-tab list projects session
# deletions instead of stranding tabs that point at gone sessions.
import ui_selection_projection  # noqa: E402
ui_selection_projection.bind(coordinator.broadcast_global)
import task_assessor
task_assessor.bind(coordinator)

# Background/lifecycle subsystems: they own their own state and reach main.py
# only through these bindings.
recovery.configure(
    coordinator=coordinator,
    delete_session_tree=lambda sid: _delete_session_tree(sid),
)
import app_lifecycle  # noqa: E402
app_lifecycle.configure(
    coordinator=coordinator,
    schedule_ticker=schedule_ticker,
    ws_broadcaster=ws_broadcaster,
    git_sha=_GIT_SHA,
)

# Rebuild the declarative tag-rule registry from every enabled extension
# so styling/markers apply from boot, not only after the periodic
# instruction reconcile.
try:
    import extension_applied_config

    extension_applied_config.reconcile_all()
except Exception:
    logger.exception("startup: extension_applied_config.reconcile_all failed")

# Publish the desired supervisor-daemon set for the platform daemon host and
# start backend-lifecycle extension daemons.
try:
    import extension_daemons

    extension_daemons.reconcile()
    _EXTENSION_DAEMONS_READY = True
except Exception:
    _EXTENSION_DAEMONS_READY = False
    logger.exception("startup: extension_daemons.reconcile failed")

# Native-CLI-jsonl tailing is owned by native_files_manager: it folds
# tail targets (session.agent_sid_set / native_files.fork_target) and
# demand (native_files.demand) off the bus, and reconciles the
# OwnedClaudeJsonlTailers. The orchestrator only publishes demand.
from native_files_manager import native_files
native_files.bind()


def _wire_surface_adapter() -> None:
    """Compose the Chat Surface Contract adapter (ADR 0006) onto its
    versioned transport (backend/adapter_api.py).

    The bare<->dotted infra-singleton aliasing this used to perform inline
    now lives in backend/adapters/__init__.py itself (see that module's
    docstring): it is self-canonicalizing, firing for every importer of
    backend.adapters rather than only callers that run after this
    function — so it belongs to the package, not the composition root.
    All that is left here is resolving the `backend` namespace package
    (needed before the first `backend.*` import below can succeed at all)
    and the actual composition-root wiring.
    """
    import importlib
    import sys
    import types
    from pathlib import Path

    if "backend" not in sys.modules:
        try:
            importlib.import_module("backend")
        except ImportError:
            backend_pkg = types.ModuleType("backend")
            backend_pkg.__path__ = [str(Path(__file__).resolve().parent)]
            sys.modules["backend"] = backend_pkg

    from backend.adapters import build_adapter
    import adapter_api
    import surface_commands

    command_port = surface_commands.build_chat_command_port(coordinator=coordinator)
    surface_adapter = build_adapter(command_port=command_port)
    adapter_api.configure(surface_adapter)
    app.include_router(adapter_api.router)


_wire_surface_adapter()

# Working-mode owners subscribe to `session.parent_deleted` and route by
# `working_mode`; cascade-delete only publishes the fact.
import working_mode as _working_mode_mod
prompt_engineer.register_bus_subscribers()
_working_mode_mod.register_bus_subscribers()

# Mount the node_link WS endpoint (only meaningful in primary mode —
# topology.yaml is loaded lazily on first hit). Importing
# `provider_remote` here also wires its inbound dispatchers into
# node_link as a side-effect of the module import. Both imports are
# behind a try so a misconfigured topology.yaml doesn't break the
# primary's startup — node_link itself will refuse connections later
# with a clear error.
if (
    extension_store.extension_id_for_role('machine-nodes') is None
    or extension_store.is_extension_runtime_ready(
        extension_store.extension_id_for_role('machine-nodes')
    )
):
    try:
        import node_link
        import node_provider_credential_sync
        import node_store
        import provider_remote  # noqa: F401 — wires dispatchers as side effect
        app.include_router(node_link.router)

        async def _on_node_state_changed(node_id: str, new_state: str) -> None:
            """Fan node up/down transitions out to every open WS client so
            the frontend can render node-status badges without polling.

            Carries the backend-owned `last_seen` so the frontend never
            has to invent a timestamp from its own clock (CLAUDE.md state-
            ownership: compute server-side, reflect on the frontend)."""
            conn = node_store.get_connection(node_id)
            payload = {
                "node_id": node_id,
                "state": new_state,
                "last_seen": conn.last_seen if conn else None,
                "app_commit_sha": conn.app_commit_sha if conn else "",
                "app_dirty": conn.app_dirty if conn else False,
                "primary_commit_sha": node_store.app_version.current_commit_sha(),
                "primary_dirty": node_store.app_version.current_dirty(),
                "version_status": (
                    node_store.connection_version_status(conn)
                    if conn else "unknown"
                ),
            }
            try:
                await coordinator.broadcast_global("node_state_changed", payload)
            except Exception:
                logger.exception("node_state_changed broadcast failed")

        node_store.add_listener(_on_node_state_changed)

        async def _on_node_provider_credentials_changed(
            node_id: str,
            provider_credentials: list[dict],
        ) -> None:
            try:
                await coordinator.broadcast_global(
                    "node_provider_credentials_changed",
                    {
                        "node_id": node_id,
                        "provider_credentials": provider_credentials,
                    },
                )
            except Exception:
                logger.exception(
                    "node provider credential status broadcast failed for %s",
                    node_id,
                )

        node_provider_credential_sync.add_listener(
            _on_node_provider_credentials_changed,
        )

        async def _on_node_connected_recover(node_id: str, new_state: str) -> None:
            """When a node (re)connects, reconcile every pending remote run
            dir it owns — finalize completed/dead runs, rehook alive ones.
            Background task: recovery RPC round-trips must not block the
            node handshake path that fires this listener."""
            if new_state != "connected":
                return
            import run_recovery
            asyncio.get_running_loop().create_task(
                run_recovery.integrate_remote_runs_for_node(node_id),
                name=f"remote-recovery-{node_id}",
            )

        node_store.add_listener(_on_node_connected_recover)

        import node_config_sync
        # A (re)connecting worker gets the current extension, provider, and
        # harness state pushed so it never runs a stale projection after
        # downtime.
        node_store.add_listener(node_config_sync.on_node_state)
        import run_recovery as _run_recovery_mod
        _run_recovery_mod.set_remote_recovery_coordinator(coordinator)

        async def _on_node_registration(event_type: str, payload: dict) -> None:
            """Fan node registration-lifecycle events
            (`node_registration_requested` / `node_registration_resolved`) out
            to every open browser so the approval popup appears/dismisses
            without polling. node_link calls this via set_registration_listener
            to avoid importing the coordinator (circular import)."""
            try:
                await coordinator.broadcast_global(event_type, payload)
            except Exception:
                logger.exception("node registration broadcast failed (%s)", event_type)

        node_link.set_registration_listener(_on_node_registration)
        logger.info("multi-machine: node_link WS endpoint mounted")
    except Exception:
        logger.exception("multi-machine: node_link mount failed at startup")


# ============================================================================
# REST Endpoints
# ============================================================================


from session_helpers import (
    require_session as _require_session,
    session_lite as _session_lite,
    existing_session_ids as _existing_session_ids,
    session_lite_by_id as _session_lite_by_id,
)
async def _broadcast_projects_changed() -> None:
    """Single source for the projects_changed fan-out frame. Any
    mutation to the projects list (CRUD or auto-add-from-session) ends
    with this broadcast so open clients refresh the sidebar picker.
    Also rebuilds project mappings since project data changed."""
    await coordinator.broadcast_global("projects_changed", {})
    # Rebuild mappings in background — non-blocking.
    projects = await asyncio.to_thread(project_store.list_projects)
    await asyncio.to_thread(project_mapping_store.rebuild_and_save, projects)
    await coordinator.broadcast_global("project_mappings_changed", {})


# ── Project structure updates ──────────────────────────────────


def _require_project_updates_internal(request: Request, x_internal_token: str) -> None:
    principal = coordinator.request_principal(request, x_internal_token)
    if principal is None:
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    _require_builtin_extension(extension_store.extension_id_for_role('project-structure'))
    if (
        principal[0] != "extension"
        or principal[1] != extension_store.extension_id_for_role('project-structure')
    ):
        raise HTTPException(status_code=403, detail="project-structure extension is required")


async def _delete_session_tree(session_id: str) -> bool:
    # Per-step timing so a slow delete repro names the offending step
    # (cancel / cascade / store / run-dirs / task-refs / fanout). Logged
    # once at the end; the wrapping endpoint also logs the handler total.
    import time as _time
    _dt0 = _time.perf_counter()
    _dsteps: list[tuple[str, float]] = []

    def _dmark(label: str, t0: float) -> None:
        _dsteps.append((label, (_time.perf_counter() - t0) * 1000.0))

    _t = _time.perf_counter()
    await coordinator.cancel_session(session_id)
    _dmark("cancel_session", _t)
    _t = _time.perf_counter()
    try:
        # The list() summary already carries `working_mode` and
        # `working_mode_meta` (session_store._build_summary_for_root), so we
        # can find this session's working-mode children with a pure in-memory
        # scan. Calling `_session_lite` here used to load + deepcopy the full
        # session tree per working-mode session (~778 of them), which was the
        # dominant delete cost after run-dir reaping was indexed.
        for s in await asyncio.to_thread(session_manager.list):
            if not s.get("working_mode"):
                continue
            meta = s.get("working_mode_meta") or {}
            if meta.get("parent_session_id") != session_id:
                continue
            child_id = s["id"]
            await coordinator.cancel_session(child_id)
            await event_bus.publish(BusEvent(
                type="session.parent_deleted",
                root_id=child_id,
                sid=child_id,
                payload={
                    "parent_session_id": session_id,
                    "child_session_id": child_id,
                    "working_mode": s.get("working_mode"),
                },
                persist=False,
            ))
    except Exception:
        logger.exception("cascade working-mode cleanup failed during session delete")
    _dmark("working_mode_cascade", _t)

    _t = _time.perf_counter()
    removed_sids = await asyncio.to_thread(session_manager.subtree_ids, session_id)
    # `cancel_session` above only reaches `session_id` itself and its
    # `working_mode` children. Delegation/team-orchestration forks live in
    # the same tree but aren't `working_mode` sessions, so without this
    # pass their runner processes are orphaned: still running, still
    # streaming provider events against a root that's about to vanish —
    # turn_manager._publish_provider_stream_event then raises "cannot
    # resolve root" on every event, indefinitely, until the runner exits
    # on its own. `cancel_session` is idempotent, so re-cancelling
    # `session_id`/working-mode children already handled above is a no-op.
    await asyncio.gather(
        *(coordinator.cancel_session(sid) for sid in removed_sids),
        return_exceptions=True,
    )
    _dmark("subtree_cancel", _t)
    _t = _time.perf_counter()
    ok = await asyncio.to_thread(session_manager.delete, session_id)
    _dmark("session_delete", _t)
    if ok:
        # Await the tab close here so the endpoint can't answer "deleted"
        # while a tab for the gone session is still persisted. The
        # `session.deleted` bus projection covers every other delete caller;
        # it is idempotent, so the two paths don't conflict.
        _t = _time.perf_counter()
        await ui_selection_projection.close_tabs_for_deleted(removed_sids)
        _dmark("close_tabs", _t)
        _t = _time.perf_counter()
        try:
            await asyncio.to_thread(runs_dir.delete_runs_for_sessions, removed_sids)
        except Exception:
            logger.exception("run-dir cleanup failed during session delete")
        _dmark("runs_cleanup", _t)
        # Drop any task deep-link breadcrumbs / singleton bindings that
        # pointed at deleted sessions, so the Routines tab never links to a
        # gone session. Best-effort, store-only; safe (no-op) when the
        # routines extension isn't installed (empty store).
        _t = _time.perf_counter()
        try:
            from stores import task_store as _task_store
            for _removed in removed_sids:
                if await asyncio.to_thread(_task_store.drop_session_references, _removed):
                    # A reference changed — ping tabs to refetch. cwd/node
                    # are unknown here; a null-cwd ping invalidates broadly,
                    # like worker fan-out's cross-cwd broadcast.
                    await coordinator.broadcast_global(
                        "tasks_changed", {"cwd": None, "node_id": "primary"},
                    )
        except Exception:
            logger.debug("task reference cleanup failed during session delete", exc_info=True)
        _dmark("task_ref_cleanup", _t)
    _t = _time.perf_counter()
    await _publish_worker_fanout_required(
        session_id,
        op_label="session delete",
        caller_scope=True,
        remove_worker=True,
        outer_log_msg="worker fan-out failed during session delete",
    )
    _dmark("worker_fanout", _t)
    logger.info(
        "delete_session_tree sid=%s total=%.0fms steps=[%s]",
        session_id,
        (_time.perf_counter() - _dt0) * 1000.0,
        ", ".join(f"{n}={ms:.0f}ms" for n, ms in _dsteps),
    )
    return ok


# ============================================================================
# WebSocket — Streaming Chat (Manager/Worker)
# ============================================================================
# Mounted from here rather than from `app_composition.wire()`: the sibling
# `/{_unknown_ws_path:path}` catch-all must register AFTER node_link's WS
# route above, which main.py mounts once `wire()` has returned.
import ws_chat  # noqa: E402
ws_chat.configure(coordinator=coordinator)
app.include_router(ws_chat.router)


# Production / desktop imports serve both API and frontend. Tests that only
# need API routes set this before importing `main` so no frontend build or
# fake `dist/` stub is required.
if get_env("BETTER_CLAUDE_API_ONLY") != "1":
    frontend_mount.mount_frontend(app)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host=user_prefs.get_network_bind_address(),
        port=8000,
        reload=True,
        proxy_headers=False,
        ws_per_message_deflate=False,
    )
