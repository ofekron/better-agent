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

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

import auth
import browser_trust
import file_delivery
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
    Approve,
    ChatIntent,
    DeleteQueued,
    EditQueued,
    IntentRejected,
    Rewind,
    SendMode,
    SendPrompt,
    SendTarget,
    SendTargetKind,
    SetSelectors,
    Stop,
    TransportAck,
)
from backend.surface_contract.nodes import Attachment
from backend.surface_contract.provider_config_surface import ProviderConfigSurface
from backend.surface_contract.runs_surface import RunsSurface
from backend.surface_contract.session_surface import SessionSurface
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


def configure(
    adapter: BetterAgentAdapter | None = None,
    *,
    chat: ChatSurface | None = None,
    sessions: SessionSurface | None = None,
    providers: ProviderConfigSurface | None = None,
    runs: RunsSurface | None = None,
) -> None:
    """Wire the module-level surface singletons the routes below dispatch
    to. `configure(composed_adapter)` sets all four at once (the
    composition-root call in `backend/main.py`); the keyword form
    (`configure(chat=...)`) sets individual surfaces directly and stays
    valid standalone — pre-existing callers that only ever wired `chat`
    keep working unchanged."""
    if adapter is not None:
        chat = chat if chat is not None else adapter.chat
        sessions = sessions if sessions is not None else adapter.sessions
        providers = providers if providers is not None else adapter.providers
        runs = runs if runs is not None else adapter.runs
    globals()["chat"] = chat
    globals()["sessions"] = sessions
    globals()["providers"] = providers
    globals()["runs"] = runs


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


# ---- REST read plane -------------------------------------------------

@router.get(f"{_REST_PREFIX}/sessions/{{session_id}}/snapshot")
async def get_snapshot(session_id: str) -> JSONResponse:
    session_id = _validate_id(session_id, field="session_id")
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
async def list_sessions(cursor: str | None = None, q: str | None = None) -> JSONResponse:
    page_cursor = _decode_cursor(cursor) if cursor else None
    result = _require_sessions().list_sessions(page_cursor, q)
    return JSONResponse(_result_body(result))


@router.get(f"{_REST_PREFIX}/projects")
async def list_projects() -> JSONResponse:
    result = _require_sessions().projects()
    return JSONResponse(_result_body(result))


@router.get(f"{_REST_PREFIX}/providers")
async def list_providers() -> JSONResponse:
    return JSONResponse(_envelope(_require_providers().list_providers()))


@router.get(f"{_REST_PREFIX}/providers/{{provider_id}}/models")
async def get_provider_models(provider_id: str) -> JSONResponse:
    provider_id = _validate_id(provider_id, field="provider_id")
    return JSONResponse(_envelope(_require_providers().model_catalog(provider_id)))


@router.get(f"{_REST_PREFIX}/runtime-profiles")
async def list_runtime_profiles() -> JSONResponse:
    return JSONResponse(_envelope(_require_providers().runtime_profiles()))


@router.get(f"{_REST_PREFIX}/runs")
async def list_runs(session_id: str | None = None) -> JSONResponse:
    if session_id is not None:
        session_id = _validate_id(session_id, field="session_id")
    result = _require_runs().list_runs(session_id, None)
    return JSONResponse(_result_body(result))


@router.get(f"{_REST_PREFIX}/runs/{{run_id}}")
async def get_run_detail(run_id: str) -> JSONResponse:
    run_id = _validate_id(run_id, field="run_id")
    result = _require_runs().run_detail(run_id)
    return JSONResponse(_result_body(result))


# ---- intent wire parsing (submit() rejects everything this phase; the
# parser is still real so the transport is fully wired, not stubbed) -------

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


_INTENT_PARSERS = {
    "send_prompt": _parse_send_prompt,
    "stop": lambda data, base: Stop(**base, turn_id=str(data.get("turn_id", ""))),
    "approve": lambda data, base: Approve(
        **base,
        approval_ref=str(data.get("approval_ref", "")),
        decision=str(data.get("decision", "")),
        scope=str(data.get("scope", "")),
    ),
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


def _frame_type_name(obj: object) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", type(obj).__name__).lower()


async def _handle_intent(websocket: WebSocket, data: dict) -> None:
    try:
        intent = _parse_intent(data)
    except Exception:
        ack: TransportAck = IntentRejected(
            intent_id=str(data.get("intent_id", "")),
            code="malformed_intent",
            message="intent payload could not be parsed",
        )
    else:
        ack = _require_chat().submit(intent)
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


_FEED_SURFACES = ("sessions", "providers", "runs")


def _feed_surface(name: str):
    return {"sessions": sessions, "providers": providers, "runs": runs}[name]


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
        body = to_wire(frame)
        body["type"] = _frame_type_name(frame)
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
