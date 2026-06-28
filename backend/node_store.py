"""In-memory registry of live worker-node connections (primary-side only).

Holds zero authoritative state — the topology is the source of truth
for which nodes EXIST; the canonical record of which workers run on
which node lives in session_store/worker_store. node_store only
tracks the transient live-WS handle and connection state, plus the
inflight-RPC futures keyed by request_id.

Frontend gets a snapshot via `GET /api/nodes` and live updates via
the `node_state_changed` WS broadcast.

**Last-acked-offset persistence.** The `last_acked_offset` dict on
each `NodeConnection` records the highest `node_offset` primary has
successfully ingested per root_id. On reconnect, primary sends a
`resume_stream` with these offsets so the node only replays events
past them. UUID dedup at `event_ingester` is the correctness net;
offsets are the efficiency net.

Before A6, offsets lived ONLY in memory and were lost on primary
restart — every node had to replay from offset 0 after a crash,
flooding the network until dedup caught up. Now offsets are
persisted to `ba_home()/node_store/<node_id>.json` atomically
(tmp+rename), seeded from disk into `last_acked_offset` on
`register()`, and flushed by a 1-second background coalescer plus a
forced flush on `unregister()`. Per-ack disk writes would block the
event loop; the 1-second window bounds the post-crash replay
overhead.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from env_compat import get_env
from paths import ba_home
from topology import NodeSpec, load_topology, local_node_id


def _local_node_id_or_primary() -> str:
    """The local host's node id, falling back to the legacy `"primary"`
    sentinel when topology.yaml is absent (dynamic-only deploy). Mirrors
    `main._local_node_id_or_primary` — duplicated here because `main`
    imports `node_store` (circular import). Keep these in sync."""
    try:
        return local_node_id()
    except Exception:
        return "primary"

logger = logging.getLogger(__name__)


@dataclass
class NodeConnection:
    """One live WS to a worker-node."""
    spec: NodeSpec
    ws: Any                                  # the FastAPI WebSocket; opaque here
    connected_at: float
    last_seen: float
    # Inflight RPC futures keyed by request_id.
    pending_rpcs: dict[str, asyncio.Future] = field(default_factory=dict)
    # Inflight runs keyed by run_id — proxies a RemoteRunState held by
    # provider_remote so cancel/event-forward can route correctly.
    runs: dict[str, Any] = field(default_factory=dict)
    # Highest node_offset primary has successfully ingested per root_id.
    # Used to drive resume_stream on reconnect. Persisted by the
    # background flush coalescer (see `_offset_flush_loop`). Seeded
    # from disk in `register()` so a primary crash doesn't force every
    # node to replay from offset 0.
    last_acked_offset: dict[str, int] = field(default_factory=dict)


# state ∈ {"connected", "disconnected"}; "disconnected" entries are
# kept around so the frontend can see "node offline" badges instead of
# the node disappearing. We never drop a key once it's been seen.
_state: dict[str, str] = {}
_conns: dict[str, NodeConnection] = {}
_state_version = 0
# Subscribers fire on every transition. List of async callables.
_listeners: list[Callable[[str, str], Awaitable[None]]] = []

# ============================================================================
# last_acked_offset persistence
# ============================================================================
# A per-node JSON file at `ba_home()/node_store/<node_id>.json` records
# `{last_acked_offset: {root_id: node_offset, ...}, updated_at: "..."}`.
# Writes are atomic (tmp + rename). The 1-second background coalescer
# `_offset_flush_loop` services nodes whose offsets have moved since
# the last flush; `unregister()` force-flushes before disposing of the
# connection. Per-ack synchronous writes are NOT used — they would
# block the event loop on every event_forward.

_OFFSETS_DIR_NAME = "node_store"


def _flush_interval_s() -> float:
    """Coalescer wakeup interval. Default 1.0s; override with
    `BETTER_CLAUDE_NODE_FLUSH_INTERVAL_S` so integration tests can
    drive the loop faster than 1 wall-second per flush. Reads each
    call so tests can mutate env mid-process."""
    raw = get_env("BETTER_CLAUDE_NODE_FLUSH_INTERVAL_S").strip()
    if not raw:
        return 1.0
    try:
        v = float(raw)
        return v if v > 0 else 1.0
    except ValueError:
        return 1.0


# Set of node_ids whose `last_acked_offset` has advanced since the
# last successful disk write. Mutated by `mark_offsets_dirty`, drained
# by `_offset_flush_loop` and `flush_offsets`.
_dirty_nodes: set[str] = set()
# Per-node async lock serializing flushes. Without this, the
# coalescer + a concurrent `unregister`-triggered flush can race and
# the last `os.replace` wins — silently losing the newer snapshot.
_flush_locks: dict[str, asyncio.Lock] = {}
# Singleton background task that drains `_dirty_nodes`. Started by
# `start_offset_flush_loop` at primary startup, cancelled by
# `stop_offset_flush_loop` on shutdown.
_flush_task: Optional[asyncio.Task] = None


def _offsets_dir() -> Path:
    """`ba_home()/node_store/`. Created lazily by `_save_offsets_atomic`
    so reads don't fsync-write-cycle the parent on every access."""
    return ba_home() / _OFFSETS_DIR_NAME


def _offsets_path(node_id: str) -> Path:
    """`ba_home()/node_store/<node_id>.json`. Per-node file so a write
    can't corrupt sibling nodes' offsets even on torn rename. Read-only
    path resolution — does NOT create the directory."""
    return _offsets_dir() / f"{node_id}.json"


def _get_flush_lock(node_id: str) -> asyncio.Lock:
    """Lazy per-node lock. Per-node so two nodes can flush in parallel
    while same-node flushes serialize."""
    lock = _flush_locks.get(node_id)
    if lock is None:
        lock = asyncio.Lock()
        _flush_locks[node_id] = lock
    return lock


_OFFSETS_SCHEMA_VERSION = 1


def _load_persisted_offsets(node_id: str) -> dict[str, int]:
    """Load the persisted `{root_id: node_offset}` map for a node, or
    `{}` if the file is missing / malformed / from a different schema
    version. Soft-fails — a missing/rejected file means "replay from 0",
    same outcome as before A6 (correct via UUID dedup, just a bandwidth
    cost). Per CLAUDE.md "schema migrations are NOT supported" — a
    mismatched `schema_version` is treated as absent (operator wipes
    `ba_home()/node_store/` to start fresh)."""
    p = _offsets_path(node_id)
    try:
        if not p.exists():
            return {}
    except OSError:
        return {}
    try:
        with p.open("r", encoding="utf-8") as f:
            payload = json.load(f)
    except IsADirectoryError:
        # Something put a directory where our file should be — log
        # loudly so the operator notices, then treat as missing.
        logger.error(
            "node_store: %s is a directory, expected a file; treating "
            "as missing. Remove the dir to restore persistence.", p,
        )
        return {}
    except Exception:
        logger.exception(
            "node_store: failed to load persisted offsets for %s; "
            "treating as empty", node_id,
        )
        return {}
    if not isinstance(payload, dict):
        return {}
    schema = payload.get("schema_version")
    if schema != _OFFSETS_SCHEMA_VERSION:
        logger.warning(
            "node_store: %s has schema_version=%r (expected %d); "
            "ignoring file (no migration — wipe %s to reset)",
            p, schema, _OFFSETS_SCHEMA_VERSION, _offsets_dir(),
        )
        return {}
    offs = payload.get("last_acked_offset")
    if not isinstance(offs, dict):
        return {}
    # Strictly accept non-negative ints. Floats / strings / nulls /
    # negatives indicate a corrupt file (or a future schema), so drop
    # the entry rather than truncating silently — `int(1e20)` would
    # otherwise produce a huge offset that no future ack could ever
    # advance past; a negative offset never advances.
    out: dict[str, int] = {}
    for k, v in offs.items():
        if isinstance(v, bool):
            # bool is a subclass of int — explicit reject.
            continue
        if isinstance(v, int) and v >= 0:
            out[str(k)] = v
    return out


def _save_offsets_atomic(node_id: str, offsets: dict[str, int]) -> None:
    """Atomic write of `{root_id: node_offset}` to disk. Called from a
    worker thread via `asyncio.to_thread` — never directly from async
    code so the event loop doesn't block on disk IO.

    Durability:
      1. Write tmp + fsync(file).
      2. Atomic rename onto target via `os.replace`.
      3. fsync(parent dir) — so the rename itself survives power loss
         on ext4/xfs (the file's fsync alone doesn't sync the
         containing directory entry). No-op on macOS/APFS where
         directory metadata is journalled regardless.
    """
    d = _offsets_dir()
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{node_id}.json"
    tmp = p.with_suffix(p.suffix + ".tmp")
    payload = {
        "schema_version": _OFFSETS_SCHEMA_VERSION,
        "last_acked_offset": dict(offsets),
        "updated_at": time.time(),
    }
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)
    # Sync the directory so the rename is durable on power loss.
    try:
        dir_fd = os.open(str(d), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        # Windows + some FUSE filesystems don't allow opening dirs
        # for fsync. The file's own fsync is the best we can do
        # there; rename atomicity is OS-dependent regardless.
        pass


def mark_offsets_dirty(node_id: str) -> None:
    """Flag this node's offsets as needing a disk flush. O(1); safe to
    call from every ack site. The 1-second coalescer picks it up."""
    _dirty_nodes.add(node_id)


async def flush_offsets(
    node_id: str, *, conn: Optional["NodeConnection"] = None,
) -> None:
    """Force-flush one node's offsets to disk. Used on `unregister` so
    a graceful disconnect always lands the latest offsets, and at
    shutdown. Quietly no-ops if the connection has vanished.

    INVARIANT: serialized per-node via `_get_flush_lock(node_id)` so a
    coalescer-driven flush and an `unregister`-driven flush can't both
    `os.replace` the file in arbitrary order and lose the newer
    snapshot.

    INVARIANT: monotone — writes never regress an offset. The merge
    with disk is required because `unregister` passes the LEAVING
    connection's snapshot (which may have unflushed acks newer than
    disk), while a concurrent fresh `register` for the same node_id
    may have already coalescer-flushed its own (possibly newer)
    offsets to disk. Without the merge, the unregister-driven write
    of the leaving snapshot could overwrite newer offsets the new
    conn just persisted. Reading + merging on every flush is cheap
    (the file is tiny) and makes the per-root_id offset monotone by
    construction.

    `conn` may be supplied to pin the snapshot source — `unregister`
    uses this to flush THE OUTGOING connection's accumulated acks,
    NOT whatever `_conns[node_id]` happens to point at after a
    reconnect race. Default `None` looks up the registry (coalescer
    path).
    """
    async with _get_flush_lock(node_id):
        source = conn if conn is not None else _conns.get(node_id)
        if source is None:
            # Connection gone — nothing to flush. The persisted file is
            # already the latest snapshot we ever held.
            _dirty_nodes.discard(node_id)
            return
        # Snapshot under lock + clear-then-write order: capture the
        # current offsets, drop the dirty flag, then write. If an ack
        # arrives between snapshot and rename, it re-sets the dirty
        # flag (under no lock — `_dirty_nodes` is a plain set, GIL-safe
        # for add/discard) so the next coalescer cycle picks it up.
        snapshot = dict(source.last_acked_offset)
        _dirty_nodes.discard(node_id)
        try:
            # Monotone-merge with disk. See INVARIANT above.
            persisted = await asyncio.to_thread(_load_persisted_offsets, node_id)
            merged = dict(persisted)
            for root_id, off in snapshot.items():
                if off > merged.get(root_id, 0):
                    merged[root_id] = off
            await asyncio.to_thread(_save_offsets_atomic, node_id, merged)
        except Exception:
            # Re-mark dirty so a subsequent flush retries. Soft fail —
            # primary crash with un-flushed offsets just means the node
            # replays a few seconds of events (UUID dedup catches them).
            _dirty_nodes.add(node_id)
            logger.exception(
                "node_store: persist offsets failed for %s; will retry",
                node_id,
            )


async def _offset_flush_loop() -> None:
    """Background coalescer: every `_flush_interval_s()` flushes every
    dirty node. Wraps the inner per-node flush in a try/except so a
    transient flush failure can't tear down the loop forever."""
    while True:
        try:
            await asyncio.sleep(_flush_interval_s())
        except asyncio.CancelledError:
            return
        if not _dirty_nodes:
            continue
        for node_id in list(_dirty_nodes):
            try:
                await flush_offsets(node_id)
            except Exception:
                # `flush_offsets` already logs + re-marks dirty inside
                # its own except; this is belt-and-suspenders to keep
                # the loop alive against an unforeseen exception class.
                logger.exception(
                    "node_store: offset flush loop swallowed exception "
                    "for %s", node_id,
                )


def start_offset_flush_loop() -> None:
    """Spawn the background coalescer task. Idempotent — re-calling
    while running is a no-op. Wired from `main.py` startup."""
    global _flush_task
    if _flush_task is not None and not _flush_task.done():
        return
    _flush_task = asyncio.create_task(_offset_flush_loop())


async def stop_offset_flush_loop() -> None:
    """Cancel the background coalescer + final-flush every node still
    holding dirty offsets. Wired from shutdown."""
    global _flush_task
    if _flush_task is not None:
        _flush_task.cancel()
        try:
            await _flush_task
        except (asyncio.CancelledError, Exception):
            pass
        _flush_task = None
    for node_id in list(_dirty_nodes):
        await flush_offsets(node_id)


def add_listener(cb: Callable[[str, str], Awaitable[None]]) -> None:
    """Subscribe to (node_id, new_state) transitions. Listeners must be
    async — fired sequentially under the caller's event loop."""
    _listeners.append(cb)


async def _fire(node_id: str, state: str) -> None:
    for cb in list(_listeners):
        try:
            await cb(node_id, state)
        except Exception:
            logger.exception("node_store listener raised for %s", node_id)


async def register(spec: NodeSpec, ws: Any) -> NodeConnection:
    """Mark a node as connected with its live WS handle. Replaces any
    prior NodeConnection for the same id (re-registration races on
    rapid reconnect drop the older socket on the floor — caller's
    responsibility to close the displaced ws if needed).

    Seeds `last_acked_offset` from the persisted file so a primary
    crash + restart doesn't force the node to replay from offset 0.
    File IO runs in a worker thread to avoid blocking the event loop
    on slow disks. The disk-vs-in-memory monotone-merge that
    guarantees `max(persisted, in_memory)` across writes lives in
    `flush_offsets`; here we just seed the new connection's in-memory
    map from disk and let subsequent acks advance it forward."""
    now = time.time()
    persisted = await asyncio.to_thread(_load_persisted_offsets, spec.id)
    conn = NodeConnection(
        spec=spec,
        ws=ws,
        connected_at=now,
        last_seen=now,
        last_acked_offset=persisted,
    )
    global _state_version
    _conns[spec.id] = conn
    prev = _state.get(spec.id)
    _state[spec.id] = "connected"
    if prev != "connected":
        _state_version += 1
    if prev != "connected":
        await _fire(spec.id, "connected")
    return conn


async def unregister(node_id: str) -> None:
    # Capture the connection identity at entry. If a rapid reconnect
    # races us (a fresh `register` swaps `_conns[node_id]` to a new
    # NodeConnection while we're awaiting the flush), we must NOT
    # subsequently pop or flip state — that would kick the freshly-
    # registered live connection out of the registry.
    leaving = _conns.get(node_id)
    if leaving is None:
        # Already unregistered, nothing to do.
        return
    # Flush BEFORE we pop the conn so the in-memory snapshot is still
    # readable. Pass `leaving` explicitly so that even if a fresh
    # `register` for the same node_id races and replaces `_conns[node_id]`
    # while we're awaiting, we still flush THIS connection's
    # accumulated acks — not the freshly-seeded new connection's
    # offsets (which would silently drop our unflushed data).
    await flush_offsets(node_id, conn=leaving)
    # Re-check identity AFTER the await: if a fresh register replaced
    # the conn while we were flushing, the new one wins and we leave
    # everything else alone.
    if _conns.get(node_id) is not leaving:
        return
    global _state_version
    _conns.pop(node_id, None)
    prev = _state.get(node_id)
    _state[node_id] = "disconnected"
    if prev != "disconnected":
        _state_version += 1
    if prev != "disconnected":
        await _fire(node_id, "disconnected")


def get_connection(node_id: str) -> Optional[NodeConnection]:
    return _conns.get(node_id)


async def forget(node_id: str) -> None:
    """Remove all in-memory state for a node and broadcast disconnected.
    Used after a node is deleted from the registry/topology so snapshot()
    won't re-materialize it from the orphan-fallback path."""
    global _state_version
    had_state = node_id in _state or node_id in _conns
    _conns.pop(node_id, None)
    _state.pop(node_id, None)
    if had_state:
        _state_version += 1
    await _fire(node_id, "disconnected")


def state(node_id: str) -> str:
    """Returns 'connected', 'disconnected', or 'unknown' (node hasn't
    been seen yet — distinct from disconnected because it's a fresh
    process)."""
    return _state.get(node_id, "unknown")


def snapshot() -> list[dict]:
    """REST projection: every KNOWN node + its live state. Used by
    `GET /api/nodes`. Sources, in priority order:
      1. topology.yaml (static nodes + the primary), if present.
      2. node_registry_store (dynamic nodes approved via the popup).
      3. any node we've seen a live/closed connection for but that is
         in neither source (defensive — shouldn't normally happen).
    Tolerates a missing topology.yaml so dynamic-only deployments still
    surface their nodes."""
    specs: dict[str, NodeSpec] = {}
    try:
        for node_id, spec in load_topology().all_nodes().items():
            specs[node_id] = spec
    except Exception:
        pass

    # Always represent the local/primary host. In a dynamic-only deploy
    # (no topology.yaml) the block above raised and left `specs` with no
    # primary, so /api/nodes omitted the host — the "run on" picker then
    # offered only remote workers while its default `node_id="primary"`
    # matched no <option> (a controlled <select> silently rendering the
    # first worker), so sessions landed on "primary" regardless of the
    # visible choice, and no machine ever showed the "(host)" badge. The
    # id matches what /api/local_node_id returns, so the frontend's
    # `m.id === localNodeId` host badge lights up across the picker,
    # machine-node UI, and DirPicker. Skip when a primary already exists
    # (topology-present deploy) to avoid duplicating the host.
    if not any(s.role == "primary" for s in specs.values()):
        local_id = _local_node_id_or_primary()
        specs[local_id] = NodeSpec(
            id=local_id, role="primary", address="local", cwd_roots=(),
        )

    try:
        import node_registry_store
        for rec in node_registry_store.list_all():
            nid = rec.get("node_id")
            if nid and nid not in specs:
                specs[nid] = NodeSpec(
                    id=nid,
                    role="worker_node",
                    address=rec.get("address") or "",
                    cwd_roots=tuple(rec.get("cwd_roots") or ()),
                )
    except Exception:
        logger.exception("node_store.snapshot: registry merge failed")

    for nid in set(_state) | set(_conns):
        if nid in specs:
            continue
        conn = _conns.get(nid)
        specs[nid] = conn.spec if conn else NodeSpec(
            id=nid, role="worker_node", address="", cwd_roots=(),
        )

    out: list[dict] = []
    for node_id, spec in specs.items():
        conn = _conns.get(node_id)
        out.append({
            "id": node_id,
            "role": spec.role,
            "address": spec.address,
            "cwd_roots": list(spec.cwd_roots),
            "state": _state.get(node_id, "connected" if spec.role == "primary" else "unknown"),
            "connected_at": conn.connected_at if conn else None,
            "last_seen": conn.last_seen if conn else None,
        })
    return out


def connected_worker_node_ids_snapshot() -> tuple[int, tuple[str, ...]]:
    return (
        _state_version,
        tuple(
            sorted(
                node_id
                for node_id, conn in _conns.items()
                if _state.get(node_id) == "connected" and conn.spec.role != "primary"
            )
        ),
    )


def touch_last_seen(node_id: str) -> None:
    """Bump last_seen on every inbound message — heartbeat tracking."""
    conn = _conns.get(node_id)
    if conn:
        conn.last_seen = time.time()


def reset_for_tests() -> None:
    """Test hook only — wipes in-memory registry. Never called by
    production code paths. Best-effort cancels the background flush
    task SYNCHRONOUSLY (no await available here); callers needing a
    deterministic stop should await `stop_offset_flush_loop` instead.
    Callers expecting a fully fresh process should also nuke
    `ba_home()/node_store/` if BETTER_CLAUDE_HOME is shared."""
    global _flush_task, _state_version
    if _flush_task is not None and not _flush_task.done():
        _flush_task.cancel()
    _flush_task = None
    _conns.clear()
    _state.clear()
    _state_version += 1
    _listeners.clear()
    _dirty_nodes.clear()
    _flush_locks.clear()
