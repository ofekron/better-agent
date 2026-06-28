"""Primary-side WebSocket handler for the node ↔ primary link.

Endpoint: `/api/node/connect` (defined here, mounted onto FastAPI app
by main.py). One persistent WS per worker-node. Handshake auths via
bearer token + `PROTOCOL_VERSION` exchange.

Inbound message routing (node → primary):

  event_forward → event journal writer (same call path as local).
  jsonl_line    → shadow_jsonl.append (per-(root_id, fork_agent_sid)
                  WeakValueDictionary asyncio lock + file_version
                  truncate-on-bump).
  run_control   → RemoteProviderProxy run-queue dispatch.
  rpc_response  → resolves the matching pending future in node_store.
  ping          → pong.

Outbound from primary (caller-side API):

  send_spawn_run / send_cancel_run / send_resume_stream / rpc_call.

INVARIANT (single ingestion path): inbound `event_forward` calls
the event journal writer exactly the same way
`_subprocess_agent._ingest_agent_event` calls it for local workers.
UUID dedup handles overlap on reconnect-replay.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
import uuid
from typing import Any, Awaitable, Callable, Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status
from fastapi.exceptions import HTTPException

import node_registry_store
import node_store
import shadow_jsonl
from node_protocol import PROTOCOL_VERSION
from stores import pending_node_registrations
from topology import NodeSpec, load_topology

logger = logging.getLogger(__name__)

router = APIRouter()


# How long a held node WS waits for a human to approve/deny before the
# primary gives up and closes it. The node reconnects with backoff and
# re-requests, so a timeout is not fatal — just a fresh request.
REGISTRATION_TIMEOUT_S = 600.0


# Live registration waiters keyed by node_id. The Future's result is the
# approved NodeSpec (connect) or None (denied). Set while a brand-new
# node is holding its WS open awaiting a human decision; resolved by the
# REST approve/deny handlers via `approve_registration` / `deny_registration`.
_node_approval_waiters: dict[str, asyncio.Future] = {}

# Set by main.py so node_link can fan registration lifecycle events out
# to every open browser WS without importing the coordinator (avoids a
# circular import). Signature: async (event_type: str, payload: dict).
_registration_listener: Optional[Callable[[str, dict], Awaitable[None]]] = None


def _machine_nodes_not_ready_reason() -> Optional[str]:
    import extension_store
    return extension_store.runtime_not_ready_message(
        extension_store.BUILTIN_MACHINE_NODES_EXTENSION_ID
    )


def set_registration_listener(cb: Callable[[str, dict], Awaitable[None]]) -> None:
    global _registration_listener
    _registration_listener = cb


async def _emit_registration(event_type: str, payload: dict) -> None:
    if _registration_listener is None:
        return
    try:
        await _registration_listener(event_type, payload)
    except Exception:
        logger.exception("node_link: registration listener raised (%s)", event_type)


def _public_rec(rec: dict) -> dict:
    """Strip the secret hash before a record crosses to the frontend."""
    return {
        "node_id": rec.get("node_id"),
        "address": rec.get("address"),
        "cwd_roots": rec.get("cwd_roots") or [],
        "fingerprint": rec.get("fingerprint"),
        "status": rec.get("status"),
        "created_at": rec.get("created_at"),
        "expires_at": rec.get("expires_at"),
    }


_public_pending_cache: tuple[int, list[dict]] | None = None
_public_pending_cache_lock = threading.Lock()


def public_pending_nodes() -> list[dict]:
    global _public_pending_cache
    from stores import pending_node_registrations

    version = pending_node_registrations.version()
    with _public_pending_cache_lock:
        cached = _public_pending_cache
        if cached is not None and cached[0] == version:
            return [dict(item) for item in cached[1]]
    projected = [
        _public_rec(rec)
        for rec in pending_node_registrations.list_pending()
    ]
    version = pending_node_registrations.version()
    with _public_pending_cache_lock:
        _public_pending_cache = (version, [dict(item) for item in projected])
    return projected


def _primary_id() -> str:
    """The primary's node id for the reciprocal handshake. Falls back to
    'primary' when no topology.yaml is present (dynamic-only deployment)."""
    try:
        return load_topology().primary.id
    except Exception:
        return "primary"


def _resolve_known_spec(node_id: str, presented_secret: str) -> tuple[Optional[NodeSpec], Optional[str]]:
    """Resolve an ALREADY-KNOWN node and verify its PER-NODE secret.
    Returns (spec, None) on success, (None, reason) on auth failure, or
    (None, None) if the node may still enter the registration-approval
    flow.

    Every node — whether pre-declared in topology.yaml or not —
    authenticates with its own argon2-hashed secret in
    `node_registry_store` (trust-on-first-approve). There is NO shared
    token. topology.yaml is a MANIFEST only: it allowlists which node
    ids may join and pins each declared node's `cwd_roots` as operator
    policy (overriding the node's self-report)."""
    try:
        topo = load_topology()
    except Exception:
        topo = None
    in_topology = topo is not None and node_id in topo.all_nodes()

    # topology.yaml is an allowlist: when it is present, a node id that
    # is neither declared nor already-approved is rejected outright
    # (not pended) so a random id can't surf the approval popup.
    if topo is not None and not in_topology and node_registry_store.get(node_id) is None:
        return None, f"node {node_id!r} is not declared in topology.yaml"

    # Not yet approved (topology-declared pending first approval, or a
    # dynamic-only deploy with no topology) → caller runs the approval flow.
    rec = node_registry_store.get(node_id)
    if rec is None:
        return None, None
    if not presented_secret or not node_registry_store.verify_secret(node_id, presented_secret):
        return None, "bad secret"

    # cwd_roots authority: a topology-declared node serves the manifest's
    # roots (operator policy), NOT its self-reported roots, so a
    # compromised node can't widen its own scope. Dynamic nodes use their
    # self-reported roots from the registry record.
    if in_topology:
        spec = topo.all_nodes()[node_id]
        if spec.role != "worker_node":
            return None, f"node {node_id!r} is role={spec.role!r}, not worker_node"
        return spec, None
    spec = node_registry_store.to_spec(node_id)
    if spec is None:
        return None, "registry record vanished"
    return spec, None


async def approve_registration(node_id: str) -> tuple[Optional[dict], str]:
    """Approve a pending node registration: persist it to the registry
    (so future reconnects auto-auth) and, if the node is currently holding
    its WS open, resolve the waiter so it connects immediately. Idempotent
    via the underlying store transition."""
    rec, reason = pending_node_registrations.approve(node_id)
    if reason != "ok":
        return rec, reason
    node_registry_store.add(
        node_id=node_id,
        address=rec.get("address") or "",
        cwd_roots=rec.get("cwd_roots") or [],
        secret_hash=rec.get("secret_hash") or "",
    )
    spec = node_registry_store.to_spec(node_id)
    _resolve_waiter(node_id, spec)
    await _emit_registration("node_registration_resolved", {"node_id": node_id, "status": "approved"})
    return rec, "ok"


async def deny_registration(node_id: str) -> tuple[Optional[dict], str]:
    rec, reason = pending_node_registrations.deny(node_id)
    if reason != "ok":
        return rec, reason
    _resolve_waiter(node_id, None)
    await _emit_registration("node_registration_resolved", {"node_id": node_id, "status": "denied"})
    return rec, "ok"


def _resolve_waiter(node_id: str, spec: Optional[NodeSpec]) -> bool:
    fut = _node_approval_waiters.get(node_id)
    if fut is not None and not fut.done():
        fut.set_result(spec)
        return True
    return False


# After a denial, suppress re-pending the same node for this window so a
# rejected node's reconnect backoff doesn't re-pop the popup every minute.
# A mistaken denial self-heals once the window lapses.
_DENY_COOLDOWN_S = 300.0


def _recently_denied(rec: Optional[dict]) -> bool:
    if not rec or rec.get("status") != "denied":
        return False
    resolved_at = rec.get("resolved_at")
    if not resolved_at:
        return True
    try:
        from datetime import datetime
        age = (datetime.now() - datetime.fromisoformat(resolved_at)).total_seconds()
    except (ValueError, TypeError):
        return True
    return age < _DENY_COOLDOWN_S


async def _await_registration(
    websocket: WebSocket,
    node_id: str,
    presented_secret: str,
    reg_meta: dict,
) -> Optional[NodeSpec]:
    """Hold a brand-new node's WS open while a human approves/denies it.
    Returns the approved NodeSpec, or None to reject (denied, timed out,
    or malformed request)."""
    if not presented_secret:
        # A dynamic node MUST present a secret so approval can bind to it.
        return None
    if _recently_denied(pending_node_registrations.get(node_id)):
        return None

    secret_hash = node_registry_store.hash_secret(presented_secret)
    fingerprint = hashlib.sha256(presented_secret.encode("utf-8")).hexdigest()[:12]
    address = reg_meta.get("address") if isinstance(reg_meta.get("address"), str) else ""
    cwd_roots = [
        r for r in (reg_meta.get("cwd_roots") or [])
        if isinstance(r, str) and r.startswith("/")
    ]
    rec = pending_node_registrations.create(
        node_id=node_id,
        address=address or "",
        cwd_roots=cwd_roots,
        secret_hash=secret_hash,
        fingerprint=fingerprint,
    )
    logger.info("node_link: %s requesting registration (fp=%s)", node_id, fingerprint)
    await _emit_registration("node_registration_requested", _public_rec(rec))
    try:
        await websocket.send_json({"type": "registration_pending", "node_id": node_id})
    except Exception:
        return None

    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    _node_approval_waiters[node_id] = fut
    try:
        return await asyncio.wait_for(fut, timeout=REGISTRATION_TIMEOUT_S)
    except asyncio.TimeoutError:
        logger.info("node_link: %s registration timed out", node_id)
        return None
    finally:
        _node_approval_waiters.pop(node_id, None)


# Filled in by provider_remote at import time so we don't introduce a
# hard import cycle (provider_remote depends on node_link for sending;
# node_link depends on provider_remote for inbound run-control routing).
_run_control_dispatcher = None
_event_forward_dispatcher = None


def set_dispatchers(*, run_control, event_forward) -> None:
    """Wire-up hook called once at module-load by provider_remote."""
    global _run_control_dispatcher, _event_forward_dispatcher
    _run_control_dispatcher = run_control
    _event_forward_dispatcher = event_forward


@router.websocket("/api/node/connect")
async def node_connect(websocket: WebSocket) -> None:
    # The presented credential is the node's per-node secret (generated by
    # node_identity, or pinned via BETTER_CLAUDE_NODE_TOKEN on the node).
    # Every node — topology-declared or dynamic — authenticates the same
    # way: argon2 verification against node_registry_store. We must accept
    # the socket to read the handshake (which carries registration
    # metadata) before we can decide, so the close happens post-accept.
    auth = websocket.headers.get("authorization") or ""
    presented = auth.removeprefix("Bearer ").strip()

    await websocket.accept()
    nodes_not_ready = _machine_nodes_not_ready_reason()
    if nodes_not_ready is not None:
        await websocket.send_json({
            "type": "handshake_reject",
            "reason": nodes_not_ready,
        })
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    try:
        handshake = await asyncio.wait_for(websocket.receive_json(), timeout=10)
    except (asyncio.TimeoutError, WebSocketDisconnect):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="no handshake")
        return

    if handshake.get("type") != "handshake":
        await websocket.send_json({
            "type": "handshake_reject",
            "reason": f"expected handshake, got {handshake.get('type')!r}",
        })
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    if handshake.get("protocol_version") != PROTOCOL_VERSION:
        reason = (
            f"protocol_version mismatch: node sent "
            f"{handshake.get('protocol_version')!r}, primary expects "
            f"{PROTOCOL_VERSION}"
        )
        await websocket.send_json({"type": "handshake_reject", "reason": reason})
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    node_id = handshake.get("node_id")
    if not isinstance(node_id, str) or not node_id:
        await websocket.send_json({"type": "handshake_reject", "reason": "missing node_id"})
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    spec, auth_reason = _resolve_known_spec(node_id, presented)
    if auth_reason is not None:
        # Known node, but auth failed — hard reject.
        await websocket.send_json({"type": "handshake_reject", "reason": auth_reason})
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    if spec is None:
        # Unknown node → registration-approval flow. Blocks until a human
        # approves/denies in the UI or the request times out.
        spec = await _await_registration(websocket, node_id, presented, handshake.get("registration") or {})
        if spec is None:
            await websocket.send_json({
                "type": "handshake_reject",
                "reason": "registration not approved",
            })
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return

    # Reciprocal handshake — node uses this to verify it's talking to
    # the primary it expects.
    await websocket.send_json({
        "type": "handshake",
        "protocol_version": PROTOCOL_VERSION,
        "node_id": _primary_id(),
    })

    conn = await node_store.register(spec, websocket)
    logger.info("node_link: %s connected", node_id)

    # Send resume_stream so the node knows what we already ingested.
    try:
        last_acked = dict(conn.last_acked_offset)
        shadow_cursors = shadow_jsonl.snapshot_cursors_for(node_id)
        await websocket.send_json({
            "type": "resume_stream",
            "last_acked": last_acked,
            "shadow_jsonls": shadow_cursors,
        })
    except Exception:
        logger.exception("node_link: resume_stream send failed for %s", node_id)

    try:
        async for raw in _iter_json(websocket):
            nodes_not_ready = _machine_nodes_not_ready_reason()
            if nodes_not_ready is not None:
                await websocket.close(
                    code=status.WS_1008_POLICY_VIOLATION,
                    reason=nodes_not_ready,
                )
                return
            await _route_inbound(node_id, raw)
    except WebSocketDisconnect:
        logger.info("node_link: %s disconnected", node_id)
    except Exception:
        logger.exception("node_link: %s inbound loop crashed", node_id)
    finally:
        await node_store.unregister(node_id)


async def _iter_json(ws: WebSocket):
    while True:
        try:
            yield await ws.receive_json()
        except WebSocketDisconnect:
            return


async def _route_inbound(node_id: str, msg: dict) -> None:
    msg_type = msg.get("type")
    node_store.touch_last_seen(node_id)

    if msg_type == "ping":
        conn = node_store.get_connection(node_id)
        if conn:
            try:
                await conn.ws.send_json({"type": "pong", "ts": msg.get("ts", 0)})
            except Exception:
                pass
        return

    if msg_type == "event_forward":
        await _handle_event_forward(node_id, msg)
        return

    if msg_type == "jsonl_line":
        await _handle_jsonl_line(node_id, msg)
        return

    if msg_type == "run_control":
        await _handle_run_control(node_id, msg)
        return

    if msg_type == "rpc_response":
        _handle_rpc_response(node_id, msg)
        return

    if msg_type in ("pong",):
        return

    logger.warning("node_link: %s sent unknown msg_type=%r", node_id, msg_type)


async def _handle_event_forward(node_id: str, msg: dict) -> None:
    root_id = msg.get("root_id")
    if not isinstance(root_id, str):
        return
    sid = msg.get("sid")
    event_type = msg.get("event_type")
    data = msg.get("data") or {}
    source = msg.get("source") or f"remote_node:{node_id}"
    run_id = msg.get("run_id")
    msg_id = msg.get("msg_id")
    node_offset = msg.get("node_offset")

    try:
        # Single-code-path: SAME entry point as
        # _subprocess_agent._ingest_agent_event. UUID dedup makes this
        # idempotent for reconnect-replay.
        from event_journal import publish_event
        await publish_event(
            session_id=root_id,
            context_id=sid,
            event_type=event_type,
            data=data,
            source=source,
            run_id=run_id,
            message_id=msg_id,
        )
    except Exception:
        logger.exception("node_link: event journal write failed (node=%s)", node_id)
        return

    if isinstance(node_offset, int):
        conn = node_store.get_connection(node_id)
        if conn:
            prev = conn.last_acked_offset.get(root_id, 0)
            if node_offset > prev:
                conn.last_acked_offset[root_id] = node_offset
                # 1-second coalescer persists this to disk so primary
                # crash doesn't reset the node's resume cursor to 0.
                node_store.mark_offsets_dirty(node_id)

    # Also feed the worker-event WS broadcast path used by the manager's
    # delegate streaming. This is the same fan-out the local provider
    # does via the asyncio.Queue → ws_callback chain in _delegation.
    if _event_forward_dispatcher is not None and run_id:
        try:
            await _event_forward_dispatcher(
                node_id=node_id, run_id=run_id, event_type=event_type, data=data,
            )
        except Exception:
            logger.exception("node_link: event_forward dispatcher raised")


async def _handle_jsonl_line(node_id: str, msg: dict) -> None:
    try:
        await shadow_jsonl.append(
            node_id=node_id,
            root_id=msg["root_id"],
            fork_agent_sid=msg["fork_agent_sid"],
            file_version=int(msg["file_version"]),
            line_offset_in_version=int(msg["line_offset_in_version"]),
            line=msg["line"],
        )
    except KeyError as e:
        logger.warning("node_link: malformed jsonl_line missing %s (node=%s)", e, node_id)
        return
    except Exception:
        logger.exception("node_link: shadow_jsonl.append failed (node=%s)", node_id)
        return

    node_offset = msg.get("node_offset")
    root_id = msg.get("root_id")
    if isinstance(node_offset, int) and isinstance(root_id, str):
        conn = node_store.get_connection(node_id)
        if conn:
            prev = conn.last_acked_offset.get(root_id, 0)
            if node_offset > prev:
                conn.last_acked_offset[root_id] = node_offset
                node_store.mark_offsets_dirty(node_id)


async def _handle_run_control(node_id: str, msg: dict) -> None:
    if _run_control_dispatcher is None:
        logger.warning(
            "node_link: run_control received but provider_remote not wired "
            "(node=%s run_id=%s)", node_id, msg.get("run_id"),
        )
        return
    try:
        await _run_control_dispatcher(
            node_id=node_id,
            run_id=msg.get("run_id"),
            control_type=msg.get("control_type"),
            data=msg.get("data") or {},
        )
    except Exception:
        logger.exception("node_link: run_control dispatcher raised")


def _handle_rpc_response(node_id: str, msg: dict) -> None:
    request_id = msg.get("request_id")
    conn = node_store.get_connection(node_id)
    if not conn or not request_id:
        return
    fut = conn.pending_rpcs.pop(request_id, None)
    if fut is None or fut.done():
        return
    if msg.get("ok"):
        fut.set_result(msg.get("result"))
    else:
        fut.set_exception(RuntimeError(msg.get("error") or "rpc failed"))


# ============================================================================
# Outbound API — primary-side code calls these to talk to a node.
# ============================================================================

class NodeOffline(RuntimeError):
    """Raised when the target node has no live WS."""


async def send_spawn_run(node_id: str, payload: dict) -> None:
    conn = node_store.get_connection(node_id)
    if conn is None:
        raise NodeOffline(f"node {node_id!r} is not connected")
    await conn.ws.send_json({"type": "spawn_run", **payload})


async def send_rehook_run(node_id: str, run_id: str) -> None:
    """Ask the node to rebuild its shipping ctx for a still-running
    run (primary lost its drain state across a restart). Fire-and-
    forget: idempotent on the node, and a lost frame self-heals on
    the next reconnect-triggered recovery pass."""
    conn = node_store.get_connection(node_id)
    if conn is None:
        raise NodeOffline(f"node {node_id!r} is not connected")
    await conn.ws.send_json({"type": "rehook_run", "run_id": run_id})


async def send_cancel_run(node_id: str, run_id: str) -> bool:
    conn = node_store.get_connection(node_id)
    if conn is None:
        return False
    try:
        await conn.ws.send_json({"type": "cancel_run", "run_id": run_id})
        return True
    except Exception:
        logger.exception("node_link.send_cancel_run failed for %s", node_id)
        return False


async def send_restart(node_id: str) -> None:
    conn = node_store.get_connection(node_id)
    if conn is None:
        raise NodeOffline(f"node {node_id!r} is not connected")
    await conn.ws.send_json({"type": "restart"})


async def rpc_call(
    node_id: str,
    method: str,
    params: Optional[dict] = None,
    *,
    timeout: float = 30.0,
) -> Optional[dict]:
    """Send an `rpc_request` to a node and await its `rpc_response`.

    Used for filetree, ls, and other on-demand reads from primary into
    a node's filesystem. Returns the response `result` dict (whatever
    the node's handler shaped). Raises on timeout, node offline, or
    error reply.
    """
    conn = node_store.get_connection(node_id)
    if conn is None:
        raise NodeOffline(f"node {node_id!r} is not connected")
    request_id = str(uuid.uuid4())
    fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    conn.pending_rpcs[request_id] = fut
    try:
        await conn.ws.send_json({
            "type": "rpc_request",
            "request_id": request_id,
            "method": method,
            "params": params or {},
        })
        return await asyncio.wait_for(fut, timeout=timeout)
    except asyncio.TimeoutError:
        conn.pending_rpcs.pop(request_id, None)
        raise
    finally:
        conn.pending_rpcs.pop(request_id, None)
