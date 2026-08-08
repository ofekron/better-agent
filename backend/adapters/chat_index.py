"""Layer-2 chat index: a persisted, disposable projection of COMPLETED
turns, so `ChatSurfaceAdapter` can serve `open_session`/`children`/`older`
in O(window) instead of O(journal) once warm. The journal
(`event_journal_reader`) remains the single source of truth; on any
detected conflict (missing/stale index, node not found) the journal wins
and this module is rebuilt from it — never the other way around.

DESIGN
======

Storage: SQLite, one DB file per surface, under
`scheme_migrations.ensure("chat_index", SCHEME_VERSION) /
<sha256(surface_id)>/index.sqlite3`. SQLite is the established convention
for exactly this "queryable projection over an append-only journal" shape
in this codebase (see `backend/hydration_index_store.py`'s byte-offset
index and `backend/session_search_index.py`'s search index) — a plain
file store gives no O(1)/indexed container lookup, and a single shared DB
would serialize unrelated sessions' writers against each other for no
benefit, so per-surface files were chosen to match `hydration_index_
store.py`'s per-root_id file precedent.

Schema (`_ensure_schema`):
  - `meta(key, value)`: `incarnation` (persisted so a process restart
    doesn't look like a fresh incarnation to a resuming client, same
    convention as `SurfaceProjection`), `schema_version`.
  - `turns(turn_node_id PK, boundary_seq, settled)`: one row per turn this
    index has ever folded. `settled=1` once that turn's full
    Explanation/subagent-partitioned tree has been folded (see "Settled
    vs live" below); `boundary_seq` orders turns for the compact-window
    and `older()` paging reads. Indexed on `(settled, boundary_seq)` so
    "give me the last N settled turns" is an index range scan, not a
    table scan.
  - `headers(node_id PK, parent_id, turn_id, kind, ts, seq, status,
    run_ref, sidecar_ref, target_ref_json, manifest_count,
    manifest_has_children, preview, payload_json, usage_json)`: one row per Node this
    index has ever folded, keyed by node_id (upsert = latest-wins, same
    convergence rule the journal's own rows use). Indexed on
    `(parent_id, ts, seq)` so "give me container X's ordered children" is
    an index range scan — the O(1)-per-container-lookup constraint.

  `payload_json`/`target_ref_json` store `to_wire(node.payload)` /
  `to_wire(node.target_ref)` on write (cheap: recursive dataclass ->
  plain-JSON flattening, same helper `backend/adapters/serialize.py`
  already provides for the wire layer). On READ, `_reconstruct_payload`/
  `TargetRef(**d)` rebuild the REAL typed dataclass (`TypedPromptPayload`,
  `ToolInteractionPayload`, ...) via a small kind-keyed dispatch table —
  not just a wire-compatible plain dict. A plain dict WOULD serialize
  identically through `to_wire()` (it treats a dataclass and an
  already-JSON-shaped dict the same way), but it silently breaks anything
  that touches `.payload.<field>` in-process without going through
  `to_wire()` first — attribute access raises, `dataclasses.replace()`
  fails, and `Node.__eq__` no longer holds between a freshly-normalized
  Node and its index-round-tripped twin (the exact property the
  convergence tests assert). Real dataclasses cost one small,
  closed-set dispatch table (`NodePayload`'s own union in nodes.py is
  itself a fixed, enumerated set — not an open extension point new
  callers plug into, so a generic reflection-based coercer would add
  indirection without added coverage) and keep this index's output
  indistinguishable, in-process, from the always-fresh pipeline's.
  `status`/`kind` ARE reconstructed as
  their real enums (`ContentStatus`/`NodeKind`) — cheap, and keeps any
  future in-process comparison against them correct instead of comparing
  strings by accident.

  `preview`: bounded-chars (`_PREVIEW_CHARS`) leading text, stored for
  ASSISTANT_TEXT/THINKING/TYPED_PROMPT-kind headers (their own payload's
  `.text`, truncated) and for EXPLANATION headers (its first member's own
  preview, backfilled once the turn's full node set is known — see
  `_backfill_explanation_previews`). Nothing in the current frontend
  render path consumes a separate preview field yet (confirmed: Explana-
  tion's collapsed form is rendered from the REAL member nodes it already
  fetches in full — frontend/src/surface/nodes/Container.tsx unconditio-
  nally calls `children(explanation_id)` once the row mounts, regardless
  of collapse state); this column exists per the design brief's literal
  header-record shape and as a cheap foundation for a future header-only
  preview affordance, but it is never attached to a served `Node` object
  and therefore never reaches the wire — it cannot violate the "nothing
  returned that the current visibility state doesn't render" invariant.

Settled vs live — the actual event-driven fold rule
-----------------------------------------------------
A turn is folded into this index EXACTLY ONCE, at the moment it is known
to be structurally final: either (a) `ChatSurfaceAdapter._flush_event_
written` observes the journal open a NEW turn while a different turn_id
was current (that transition IS "the previous turn just became
immutable", purely a fact already computed from `EVENT_JOURNAL_WRITTEN`
rows, no extra bus dependency), or (b) a `lifecycle.turn_complete`/
`lifecycle.turn_stopped` fact closes the LAST turn of a session (which
never gets superseded by a "next" prompt while it's still the live tail).
Both call sites build that ONE turn's `_TurnView` via chat_adapter's
existing, already-hardened (H1-H6) pure pipeline
(`_segment_turns`/`_finish_normalize`/`_build_turn_view`) over a BOUNDED
read of just that turn's own rows, then call `merge_settled_turn` once.

This is a deliberate, narrower reading of "fold on EVENT_JOURNAL_WRITTEN"
than "re-derive on every write": the index's own writes ARE driven
entirely off EVENT_JOURNAL_WRITTEN-observed facts (no polling, no
timers), but re-running Explanation/subagent partitioning on EVERY
coalesced flush of an in-progress turn would cost O(rows-so-far) per
flush — O(n^2) total across a long-streaming turn — reintroducing exactly
the full-reprocess cost class H5 eliminated from `open_session`. A turn
is only ever partitioned once total, at its own completion boundary.
While a turn is still open, `headers`/`turns` simply doesn't have it yet
(`settled=0` is never written for it) — `ChatSurfaceAdapter` always
computes the still-live trailing turn directly via its existing bounded
one-segment build (unchanged from before this module existed: that's
exactly what `_ensure_seeded`/`open_session`'s `window[-1]` already did),
and only asks this index for the OLDER, already-settled turns before it.
`open_session`/`children`/`older` therefore always return a fully correct
answer; the index only ever makes the settled-turn portion of that answer
cheaper, never staler.

Cold start / rebuild
---------------------
`read_recent_settled_turns`/`read_children` return `None` (not an empty
result) when the index has no usable data at all for a surface — the
signal for the caller to fall back to its own full journal-scan pipeline
(today's `_all_segments`/`_build_turns`, unchanged) and, additively,
backfill every settled turn it just built via `merge_settled_turn` so
later calls get the fast path. `try_begin_rebuild`/`end_rebuild` give
concurrent callers for the SAME surface a single-flight gate (mirrors
`scheme_migrations.ensure`'s single-writer spirit, adapted to this
module's synchronous, non-blocking call shape): a second caller arriving
while a rebuild is in flight gets `False` back immediately (its own
caller should serve via the fallback pipeline directly rather than
duplicate the backfill write — reading is always safe read-during-write
in SQLite/WAL) rather than blocking.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

from backend import paths, perf, scheme_migrations
from backend.adapters.serialize import to_wire
from backend.surface_contract.identity import CONTRACT_VERSION, NodeId, SurfaceId
from backend.surface_contract.nodes import (
    Attachment,
    AssistantTextPayload,
    ChildManifest,
    CompactionOrigin,
    CompactionPayload,
    ContentStatus,
    ContinuationSessionPayload,
    DiagnosticCode,
    DiagnosticPayload,
    FactPayload,
    FailurePayload,
    FailureResolution,
    FailureSeverity,
    HarnessChangePayload,
    InstructionWidgetPayload,
    LifecycleNoticeKind,
    LifecycleNoticePayload,
    ModelChangePayload,
    ModelChangeSource,
    Node,
    NodeKind,
    PromptOrigin,
    ResultKind,
    ResultPayload,
    SendMode,
    SteeringMessagePayload,
    SubAgentTurnPayload,
    TargetRef,
    ThinkingPayload,
    ToolInteractionPayload,
    TypedPromptPayload,
    UnknownPayload,
    Usage,
    UserInteractionPayload,
    WorkerInteractionPayload,
)

logger = logging.getLogger(__name__)

SCHEME_COMPONENT = "chat_index"
# v2: adds `headers.usage_json` (SubAgentTurn-family panel-level token
# usage) — a new nullable column, so bumping this simply starts every
# surface on a fresh index directory (per-surface path is `.../<sha256
# (surface_id)>/index.sqlite3` under this version); the journal remains
# the source of truth and rebuilds it losslessly (see module docstring).
SCHEME_VERSION = 2
_PREVIEW_CHARS = 240
_DB_FILENAME = "index.sqlite3"

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS turns (
    turn_node_id TEXT PRIMARY KEY,
    boundary_seq INTEGER NOT NULL,
    settled INTEGER NOT NULL DEFAULT 0,
    -- `headers` alone can't distinguish a turn's result/runtime_change
    -- children from its ordinary body items (both share parent_id=
    -- turn_node_id, matching `children()`'s own undifferentiated-by-
    -- design contract) — CompactTurn needs that split, so it's recorded
    -- here, once per turn, instead of inventing a per-row "role" tag that
    -- only turn-level children would ever use.
    prompt_node_id TEXT,
    result_node_ids_json TEXT NOT NULL DEFAULT '[]',
    runtime_change_node_id TEXT
);
CREATE INDEX IF NOT EXISTS turns_settled_boundary
    ON turns(settled, boundary_seq);
CREATE TABLE IF NOT EXISTS headers (
    node_id TEXT PRIMARY KEY,
    parent_id TEXT,
    turn_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    ts REAL NOT NULL,
    seq INTEGER NOT NULL,
    status TEXT,
    run_ref TEXT,
    sidecar_ref TEXT,
    target_ref_json TEXT,
    manifest_count INTEGER,
    manifest_has_children INTEGER,
    preview TEXT,
    payload_json TEXT,
    usage_json TEXT
);
CREATE INDEX IF NOT EXISTS headers_parent_order
    ON headers(parent_id, ts, seq);
"""

# -- per-surface connection cache --------------------------------------

_conns_lock = threading.Lock()
_conns: dict[str, sqlite3.Connection] = {}

# -- single-flight rebuild gate ------------------------------------------

_rebuild_lock = threading.Lock()
_rebuilding: set[str] = set()


def try_begin_rebuild(surface_id: SurfaceId) -> bool:
    """True iff THIS caller now owns the cold-rebuild/backfill for
    `surface_id` and must call `end_rebuild` when done. False means
    another rebuild is already in flight — the caller should serve its
    request via the fallback pipeline directly, without also writing a
    backfill (avoids duplicate work; reading is always safe concurrently
    with an in-flight writer in SQLite/WAL, so correctness never depends
    on this gate — it is purely a work-dedup optimization)."""
    with _rebuild_lock:
        if surface_id in _rebuilding:
            perf.record_count("chat_index.rebuild.coalesced")
            return False
        _rebuilding.add(surface_id)
        perf.record_count("chat_index.rebuild.began")
        return True


def end_rebuild(surface_id: SurfaceId) -> None:
    with _rebuild_lock:
        _rebuilding.discard(surface_id)


def _session_dir(surface_id: SurfaceId) -> Path:
    root = scheme_migrations.ensure(SCHEME_COMPONENT, SCHEME_VERSION)
    name = hashlib.sha256(surface_id.encode("utf-8")).hexdigest()
    target = root / name
    try:
        target.mkdir(mode=0o700, exist_ok=False)
    except FileExistsError:
        paths.require_private_directory(target)
    else:
        paths.make_private_directory(target)
        paths.require_private_directory(target)
    return target


def _conn(surface_id: SurfaceId) -> sqlite3.Connection:
    with _conns_lock:
        conn = _conns.get(surface_id)
        if conn is not None:
            return conn
        db_path = _session_dir(surface_id) / _DB_FILENAME
        is_new = not db_path.exists()
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA_SQL)
        if is_new:
            paths.make_private_file(db_path)
        conn.commit()
        _conns[surface_id] = conn
        return conn


def invalidate(surface_id: SurfaceId) -> None:
    """Drop the in-process connection + on-disk DB for `surface_id`,
    forcing a fresh cold rebuild on next access. Never called by the
    normal fold/read paths (this index never diverges from the journal
    without detecting it via a missing/short read) — provided for tests
    and for an operator-triggered forced rebuild."""
    with _conns_lock:
        conn = _conns.pop(surface_id, None)
    if conn is not None:
        conn.close()
    db_dir = _session_dir(surface_id)
    for entry in db_dir.glob(f"{_DB_FILENAME}*"):
        entry.unlink(missing_ok=True)


def incarnation(surface_id: SurfaceId) -> str:
    """This index's own persisted identity token for `surface_id` —
    created once, stable across process restarts (same convention as
    `SurfaceProjection`'s `component=`-mode incarnation). Deliberately
    INDEPENDENT of `ChatSurfaceAdapter`'s own (currently per-process-
    random, not persisted) `SurfaceProjection` incarnation: settled-turn
    content is a durable derivation of the journal and stays valid across
    a process restart, unlike a live subscription cursor's incarnation
    token, so tying the two together would force a needless full rebuild
    on every restart."""
    conn = _conn(surface_id)
    row = conn.execute("SELECT value FROM meta WHERE key='incarnation'").fetchone()
    if row is not None:
        return row["value"]
    token = os.urandom(8).hex()
    conn.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES ('incarnation', ?)", (token,),
    )
    conn.commit()
    row = conn.execute("SELECT value FROM meta WHERE key='incarnation'").fetchone()
    return row["value"]


def watermark_seq(surface_id: SurfaceId) -> int:
    """Highest journal seq this index has folded any node from, for
    `surface_id`. Purely informational/diagnostic (see module docstring —
    freshness for serving purposes is per-turn `settled`, not a single
    global cutoff) except for one use: `merge_settled_turn` refuses to
    move it backwards, and a caller noticing the LIVE journal's own
    `current_seq` is now BELOW this stored watermark (an abnormal
    journal reset/truncation) should treat the index as untrustworthy and
    `invalidate()` it."""
    conn = _conn(surface_id)
    row = conn.execute("SELECT value FROM meta WHERE key='watermark_seq'").fetchone()
    return int(row["value"]) if row is not None else 0


def _bump_watermark(conn: sqlite3.Connection, new_seq: int) -> None:
    row = conn.execute("SELECT value FROM meta WHERE key='watermark_seq'").fetchone()
    current = int(row["value"]) if row is not None else 0
    if new_seq > current:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES ('watermark_seq', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(new_seq),),
        )


# -- Node <-> row (de)serialization --------------------------------------


def _preview_of(node: Node) -> str | None:
    text = getattr(node.payload, "text", None) if node.payload is not None else None
    if isinstance(text, str) and text:
        return text[:_PREVIEW_CHARS]
    return None


def _node_to_row(node: Node) -> tuple:
    manifest = node.child_manifest
    payload_json = json.dumps(to_wire(node.payload)) if node.payload is not None else None
    target_ref_json = json.dumps(to_wire(node.target_ref)) if node.target_ref is not None else None
    usage_json = json.dumps(to_wire(node.usage)) if node.usage is not None else None
    return (
        node.node_id,
        node.parent_id,
        node.turn_id,
        node.kind.value,
        node.ts,
        node.seq,
        node.status.value if node.status is not None else None,
        node.run_ref,
        node.sidecar_ref,
        target_ref_json,
        manifest.renderable_child_count if manifest is not None else None,
        (1 if manifest.has_children else 0) if manifest is not None else None,
        _preview_of(node),
        payload_json,
        usage_json,
    )


def _reconstruct_attachments(items: list | None) -> tuple[Attachment, ...]:
    return tuple(
        Attachment(name=a["name"], media_type=a["media_type"], ref=a["ref"], size=a.get("size"))
        for a in (items or [])
    )


def _reconstruct_payload(kind: NodeKind, d: dict | None) -> object | None:
    """Inverse of `to_wire(node.payload)` — see the module docstring's
    "payload_json/target_ref_json" section for why this reconstructs REAL
    typed dataclasses rather than handing back the raw dict. `NodeKind`'s
    payload union (`nodes.py`'s `NodePayload`) is closed/enumerated, so
    this dispatch is exhaustive by construction; structural kinds
    (TURN/EXPLANATION/SUBAGENT_TURN_KINDS) always have `payload=None` and
    fall through the final `return None`."""
    if d is None:
        return None
    if kind == NodeKind.INSTRUCTION_WIDGET:
        return InstructionWidgetPayload(text=d["text"], action=d.get("action"))
    if kind == NodeKind.TYPED_PROMPT:
        return TypedPromptPayload(
            text=d["text"], attachments=_reconstruct_attachments(d.get("attachments")),
            send_mode=SendMode(d.get("send_mode", SendMode.QUEUE.value)),
            origin=PromptOrigin(d.get("origin", PromptOrigin.USER.value)),
            source_session_ref=d.get("source_session_ref"), sent_text=d.get("sent_text"),
            intent_id=d.get("intent_id"),
        )
    if kind == NodeKind.ASSISTANT_TEXT:
        return AssistantTextPayload(text=d["text"])
    if kind == NodeKind.THINKING:
        return ThinkingPayload(text=d["text"], redacted=bool(d.get("redacted", False)))
    if kind == NodeKind.TOOL_INTERACTION:
        return ToolInteractionPayload(
            tool_name=d["tool_name"], args=d.get("args") or {}, result=d.get("result"),
            interaction_ref=d.get("interaction_ref"), ui_kind=d.get("ui_kind"),
            derived_view=d.get("derived_view"),
        )
    if kind == NodeKind.WORKER_INTERACTION:
        return WorkerInteractionPayload(fact_kind=d["fact_kind"], fact=d.get("fact") or {})
    if kind in (NodeKind.WORKER_TURN, NodeKind.SUB_SESSION_TURN, NodeKind.SESSION_TURN):
        return SubAgentTurnPayload(label=d.get("label"), created=bool(d.get("created", False)))
    if kind == NodeKind.STEERING_MESSAGE:
        return SteeringMessagePayload(
            text=d["text"], target=d["target"],
            attachments=_reconstruct_attachments(d.get("attachments")),
        )
    if kind == NodeKind.MODEL_CHANGE:
        return ModelChangePayload(
            from_run_ref=d.get("from_run_ref"), to_run_ref=d["to_run_ref"],
            source=ModelChangeSource(d["source"]),
        )
    if kind == NodeKind.HARNESS_CHANGE:
        return HarnessChangePayload(
            from_harness_profile_id=d.get("from_harness_profile_id"),
            to_harness_profile_id=d["to_harness_profile_id"],
        )
    if kind == NodeKind.RESULT:
        return ResultPayload(
            result_kind=ResultKind(d["result_kind"]), text=d.get("text"),
            is_error=bool(d.get("is_error", False)),
        )
    if kind == NodeKind.COMPACTION:
        return CompactionPayload(
            origin=CompactionOrigin(d["origin"]), summary=d["summary"],
            replaced_node_ids=tuple(d.get("replaced_node_ids") or ()),
        )
    if kind == NodeKind.CONTINUATION_SESSION:
        return ContinuationSessionPayload(
            execution_ref=d["execution_ref"], chain_depth=d["chain_depth"], summary=d.get("summary"),
        )
    if kind == NodeKind.FAILURE:
        return FailurePayload(
            code=d["code"], text=d["text"], data=d.get("data"),
            severity=FailureSeverity(d.get("severity", FailureSeverity.ERROR.value)),
            retryable=bool(d.get("retryable", False)),
            resolution=FailureResolution(d.get("resolution", FailureResolution.NONE.value)),
        )
    if kind == NodeKind.DIAGNOSTIC:
        return DiagnosticPayload(
            severity=d["severity"], code=DiagnosticCode(d["code"]), text=d["text"], data=d.get("data"),
        )
    if kind == NodeKind.USER_INTERACTION:
        return UserInteractionPayload(interaction_ref=d.get("interaction_ref", ""))
    if kind == NodeKind.LIFECYCLE_NOTICE:
        return LifecycleNoticePayload(kind=LifecycleNoticeKind(d["kind"]), data=d.get("data"))
    if kind == NodeKind.FACT:
        return FactPayload(kind=d["kind"], data=d.get("data") or {})
    if kind == NodeKind.UNKNOWN:
        return UnknownPayload(label=d.get("label", ""), payload=d.get("payload") or {})
    return None


def _row_to_node(surface_id: SurfaceId, row: sqlite3.Row) -> Node:
    manifest = None
    if row["manifest_count"] is not None:
        manifest = ChildManifest(
            renderable_child_count=row["manifest_count"],
            has_children=bool(row["manifest_has_children"]),
        )
    kind = NodeKind(row["kind"])
    payload_dict = json.loads(row["payload_json"]) if row["payload_json"] is not None else None
    payload = _reconstruct_payload(kind, payload_dict)
    target_ref_dict = json.loads(row["target_ref_json"]) if row["target_ref_json"] is not None else None
    target_ref = TargetRef(**target_ref_dict) if target_ref_dict is not None else None
    usage_dict = json.loads(row["usage_json"]) if row["usage_json"] is not None else None
    usage = Usage(**usage_dict) if usage_dict is not None else None
    return Node(
        cv=CONTRACT_VERSION,
        node_id=row["node_id"],
        parent_id=row["parent_id"],
        turn_id=row["turn_id"],
        surface_id=surface_id,
        kind=kind,
        ts=row["ts"],
        seq=row["seq"],
        status=ContentStatus(row["status"]) if row["status"] else None,
        payload=payload,
        run_ref=row["run_ref"],
        sidecar_ref=row["sidecar_ref"],
        target_ref=target_ref,
        child_manifest=manifest,
        usage=usage,
    )


def _backfill_explanation_previews(conn: sqlite3.Connection, explanation_ids: set[NodeId]) -> None:
    """Explanation headers carry no payload of their own (structural,
    always None by contract) — their `preview` is their earliest member's
    own preview/text, one level down. Must run AFTER every member row of
    this batch is already inserted (children lookup by parent_id)."""
    for exp_id in explanation_ids:
        member = conn.execute(
            "SELECT preview, payload_json FROM headers WHERE parent_id=? "
            "ORDER BY ts, seq LIMIT 1",
            (exp_id,),
        ).fetchone()
        if member is None:
            continue
        preview = member["preview"]
        if preview is None and member["payload_json"] is not None:
            try:
                payload = json.loads(member["payload_json"])
            except (TypeError, ValueError):
                payload = None
            text = payload.get("text") if isinstance(payload, dict) else None
            preview = text[:_PREVIEW_CHARS] if isinstance(text, str) and text else None
        if preview is not None:
            conn.execute("UPDATE headers SET preview=? WHERE node_id=?", (preview, exp_id))


# -- write API -------------------------------------------------------------


def merge_settled_turn(
    surface_id: SurfaceId, turn_node_id: NodeId, boundary_seq: int,
    nodes: dict[NodeId, Node], *,
    prompt: Node | None, results: tuple[Node, ...], runtime_change: Node | None,
) -> None:
    """Persist every node in `nodes` (a completed turn's full, already-
    partitioned tree: the turn node itself, prompt, results, runtime_
    change, failure nodes, Explanation/subagent-structural nodes, and
    every leaf they contain) and mark `turn_node_id` settled at
    `boundary_seq`. `prompt`/`results`/`runtime_change` are the SAME Node
    objects already present in `nodes` — recorded a second time (by id
    only) on the `turns` row so `IndexedTurn`/`CompactTurn` reconstruction
    doesn't have to guess a turn-child's role from its kind alone (see
    `turns` table comment). Idempotent/latest-wins per node_id (INSERT OR
    REPLACE) — safe to call again for the same turn (e.g. a resubscribe
    replay or a retried backfill)."""
    if not nodes:
        return
    conn = _conn(surface_id)
    max_seq = boundary_seq
    explanation_ids: set[NodeId] = set()
    with conn:
        for node in nodes.values():
            conn.execute(
                "INSERT OR REPLACE INTO headers("
                "node_id, parent_id, turn_id, kind, ts, seq, status, run_ref, "
                "sidecar_ref, target_ref_json, manifest_count, manifest_has_children, "
                "preview, payload_json, usage_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                _node_to_row(node),
            )
            max_seq = max(max_seq, node.seq)
            if node.kind == NodeKind.EXPLANATION:
                explanation_ids.add(node.node_id)
        _backfill_explanation_previews(conn, explanation_ids)
        conn.execute(
            "INSERT INTO turns(turn_node_id, boundary_seq, settled, prompt_node_id, "
            "result_node_ids_json, runtime_change_node_id) VALUES (?, ?, 1, ?, ?, ?) "
            "ON CONFLICT(turn_node_id) DO UPDATE SET boundary_seq=excluded.boundary_seq, "
            "settled=1, prompt_node_id=excluded.prompt_node_id, "
            "result_node_ids_json=excluded.result_node_ids_json, "
            "runtime_change_node_id=excluded.runtime_change_node_id",
            (
                turn_node_id, boundary_seq,
                prompt.node_id if prompt is not None else None,
                json.dumps([n.node_id for n in results]),
                runtime_change.node_id if runtime_change is not None else None,
            ),
        )
        _bump_watermark(conn, max_seq)
    perf.record_count("chat_index.merge_settled_turn.turns", 1)
    perf.record_count("chat_index.merge_settled_turn.nodes", len(nodes))


# -- read API --------------------------------------------------------------


@dataclass(frozen=True)
class IndexedTurn:
    """One settled turn's own header row, its prompt/results/runtime_
    change (for `CompactTurn`), and its FULL children-by-parent map
    (every node this turn owns, at every nesting depth — matching
    `chat_adapter._TurnView.index`'s scope), so a caller can build both a
    `CompactTurn` and answer any `children(node_id)` under this turn
    without a further journal read."""

    turn_node_id: NodeId
    boundary_seq: int
    turn: Node
    prompt: Node | None
    results: tuple[Node, ...]
    runtime_change: Node | None
    children_by_parent: dict[NodeId, tuple[Node, ...]]


def _load_turn(
    surface_id: SurfaceId, conn: sqlite3.Connection, turn_node_id: NodeId, boundary_seq: int,
) -> IndexedTurn | None:
    turn_row = conn.execute("SELECT * FROM headers WHERE node_id=?", (turn_node_id,)).fetchone()
    if turn_row is None:
        return None
    turn_node = _row_to_node(surface_id, turn_row)
    turn_meta = conn.execute(
        "SELECT prompt_node_id, result_node_ids_json, runtime_change_node_id "
        "FROM turns WHERE turn_node_id=?",
        (turn_node_id,),
    ).fetchone()
    rows = conn.execute(
        "SELECT * FROM headers WHERE turn_id=? ORDER BY ts, seq",
        (turn_node.turn_id,),
    ).fetchall()
    by_id: dict[NodeId, Node] = {}
    children_by_parent: dict[NodeId, list[Node]] = {}
    for row in rows:
        node = _row_to_node(surface_id, row)
        by_id[node.node_id] = node
        if node.node_id != turn_node_id:
            children_by_parent.setdefault(node.parent_id, []).append(node)
    prompt = by_id.get(turn_meta["prompt_node_id"]) if turn_meta and turn_meta["prompt_node_id"] else None
    result_ids = json.loads(turn_meta["result_node_ids_json"]) if turn_meta else []
    results = tuple(by_id[rid] for rid in result_ids if rid in by_id)
    runtime_change = (
        by_id.get(turn_meta["runtime_change_node_id"])
        if turn_meta and turn_meta["runtime_change_node_id"] else None
    )
    return IndexedTurn(
        turn_node_id=turn_node_id,
        boundary_seq=boundary_seq,
        turn=turn_node,
        prompt=prompt,
        results=results,
        runtime_change=runtime_change,
        children_by_parent={k: tuple(v) for k, v in children_by_parent.items()},
    )


def read_recent_settled_turns(surface_id: SurfaceId, limit: int) -> list[IndexedTurn] | None:
    """The most recent `limit` SETTLED turns, oldest-first (matching
    `_build_turns`' segment order) — or `None` if this surface has no
    settled turns indexed at all yet (cold: caller must fall back +
    backfill). An empty-but-real index (surface exists, zero turns
    settled so far, e.g. a brand new session whose only turn is still
    live) returns `[]`, distinct from `None`."""
    conn = _conn(surface_id)
    has_any = conn.execute("SELECT 1 FROM turns WHERE settled=1 LIMIT 1").fetchone()
    if has_any is None:
        perf.record_count("chat_index.read.cold_miss")
        return None
    rows = conn.execute(
        "SELECT turn_node_id, boundary_seq FROM turns WHERE settled=1 "
        "ORDER BY boundary_seq DESC LIMIT ?",
        (limit,),
    ).fetchall()
    perf.record_count("chat_index.read.recent_turns", len(rows))
    out = [
        _load_turn(surface_id, conn, row["turn_node_id"], row["boundary_seq"])
        for row in rows
    ]
    out = [t for t in out if t is not None]
    out.reverse()  # oldest-first
    return out


def read_older_settled_turns(
    surface_id: SurfaceId, *, before_boundary_seq: int, limit: int,
) -> list[IndexedTurn]:
    """Settled turns strictly before `before_boundary_seq`, oldest-first,
    most-recent-`limit`-of-those. Always returns a (possibly empty) list —
    "no index yet" is only meaningful for the compact window
    (`read_recent_settled_turns`); an `older()` page past the index's
    settled coverage legitimately has zero results without that being a
    cold-index signal."""
    conn = _conn(surface_id)
    rows = conn.execute(
        "SELECT turn_node_id, boundary_seq FROM turns WHERE settled=1 AND boundary_seq < ? "
        "ORDER BY boundary_seq DESC LIMIT ?",
        (before_boundary_seq, limit),
    ).fetchall()
    perf.record_count("chat_index.read.older_turns", len(rows))
    out = [
        _load_turn(surface_id, conn, row["turn_node_id"], row["boundary_seq"])
        for row in rows
    ]
    out = [t for t in out if t is not None]
    out.reverse()
    return out


def has_older_settled_turn(surface_id: SurfaceId, *, before_boundary_seq: int) -> bool:
    conn = _conn(surface_id)
    row = conn.execute(
        "SELECT 1 FROM turns WHERE settled=1 AND boundary_seq < ? LIMIT 1",
        (before_boundary_seq,),
    ).fetchone()
    return row is not None


def read_children(surface_id: SurfaceId, node_id: NodeId) -> tuple[Node, ...] | None:
    """Direct children of `node_id` (any container: a settled turn, an
    Explanation, a subagent-turn, ...), ordered `(ts, seq)` — or `None` if
    `node_id` is not known to this index at all (caller falls back to its
    own combined-index scan). An empty tuple means `node_id` IS indexed
    but genuinely has no children."""
    conn = _conn(surface_id)
    known = conn.execute("SELECT 1 FROM headers WHERE node_id=?", (node_id,)).fetchone()
    rows = conn.execute(
        "SELECT * FROM headers WHERE parent_id=? ORDER BY ts, seq", (node_id,),
    ).fetchall()
    if not rows and known is None:
        perf.record_count("chat_index.read.children_miss")
        return None
    perf.record_count("chat_index.read.children_hit", len(rows))
    return tuple(_row_to_node(surface_id, row) for row in rows)
