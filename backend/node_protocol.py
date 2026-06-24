"""Wire protocol between primary and worker-node backends.

INVARIANT — Trust boundary: the bearer token (`BETTER_CLAUDE_NODE_TOKEN`)
authenticates each node's WS connection to primary. Primary's per-worker
`internal_token` is shipped to nodes inside `spawn_run` payloads (so
the spawned worker on the node can call back into primary's
`/api/internal/*` endpoints — ask-fork, delegate, etc.). A compromised
node therefore = primary
compromised. Trust model is LAN/VPN — no further mitigations layered
in v1.

Topology: ALL nodes dial primary (NAT-friendly). Each node holds exactly
one persistent WS to primary. Multiplexing is by `run_id` for run-scoped
messages; node identity is implicit per WS connection.

Single-code-path INVARIANT (per CLAUDE.md): translation logic that
turns a claude subprocess's stream into `StreamEvent`s lives ONLY in
the node-side `ClaudeProvider`. The node serializes each queue item
into `event_forward` (for ingestible events) or `run_control` (for
session_discovered/complete/error control). Primary's
`RemoteProviderProxy` is PURE TRANSPORT — it deserializes back into
`StreamEvent` and re-pushes onto a local `asyncio.Queue` exactly like
the local provider would. No fork.

Resume cursor: every event_forward/jsonl_line message carries a
node-side monotonic `node_offset`. Primary remembers
`last_acked_node_offset[root_id]` in-memory; on reconnect, sends a
`resume_stream` with those offsets and the node replays. UUID dedup
(event_ingester) is the correctness net; offset-based resume is the
efficiency net. Primary's `seq` is local-only and never crosses the
wire.

Shadow JSONL file versioning: claude rewrites its jsonl on
compaction. Node detects via tail-F file rotation (offset reset
backward) and bumps `file_version`. Each `jsonl_line` carries
`(file_version, line_offset_in_version)`. Primary truncates its
shadow file when it sees a higher `file_version`.
"""

from __future__ import annotations

from typing import Literal, Optional, TypedDict


PROTOCOL_VERSION = 1


# ============================================================================
# Handshake — first message in each direction after WS accept.
# ============================================================================
class Handshake(TypedDict):
    type: Literal["handshake"]
    protocol_version: int
    node_id: str  # The node's id from topology.yaml. Set by both ends.


class HandshakeReject(TypedDict):
    type: Literal["handshake_reject"]
    reason: str


# ============================================================================
# Primary → Node messages
# ============================================================================
class SpawnRun(TypedDict, total=False):
    """Mirrors ClaudeProvider.start_run kwargs. Sent when primary's
    RemoteProviderProxy.start_run wants the node to spawn a worker."""
    type: Literal["spawn_run"]
    run_id: str
    prompt: str
    cwd: str
    model: Optional[str]
    reasoning_effort: Optional[str]
    session_id: Optional[str]
    mode: str  # "native" | "manager"
    app_session_id: str
    worker_agent_session_id: Optional[str]
    root_id: str  # primary's root_id this run's events should be ingested into
    fork: bool
    setting_sources: Optional[list[str]]
    disallowed_tools: Optional[list[str]]
    backend_url: Optional[str]       # what URL the spawned worker should call back to
    internal_token: Optional[str]    # primary's internal_token (so worker can authenticate)
    supervised: bool
    supervisor_agent_session_id: Optional[str]
    browser_test_enabled: bool
    open_file_panel_enabled: bool
    extra_env: Optional[dict[str, str]]
    disabled_builtin_extensions: Optional[list[str]]


class CancelRun(TypedDict):
    type: Literal["cancel_run"]
    run_id: str


class Restart(TypedDict):
    type: Literal["restart"]


class ResumeStream(TypedDict):
    """Sent on WS (re)connect. Per root, primary's last-acked node_offset
    and per-active-worker `(file_version, shadow_size)` for shadow-jsonl
    resync."""
    type: Literal["resume_stream"]
    last_acked: dict[str, int]                                 # root_id → node_offset
    shadow_jsonls: dict[str, "ShadowJsonlCursor"]              # f"{root_id}:{fork_agent_sid}" → cursor


class ShadowJsonlCursor(TypedDict):
    file_version: int
    shadow_size: int


class RpcRequest(TypedDict, total=False):
    """Generic request/response over WS for non-run RPCs (filetree, ls,
    etc.). Correlated by `request_id`."""
    type: Literal["rpc_request"]
    request_id: str
    method: str          # e.g. "list_dir"
    params: dict


# ============================================================================
# Node → Primary messages
# ============================================================================
class EventForward(TypedDict, total=False):
    """One `StreamEvent` from a worker's queue on the node, wrapped for
    primary's `event_ingester.ingest`."""
    type: Literal["event_forward"]
    node_offset: int
    root_id: str
    sid: str
    event_type: str
    data: dict
    source: str          # e.g. "remote_node:linux"
    run_id: Optional[str]
    msg_id: Optional[str]


class JsonlLine(TypedDict):
    """One raw claude-jsonl line from a worker's claude session, shipped
    to primary's shadow file."""
    type: Literal["jsonl_line"]
    node_offset: int
    root_id: str
    fork_agent_sid: str
    file_version: int
    line_offset_in_version: int
    line: str


class RunControl(TypedDict, total=False):
    """Run-control events (session_discovered/complete/error) lifted out
    of the queue for primary to use directly (set fork agent_sid, end the
    proxy's queue drain, etc.)."""
    type: Literal["run_control"]
    node_offset: int
    run_id: str
    control_type: Literal["session_discovered", "complete", "error"]
    data: dict


class RpcResponse(TypedDict, total=False):
    type: Literal["rpc_response"]
    request_id: str
    ok: bool
    result: Optional[dict]
    error: Optional[str]


# ============================================================================
# Bidirectional — ping/pong heartbeats (cheap, used by both directions).
# ============================================================================
class Ping(TypedDict):
    type: Literal["ping"]
    ts: float


class Pong(TypedDict):
    type: Literal["pong"]
    ts: float


# Union for type-narrowing convenience.
NodeBoundMessage = (
    SpawnRun | CancelRun | ResumeStream | RpcRequest | Restart
    | Ping | Pong | Handshake
)
PrimaryBoundMessage = (
    EventForward | JsonlLine | RunControl | RpcResponse | Ping | Pong
    | Handshake | HandshakeReject
)
