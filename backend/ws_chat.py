"""WebSocket chat surface: the /ws/chat protocol and its per-connection outbox."""

import asyncio
import json
import logging
import os
import time
import uuid
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

import browser_trust
import auth
import perf
import recovery
from backend.surface_contract.intents import SendTarget, SendTargetKind
# Bounded-outbox/slow-consumer-disconnect mechanism — shared with
# adapter_api.py's /ws/v2/surface route (backend/ws_outbox.py). Kept as a
# local alias so every existing `ws_chat._WebSocketOutbox`/`_WebSocketOutbox(...)`
# reference (this module's own route handler, backend/scripts/test_ws_outbox.py)
# is unaffected by the move.
from backend.ws_outbox import WebSocketOutbox as _WebSocketOutbox
from i18n import t
from session_detail_api import _build_messages_replay_delta, _floor_events_from_seq
from session_manager import manager as session_manager
from stores import pending_approvals
from ws_snapshot_binary import SNAPSHOT_BINARY_SUBPROTOCOL

logger = logging.getLogger(__name__)

router = APIRouter()

coordinator: Any = None
# ChatCommandPort instance (backend/adapters/command_port.py), built by
# backend/surface_commands.py — the SAME transport-neutral command logic
# backend.adapters.chat_adapter.ChatSurfaceAdapter.submit() dispatches to
# for the new Chat Surface Contract transport. Legacy WS handlers call it
# directly (not through submit()) so they keep their exact legacy
# reply-frame contract, which submit()'s accept/reject-only ack cannot
# express. See surface_commands.py's module docstring for what moved.
_command_port: Any = None


def configure(*, coordinator: Any) -> None:
    import surface_commands

    globals()["coordinator"] = coordinator
    globals()["_command_port"] = surface_commands.build_chat_command_port(
        coordinator=coordinator,
    )


async def _request_subscribed_session_recovery(app_session_id: str) -> None:
    await recovery.request_recovered_session(app_session_id)


async def _handle_stop_message(
    active_coordinator: Any,
    app_session_id: str,
    send,
) -> bool:
    # Builds the port from the PASSED-IN coordinator rather than the
    # module-level `_command_port` — this stays callable with an
    # explicit coordinator and no prior `configure()` call (e.g.
    # backend/scripts/test_turn_gating.py), and the port is stateless
    # beyond that reference (see surface_commands.py's factory
    # docstring), so building it fresh here is equivalent to reusing a
    # cached instance.
    import surface_commands

    result = await surface_commands.build_chat_command_port(
        coordinator=active_coordinator,
    ).stop(app_session_id)
    if not result.accepted:
        await send({
            "type": "error",
            "data": {"error": t("error.ws_no_active_turn_to_stop")},
        })
        return False
    await send({
        "type": "stop_acknowledged",
        "data": {"app_session_id": app_session_id, "success": True},
    })
    return True


async def _send_snapshot_refresh_roots(scope, refresh_id, send) -> bool:
    authority = await asyncio.to_thread(_snapshot_refresh_authority, scope)
    if authority is None:
        return await send({
            "type": "snapshot_refresh_complete",
            "data": {
                "refresh_id": refresh_id,
                "success": False,
                "root_ids": [],
            },
        })
    ordered_root_ids = sorted(authority)
    for root_id in ordered_root_ids:
        if not await send({
            "type": "session_reconciled",
            "data": {
                "root_id": root_id,
                "scope_sids": authority[root_id],
                "snapshot_refresh_id": refresh_id,
            },
        }):
            return False
    return await send({
        "type": "snapshot_refresh_complete",
        "data": {
            "refresh_id": refresh_id,
            "success": True,
            "root_ids": ordered_root_ids,
        },
    })


_SNAPSHOT_REFRESH_MAX_SCOPE_SIDS = 512
_SNAPSHOT_REFRESH_MAX_SCOPE_BYTES = 128 * 1024


def _snapshot_refresh_authority(scope):
    authority = {}
    all_scope_sids = set()
    session_ids = sorted({session_id for session_id, _message_id in scope})
    for session_id in session_ids:
        root_id = session_manager._root_id_for(session_id) or session_id
        scope_sids = session_manager.subtree_ids(root_id) or {root_id}
        scope_sids.add(root_id)
        ordered_scope = sorted(scope_sids)
        if (
            len(ordered_scope) > _SNAPSHOT_REFRESH_MAX_SCOPE_SIDS
            or any(
                not isinstance(sid, str) or not sid or len(sid) > 256
                for sid in ordered_scope
            )
            or sum(len(sid.encode("utf-8")) for sid in ordered_scope)
            > _SNAPSHOT_REFRESH_MAX_SCOPE_BYTES
        ):
            return None
        authority[root_id] = ordered_scope
        all_scope_sids.update(ordered_scope)
        if (
            len(all_scope_sids) > _SNAPSHOT_REFRESH_MAX_SCOPE_SIDS
            or sum(len(sid.encode("utf-8")) for sid in all_scope_sids)
            > _SNAPSHOT_REFRESH_MAX_SCOPE_BYTES
        ):
            return None
    return authority or None


@router.websocket("/ws/chat")
async def websocket_chat(websocket: WebSocket):
    # Auth gate. SessionMiddleware populates `websocket.session` on
    # the scope from the cookie sent in the upgrade request (same-
    # origin via Vite proxy in dev, single-port backend in prod —
    # both see the better_agent_session cookie).
    #
    # We MUST accept() before sending a close-with-code: closing
    # pre-accept terminates the handshake with HTTP 403 and the
    # browser never sees a WebSocket close frame (it surfaces as
    # CloseEvent code 1006 "abnormal" — indistinguishable from a
    # backend restart, triggering useWebSocket's reconnect loop).
    # Accepting first means the client gets a real close frame with
    # code 1008, which useWebSocket maps to <Login /> swap.
    if not browser_trust.validate_websocket(websocket):
        await websocket.close(code=1008)
        return
    await _accept_ws_if_needed(websocket)
    user = websocket.session.get("user")
    if not user:
        # Bearer-token fallback for native clients — same rationale as
        # the REST middleware. WS headers are not generally writable
        # from JS in browsers, so we accept the token as a query param
        # too.
        tok = websocket.query_params.get("token")
        tok_user = auth.verify_token(tok) if tok else None
        if tok_user:
            # Do NOT write into websocket.session — the upgrade handshake
            # is itself an HTTP response, so SessionMiddleware would
            # resurrect/extend a cleared or absent session cookie for a
            # connection that only carried a bearer token. See the matching
            # fix in main.py's auth_gate.
            user = tok_user
    if not user:
        await websocket.close(code=1008)
        return
    logger.info("WebSocket connected")
    outbox: _WebSocketOutbox | None = None

    from ws_snapshot_transport import SnapshotTransport

    snapshot_transport: SnapshotTransport | None = None

    async def _send_prepared(event_dict, serialized=None):
        if outbox is None:
            return False
        return await outbox.send(event_dict, serialized)

    async def _send_binary(payload: bytes):
        if outbox is None:
            return False
        return await outbox.send_binary(payload, event_type="snapshot_chunk")

    async def _refresh_snapshot_roots(scope, refresh_id):
        return await _send_snapshot_refresh_roots(
            scope,
            refresh_id,
            _send_prepared,
        )

    async def ws_callback(event_dict):
        if snapshot_transport is None:
            return False
        return await snapshot_transport.send_event(event_dict)

    # Per-connection token so subscription bookkeeping in the coordinator
    # keys on a value that is unique per WS connection and NEVER reused
    # (unlike `id(ws_callback)`, which CPython recycles once the closure is
    # GC'd, letting a stale leaked entry from a dead connection collide with
    # a fresh one and dedupe its re-subscribe away — starving the focused
    # session of live events until a manual switch). Cleanup on disconnect
    # goes through `coordinator.unregister_all_ws`, which drops EVERY session
    # this socket subscribed to (a single socket subscribes to many panes;
    # the old single-`current_app_session_id` cleanup leaked the rest).
    ws_callback._bc_conn_token = uuid.uuid4().hex  # type: ignore[attr-defined]

    async def _close_ws_connection() -> None:
        await asyncio.to_thread(coordinator.unregister_all_ws, ws_callback)

    outbox = _WebSocketOutbox(websocket, on_close=_close_ws_connection)
    snapshot_transport = SnapshotTransport(
        principal=json.dumps(user, sort_keys=True, default=str),
        send=_send_prepared,
        send_binary=_send_binary,
        binary=_snapshot_binary_enabled(websocket),
        refresh=_refresh_snapshot_roots,
    )
    coordinator.register_global_ws(ws_callback)

    def _register(sid: str, *, from_seq: int = 0) -> None:
        coordinator.register_ws(sid, ws_callback, from_seq=from_seq)

    def _unregister(sid: str) -> None:
        coordinator.unregister_ws(sid, ws_callback)

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await ws_callback({"type": "error", "data": {"error": t("error.ws_invalid_json")}})
                continue

            msg_type = msg.get("type")

            if msg_type == "ping":
                # Application-level heartbeat reply. Neither the ASGI
                # server nor a reverse proxy in this stack configures a WS
                # idle timeout, so a connection killed silently by a
                # mobile network transition (OS-suspended background
                # sockets, WiFi<->cellular handoff, carrier NAT idle-drop)
                # would otherwise sit open on both ends forever. The
                # client's heartbeat watchdog (useWebSocket.ts) uses the
                # absence of this reply to detect and repair that. The
                # ingress validator requires every frame to carry a data
                # record — a data-less pong fails invalid_envelope and the
                # watchdog self-closes at timeout.
                await _send_prepared({"type": "pong", "data": {}})
                continue
            if msg_type == "snapshot_ack":
                await snapshot_transport.acknowledge(msg)
                continue
            if msg_type == "snapshot_resume":
                await snapshot_transport.resume(msg)
                continue
            if msg_type == "snapshot_refresh":
                await snapshot_transport.refresh(msg)
                continue

            # Lightweight viewing-without-prompting hook: lets a client
            # tell the backend "I am viewing this session now; register
            # my ws_callback for it." `BetterAgentJsonlTailer` (started
            # by `register_ws`) is the sole live-event WS producer; any
            # worker fan-out from `/api/internal/ask-fork` also reaches
            # this socket via the same callback registry.
            if msg_type == "subscribe":
                sub_sid = msg.get("app_session_id")
                if sub_sid:
                    try:
                        sub_sid_text = str(sub_sid)
                        asyncio.create_task(
                            _request_subscribed_session_recovery(sub_sid_text),
                            name=f"recover-selected-{sub_sid_text[:8]}",
                        )
                    except Exception:
                        logger.debug("startup recovery priority request failed", exc_info=True)
                    # `events_from_seq` is the watermark from the REST
                    # snapshot's `max_seq_by_sid`. The wire tailer drains
                    # `events_from_seq+1..cursor` to this WS before live
                    # events flow — gap-free, dup-free.
                    try:
                        events_from_seq = int(msg.get("events_from_seq") or 0)
                    except (TypeError, ValueError):
                        events_from_seq = 0
                    events_cursor_known = msg.get("events_cursor_known") is True
                    events_from_seq = await asyncio.to_thread(
                        _floor_events_from_seq,
                        sub_sid,
                        events_from_seq,
                        cursor_known=events_cursor_known,
                    )
                    _register(sub_sid, from_seq=events_from_seq)
                    # Sequence-cursor replay. The frontend hands us the
                    # highest seq it has already applied; we send back
                    # every persisted message with `seq >= since_seq` so
                    # reconnects (and cold loads with since_seq=0)
                    # converge on the canonical state without needing
                    # a separate REST refetch path. Includes the live
                    # in-flight assistant message if one is mid-stream
                    # — its in-memory state may be a few ms ahead of
                    # the on-disk snapshot.
                    try:
                        since_seq = int(msg.get("since_seq") or 0)
                    except (TypeError, ValueError):
                        since_seq = 0
                    try:
                        # Unified projection (INV-15 / ADR-1, originally
                        # DIV-1 / OQ-15): WS replay reads the SAME
                        # session_manager cache REST reads. The fold
                        # (SessionProjectionDrainer) keeps that cache
                        # projected from events.jsonl as rows are written —
                        # no inline reconcile here. Cap replay at the same
                        # `msg_limit` REST uses so cold-hop `since_seq=0`
                        # doesn't ship the entire history; frontend
                        # upsert-by-id makes the overlap with REST
                        # harmless.
                        asyncio.create_task(
                            asyncio.to_thread(
                                coordinator.turn_manager.tick_running_state,
                                sub_sid,
                            )
                        )
                        replay_start = time.perf_counter()
                        delta = await asyncio.to_thread(
                            _build_messages_replay_delta,
                            sub_sid,
                            since_seq,
                            limit=50,
                        )
                        replay_delta_ms = (time.perf_counter() - replay_start) * 1000
                        replay_build_ms = replay_delta_ms
                        if delta is not None:
                            replay_post_start = time.perf_counter()
                            replay_msgs = delta["messages"]
                            in_flight = delta.get("in_flight")
                            replay_post_ms = (time.perf_counter() - replay_post_start) * 1000
                            replay_build_ms = replay_delta_ms + replay_post_ms
                            perf.record("ws.replay.delta", replay_delta_ms)
                            perf.record("ws.replay.post", replay_post_ms)
                            send_start = time.perf_counter()
                            await ws_callback({
                                "type": "messages_replay",
                                "data": {
                                    "app_session_id": sub_sid,
                                    "since_seq": since_seq,
                                    "next_seq": delta["next_seq"],
                                    "messages": replay_msgs,
                                },
                            })
                            send_ms = (time.perf_counter() - send_start) * 1000
                            if logger.isEnabledFor(logging.DEBUG):
                                replay_asst = [
                                    m for m in replay_msgs
                                    if m.get("role") == "assistant"
                                ]
                                last_asst_evts = replay_asst[-1].get("events") if replay_asst else None
                                last_asst_stub = replay_asst[-1].get("stub") if replay_asst else None
                                logger.debug(
                                    "WS replay %s: since_seq=%d next_seq=%d msgs=%d "
                                    "inflight=%s last_asst_evts=%s "
                                    "last_asst_stub=%s build=%.1fms delta=%.1fms post=%.1fms send=%.1fms",
                                    sub_sid[:8],
                                    since_seq,
                                    delta["next_seq"],
                                    len(replay_msgs),
                                    in_flight is not None,
                                    len(last_asst_evts) if last_asst_evts else None,
                                    last_asst_stub.get("event_count") if last_asst_stub else None,
                                    replay_build_ms,
                                    replay_delta_ms,
                                    replay_post_ms,
                                    send_ms,
                                )
                            elif replay_build_ms >= 100 or send_ms >= 100:
                                logger.info(
                                    "WS replay %s timings build=%.1fms send=%.1fms",
                                    sub_sid[:8], replay_build_ms, send_ms,
                                )
                    except Exception:
                        logger.exception("messages_replay on subscribe failed")
                    # Push current run_state snapshot so the freshly
                    # subscribed client knows what's running for this
                    # session right now (no waiting for the next
                    # transition).
                    try:
                        _sub_runs = coordinator.turn_manager.get_run_state(sub_sid)
                        if logger.isEnabledFor(logging.DEBUG):
                            logger.debug(
                                "RUNSTATE_DBG[subscribe] sid=%s serves runs=%s",
                                sub_sid[:8],
                                [
                                    f"{(r.get('run_id') or '?')[:8]}|{r.get('kind')}"
                                    f"|pid={r.get('pid')}|started={r.get('started_at')}"
                                    for r in _sub_runs
                                ],
                            )
                        await ws_callback({
                            "type": "run_state",
                            "data": {
                                "app_session_id": sub_sid,
                                "runs": _sub_runs,
                            },
                        })
                    except Exception:
                        logger.exception("run_state replay on subscribe failed")
                    # Re-emit pending fresh-worker approvals so a
                    # frontend that reconnected mid-wait sees the inline
                    # card without depending on the REST rehydration
                    # race. Gate on `approval_waiters` so only
                    # approvals the backend is ACTIVELY waiting on get
                    # re-emitted — orphan disk records (resolved while
                    # the user was disconnected, or stranded by a
                    # crash before the runner re-retried) don't
                    # resurrect dismissed cards.
                    try:
                        active_dids = set(coordinator.approval_waiters.keys())
                        for rec in pending_approvals.list_pending(cwd=None):
                            if rec.get("app_session_id") != sub_sid:
                                continue
                            if rec.get("delegation_id") not in active_dids:
                                continue
                            await ws_callback({
                                "type": "worker_creation_requested",
                                "data": rec,
                            })
                    except Exception:
                        logger.exception("re-emit pending approvals on subscribe failed")
                    # Stale-queue cleanup: if the frontend has a stale
                    # queuedBySession entry for this session (consumed while
                    # unsubscribed), tell it to clear it now. The live
                    # queue_consumed event only reaches subscribers at emit
                    # time, so this re-emit covers the gap.
                    try:
                        import session_queue_projection
                        persisted_queued = await asyncio.to_thread(
                            session_queue_projection.queued_prompts,
                            sub_sid,
                            storage_identity=(
                                session_manager._root_repository.storage_identity()
                            ),
                        )
                        if (
                            not coordinator.has_queued_prompts(sub_sid)
                            and not persisted_queued
                        ):
                            await ws_callback({
                                "type": "queue_consumed",
                                "data": {
                                    "app_session_id": sub_sid,
                                    "queued_id": None,
                                },
                            })
                    except Exception:
                        logger.debug("queue_consumed re-emit on subscribe failed", exc_info=True)
                continue
            if msg_type == "unsubscribe":
                sub_sid = msg.get("app_session_id")
                if sub_sid:
                    _unregister(sub_sid)
                continue

            if msg_type == "send_message":
                # Thin transport shell: parse the frame, build neutral args,
                # hand off to surface_commands' _ChatCommandPortImpl.send_prompt
                # (ADR 0006 command plane). All validation, offline/suspension
                # checks, orchestration-mode gate, Ask-singleton + virtual-session
                # routing, client_id dedup, fork rearm, alter-mode, admission
                # claim, steer/interrupt handling, durable admission, lifecycle
                # emits, and ordered reply frames live there now — this handler
                # only supplies the pieces genuinely bound to this WebSocket
                # connection: `ws_callback` itself (forwarded for turn
                # streaming and deferred async acks, not just reply framing),
                # `_register` (per-connection subscriber bookkeeping), and a
                # `notify` shim that reproduces the exact legacy frame shape.
                async def _ws_notify(frame_type: str, data: dict) -> None:
                    await ws_callback({"type": frame_type, "data": data})

                await _command_port.send_prompt(
                    msg.get("app_session_id"),
                    msg.get("prompt", "").strip(),
                    (),
                    msg.get("send_mode"),
                    SendTarget(kind=SendTargetKind.CURRENT),
                    msg.get("client_id") or "",
                    notify=_ws_notify,
                    ws_callback=ws_callback,
                    register=_register,
                    images=msg.get("images") or [],
                    files=msg.get("files") or [],
                    model=msg.get("model"),
                    cwd=msg.get("cwd", os.path.expanduser("~")),
                    orchestration_mode=msg.get("orchestration_mode"),
                    cli_prompt=msg.get("cli_prompt"),
                    raw_disallowed_tools=msg.get("disallowed_tools"),
                    raw_disabled_builtin_extensions=msg.get("disabled_builtin_extensions"),
                    backend_url=msg.get("backend_url"),
                    raw_known_worker_registry_cwds=msg.get("known_worker_registry_cwds"),
                    raw_capability_contexts=msg.get("capability_contexts"),
                    harness_profile_id=str(msg.get("harness_profile_id") or "").strip(),
                    orchestrator_send_target=msg.get("send_target"),
                    client_id=msg.get("client_id"),
                )

            elif msg_type == "stop_message":
                app_session_id = msg.get("app_session_id")
                if app_session_id:
                    await _handle_stop_message(
                        coordinator,
                        app_session_id,
                        ws_callback,
                    )

            elif msg_type == "promote_queued":
                app_session_id = msg.get("app_session_id")
                if app_session_id:
                    action = msg.get("action")
                    if action not in ("interrupt", "steer"):
                        await ws_callback({
                            "type": "error",
                            "data": {
                                "error": t("error.ws_invalid_send_mode"),
                                "app_session_id": app_session_id,
                                "session_id": app_session_id,
                                "client_id": msg.get("client_id"),
                            },
                        })
                        continue
                    queued_ids_raw = msg.get("queued_ids")
                    queued_ids = (
                        [qid for qid in queued_ids_raw if isinstance(qid, str)]
                        if isinstance(queued_ids_raw, list)
                        else None
                    )
                    promoted = await coordinator.promote_queued(
                        app_session_id,
                        action=action,
                        queued_id=msg.get("queued_id"),
                        queued_ids=queued_ids,
                    )
                    if not promoted:
                        await ws_callback({"type": "error", "data": {"error": t("error.ws_no_queued_prompt")}})

            elif msg_type == "cancel_queued":
                app_session_id = msg.get("app_session_id")
                if app_session_id:
                    queued_id = msg.get("queued_id")
                    await _command_port.delete_queued(
                        app_session_id,
                        queued_id if isinstance(queued_id, str) else None,
                    )

            elif msg_type == "update_queued":
                app_session_id = msg.get("app_session_id")
                queued_id = msg.get("queued_id")
                content = msg.get("content")
                if (
                    isinstance(app_session_id, str)
                    and isinstance(queued_id, str)
                    and isinstance(content, str)
                ):
                    await _command_port.edit_queued(
                        app_session_id, queued_id, content,
                    )

            elif msg_type == "begin_queued_edit":
                app_session_id = msg.get("app_session_id")
                queued_id = msg.get("queued_id")
                if isinstance(app_session_id, str) and isinstance(queued_id, str):
                    coordinator.begin_queued_edit(app_session_id, queued_id)

            elif msg_type == "finish_queued_edit":
                app_session_id = msg.get("app_session_id")
                queued_id = msg.get("queued_id")
                if isinstance(app_session_id, str) and isinstance(queued_id, str):
                    coordinator.finish_queued_edit(app_session_id, queued_id)

    except WebSocketDisconnect:
        logger.info("WebSocket disconnected")
    except RuntimeError as e:
        if "not connected" in str(e):
            logger.info(f"WebSocket connection issue: {e}")
        else:
            logger.exception(f"WebSocket RuntimeError: {e}")
    except Exception as e:
        logger.exception(f"WebSocket error: {e}")
    finally:
        # WS disconnect: just stop fanning events to this socket. Do NOT
        # cancel the coordinator-owned prompt processor — its turn
        # continues, the detached runner keeps producing events, and the
        # session JSON keeps being updated. The next connect+refetch
        # picks up where this socket left off.
        #
        # Unregister EVERY session this socket subscribed to — not just the
        # last one. Leaving non-last subscriptions registered leaks their
        # `ws_callbacks` / `_subscriber_index` entries; on reconnect the
        # stale entry blocks a fresh re-subscribe, starving the focused
        # session of live events until a manual switch.
        await asyncio.to_thread(coordinator.unregister_all_ws, ws_callback)
        if snapshot_transport is not None:
            await snapshot_transport.close()
        if outbox is not None:
            await outbox.close()
            await outbox.wait_closed()


def _snapshot_binary_offered(websocket: WebSocket) -> bool:
    raw = websocket.headers.get("sec-websocket-protocol", "")
    if not isinstance(raw, str) or len(raw) > 1024:
        return False
    return SNAPSHOT_BINARY_SUBPROTOCOL in {
        value.strip() for value in raw.split(",") if value.strip()
    }


def _snapshot_binary_enabled(websocket: WebSocket) -> bool:
    return websocket.scope.get("better_agent_snapshot_binary_v1") is True


async def _accept_ws_if_needed(websocket: WebSocket) -> None:
    if websocket.application_state == WebSocketState.CONNECTED:
        logger.warning("WebSocket entered /ws/chat already accepted")
        return
    binary = _snapshot_binary_offered(websocket)
    await websocket.accept(
        subprotocol=SNAPSHOT_BINARY_SUBPROTOCOL if binary else None,
    )
    websocket.scope["better_agent_snapshot_binary_v1"] = binary


@router.websocket("/{_unknown_ws_path:path}")
async def unknown_websocket(websocket: WebSocket, _unknown_ws_path: str):
    await websocket.close(code=1008)
