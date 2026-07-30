"""Node-side WebSocket client that dials primary.

Run from `main_node.py`. Maintains one persistent WS to
`primary.address + /api/node/connect`. On every drop, reconnects
with exponential backoff (1, 2, 5, 15, 60s caps).

INVARIANT (single-code-path): the node spawns claude subprocesses via
the SAME `ClaudeProvider` the primary uses for its own local workers.
The node's `_drive_run_queue` simply pulls `StreamEvent`s off the
provider's queue and ships them to primary as `event_forward` /
`run_control` messages. NO translation logic on the primary side.

INVARIANT (backpressure): the outbound WS sender wraps an
`asyncio.Queue(maxsize=10000)`. Producers (jsonl_tailer,
provider-queue drainers) use `await queue.put(...)` which blocks
when the queue is full. This naturally back-pressures the local
tailers if primary's network is slow — no memory blow-up.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

import websockets

import app_version
import repository_alignment
import node_identity
import node_rpc_handlers as rpc_handlers
import perf
from node_protocol import PROTOCOL_VERSION
from topology import load_topology

logger = logging.getLogger(__name__)


_BACKOFF_LADDER = (1.0, 2.0, 5.0, 15.0, 60.0)

# How long the node holds its socket open waiting for a human to
# approve/deny its registration on primary. Slightly longer than the
# primary's REGISTRATION_TIMEOUT_S so the node receives primary's
# reject frame rather than racing it.
_REGISTRATION_WAIT_S = 660.0


class NodeClient:
    """Node-side singleton — one instance per node process."""

    def __init__(self) -> None:
        self._ws: Optional[Any] = None
        self._send_queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=10000)
        # INVARIANT: process-singleton — registering at __init__ is
        # safe; no second NodeClient overwrites the gauge.
        perf.register_queue("node.send", self._send_queue.qsize)
        self._sender_task: Optional[asyncio.Task] = None
        self._receiver_task: Optional[asyncio.Task] = None
        self._rpc_tasks: set[asyncio.Task[None]] = set()
        self._incident_replay_tasks: set[asyncio.Task[None]] = set()
        self._connection_generation = 0
        self._connected: asyncio.Event = asyncio.Event()
        self._stop: asyncio.Event = asyncio.Event()
        # node_offset counter; monotonic across reconnects for the
        # process's lifetime. Resume by offset is per-root, primary
        # tells us what it has on reconnect.
        self._next_node_offset: int = 1
        # The (root_id, run_id) → spawn_run payload, so we can reissue
        # spawn_runs on reconnect for the in-flight runs we still own.
        # NOTE: spawn_run reissue is NOT in v1 — if WS dies between
        # spawn_run send and any event, primary's RemoteProviderProxy
        # marks the run failed. Tracked for follow-up.
        self._inflight_spawns: dict[str, dict] = {}

    # ----- Lifecycle -----------------------------------------------------

    async def start(self) -> None:
        """Spawn sender + receiver tasks; reconnect loop runs forever
        until `stop()`."""
        if self._sender_task is not None:
            raise RuntimeError("NodeClient already started")
        self._sender_task = asyncio.create_task(self._reconnect_loop(), name="node-client")
        logger.info("node_client: started")

    async def stop(self) -> None:
        self._stop.set()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
        if self._sender_task is not None:
            self._sender_task.cancel()
            try:
                await self._sender_task
            except (asyncio.CancelledError, Exception):
                pass
            self._sender_task = None
        await self._cancel_and_drain_tasks(self._rpc_tasks)
        await self._cancel_and_drain_tasks(self._incident_replay_tasks)

    # ----- Outbound API for node code ------------------------------------

    async def send(self, message: dict) -> None:
        """Queue a message for primary. Blocks if the send queue is
        full (backpressure to local producers)."""
        await self._send_queue.put(message)

    async def send_event_forward(
        self,
        *,
        root_id: str,
        sid: str,
        event_type: str,
        data: dict,
        source: str,
        run_id: Optional[str] = None,
        msg_id: Optional[str] = None,
    ) -> None:
        node_offset = self._next_node_offset
        self._next_node_offset += 1
        await self.send({
            "type": "event_forward",
            "node_offset": node_offset,
            "root_id": root_id,
            "sid": sid,
            "event_type": event_type,
            "data": data,
            "source": source,
            "run_id": run_id,
            "msg_id": msg_id,
        })

    async def send_jsonl_line(
        self,
        *,
        root_id: str,
        fork_agent_sid: str,
        file_version: int,
        line_offset_in_version: int,
        line: str,
    ) -> None:
        node_offset = self._next_node_offset
        self._next_node_offset += 1
        await self.send({
            "type": "jsonl_line",
            "node_offset": node_offset,
            "root_id": root_id,
            "fork_agent_sid": fork_agent_sid,
            "file_version": file_version,
            "line_offset_in_version": line_offset_in_version,
            "line": line,
        })

    async def send_run_control(
        self,
        *,
        run_id: str,
        control_type: str,
        data: dict,
    ) -> None:
        node_offset = self._next_node_offset
        self._next_node_offset += 1
        await self.send({
            "type": "run_control",
            "node_offset": node_offset,
            "run_id": run_id,
            "control_type": control_type,
            "data": data,
        })

    async def send_extension_incident(self, incident: dict) -> None:
        await self.send({"type": "extension_incident", **incident})

    # ----- Inbound dispatch (called by receiver loop) --------------------

    async def _route_inbound(
        self,
        msg: dict,
        connection_generation: int | None = None,
    ) -> None:
        msg_type = msg.get("type")
        if msg_type == "ping":
            await self.send({"type": "pong", "ts": msg.get("ts", 0)})
            return
        if msg_type == "pong":
            return
        if msg_type == "spawn_run":
            asyncio.create_task(rpc_handlers.handle_spawn_run(self, msg))
            return
        if msg_type == "cancel_run":
            asyncio.create_task(rpc_handlers.handle_cancel_run(self, msg))
            return
        if msg_type == "rehook_run":
            asyncio.create_task(rpc_handlers.handle_rehook_run(self, msg))
            return
        if msg_type == "resume_stream":
            asyncio.create_task(rpc_handlers.handle_resume_stream(self, msg))
            return
        if msg_type == "extension_incident_ack":
            import extension_incident_outbox
            extension_incident_outbox.ack(str(msg.get("incident_id") or ""))
            return
        if msg_type == "rpc_request":
            generation = (
                self._connection_generation
                if connection_generation is None
                else connection_generation
            )
            task = asyncio.create_task(
                self._handle_rpc_request(msg, generation),
                name="node-rpc-request",
            )
            self._rpc_tasks.add(task)
            task.add_done_callback(self._rpc_tasks.discard)
            return
        if msg_type == "restart":
            asyncio.create_task(rpc_handlers.handle_restart(self, msg))
            return
        logger.warning("node_client: unknown msg_type=%r", msg_type)

    def _assert_connection_generation(self, generation: int) -> None:
        if generation != self._connection_generation:
            raise rpc_handlers.StaleProjectionGeneration(
                f"stale node connection generation {generation}; "
                f"current generation is {self._connection_generation}"
            )

    async def _handle_rpc_request(
        self,
        msg: dict,
        connection_generation: int,
    ) -> None:
        request_id = msg.get("request_id")
        method = msg.get("method") or ""
        params = msg.get("params") or {}
        try:
            result = await rpc_handlers.dispatch_rpc(
                method,
                params,
                assert_projection_current=lambda: self._assert_connection_generation(
                    connection_generation
                ),
            )
            self._assert_connection_generation(connection_generation)
            await self.send({
                "type": "rpc_response",
                "request_id": request_id,
                "ok": True,
                "result": result,
            })
        except rpc_handlers.StaleProjectionGeneration:
            logger.info(
                "node_client: dropped stale rpc request method=%s generation=%d",
                method,
                connection_generation,
            )
        except Exception as e:
            logger.exception("node_client: rpc handler raised (method=%s)", method)
            try:
                self._assert_connection_generation(connection_generation)
            except rpc_handlers.StaleProjectionGeneration:
                return
            await self.send({
                "type": "rpc_response",
                "request_id": request_id,
                "ok": False,
                "error": f"{type(e).__name__}: {e}",
            })

    # ----- Reconnect loop ------------------------------------------------

    async def _reconnect_loop(self) -> None:
        backoff_idx = 0
        while not self._stop.is_set():
            try:
                await self._connect_once()
                backoff_idx = 0  # successful connect resets backoff
            except Exception as e:
                logger.warning("node_client: connect failed: %s", e)
            if self._stop.is_set():
                break
            delay = _BACKOFF_LADDER[min(backoff_idx, len(_BACKOFF_LADDER) - 1)]
            backoff_idx += 1
            logger.info("node_client: reconnecting in %.0fs", delay)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    async def _connect_once(self) -> None:
        topology = load_topology()
        primary_url = topology.primary.address.rstrip("/") + "/api/node/connect"
        # Normalize ws/wss scheme: topology.yaml gives http/https or ws/wss.
        if primary_url.startswith("http://"):
            primary_url = "ws://" + primary_url[len("http://"):]
        elif primary_url.startswith("https://"):
            primary_url = "wss://" + primary_url[len("https://"):]

        identity = node_identity.load_or_create()
        token = identity.secret
        my_id = identity.node_id
        build_info = await asyncio.to_thread(app_version.current_build_info)
        repositories = await asyncio.to_thread(
            repository_alignment.current_repositories,
            build_info,
        )
        handshake = {
            "type": "handshake",
            "protocol_version": PROTOCOL_VERSION,
            "node_id": my_id,
            "app_version": build_info,
            "repositories": repositories,
            "registration": {
                "address": identity.address,
                "cwd_roots": list(identity.cwd_roots),
            },
        }

        logger.info("node_client: dialing %s", primary_url)
        async with websockets.connect(
            primary_url,
            additional_headers={"Authorization": f"Bearer {token}"},
            max_size=64 * 1024 * 1024,  # claude messages can be big
        ) as ws:
            self._ws = ws

            # Outbound handshake. `registration` metadata is what the
            # primary shows in its approval popup for a brand-new node;
            # a topology-static / already-approved node ignores it.
            await ws.send(_to_json(handshake))

            # Inbound handshake. A brand-new node first gets a
            # `registration_pending` frame and must keep waiting (up to
            # _REGISTRATION_WAIT_S) for a human to approve it on primary.
            raw = await asyncio.wait_for(ws.recv(), timeout=10)
            msg = _from_json(raw)
            if msg.get("type") == "registration_pending":
                logger.info(
                    "node_client: awaiting approval on primary for node %s "
                    "(approve it in the Better Agent UI)", my_id,
                )
                raw = await asyncio.wait_for(ws.recv(), timeout=_REGISTRATION_WAIT_S)
                msg = _from_json(raw)

            if msg.get("type") == "handshake_reject":
                raise RuntimeError(f"primary rejected handshake: {msg.get('reason')}")
            if (
                msg.get("type") != "handshake"
                or msg.get("protocol_version") != PROTOCOL_VERSION
            ):
                raise RuntimeError(f"primary handshake mismatch: {msg!r}")
            repository_alignment.record_primary_attestation(
                msg.get("repository_alignment_accepted") is True
            )

            self._connection_generation += 1
            connection_generation = self._connection_generation
            self._connected.set()
            logger.info("node_client: connected to primary as %s", my_id)

            sender = asyncio.create_task(self._sender_loop(ws), name="node-sender")
            receiver = asyncio.create_task(
                self._receiver_loop(ws, connection_generation),
                name="node-receiver",
            )
            incident_replay = self._start_extension_incident_replay()
            try:
                completed, _ = await asyncio.wait(
                    [sender, receiver],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in completed:
                    if task.cancelled():
                        logger.warning(
                            "node_client: transport task %s cancelled",
                            task.get_name(),
                        )
                        continue
                    error = task.exception()
                    if error is None:
                        logger.warning(
                            "node_client: transport task %s ended",
                            task.get_name(),
                        )
                    else:
                        logger.warning(
                            "node_client: transport task %s failed: %s",
                            task.get_name(),
                            error,
                            exc_info=(type(error), error, error.__traceback__),
                        )
            finally:
                await self._cancel_extension_incident_replay(incident_replay)
                for t in (sender, receiver):
                    if not t.done():
                        t.cancel()
                await asyncio.gather(sender, receiver, return_exceptions=True)
                if self._connection_generation == connection_generation:
                    self._connection_generation += 1
                self._connected.clear()
                self._ws = None

    async def _cancel_and_drain_tasks(
        self,
        tasks: set[asyncio.Task[None]],
    ) -> None:
        pending = list(tasks)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        tasks.difference_update(pending)

    def _start_extension_incident_replay(self) -> asyncio.Task[None]:
        task = asyncio.create_task(
            self._replay_extension_incidents(),
            name="node-extension-incident-replay",
        )
        self._incident_replay_tasks.add(task)
        task.add_done_callback(self._incident_replay_tasks.discard)
        return task

    async def _cancel_extension_incident_replay(
        self,
        task: asyncio.Task[None],
    ) -> None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self._incident_replay_tasks.discard(task)

    async def _sender_loop(self, ws) -> None:
        while True:
            msg = await self._send_queue.get()
            await ws.send(_to_json(msg))

    async def _replay_extension_incidents(self) -> None:
        import extension_incident_outbox
        for incident in extension_incident_outbox.pending():
            await self.send_extension_incident(incident)

    async def _receiver_loop(self, ws, connection_generation: int) -> None:
        async for raw in ws:
            try:
                msg = _from_json(raw)
            except Exception:
                logger.exception("node_client: bad json from primary")
                continue
            await self._route_inbound(msg, connection_generation)


def _to_json(msg: dict) -> str:
    import json as _json
    return _json.dumps(msg)


def _from_json(raw) -> dict:
    import json as _json
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    return _json.loads(raw)


# Process-singleton (set by main_node.py at startup).
_singleton: Optional[NodeClient] = None


def set_singleton(client: NodeClient) -> None:
    global _singleton
    _singleton = client


def get() -> NodeClient:
    if _singleton is None:
        raise RuntimeError("node_client not initialized (call set_singleton)")
    return _singleton
