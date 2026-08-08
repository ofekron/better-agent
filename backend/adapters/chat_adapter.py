"""Concrete ChatSurface implementation (ADR 0006).

Reads exclusively through `event_journal_reader.read_events` (the journal
is the only persistent source) for the content plane, normalizes/derives
via `backend.adapters.{normalize,derive}`, and pushes live deltas from
`event_journal.written` / `lifecycle.turn_*` bus facts. `fetch_sidecar`
is the one exception: it reads through
`backend.adapters.store_access.store_access` (worker + run records), the
same façade the other adapters use. See
`backend/scripts/test_adapter_boundaries.py` for the enforced import
boundary this file must satisfy.

Turn identity has no dedicated journal field (event_ingester rows carry
`seq`/`ts`/`type`/`data`/`sid`/`source`, optionally `msg_id`/`run_id` — no
`turn_id`). Turns are therefore delimited structurally: any row that
normalizes to a `TYPED_PROMPT` node starts a new turn, whose `turn_id` is
that prompt node's `node_id`; every following row belongs to that turn
until the next prompt row. Rows preceding the first prompt in a root have
no anchoring turn and are dropped (chat-panel.md turns are always
prompt-anchored).
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field, replace

from backend import perf
from backend.adapters import chat_index
from backend.adapters import derive as _derive
from backend.adapters.normalize import (
    ParentLink,
    derive_link,
    enrich_typed_prompt_node,
    failure_payload_for_reason,
    is_canonical_prompt_row,
    is_dropped_control_row_type,
    normalize_journal_row,
    pair_tool_results,
    resolve_parents,
    typed_prompt_node_id,
    user_message_failed_node_id,
)
from backend.adapters.projection import BusBoundProjection, SurfaceProjection
from backend.adapters.store_access import WorkerRecord, store_access
from backend.event_bus import BusEvent
from backend.event_journal import EVENT_JOURNAL_WRITTEN, event_journal_reader
from backend.surface_contract.chat_surface import (
    ChatSurface,
    CompactSessionSnapshot,
    CompactTurn,
    OlderPage,
    SearchMatch,
)
from backend.surface_contract.frames import (
    NodeUpsert,
    ResyncRequired,
    TerminalReason,
    TextDelta,
    TurnLifecycle,
    TurnPhase,
    Usage,
    UserInteractionUpsert,
)
from backend.surface_contract.identity import (
    CONTRACT_VERSION,
    Emit,
    Focus,
    NodeId,
    Ok,
    PageCursor,
    ProjectionResult,
    SessionId,
    SidecarRef,
    StaleCursor,
    Subscription,
    SurfaceCursor,
    SurfaceId,
)
from backend.surface_contract.intents import (
    ChatIntent,
    DeleteQueued,
    EditQueued,
    IntentAccepted,
    IntentRejected,
    ResolveInteraction,
    Rewind,
    SendPrompt,
    Stop,
    TransportAck,
)
from backend.surface_contract.nodes import (
    ContentStatus,
    DELEGATE_CHOICE_REF_PREFIX,
    Node,
    NodeKind,
    RUNTIME_CHANGED_KINDS,
    SUBAGENT_TURN_KINDS,
    Sidecar,
    UserInteraction,
    UserInteractionKind,
    UserInteractionState,
    usage_from_raw,
)

logger = logging.getLogger(__name__)

_COMPACT_TURN_WINDOW = 5
_RENDER_HISTORY_CAP = 500
_TOOL_PREFIX = "tool:"
_RESULT_SUFFIX = ":result"

# Kinds whose payload carries a monotonically-growing `.text` (streamed via
# runner_better_agent.py's feed_text_delta/feed_thinking_delta — each
# journal row rewrites the FULL cumulative text, per the journal's
# full-snapshot convergence convention). `_on_event_written` diffs a new
# row's text against the node's previously-broadcast text to detect a pure
# append and emit a cheap TextDelta instead of the full Node.
_TEXT_DELTA_KINDS = frozenset({NodeKind.ASSISTANT_TEXT, NodeKind.THINKING})
# Self-healing: after this many consecutive TextDelta frames for the same
# node, force one full NodeUpsert so a client that missed/mis-applied a
# delta resyncs from ground truth without waiting for the node to finish.
_FULL_SYNC_EVERY_N_DELTAS = 20
# Bound on state.last_text/delta_count — same eviction idiom as
# render_seq_history, so a long-lived surface with many text-bearing nodes
# can't grow this unboundedly.
_TEXT_CACHE_CAP = 2000


def _turn_node_id(turn_id: str) -> NodeId:
    """The TURN node's own node_id for a given raw turn_id — MUST mirror
    `backend.adapters.derive.derive_turn`'s `f"turn:{turn_id}"` literal
    (not importable from here: derive.py computes it inline, not behind a
    helper). Node.turn_id itself always stays the raw, unprefixed id
    (matching every producer in this file/normalize.py); only a Node's
    `parent_id`, when attaching directly to the turn (results,
    runtime_change, failure), uses this prefixed form."""
    return f"turn:{turn_id}"


async def _drop_frames(frame_type: str, payload: dict) -> None:
    """`notify` for `ChatSurfaceAdapter.submit()`'s SendPrompt dispatch: v2
    acks are projection-fact based (ADR 0006 §5) — the admission-time
    reply frames the legacy WS transport would send here are intentionally
    dropped. The real outcome surfaces later as a projection fact over the
    live plane (`EVENT_JOURNAL_WRITTEN` / `lifecycle.*` bus facts), not as
    a reply frame from this call."""


def _row_seq(row: dict) -> int:
    seq = row.get("seq")
    return seq if isinstance(seq, int) else 0


def _collect_prompt_meta(rows: list[dict]) -> dict[str, dict]:
    """Scan a row batch for `prompt_meta` facts (turn_manager.py), keyed by
    the row's OWN journal-ownership `msg_id` — the assistant/turn-owning
    message id every render row of that turn shares (see
    `TurnManager._publish_prompt_meta`), not the `user_msg["id"]` carried
    inside the fact's `data` payload.

    Root-wide, not per-turn-segment: `prompt_meta` is published at turn
    DISPATCH, before the provider's own `type: "user"` echo row exists, so
    it always lands earlier in `events.jsonl` than the TYPED_PROMPT row it
    describes — `_segment_turns` would otherwise misfile it into the
    PREVIOUS turn's segment. Scanning the full row list up front sidesteps
    that entirely."""
    meta: dict[str, dict] = {}
    for row in rows:
        if row.get("type") != "prompt_meta":
            continue
        msg_id = row.get("msg_id")
        data = row.get("data")
        if isinstance(msg_id, str) and msg_id and isinstance(data, dict):
            meta[msg_id] = data
    return meta


class _SubscriptionImpl:
    def __init__(self, close_fn) -> None:
        self._close_fn = close_fn

    def close(self) -> None:
        self._close_fn()


@dataclass
class _TurnView:
    turn_id: str
    boundary_seq: int
    turn_node: Node
    prompt: Node | None
    results: tuple[Node, ...]
    runtime_change: Node | None
    live_nodes: tuple[Node, ...]  # paired + parent-resolved raw nodes for this turn
    index: dict[NodeId, Node]  # every node this turn owns, parent_id fully stamped
    prompt_row: dict  # raw row that produced this turn's TYPED_PROMPT node (always segment rows[0])


@dataclass
class _SurfaceState:
    projection: SurfaceProjection = field(default_factory=SurfaceProjection)
    lock: threading.Lock = field(default_factory=threading.Lock)
    seeded: bool = False
    last_seq: int = 0
    current_turn_id: str | None = None
    # tool_use_id -> unresolved (result=None) tool_interaction Node, for the
    # currently-live turn only; reset whenever a new turn boundary is seen.
    pending_tool_uses: dict[str, Node] = field(default_factory=dict)
    # delegation_id -> {node_id: WORKER_INTERACTION Node}, every fact
    # journaled so far for that delegation, currently-live turn only;
    # reset whenever a new turn boundary is seen (same scope/reset rule as
    # `pending_tool_uses`; node_id-keyed so a hypothetical re-processed
    # row can never double-count). `_process_row` rebuilds that
    # delegation's WORKER_TURN/SUB_SESSION_TURN/SESSION_TURN container
    # from this on every new fact via `derive.build_subagent_panel_
    # container` (see its docstring).
    worker_turn_facts: dict[str, dict[NodeId, Node]] = field(default_factory=dict)
    # render_rev -> journal seq as-of that bump, so a resubscribing cursor's
    # render_rev can be turned back into a replay baseline. Bounded so a
    # long-lived surface can't grow this unboundedly.
    render_seq_history: "OrderedDict[int, int]" = field(default_factory=OrderedDict)
    # msg_id (assistant-owned, see `_collect_prompt_meta`) -> prompt_meta
    # data. Accumulates monotonically (a msg_id's meta never changes once
    # written), so live updates and full rescans can merge freely.
    prompt_meta: dict[str, dict] = field(default_factory=dict)
    # Raw row of the currently-live turn's TYPED_PROMPT-producing row, so a
    # `prompt_meta` fact arriving AFTER that row (live path) can re-derive
    # and re-broadcast the now-enriched node. None until the first prompt.
    current_prompt_row: dict | None = None
    # Last text broadcast for a _TEXT_DELTA_KINDS node (TextDelta baseline)
    # and how many consecutive deltas have been sent since its last full
    # NodeUpsert — both bounded (oldest evicted) like render_seq_history.
    last_text: "OrderedDict[NodeId, str]" = field(default_factory=OrderedDict)
    delta_count: dict[NodeId, int] = field(default_factory=dict)
    # True while a coalescing flush is scheduled for the next event-loop
    # tick (see `ChatSurfaceAdapter._on_event_written`) — guards against
    # scheduling a second flush for a journal write that arrives while one
    # is already pending; the pending flush's `read_events(after_seq=...)`
    # picks up that write too once it runs.
    flush_scheduled: bool = False
    # Lowest seq ever seen with no owning turn yet (a journal-ordering
    # inversion: provider-stream rows racing the dispatch-time canonical
    # prompt row into the journal — see `ChatSurfaceAdapter._process_row`).
    # None when there is no unresolved backlog. `_catch_up_pre_anchor_rows`
    # re-reads from here once the next anchor resolves `current_turn_id`,
    # so those rows are recovered instead of staying forever behind
    # `last_seq`.
    pre_anchor_watermark: int | None = None
    # The seq of `current_turn_id`'s own anchor (TYPED_PROMPT-producing)
    # row — set alongside `current_prompt_row`. Lets chat_index folding
    # (`_fold_turn_into_index`) bound its read to just one turn's rows
    # (`_read_all_rows(after_seq=this-1)`) instead of the whole journal.
    current_turn_boundary_seq: int | None = None


_TurnSegment = tuple[str, list[dict], list[list[Node]]]


def _segment_turns(rows: list[dict]) -> list[_TurnSegment]:
    """Group ordered rows into (turn_id, rows, produced_by_row) segments. A
    row is a turn boundary iff it normalizes to a TYPED_PROMPT node.

    Rows seen before ANY turn has ever been anchored are BUFFERED, not
    dropped — a journal-ordering inversion (provider-stream rows racing
    the dispatch-time canonical prompt row into the journal; see
    `ChatSurfaceAdapter._flush_event_written`'s matching live-path
    recovery) means a turn's own content can carry a LOWER seq than its
    own anchor. The first anchor to arrive claims the whole buffered
    backlog — logically correct (nothing else could own that content: no
    turn was open yet) and the ONLY information available to attribute
    it, since chat-panel.md turns have no dedicated journal `turn_id`
    field (see this module's docstring). The anchor row is kept as
    `current_rows[0]` (buffered rows appended AFTER it) so `boundary_seq`/
    `prompt_row` — both `rows[0]`-derived — stay correct regardless of the
    backlog's actual (lower) seqs, and downstream tool_use/tool_result
    pairing still sees the backlog in its own original relative order.

    Each row is normalized EXACTLY ONCE here (with placeholder surface_id/
    turn_id="_", since node_id derivation never depends on either — only
    the Node.surface_id/turn_id fields do) and the produced nodes are
    carried alongside the row for `_build_turn_view` to restamp
    (`replace(n, surface_id=..., turn_id=...)`) rather than re-normalizing
    — normalize_journal_row's JSON-shape dispatch/base64-decoding is the
    expensive part of hydrating a huge turn; this way it runs once per row
    per `_all_segments`/`_replay` call, not twice.

    A TYPED_PROMPT-producing row is instead treated as the CURRENTLY OPEN
    turn's own echo (dropped, not a boundary) only when BOTH: (1) that
    turn's own anchor row (the row that opened it) `is_canonical_prompt_
    row`, AND (2) this new row is NOT `is_canonical_prompt_row` — i.e. a
    raw provider-transcript echo of the SAME prompt (see that function's
    docstring). The `is_canonical_prompt_row` gate on the anchor itself
    is required, not optional: pre-fix journaled data and bare test
    fixtures with MULTIPLE real, separate turns never stamp `origin` on
    ANY of their rows, so without this gate every row after the first
    would be misrecognized as an echo and collapsed into one turn — a
    real backward-compatibility regression. Once a turn IS anchored by a
    canonical row, a second GENUINE canonical row (e.g. an interrupt)
    still opens a new turn correctly, since it satisfies neither
    condition (its own `is_canonical_prompt_row` is True)."""
    segments: list[_TurnSegment] = []
    current_rows: list[dict] | None = None
    current_produced: list[list[Node]] | None = None
    current_turn_id: str | None = None
    current_prompt_row: dict | None = None
    pending_pre_anchor_rows: list[dict] = []
    pending_pre_anchor_produced: list[list[Node]] = []
    for row in rows:
        produced = normalize_journal_row(row, surface_id="_", turn_id="_", cv=CONTRACT_VERSION)
        prompt_node = next((n for n in produced if n.kind == NodeKind.TYPED_PROMPT), None)
        if (
            prompt_node is not None
            and current_prompt_row is not None
            and is_canonical_prompt_row(current_prompt_row)
            and not is_canonical_prompt_row(row)
        ):
            continue  # echo of the currently-open canonical turn's own prompt
        if prompt_node is not None:
            if current_rows is not None:
                segments.append((current_turn_id, current_rows, current_produced))
            current_turn_id = prompt_node.node_id
            current_prompt_row = row
            current_rows = [row, *pending_pre_anchor_rows]
            current_produced = [produced, *pending_pre_anchor_produced]
            pending_pre_anchor_rows = []
            pending_pre_anchor_produced = []
        elif current_rows is not None:
            current_rows.append(row)
            current_produced.append(produced)
        else:
            pending_pre_anchor_rows.append(row)
            pending_pre_anchor_produced.append(produced)
    if current_rows is not None:
        segments.append((current_turn_id, current_rows, current_produced))
    return segments


def _finish_normalize(
    surface_id: str,
    turn_id: str,
    rows: list[dict],
    produced_by_row: list[list[Node]],
    prompt_meta: dict[str, dict] | None = None,
) -> tuple[list[Node], dict[NodeId, bool]]:
    """Restamp already-normalized (placeholder-id) nodes with the real
    surface_id/turn_id and run the per-segment enrich/pair/resolve passes
    — shared by `_build_turn_view` (rows pre-produced by `_segment_turns`)
    and `_replay` (rows pre-produced inline from a single normalize pass
    per row), so normalize_journal_row runs exactly once per row
    regardless of caller.

    Also returns `is_sidechain`: node_id -> the ORIGINATING row's
    `isSidechain` flag (`ParentLink.is_sidechain`, already computed here
    for every produced node's parent-linkage anyway) — the signal
    `derive.build_subagent_turns` needs to segregate sidechain content,
    without that pure function importing anything row-shaped itself."""
    raw_nodes: list[Node] = []
    links: dict[NodeId, ParentLink] = {}
    for row, produced in zip(rows, produced_by_row):
        produced = [replace(n, surface_id=surface_id, turn_id=turn_id) for n in produced]
        if prompt_meta:
            row_data = row.get("data")
            row_data = row_data if isinstance(row_data, dict) else {}
            meta = prompt_meta.get(row.get("msg_id")) if isinstance(row.get("msg_id"), str) else None
            produced = [
                enrich_typed_prompt_node(n, row_data=row_data, meta=meta) for n in produced
            ]
        link = derive_link(row)
        for n in produced:
            links[n.node_id] = link
        raw_nodes.extend(produced)
    raw_nodes = pair_tool_results(raw_nodes)
    raw_nodes = resolve_parents(raw_nodes, links)
    is_sidechain = {node_id: link.is_sidechain for node_id, link in links.items()}
    return raw_nodes, is_sidechain


def _build_turn_view(
    surface_id: str,
    turn_id: str,
    rows: list[dict],
    produced_by_row: list[list[Node]],
    prompt_meta: dict[str, dict] | None = None,
    orchestration_mode: str | None = None,
) -> _TurnView:
    boundary_seq = _row_seq(rows[0])
    raw_nodes, is_sidechain = _finish_normalize(surface_id, turn_id, rows, produced_by_row, prompt_meta)

    runtime_change = next((n for n in raw_nodes if n.kind in RUNTIME_CHANGED_KINDS), None)
    # FAILURE nodes attach directly to the turn (never Explanation-
    # wrapped) — excluded from `derive_turn`'s body pipeline the same way
    # runtime_change is, so replay/reload reconstructs the exact Node
    # `_on_user_message_failed`'s live broadcast and `_on_event_written`'s
    # journal-catch-up already produce (both set parent_id=_turn_node_id
    # (turn_id) directly; `derive_body` would otherwise wrap this in a synthetic
    # Explanation like any other body item).
    failure_nodes = [n for n in raw_nodes if n.kind == NodeKind.FAILURE]
    body_source_raw = [
        n for n in raw_nodes
        if n.kind not in RUNTIME_CHANGED_KINDS and n.kind != NodeKind.FAILURE
    ]
    # Compaction-replay children segregate out BEFORE derive_body, exactly
    # like sidechain content below — see `derive.extract_compaction_
    # children`'s docstring. `compaction_children` (compaction node_id ->
    # its own children) is reattached to `index` once the compaction
    # node's FINAL Node is known, below.
    body_source_raw, compaction_children = _derive.extract_compaction_children(body_source_raw)
    # SubAgentTurn family (worker/sub_session/session): every WORKER_
    # INTERACTION fact for one delegation_id groups into ONE panel
    # container BEFORE derive_body runs — same "must never flat-merge"
    # rule as sidechain content below, see `derive.build_subagent_turns_
    # for_panels`'s docstring. `panel_index` holds every grouped fact,
    # merged into `index` below.
    body_source_raw, panel_index = _derive.build_subagent_turns_for_panels(
        body_source_raw, surface_id=surface_id, turn_id=turn_id, cv=CONTRACT_VERSION,
    )
    # chat-panel.md grammar: sidechain content segregates into its own
    # NATIVE_SUBAGENT_TURN structural node(s) BEFORE derive_turn runs — it
    # must never flat-merge into the turn's own body (see
    # `derive.build_subagent_turns`'s docstring). `subagent_index` holds
    # every node this pulled out (at every nesting depth); merged into
    # `index` below so `children()` can still serve them one level at a
    # time, the same generic mechanism Explanation members already use.
    body_source, subagent_index = _derive.build_subagent_turns(
        body_source_raw, is_sidechain, surface_id=surface_id, turn_id=turn_id, cv=CONTRACT_VERSION,
    )

    derived = _derive.derive_turn(
        turn_id, body_source, surface_id=surface_id, cv=CONTRACT_VERSION,
        orchestration_mode=orchestration_mode,
    )
    turn_node = replace(derived["turn"], parent_id=surface_id)
    prompt = replace(derived["prompt"], parent_id=turn_node.node_id) if derived["prompt"] else None
    result_nodes = derived["result"]
    results = tuple(replace(n, parent_id=turn_node.node_id) for n in result_nodes)

    index: dict[NodeId, Node] = {turn_node.node_id: turn_node}
    if prompt is not None:
        index[prompt.node_id] = prompt
    for n in results:
        index[n.node_id] = n
    if runtime_change is not None:
        runtime_change = replace(runtime_change, parent_id=turn_node.node_id)
        index[runtime_change.node_id] = runtime_change
    for fn in failure_nodes:
        fn = replace(fn, parent_id=turn_node.node_id)
        index[fn.node_id] = fn

    body_items, body_index = _derive.attach_body_items(derived["body"], parent_id=turn_node.node_id)
    index.update(body_index)
    # Reattach each compaction node's own children now that its FINAL Node
    # (possibly Explanation-wrapped) is in `index`, and give the compaction
    # node itself a real `child_manifest` — see `derive.extract_compaction_
    # children`'s docstring.
    for compaction_id, children in compaction_children.items():
        owner = index.get(compaction_id)
        if owner is None:
            continue
        index[compaction_id] = replace(owner, child_manifest=_derive.compaction_child_manifest(children))
        for child in children:
            index[child.node_id] = child
    trailing_item = body_items[-1] if body_items else None

    # The live turn's "extended form" (live_nodes) is bounded to this
    # turn's own top-level items — NOT the (potentially huge) sidechain
    # subtrees now hanging off any NATIVE_SUBAGENT_TURN, which stay lazy
    # (children(), one level at a time — same contract Explanation members
    # already have). Exception: when the CHRONOLOGICALLY LAST body item
    # is itself a subagent turn, its own direct children are served
    # eagerly too — one level, matching the one-level children() contract
    # — so a client watching a live turn can follow the currently-active
    # subagent without an extra round trip; anything deeper, or any
    # EARLIER subagent turn in the same live turn, is fetched on demand.
    bounded_nodes = list(index.values())
    if trailing_item is not None and trailing_item.kind in SUBAGENT_TURN_KINDS:
        bounded_nodes.extend(
            n for n in subagent_index.values() if n.parent_id == trailing_item.node_id
        )
        bounded_nodes.extend(
            n for n in panel_index.values() if n.parent_id == trailing_item.node_id
        )
    index.update(subagent_index)
    index.update(panel_index)

    return _TurnView(
        turn_id=turn_id, boundary_seq=boundary_seq, turn_node=turn_node, prompt=prompt,
        results=results, runtime_change=runtime_change, live_nodes=tuple(bounded_nodes), index=index,
        prompt_row=rows[0],
    )


def _to_compact(view: _TurnView) -> CompactTurn:
    return CompactTurn(
        turn=view.turn_node, prompt=view.prompt, results=view.results,
        manifest=view.turn_node.child_manifest, runtime_change=view.runtime_change,
    )


def _path_to(index: dict[NodeId, Node], node_id: NodeId) -> tuple[NodeId, ...]:
    path: list[NodeId] = []
    current: NodeId | None = node_id
    seen: set[NodeId] = set()
    while current is not None and current not in seen:
        seen.add(current)
        path.append(current)
        node = index.get(current)
        current = node.parent_id if node is not None else None
    return tuple(reversed(path))


_WORKER_SIDECAR_PANEL_KIND = "worker_panel"
# Honest terminal status for a `sidecar_ref` that will NEVER resolve to a
# `WorkerRecord` — distinct from the running/complete/failed statuses a
# REAL worker record produces (`_worker_sidecar_status`). See
# `fetch_sidecar`'s docstring for why this is `Ok`, not `Rebuilding`.
_WORKER_SIDECAR_STATUS_NOT_FOUND = "not_found"


def _worker_sidecar_status(success: bool | None) -> str:
    if success is True:
        return "complete"
    if success is False:
        return "failed"
    return "running"


def _map_worker_sidecar(ref: SidecarRef, worker: WorkerRecord) -> Sidecar:
    # The worker's own Better Agent session runs its own provider
    # execution(s) under `worker.agent_session_id` — store_access's shared
    # run<->session linkage heuristic (see get_latest_run_record) is a
    # real, non-invented source for success/error, unlike
    # instructions_preview below.
    run = store_access.get_latest_run_record(worker.agent_session_id)
    success = run.success if run is not None else None
    error = run.error if run is not None else None
    return Sidecar(
        sidecar_ref=ref,
        panel_kind=_WORKER_SIDECAR_PANEL_KIND,
        status=_worker_sidecar_status(success),
        payload={
            "worker_session_id": worker.agent_session_id,
            "orchestration_mode": worker.orchestration_mode,
            "cwd": worker.cwd,
            "node_id": worker.node_id,
            "delegation_count": worker.delegation_count,
            "last_active": worker.last_active,
            "token_usage": worker.token_usage,
            "success": success,
            "error": error,
            # gap: instructions_preview has no store source of its own —
            # it's only ever carried inline in the WORKER_INTERACTION
            # fact's own payload (backend/adapters/normalize.py), which the
            # frontend already reads directly off the chat node without a
            # sidecar fetch.
        },
    )


def _map_pending_interactions(session_id: SessionId) -> tuple[UserInteraction, ...]:
    """Cold-hydration source for `CompactSessionSnapshot.interactions` —
    every currently-pending UserInteraction across the 3 legacy mechanisms,
    read through `store_access` (the one sanctioned door adapters have onto
    persistent/in-memory stores). Pending-only, mirroring the legacy
    stores' own scope (see `store_access.list_pending_interactions`'s
    docstring) — resolved ones arrive live via `UserInteractionUpsert`
    instead."""
    return tuple(
        UserInteraction(
            interaction_ref=rec.interaction_ref,
            kind=UserInteractionKind(rec.kind),
            request=rec.request,
            state=UserInteractionState.PENDING,
        )
        for rec in store_access.list_pending_interactions(session_id)
    )


def _searchable_text(node: Node) -> str | None:
    payload = node.payload
    if payload is None:
        return None
    text = getattr(payload, "text", None)
    if isinstance(text, str) and text:
        return text
    result = getattr(payload, "result", None)
    tool_name = getattr(payload, "tool_name", None)
    if tool_name is not None:
        parts = [tool_name]
        if isinstance(result, dict) and isinstance(result.get("output"), str):
            parts.append(result["output"])
        return " ".join(p for p in parts if p)
    summary = getattr(payload, "summary", None)
    if isinstance(summary, str):
        return summary
    label = getattr(payload, "label", None)
    if isinstance(label, str):
        return label
    return None


class ChatSurfaceAdapter(ChatSurface):
    def __init__(self) -> None:
        self._surfaces: dict[SurfaceId, _SurfaceState] = {}
        self._surfaces_lock = threading.Lock()
        self._binder = BusBoundProjection()
        self._control_emits: list[Emit] = []
        self._control_lock = threading.Lock()

    def bind(self) -> None:
        """Idempotent: attaches the live-plane bus subscriptions. Calling
        this again (e.g. after a reconnect) re-subscribes under the same
        deterministic names rather than duplicating.

        `session.fire.msg_ask_result_set` is an EXISTING fact
        (`session_manager.set_msg_ask_result`, fired both when the
        session-bridge delegation picker is first stamped AND when it's
        resolved) — no new producer needed for that mechanism; this is a
        pure consumer addition. See `_on_ask_result_set`.

        `interaction.fire.requested`/`interaction.fire.resolved` are NEW
        facts published by the tool-approval and worker-creation-approval
        mutation sites (`backend/tool_approvals_api.py`,
        `backend/pending_approvals_api.py`, and their v2-direct-mutation
        counterparts in `backend/surface_commands.py`'s
        `resolve_interaction`) — self-contained payloads a single generic
        handler maps straight onto a `UserInteraction`. See
        `_on_interaction_fact`."""
        self._binder.bind(
            [
                (EVENT_JOURNAL_WRITTEN, self._on_event_written),
                ("lifecycle.turn_start", self._on_lifecycle),
                ("lifecycle.turn_complete", self._on_lifecycle),
                ("lifecycle.turn_stopped", self._on_lifecycle),
                ("user_message_failed", self._on_user_message_failed),
                ("session.fire.msg_ask_result_set", self._on_ask_result_set),
                ("interaction.fire.requested", self._on_interaction_fact),
                ("interaction.fire.resolved", self._on_interaction_fact),
            ]
        )

    # ---- read plane ------------------------------------------------

    @staticmethod
    def _session_orchestration_mode(session_id: SessionId) -> str | None:
        """The owning session's frozen `orchestration_mode` — read fresh
        (no caching: `store_access.get_session_record` is an in-memory
        lookup, and this value never actually changes for a given
        `session_id` once the record exists, so caching would add
        invalidation complexity for zero staleness benefit). None when the
        session record can't be resolved (e.g. a race with session
        deletion) — never guessed."""
        record = store_access.get_session_record(session_id)
        return record.orchestration_mode if record is not None else None

    def open_session(self, session_id: SessionId) -> ProjectionResult[CompactSessionSnapshot]:
        # Only the compact window's turns are ever rendered by this
        # response — building a full `_TurnView` (derive_turn/derive_body,
        # replace-heavy tree construction) for every OLDER segment too
        # would reprocess the entire journal on every open, for data the
        # response never uses. Older turns are paged in on demand via
        # `older()`, which builds only the page it serves.
        state = self._get_or_create(session_id)
        fast = self._open_session_fast(session_id)
        if fast is not None:
            return fast
        segments, prompt_meta = self._all_segments(session_id)
        window_segments = segments[-_COMPACT_TURN_WINDOW:]
        orchestration_mode = self._session_orchestration_mode(session_id)
        window = self._build_turns(session_id, window_segments, prompt_meta, orchestration_mode)
        older_cursor = None
        if len(segments) > len(window_segments):
            older_cursor = PageCursor(
                surface_id=session_id, snapshot=state.projection.snapshot(),
                token=str(window[0].boundary_seq),
            )
        live_nodes = window[-1].live_nodes if window else ()
        snapshot = CompactSessionSnapshot(
            session_id=session_id, surface_id=session_id, instruction_widget=None,
            turns=tuple(_to_compact(t) for t in window), live_turn_nodes=live_nodes,
            runs=(), older_cursor=older_cursor,
            interactions=_map_pending_interactions(session_id),
        )
        self._ensure_seeded(session_id, window)
        # Exclude the last window turn: it may still be the live trailing
        # turn (not yet structurally final) — see `_backfill_index`'s
        # docstring for why this never reintroduces O(journal) cost here.
        self._backfill_index(session_id, window[:-1])
        return Ok(snapshot, state.projection.snapshot())

    @staticmethod
    def _compact_from_indexed(t: "chat_index.IndexedTurn") -> CompactTurn:
        return CompactTurn(
            turn=t.turn, prompt=t.prompt, results=t.results,
            manifest=t.turn.child_manifest, runtime_change=t.runtime_change,
        )

    def _open_session_fast(
        self, session_id: SessionId,
    ) -> ProjectionResult[CompactSessionSnapshot] | None:
        """O(window): the compact window's OLDER turns come straight from
        chat_index (already-settled, immutable); only the still-open
        trailing turn is computed directly, bounded to just its own rows
        via `state.current_turn_boundary_seq`. Returns `None` — never a
        wrong/partial answer — the instant any precondition doesn't hold,
        so `open_session`'s existing full pipeline stays the correctness
        backstop for every case this fast path doesn't cover.

        Checks chat_index FIRST, before touching `_ensure_seeded`/the
        journal at all: on a cold index (no settled turns for this
        surface) there is nothing this fast path could ever accelerate,
        so it must return `None` without normalizing a single row —
        otherwise a cold-index session with exactly one (still-live) turn
        would pay for a bounded-but-real normalize pass HERE and then pay
        AGAIN for `open_session`'s fallback `_all_segments` full pass,
        double-normalizing every row (the exact regression H5 removed).

        Once `settled` is known non-cold, `_ensure_seeded` is called
        (not just `state.seeded` checked): on a freshly-constructed
        adapter (e.g. right after a process restart) nothing has seeded
        `current_turn_id`/`current_turn_boundary_seq` yet, and `_ensure_
        seeded`'s own seed read is now index-bounded (`_ensure_seeded_
        turns_bounded`) rather than O(journal) — this is what makes the
        fast path actually available on a cold PROCESS with a warm
        on-disk index, not just within one already-seeded adapter
        instance's lifetime."""
        settled = chat_index.read_recent_settled_turns(session_id, _COMPACT_TURN_WINDOW - 1)
        if settled is None:
            return None
        state = self._ensure_seeded(session_id)
        with state.lock:
            trailing_turn_id = state.current_turn_id
            trailing_boundary_seq = state.current_turn_boundary_seq
            prompt_meta = dict(state.prompt_meta)
        if trailing_turn_id is None or trailing_boundary_seq is None:
            return None
        trailing_rows = self._read_all_rows(session_id, after_seq=trailing_boundary_seq - 1)
        if not trailing_rows:
            return None
        trailing_segments = _segment_turns(trailing_rows)
        if not trailing_segments or trailing_segments[-1][0] != trailing_turn_id:
            return None  # a live-path race outran this read — fall back rather than risk a mismatch
        trailing_view = _build_turn_view(
            session_id, *trailing_segments[-1], prompt_meta,
            self._session_orchestration_mode(session_id),
        )
        window = [self._compact_from_indexed(t) for t in settled] + [_to_compact(trailing_view)]
        oldest_boundary = settled[0].boundary_seq if settled else trailing_view.boundary_seq
        older_cursor = None
        if chat_index.has_older_settled_turn(session_id, before_boundary_seq=oldest_boundary):
            older_cursor = PageCursor(
                surface_id=session_id, snapshot=state.projection.snapshot(), token=str(oldest_boundary),
            )
        snapshot = CompactSessionSnapshot(
            session_id=session_id, surface_id=session_id, instruction_widget=None,
            turns=tuple(window), live_turn_nodes=trailing_view.live_nodes,
            runs=(), older_cursor=older_cursor,
            interactions=_map_pending_interactions(session_id),
        )
        perf.record_count("chat_adapter.open_session.index_fast_path")
        return Ok(snapshot, state.projection.snapshot())

    def _backfill_index(self, surface_id: SurfaceId, views: list["_TurnView"]) -> None:
        """Persist already-built `_TurnView`s as settled turns — NEVER
        builds anything itself (no `_build_turn_view` calls of its own):
        callers pass views they needed to build for their own response
        anyway (`open_session`'s window, `older`'s page), so warming the
        index this way costs exactly zero extra `_build_turn_view` calls
        beyond what the request already paid for. This is deliberately
        NOT "backfill the whole journal on first cold call" — that would
        reintroduce the exact O(journal) `_build_turn_view` cost H5
        eliminated from a single open_session/older call. A long history
        instead warms incrementally: one window/page at a time, exactly
        as far as a caller has actually paged. Single-flight per surface
        (`chat_index.try_begin_rebuild`) — a concurrent backfill already
        in flight for the same surface is skipped, not duplicated."""
        if not views:
            return
        if not chat_index.try_begin_rebuild(surface_id):
            return
        try:
            for view in views:
                try:
                    chat_index.merge_settled_turn(
                        surface_id, view.turn_node.node_id, view.boundary_seq, view.index,
                        prompt=view.prompt, results=view.results, runtime_change=view.runtime_change,
                    )
                except Exception:
                    logger.exception(
                        "chat_index: backfill failed for turn_id=%s surface_id=%s", view.turn_id, surface_id,
                    )
        finally:
            chat_index.end_rebuild(surface_id)

    def children(
        self, surface_id: SurfaceId, node_id: NodeId, at_render_rev: int
    ) -> ProjectionResult[tuple[Node, ...]]:
        state = self._get_or_create(surface_id)
        current = state.projection.snapshot()
        if at_render_rev != current.render_rev:
            return StaleCursor()
        # Fast path: `node_id` belongs to an already-SETTLED turn (see
        # chat_index.py's module docstring) — its children are immutable,
        # so an O(1)-indexed lookup is exact, not an approximation. `None`
        # means `node_id` isn't known to the index at all (e.g. it's part
        # of the still-open trailing turn, or the index hasn't settled
        # this turn yet) — falls through to the existing full scan.
        indexed = chat_index.read_children(surface_id, node_id)
        if indexed is not None:
            perf.record_count("chat_adapter.children.index_hit")
            return Ok(indexed, current)
        combined: dict[NodeId, Node] = {}
        for t in self._all_turns(surface_id):
            combined.update(t.index)
        kids = sorted(
            (n for n in combined.values() if n.parent_id == node_id), key=lambda n: (n.ts, n.seq),
        )
        return Ok(tuple(kids), current)

    def older(self, cursor: PageCursor) -> ProjectionResult[OlderPage]:
        state = self._get_or_create(cursor.surface_id)
        current = state.projection.snapshot()
        if cursor.snapshot.incarnation != current.incarnation:
            return StaleCursor()
        try:
            boundary = int(cursor.token)
        except ValueError:
            return StaleCursor()
        fast = self._older_fast(cursor.surface_id, boundary, current)
        if fast is not None:
            return fast
        # Same "only build what's served" bound as open_session: only the
        # requested page's segments get a full _TurnView.
        segments, prompt_meta = self._all_segments(cursor.surface_id)
        eligible_segments = [s for s in segments if _row_seq(s[1][0]) < boundary]
        page_segments = eligible_segments[-_COMPACT_TURN_WINDOW:]
        orchestration_mode = self._session_orchestration_mode(cursor.surface_id)
        page = self._build_turns(cursor.surface_id, page_segments, prompt_meta, orchestration_mode)
        next_cursor = None
        if len(eligible_segments) > len(page_segments):
            next_cursor = PageCursor(
                surface_id=cursor.surface_id, snapshot=current, token=str(page[0].boundary_seq),
            )
        # Unlike open_session's window, EVERY turn `older()` builds is
        # already strictly before the live compact window, so none of
        # `page` can be the still-open trailing turn — safe to fold all
        # of it.
        self._backfill_index(cursor.surface_id, page)
        return Ok(
            OlderPage(turns=tuple(_to_compact(t) for t in page), runs=(), older_cursor=next_cursor),
            current,
        )

    def _older_fast(
        self, surface_id: SurfaceId, boundary: int, current: object,
    ) -> ProjectionResult[OlderPage] | None:
        """O(page): every turn `older()` ever pages through is by
        definition already-settled (paging only walks BACKWARD from the
        compact window), so a page fully covered by chat_index is always
        exact. An empty index result is treated as "uncovered, fall back"
        rather than "no more history" — conservative, since the index
        alone can't distinguish a cold/partial index from genuine
        end-of-history; the fallback pipeline always answers that
        correctly, just without the speedup."""
        page = chat_index.read_older_settled_turns(surface_id, before_boundary_seq=boundary, limit=_COMPACT_TURN_WINDOW)
        if not page:
            return None
        next_cursor = None
        if chat_index.has_older_settled_turn(surface_id, before_boundary_seq=page[0].boundary_seq):
            next_cursor = PageCursor(
                surface_id=surface_id, snapshot=current, token=str(page[0].boundary_seq),
            )
        perf.record_count("chat_adapter.older.index_fast_path")
        return Ok(
            OlderPage(
                turns=tuple(self._compact_from_indexed(t) for t in page), runs=(), older_cursor=next_cursor,
            ),
            current,
        )

    def search(
        self, session_id: SessionId, query: str
    ) -> ProjectionResult[tuple[SearchMatch, ...]]:
        state = self._get_or_create(session_id)
        current = state.projection.snapshot()
        needle = query.lower()
        if not needle:
            return Ok((), current)
        matches: list[SearchMatch] = []
        for t in self._all_turns(session_id):
            for node_id, node in t.index.items():
                text = _searchable_text(node)
                if text and needle in text.lower():
                    matches.append(
                        SearchMatch(turn_id=t.turn_id, node_id=node_id, path=_path_to(t.index, node_id))
                    )
        return Ok(tuple(matches), current)

    def fetch_sidecar(
        self, session_id: SessionId, sidecar_ref: SidecarRef
    ) -> ProjectionResult[Sidecar]:
        # `sidecar_ref` is treated as the worker's Better Agent session id
        # (agent_session_id / WorkerPanel.worker_session_id in
        # frontend/src/types.ts), the only identifier
        # backend/stores/worker_store.py indexes workers by. No other
        # sidecar kind (panel_kind) has a store source reachable here.
        #
        # `derive.build_subagent_panel_container` stamps `sidecar_ref` from
        # the SAME `worker_session_id` every SubAgentTurn-family fact
        # carries — real when the target happens to be a REGISTERED worker
        # (`stores/worker_store.py`'s standing delegate-able roster,
        # populated only by `workers_api.py`'s `upsert_worker`), which is
        # NOT true for most ask/delegate_task/Task-tool delegation targets
        # (confirmed: neither flow ever calls `upsert_worker` — a
        # PERMANENT absence for those refs, not a race to retry through).
        # `Rebuilding` would loop forever for them; an honest terminal
        # `Ok(Sidecar(status="not_found"))` lets a caller stop asking
        # without fabricating worker data that will never exist.
        worker = store_access.get_worker_record(sidecar_ref)
        state = self._get_or_create(session_id)
        if worker is None:
            return Ok(
                Sidecar(
                    sidecar_ref=sidecar_ref, panel_kind=_WORKER_SIDECAR_PANEL_KIND,
                    status=_WORKER_SIDECAR_STATUS_NOT_FOUND, payload={},
                ),
                state.projection.snapshot(),
            )
        return Ok(_map_worker_sidecar(sidecar_ref, worker), state.projection.snapshot())

    # ---- live plane --------------------------------------------------

    def subscribe(
        self, cursors: tuple[SurfaceCursor, ...], focus: Focus, emit: Emit
    ) -> Subscription:
        registered: list[tuple[_SurfaceState, Emit]] = []
        for cursor in cursors:
            state = self._ensure_seeded(cursor.surface_id)
            current = state.projection.snapshot()
            if cursor.incarnation != current.incarnation:
                emit(ResyncRequired(cv=CONTRACT_VERSION, surface_id=cursor.surface_id))
                continue
            state.projection.register(emit)
            registered.append((state, emit))
            self._replay(state, cursor, emit)

        def close() -> None:
            for state, e in registered:
                state.projection.unregister(e)

        return _SubscriptionImpl(close)

    def subscribe_control(self, emit: Emit) -> Subscription:
        with self._control_lock:
            self._control_emits.append(emit)

        def close() -> None:
            with self._control_lock:
                try:
                    self._control_emits.remove(emit)
                except ValueError:
                    pass

        return _SubscriptionImpl(close)

    # ---- command plane -------------------------------------------------

    def submit(self, intent: ChatIntent) -> TransportAck:
        port = getattr(self, "_command_port", None)
        if port is None:
            return IntentRejected(
                intent_id=intent.intent_id,
                code="unsupported_contract_phase",
                message="command plane not wired yet; the legacy WS path remains authoritative",
            )
        # Acks are accept/reject only (see surface_contract/intents.py's
        # module docstring) — the real outcome surfaces later as a
        # projection fact over the live plane, so a well-formed,
        # currently-supported intent is admitted and its port coroutine
        # scheduled WITHOUT this synchronous call waiting on it. `submit`
        # itself stays sync (fixed by the `ChatSurface` ABC), so it never
        # awaits — only intents this port has real support for get past
        # this point; anything else is rejected synchronously, before any
        # coroutine is scheduled.
        if isinstance(intent, Stop):
            coro = port.stop(intent.session_id)
        elif isinstance(intent, EditQueued):
            coro = port.edit_queued(intent.session_id, intent.node_id, intent.text)
        elif isinstance(intent, DeleteQueued):
            coro = port.delete_queued(intent.session_id, intent.node_id)
        elif isinstance(intent, Rewind):
            coro = port.rewind(intent.session_id, intent.node_id)
        elif isinstance(intent, ResolveInteraction):
            coro = port.resolve_interaction(intent.session_id, intent.interaction_ref, intent.response)
        elif isinstance(intent, SendPrompt):
            # The only rejection determinable without an await: no text and
            # no attachments can never be a valid prompt (mirrors the
            # legacy WS handler's own first-line check). Every other
            # outcome (session state, dedup, admission) needs I/O the port
            # coroutine performs after this call returns, so those surface
            # later as projection facts — not a synchronous IntentRejected
            # — via `_drop_frames` below (v2 acks are projection-fact
            # based; see this module's docstring and ADR 0006 §5).
            if not intent.text.strip() and not intent.attachments:
                return IntentRejected(
                    intent_id=intent.intent_id,
                    code="empty_prompt",
                    message="text and attachments are both empty",
                )
            coro = port.send_prompt(
                intent.session_id,
                intent.text,
                intent.attachments,
                intent.send_mode,
                intent.target,
                intent.intent_id,
                notify=_drop_frames,
            )
        else:
            return IntentRejected(
                intent_id=intent.intent_id,
                code="unsupported",
                message=f"{type(intent).__name__} intents are not yet supported on this transport",
            )
        asyncio.get_running_loop().create_task(coro)
        return IntentAccepted(intent_id=intent.intent_id)

    # ---- internals: surface state --------------------------------------

    def _get_or_create(self, surface_id: SurfaceId) -> _SurfaceState:
        with self._surfaces_lock:
            state = self._surfaces.get(surface_id)
            if state is None:
                state = _SurfaceState()
                self._surfaces[surface_id] = state
            return state

    def _all_segments(self, surface_id: SurfaceId) -> tuple[list[_TurnSegment], dict[str, dict]]:
        """Read + segment the full journal (single normalize pass per row,
        see `_segment_turns`) WITHOUT building a `_TurnView` for every
        segment — callers that only need a bounded window (`open_session`,
        `older`) build views for just the segments they'll serve via
        `_build_turns`; callers that genuinely need every turn's index
        (`children`, `search`) pass every segment through it."""
        rows = self._read_all_rows(surface_id)
        prompt_meta = _collect_prompt_meta(rows)
        if prompt_meta:
            state = self._get_or_create(surface_id)
            with state.lock:
                state.prompt_meta.update(prompt_meta)
        return _segment_turns(rows), prompt_meta

    @staticmethod
    def _build_turns(
        surface_id: SurfaceId, segments: list[_TurnSegment], prompt_meta: dict[str, dict],
        orchestration_mode: str | None = None,
    ) -> list[_TurnView]:
        return [
            _build_turn_view(surface_id, turn_id, seg_rows, seg_produced, prompt_meta, orchestration_mode)
            for turn_id, seg_rows, seg_produced in segments
        ]

    def _all_turns(self, surface_id: SurfaceId) -> list[_TurnView]:
        segments, prompt_meta = self._all_segments(surface_id)
        orchestration_mode = self._session_orchestration_mode(surface_id)
        return self._build_turns(surface_id, segments, prompt_meta, orchestration_mode)

    @staticmethod
    def _read_all_rows(
        root_id: str, *, after_seq: int = 0, before_seq: int | None = None,
    ) -> list[dict]:
        """Paginated read of every row with `after_seq < seq` (and, when
        given, `seq < before_seq`). The bounded form is
        `_catch_up_pre_anchor_rows`'s supplementary read for a backlog
        that's behind `state.last_seq` (already consumed by an earlier
        `after_seq=state.last_seq` flush read) but still below the
        anchor row that just resolved its turn — the unbounded form
        (`before_seq=None`, the original signature every other caller
        still uses) is unaffected."""
        rows: list[dict] = []
        after = after_seq
        while True:
            batch, _total, has_more = event_journal_reader.read_events(root_id, after_seq=after, limit=1000)
            if not batch:
                break
            if before_seq is None:
                rows.extend(batch)
            else:
                in_range = [r for r in batch if _row_seq(r) < before_seq]
                rows.extend(in_range)
                if len(in_range) < len(batch):
                    break  # hit a row >= before_seq: the rest belongs to a later read
            next_after = _row_seq(batch[-1])
            if next_after <= after:
                break
            after = next_after
            if not has_more:
                break
        return rows

    def _ensure_seeded(
        self, surface_id: SurfaceId, turns: list[_TurnView] | None = None,
    ) -> _SurfaceState:
        """One-time bootstrap of live-tracking state (last_seq/current_turn_id/
        pending_tool_uses) from the journal's current tail. Guarded so a
        concurrent live fact that seeded first is never clobbered by a
        later open_session()/subscribe() call re-running this with a
        now-stale `turns` snapshot — that would regress `last_seq`
        backwards and reopen an already-resolved tool_use."""
        state = self._get_or_create(surface_id)
        with state.lock:
            if state.seeded:
                return state
        if turns is None:
            turns = self._ensure_seeded_turns_bounded(surface_id)
        seq = event_journal_reader.current_seq(surface_id) or 0
        pending: dict[str, Node] = {}
        worker_facts: dict[str, dict[NodeId, Node]] = {}
        if turns:
            for n in turns[-1].live_nodes:
                if (
                    n.kind == NodeKind.TOOL_INTERACTION
                    and n.payload is not None
                    and n.payload.result is None
                    and n.node_id.startswith(_TOOL_PREFIX)
                    and not n.node_id.endswith(_RESULT_SUFFIX)
                ):
                    pending[n.node_id[len(_TOOL_PREFIX):]] = n
            # Seed from the FULL turn index (not just `live_nodes`, which
            # only eagerly includes a TRAILING panel's own children) — a
            # mid-turn reconnect needs every delegation's accumulated
            # facts so far to rebuild its container correctly on the next
            # live fact, regardless of which panel is chronologically last.
            for n in turns[-1].index.values():
                delegation_id = _derive.delegation_id_of(n)
                if delegation_id:
                    worker_facts.setdefault(delegation_id, {})[n.node_id] = n
        with state.lock:
            if state.seeded:
                return state
            state.current_turn_id = turns[-1].turn_id if turns else None
            state.last_seq = seq
            state.pending_tool_uses = pending
            state.worker_turn_facts = worker_facts
            state.current_prompt_row = turns[-1].prompt_row if turns else None
            state.current_turn_boundary_seq = turns[-1].boundary_seq if turns else None
            # render_rev 0 (never bumped) is what open_session's snapshot
            # hands back — a cursor presenting it has already seen
            # everything through `seq`, not literally "seq 0".
            state.render_seq_history.setdefault(0, seq)
            state.seeded = True
        return state

    def _ensure_seeded_turns_bounded(self, surface_id: SurfaceId) -> list["_TurnView"]:
        """Only `turns[-1]` is ever needed by `_ensure_seeded` — build a
        `_TurnView` for just the last segment, bounded to a read of just
        the rows AFTER the most recent SETTLED turn chat_index already
        knows about (falling back to seq 0, i.e. the whole journal, when
        chat_index has nothing yet — the same O(journal) cost `_ensure_
        seeded` always paid before this index existed, never worse).

        This is what makes the index's benefit survive a process restart:
        without it, a freshly-constructed `ChatSurfaceAdapter` would need
        one full O(journal) seed pass before `_open_session_fast`/etc.
        could engage at all, even with a fully warm on-disk index."""
        settled = chat_index.read_recent_settled_turns(surface_id, 1)
        after_seq = (settled[-1].boundary_seq - 1) if settled else 0
        rows = self._read_all_rows(surface_id, after_seq=after_seq)
        if not rows:
            return []
        segments = _segment_turns(rows)
        if not segments:
            return []
        turn_id, seg_rows, seg_produced = segments[-1]
        # `_collect_prompt_meta` scans the WHOLE bounded read (not just
        # this segment's own rows): a `prompt_meta` fact can land with a
        # LOWER seq than the TYPED_PROMPT row it describes (published at
        # dispatch, before the provider's own echo row — see
        # `_collect_prompt_meta`'s own docstring), so it can fall inside
        # the previous (settled) turn's row range, which `rows` already
        # covers as a safety margin at no extra read cost.
        prompt_meta = _collect_prompt_meta(rows)
        orchestration_mode = self._session_orchestration_mode(surface_id)
        return [_build_turn_view(surface_id, turn_id, seg_rows, seg_produced, prompt_meta, orchestration_mode)]

    # ---- internals: live fact handlers ----------------------------------

    async def _on_event_written(self, event: BusEvent) -> None:
        """Debounces rapid-fire EVENT_JOURNAL_WRITTEN facts for the same
        surface onto one flush per event-loop tick. If a flush is already
        scheduled for this surface, this call is a no-op — the pending
        flush's `read_events(after_seq=state.last_seq)` subsumes whatever
        this call would have read, since a row is durably journaled
        before its bus fact publishes, and `last_seq` only advances once
        the flush actually runs. `asyncio.sleep(0)` is a pure event-loop
        yield (reschedules via call_soon; no wall-clock wait) — it lets
        every `_on_event_written` call already queued for THIS tick (e.g.
        several rapid feed_text_delta journal writes) land before the
        flush reads, so one flush naturally coalesces all of them.

        Perf-instrumented (additive only, no behavior change): every call
        counts as one `entries` sample; a call that finds a flush already
        scheduled and returns immediately counts as `skipped_already_
        scheduled` — the two together answer "did a live write for this
        surface ever reach the adapter at all" from the perf rollup alone,
        without needing debug logs turned on."""
        perf.record_count("chat_adapter.on_event_written.entries")
        surface_id = event.root_id
        state = self._ensure_seeded(surface_id)
        with state.lock:
            if state.flush_scheduled:
                perf.record_count("chat_adapter.on_event_written.skipped_already_scheduled")
                return
            state.flush_scheduled = True
        perf.record_count("chat_adapter.on_event_written.scheduled")
        await asyncio.sleep(0)
        with state.lock:
            state.flush_scheduled = False
        await self._flush_event_written(surface_id, state)

    async def _flush_event_written(self, surface_id: SurfaceId, state: _SurfaceState) -> None:
        perf.record_count("chat_adapter.flush.ran")
        with state.lock:
            after = state.last_seq
        rows, _total, _has_more = event_journal_reader.read_events(surface_id, after_seq=after, limit=1000)
        perf.record_count("chat_adapter.flush.rows_flushed", len(rows))
        if not rows:
            return

        # Per-batch coalescing (ADR upsert semantics: latest-wins per
        # node_id). A dict keeps each node_id's FIRST-occurrence position
        # while later occurrences in this batch overwrite its value — so
        # several rows updating the SAME node (e.g. a fast-streaming text
        # block, or several coalesced _on_event_written ticks worth of
        # writes) net out to exactly one outbound frame per node_id.
        coalesced: dict[NodeId, Node] = {}
        # Perf-instrumented (additive only): every reason a row's content
        # never reaches `coalesced` as its OWN outbound frame is counted
        # here — `turn_id_none`/`echo_dedup` are the two existing `continue`
        # guards above; `control_row_excluded` is a recognized backend
        # control/telemetry row type (`normalize.is_dropped_control_row_
        # type`) that normalizes to zero nodes by design;
        # `empty_after_normalize` is everything else that normalized to
        # zero nodes (a genuine unexpected-drop signal); `coalesced_away`
        # (below, once the full batch is known) is real content that DID
        # normalize but was superseded by a later row for the same
        # node_id within this same flush.
        drop_counts: dict[str, int] = {
            "turn_id_none": 0, "echo_dedup": 0,
            "control_row_excluded": 0, "empty_after_normalize": 0,
        }
        node_touches = 0
        # (closed_turn_id, boundary_seq, before_seq) for every turn this
        # flush observed transition away from (a fresh TYPED_PROMPT row
        # opened while a DIFFERENT turn_id was current) — that transition
        # is itself the fact that the previous turn is now structurally
        # immutable. Folded into chat_index AFTER `state.lock` is released
        # below (see the loop's `prev_boundary_seq` capture) — chat_index's
        # own bounded read/build/write must never run while holding a lock
        # shared with the hot broadcast path.
        closed_turns: list[tuple[str, int, int]] = []
        with state.lock:
            state.prompt_meta.update(_collect_prompt_meta(rows))
            turn_id = state.current_turn_id
            max_seq = state.last_seq
            for row in rows:
                max_seq = max(max_seq, _row_seq(row))
                prior_turn_id = turn_id
                prior_boundary_seq = state.current_turn_boundary_seq
                turn_id, touches = self._process_row(state, row, turn_id, surface_id, coalesced, drop_counts)
                node_touches += touches
                if turn_id != prior_turn_id and prior_turn_id is not None and prior_boundary_seq is not None:
                    closed_turns.append((prior_turn_id, prior_boundary_seq, _row_seq(row)))
                if (
                    turn_id is not None and prior_turn_id is None
                    and state.pre_anchor_watermark is not None
                ):
                    # This row just resolved the turn a backlog of earlier-
                    # seq, no-owner-yet rows was waiting on (a journal-
                    # ordering inversion — see `_process_row`'s `turn_id is
                    # None` branch) — recover them NOW, in this same flush
                    # cycle, before they fall permanently behind `last_seq`.
                    node_touches += self._catch_up_pre_anchor_rows(
                        state, surface_id, turn_id, before_seq=_row_seq(row),
                        coalesced=coalesced, drop_counts=drop_counts,
                    )
            state.current_turn_id = turn_id
            state.last_seq = max_seq
            render_rev = state.projection.bump_render()
            state.render_seq_history[render_rev] = max_seq
            while len(state.render_seq_history) > _RENDER_HISTORY_CAP:
                state.render_seq_history.popitem(last=False)
            # The delta/upsert decision (and its last_text/delta_count
            # bookkeeping) runs once per node_id per batch, over each
            # node's FINAL coalesced value — so a node updated several
            # times within one flush yields one TextDelta spanning the
            # whole batch's growth, not one per row.
            outbound = [self._to_outbound(state, n) for n in coalesced.values()]
            snapshot = state.projection.snapshot()

        for closed_turn_id, boundary_seq, before_seq in closed_turns:
            self._fold_turn_into_index(surface_id, closed_turn_id, boundary_seq, before_seq)

        # A node_id touched more than once in this batch nets out to one
        # `coalesced` entry (latest-wins) — every touch beyond the first
        # per node_id is real row content that never got its own outbound
        # frame, folded into the node's final broadcast value instead.
        drop_counts["coalesced_away"] = max(0, node_touches - len(coalesced))
        frames_emitted = {"upsert": 0, "text_delta": 0}
        for item in outbound:
            frames_emitted["text_delta" if item[0] == "delta" else "upsert"] += 1
        for reason, count in drop_counts.items():
            if count:
                perf.record_count(f"chat_adapter.flush.dropped.{reason}", count)
        if frames_emitted["upsert"]:
            perf.record_count("chat_adapter.flush.frames_emitted.upsert", frames_emitted["upsert"])
        if frames_emitted["text_delta"]:
            perf.record_count("chat_adapter.flush.frames_emitted.text_delta", frames_emitted["text_delta"])
        logger.debug(
            "chat_adapter.flush surface_id=%s rows=%d turn_id_none=%d echo_dedup=%d "
            "control_row_excluded=%d empty_after_normalize=%d coalesced_away=%d "
            "upserts=%d text_deltas=%d",
            surface_id, len(rows), drop_counts["turn_id_none"], drop_counts["echo_dedup"],
            drop_counts["control_row_excluded"], drop_counts["empty_after_normalize"],
            drop_counts["coalesced_away"], frames_emitted["upsert"], frames_emitted["text_delta"],
        )

        for item in outbound:
            if item[0] == "delta":
                _, node_id, appended_text = item
                state.projection.broadcast(
                    TextDelta(
                        cv=CONTRACT_VERSION, surface_id=surface_id, snapshot=snapshot,
                        node_id=node_id, appended_text=appended_text,
                    )
                )
            else:
                _, n = item
                state.projection.broadcast(
                    NodeUpsert(cv=CONTRACT_VERSION, surface_id=surface_id, snapshot=snapshot, node=n)
                )

    def _process_row(
        self, state: _SurfaceState, row: dict, turn_id: str | None, surface_id: SurfaceId,
        coalesced: dict[NodeId, Node], drop_counts: dict[str, int],
    ) -> tuple[str | None, int]:
        """Process one journal row against the CURRENTLY KNOWN `turn_id`,
        merging any produced node(s) into `coalesced` and returning the
        (possibly newly-opened) `turn_id` plus how many node_ids this row
        touched. MUST run under `state.lock` — mutates `state.pending_
        tool_uses`/`state.current_prompt_row`/`state.pre_anchor_
        watermark`.

        Shared by `_flush_event_written`'s main row loop and
        `_catch_up_pre_anchor_rows`: a pre-anchor row (dropped as
        `turn_id_none` at first sighting) is reprocessed through this
        EXACT same logic once its turn's anchor is known, so echo-dedup/
        control-row/empty-normalize classification is identical either
        way — there is only one place that decides what a row means."""
        if row.get("type") == "prompt_meta":
            touches = 0
            for n in self._late_prompt_meta_frames(state, row, turn_id, surface_id):
                touches += 1
                coalesced[n.node_id] = n
            return turn_id, touches
        produced = normalize_journal_row(row, surface_id=surface_id, turn_id="_", cv=CONTRACT_VERSION)
        # A compaction row's own children are produced alongside it in this
        # SAME list (`normalize._compaction_replay_children`) — fill in the
        # compaction node's `child_manifest` before broadcast; see
        # `derive.attach_compaction_manifests`'s docstring for why no
        # extraction is needed on this flat per-row delivery path.
        produced = _derive.attach_compaction_manifests(produced)
        prompt_node = next((n for n in produced if n.kind == NodeKind.TYPED_PROMPT), None)
        if (
            prompt_node is not None
            and state.current_prompt_row is not None
            and is_canonical_prompt_row(state.current_prompt_row)
            and not is_canonical_prompt_row(row)
        ):
            drop_counts["echo_dedup"] += 1
            return turn_id, 0  # echo of the currently-open canonical turn's own prompt
        if prompt_node is not None:
            turn_id = prompt_node.node_id
            state.pending_tool_uses = {}
            state.worker_turn_facts = {}
            state.current_prompt_row = row
            state.current_turn_boundary_seq = _row_seq(row)
        if turn_id is None:
            drop_counts["turn_id_none"] += 1
            row_seq = _row_seq(row)
            if state.pre_anchor_watermark is None or row_seq < state.pre_anchor_watermark:
                state.pre_anchor_watermark = row_seq
            return turn_id, 0  # pre-anchor row: buffered via the watermark, recovered once anchored
        if not produced:
            if is_dropped_control_row_type(row.get("type")):
                drop_counts["control_row_excluded"] += 1
            else:
                drop_counts["empty_after_normalize"] += 1
            return turn_id, 0
        row_data = row.get("data")
        row_data = row_data if isinstance(row_data, dict) else {}
        row_msg_id = row.get("msg_id")
        meta = state.prompt_meta.get(row_msg_id) if isinstance(row_msg_id, str) else None
        touches = 0
        for n in produced:
            n = replace(n, turn_id=turn_id)
            if n.kind == NodeKind.TYPED_PROMPT:
                n = enrich_typed_prompt_node(n, row_data=row_data, meta=meta)
            elif n.kind == NodeKind.FAILURE:
                # Attach directly to the turn (never Explanation-wrapped)
                # — matches `_on_user_message_failed`'s live broadcast AND
                # `_build_turn_view`'s replay attachment, so this row's
                # node_id resolves to the identical Node wherever it's
                # observed.
                n = replace(n, parent_id=_turn_node_id(turn_id))
            elif n.kind == NodeKind.WORKER_INTERACTION:
                # SubAgentTurn family, live path: nest this fact under its
                # delegation's own WORKER_TURN/SUB_SESSION_TURN/SESSION_
                # TURN container (constructing it on the delegation's
                # first-seen fact, rebuilding it on every later one) —
                # `state.worker_turn_facts` accumulates every fact seen so
                # far for that delegation, same scope as `pending_tool_
                # uses`. A fact with no recoverable delegation_id (never
                # observed in practice — every producer always sends one)
                # is left unparented rather than silently dropped.
                delegation_id = _derive.delegation_id_of(n)
                if delegation_id:
                    container_id = _derive.worker_turn_container_id(delegation_id)
                    n = replace(n, parent_id=container_id)
                    facts = state.worker_turn_facts.setdefault(delegation_id, {})
                    facts[n.node_id] = n
                    container = _derive.build_subagent_panel_container(
                        list(facts.values()), surface_id=surface_id, turn_id=turn_id, cv=CONTRACT_VERSION,
                    )
                    touches += 1
                    coalesced[container.node_id] = container
            merged = self._merge_live_node(state, n)
            touches += 1
            coalesced[merged.node_id] = merged
        return turn_id, touches

    def _catch_up_pre_anchor_rows(
        self, state: _SurfaceState, surface_id: SurfaceId, turn_id: str, before_seq: int,
        coalesced: dict[NodeId, Node], drop_counts: dict[str, int],
    ) -> int:
        """Re-reads every row from `state.pre_anchor_watermark` (inclusive)
        up to (not including) `before_seq` — the anchor row that just
        resolved `turn_id` — and reprocesses each through `_process_row`
        now that a turn exists to own them.

        Recovers a live-path journal-ordering inversion: provider-stream
        rows racing the dispatch-time canonical prompt row into the
        journal, so the turn's own content carries a LOWER seq than its
        anchor. Those rows were durably journaled and already read (and
        dropped as `turn_id_none`) by an earlier flush — or earlier in
        THIS same flush — so they are behind `state.last_seq` and would
        never be read again by the normal `after_seq=state.last_seq`
        pagination. Recovering them HERE, in the SAME flush cycle that
        resolves their anchor, is purely event-driven off the anchor's
        own arrival — no sleep/timer/poll. MUST run under `state.lock`
        (via `_process_row`). Returns how many node_ids the recovered
        rows touched (folded into the caller's own `node_touches`)."""
        watermark = state.pre_anchor_watermark
        if watermark is None:
            return 0
        rows = self._read_all_rows(surface_id, after_seq=watermark - 1, before_seq=before_seq)
        touches = 0
        for row in rows:
            _, row_touches = self._process_row(state, row, turn_id, surface_id, coalesced, drop_counts)
            touches += row_touches
        state.pre_anchor_watermark = None
        if rows:
            perf.record_count("chat_adapter.flush.pre_anchor_caught_up", len(rows))
        return touches

    def _fold_turn_into_index(
        self, surface_id: SurfaceId, turn_id: str, boundary_seq: int, before_seq: int | None,
    ) -> None:
        """Build `turn_id`'s full `_TurnView` (chat-panel.md's Explanation/
        subagent-partitioned tree — the SAME pure pipeline `open_session`/
        `children` already use) over a BOUNDED read of just that one
        turn's rows (`[boundary_seq, before_seq)`, or open-ended when
        `before_seq` is None) and persist it into `chat_index` as a
        settled turn. Called once a turn is known to be structurally
        final (see chat_index.py's module docstring: a newer turn opened,
        or a terminal lifecycle fact fired for it) — never on every
        journal write, which would cost O(rows-so-far) per flush instead
        of O(rows-in-this-turn) total.

        MUST run OUTSIDE `state.lock` (bounded journal read + sqlite I/O)
        and must never raise into its caller — chat_index is a disposable
        best-effort accelerator; any failure here just means this turn
        stays un-settled a while longer and keeps being served via the
        existing full-pipeline fallback, never a wrong or missing
        response."""
        try:
            rows = self._read_all_rows(surface_id, after_seq=boundary_seq - 1, before_seq=before_seq)
            if not rows:
                return
            segments = _segment_turns(rows)
            if not segments or segments[-1][0] != turn_id:
                return  # a race outran this read (e.g. the turn moved on again) — skip, retried on its own next transition
            state = self._get_or_create(surface_id)
            with state.lock:
                prompt_meta = dict(state.prompt_meta)
            view = _build_turn_view(
                surface_id, *segments[-1], prompt_meta,
                self._session_orchestration_mode(surface_id),
            )
            chat_index.merge_settled_turn(
                surface_id, view.turn_node.node_id, view.boundary_seq, view.index,
                prompt=view.prompt, results=view.results, runtime_change=view.runtime_change,
            )
        except Exception:
            logger.exception("chat_index: failed to fold turn_id=%s surface_id=%s", turn_id, surface_id)

    @staticmethod
    def _to_outbound(
        state: _SurfaceState, n: Node,
    ) -> tuple[str, Node] | tuple[str, NodeId, str]:
        """Decide NodeUpsert vs TextDelta for one node's final value in this
        batch. A TextDelta iff `n`'s kind streams incremental text
        (`_TEXT_DELTA_KINDS`) AND its new text is a proper append of the
        last text this surface broadcast for that node_id. Every other
        case — first sighting, a non-append rewrite, or every
        `_FULL_SYNC_EVERY_N_DELTAS`th delta (self-healing periodic full
        sync) — falls back to a full NodeUpsert."""
        if n.kind not in _TEXT_DELTA_KINDS or n.payload is None:
            return ("upsert", n)
        new_text = getattr(n.payload, "text", None)
        if not isinstance(new_text, str):
            return ("upsert", n)
        prev_text = state.last_text.get(n.node_id)
        state.last_text[n.node_id] = new_text
        state.last_text.move_to_end(n.node_id)
        while len(state.last_text) > _TEXT_CACHE_CAP:
            oldest_id, _ = state.last_text.popitem(last=False)
            state.delta_count.pop(oldest_id, None)
        if prev_text is None or new_text == prev_text or not new_text.startswith(prev_text):
            state.delta_count[n.node_id] = 0
            return ("upsert", n)
        count = state.delta_count.get(n.node_id, 0) + 1
        if count >= _FULL_SYNC_EVERY_N_DELTAS:
            state.delta_count[n.node_id] = 0
            return ("upsert", n)
        state.delta_count[n.node_id] = count
        return ("delta", n.node_id, new_text[len(prev_text):])

    def _late_prompt_meta_frames(
        self, state: _SurfaceState, meta_row: dict, turn_id: str | None, surface_id: str,
    ) -> list[Node]:
        """A `prompt_meta` fact observed AFTER its TYPED_PROMPT row already
        emitted a live NodeUpsert: re-derive that node from the tracked raw
        row and re-broadcast it enriched.

        Only fires while `state.current_prompt_row` still IS the row this
        meta describes (same journal-ownership `msg_id`) — if the turn has
        since moved on, the meta is a moot live-freshness concern; the
        durable state is already correct for the next seed/replay via
        `_all_turns`'s independent full-row rescan regardless of arrival
        order."""
        current = state.current_prompt_row
        row_msg_id = meta_row.get("msg_id")
        if (
            current is None
            or turn_id is None
            or not isinstance(row_msg_id, str)
            or current.get("msg_id") != row_msg_id
        ):
            return []
        row_data = current.get("data")
        row_data = row_data if isinstance(row_data, dict) else {}
        meta = state.prompt_meta.get(row_msg_id)
        produced = normalize_journal_row(
            current, surface_id=surface_id, turn_id=turn_id, cv=CONTRACT_VERSION,
        )
        return [
            self._merge_live_node(state, enrich_typed_prompt_node(n, row_data=row_data, meta=meta))
            for n in produced
            if n.kind == NodeKind.TYPED_PROMPT
        ]

    @staticmethod
    def _merge_live_node(state: _SurfaceState, n: Node) -> Node:
        """Reproduce `pair_tool_results`' merge for a single new row against
        the live-tracked pending tool_use, so a live tool_result emits an
        updated `tool:X` NodeUpsert (matching the compact/paired tree)
        instead of a standalone `tool:X:result` node. Falls back to the raw
        node when no tracked base exists (e.g. seeded cold)."""
        if n.kind == NodeKind.TOOL_INTERACTION and n.node_id.endswith(_RESULT_SUFFIX):
            tool_use_id = n.node_id[len(_TOOL_PREFIX): -len(_RESULT_SUFFIX)]
            base = state.pending_tool_uses.pop(tool_use_id, None)
            if base is not None:
                return replace(
                    base, status=ContentStatus.COMPLETE, payload=replace(base.payload, result=n.payload.result),
                )
            return n
        if (
            n.kind == NodeKind.TOOL_INTERACTION
            and n.payload is not None
            and n.payload.result is None
            and n.node_id.startswith(_TOOL_PREFIX)
        ):
            state.pending_tool_uses[n.node_id[len(_TOOL_PREFIX):]] = n
        return n

    @staticmethod
    def _usage_from_payload(raw: object) -> Usage | None:
        """`event.payload.get("usage")` is `trace_collector._normalize_
        token_usage`'s dict shape (`_publish_terminal_lifecycle`'s own
        docstring) — the SAME shape `nodes.usage_from_raw` parses (also
        used by `derive`'s SubAgentTurn-family panel construction from a
        journaled `worker_complete` fact's `token_usage`); this is a thin
        alias kept for this file's existing call site."""
        return usage_from_raw(raw)

    async def _on_lifecycle(self, event: BusEvent) -> None:
        phase_by_type = {
            "lifecycle.turn_start": TurnPhase.RUNNING,
            "lifecycle.turn_complete": TurnPhase.COMPLETED,
            "lifecycle.turn_stopped": TurnPhase.STOPPED,
        }
        phase = phase_by_type.get(event.type)
        if phase is None:
            return
        surface_id = event.root_id
        state = self._ensure_seeded(surface_id)

        reason = None
        if phase != TurnPhase.RUNNING:
            reason_by_payload = {
                "success": TerminalReason.OK,
                "cancelled": TerminalReason.USER_STOPPED,
                "error": TerminalReason.PROVIDER_ERROR,
            }
            default_reason = TerminalReason.OK if phase == TurnPhase.COMPLETED else TerminalReason.USER_STOPPED
            reason = reason_by_payload.get(event.payload.get("reason"), default_reason)

        # Prefer the contract-derivation from `prompt_uuid` — the SAME
        # node_id `_segment_turns`/normalize.py would assign the TYPED_PROMPT
        # row this turn is anchored on — so a lifecycle frame's turn_id
        # matches the turn_id turns get when built from raw journal rows.
        # Falls back to the pre-existing execution/user_turn_id fields when
        # `prompt_uuid` isn't yet known (e.g. turn_start, before the
        # provider's user-row echo has been folded).
        turn_id = (
            typed_prompt_node_id(event.payload.get("prompt_uuid"))
            or event.payload.get("execution_turn_id")
            or event.payload.get("user_turn_id")
            or ""
        )

        closing_turn_id: str | None = None
        closing_boundary_seq: int | None = None
        with state.lock:
            render_rev = state.projection.bump_render()
            state.render_seq_history[render_rev] = state.last_seq
            snapshot = state.projection.snapshot()
            if (
                phase != TurnPhase.RUNNING
                and turn_id == state.current_turn_id
                and state.current_turn_boundary_seq is not None
            ):
                # This IS the still-live trailing turn's own completion —
                # the ONE case `_flush_event_written`'s "a newer turn
                # opened" transition-fold can never observe on its own
                # (no successor row exists yet to trigger it). Guarded on
                # `turn_id == state.current_turn_id`: a stale/reordered
                # lifecycle fact for an ALREADY-superseded turn is simply
                # skipped — that turn was (or will be) folded via the
                # transition path instead; folding the WRONG (still-open)
                # turn here would prematurely mark live content settled.
                closing_turn_id = turn_id
                closing_boundary_seq = state.current_turn_boundary_seq

        usage = self._usage_from_payload(event.payload.get("usage")) if phase != TurnPhase.RUNNING else None
        state.projection.broadcast(
            TurnLifecycle(
                cv=CONTRACT_VERSION, surface_id=surface_id, snapshot=snapshot,
                turn_id=turn_id, phase=phase, reason=reason, usage=usage,
            )
        )
        if closing_turn_id is not None and closing_boundary_seq is not None:
            self._fold_turn_into_index(surface_id, closing_turn_id, closing_boundary_seq, before_seq=None)

    async def _on_user_message_failed(self, event: BusEvent) -> None:
        """`user_message_failed` (backend/user_msg_lifecycle.py `emit_failed`,
        payload `{lifecycle_msg_id, reason, error}`) -> an immediate live
        FAILURE NodeUpsert on the affected turn, ahead of the journal
        write's own round trip.

        `user_message_failed` IS itself persisted to events.jsonl
        (BusEvent.persist defaults True; see backend/event_bus.py's
        docstring and the wildcard `_persist_to_event_journal` subscriber
        in backend/event_bus_subscribers.py) and `normalize.py` now has a
        matching row branch (`_handle_user_message_failed`), so the SAME
        node also arrives moments later via `_on_event_written`'s normal
        journal-catch-up path (harmless — NodeUpsert is keyed by node_id,
        so it's an idempotent re-broadcast of identical content) and gets
        reconstructed identically on any later reload/replay. This
        handler exists purely to not make the live UI wait for that round
        trip. `failure_payload_for_reason`/`user_message_failed_node_id`
        (backend/adapters/normalize.py) are the single source of truth
        for the reason mapping and node identity — never duplicated here.
        """
        payload = event.payload if isinstance(event.payload, dict) else {}
        lifecycle_msg_id = payload.get("lifecycle_msg_id") or event.msg_id
        if not isinstance(lifecycle_msg_id, str) or not lifecycle_msg_id:
            return
        reason = payload.get("reason")
        reason = reason if isinstance(reason, str) and reason else "unknown"
        error = payload.get("error")
        failure_payload = failure_payload_for_reason(
            reason, error if isinstance(error, str) else None,
        )

        surface_id = event.root_id
        state = self._ensure_seeded(surface_id)
        with state.lock:
            turn_id = state.current_turn_id
            if turn_id is None:
                # No turn has ever been observed on this surface yet (the
                # very first prompt failed before durable admission) — no
                # TURN node exists to attach to, and Node.turn_id is
                # required, so there is structurally nowhere to hang this
                # node without inventing turn machinery. Documented gap,
                # not silently invented. `_on_event_written`'s later
                # journal-catch-up pass has the exact same limitation for
                # the exact same reason (see its FAILURE handling below).
                return
            node = Node(
                cv=CONTRACT_VERSION,
                node_id=user_message_failed_node_id(lifecycle_msg_id),
                parent_id=_turn_node_id(turn_id),
                turn_id=turn_id,
                surface_id=surface_id,
                kind=NodeKind.FAILURE,
                ts=time.time(),
                seq=state.last_seq,
                status=None,
                payload=failure_payload,
            )
            render_rev = state.projection.bump_render()
            state.render_seq_history[render_rev] = state.last_seq
            snapshot = state.projection.snapshot()

        state.projection.broadcast(
            NodeUpsert(cv=CONTRACT_VERSION, surface_id=surface_id, snapshot=snapshot, node=node)
        )

    async def _on_ask_result_set(self, event: BusEvent) -> None:
        """`session_manager.set_msg_ask_result`'s `session.fire.
        msg_ask_result_set` fact, filtered to the session-bridge delegation
        picker's own `purpose` (`session_bridge._await_picker`/
        `_mark_picker_resolved`) — this is the ONE of the 3 legacy
        interaction mechanisms that ALREADY publishes a bus fact for both
        its requested AND resolved transitions (the Ask-singleton's own
        `propose_sessions` picker uses the same field but never sets
        `purpose`, so it's naturally excluded — that flow isn't one of the
        3 mechanisms this contract recast unifies). No new producer needed;
        see `bind`'s docstring."""
        payload = event.payload if isinstance(event.payload, dict) else {}
        ask_result = payload.get("ask_result")
        if not isinstance(ask_result, dict) or ask_result.get("purpose") != "delegate_approval":
            return
        delegation_id = ask_result.get("delegation_id")
        if not isinstance(delegation_id, str) or not delegation_id:
            return
        surface_id = event.root_id
        resolved = bool(ask_result.get("resolved"))
        response = {"picked_ref": ask_result.get("chosen_session_id") or None} if resolved else None
        interaction = UserInteraction(
            interaction_ref=f"{DELEGATE_CHOICE_REF_PREFIX}{delegation_id}",
            kind=UserInteractionKind.CHOICE,
            request={
                "candidates": list(ask_result.get("session_ids") or ()),
                "prompt_preview": ask_result.get("prompt_preview", ""),
                "create_new": bool(ask_result.get("create_new", False)),
            },
            state=UserInteractionState.RESOLVED if resolved else UserInteractionState.PENDING,
            response=response,
        )
        state = self._ensure_seeded(surface_id)
        with state.lock:
            snapshot = state.projection.snapshot()
        state.projection.broadcast(
            UserInteractionUpsert(
                cv=CONTRACT_VERSION, surface_id=surface_id, snapshot=snapshot,
                user_interaction=interaction,
            )
        )

    async def _on_interaction_fact(self, event: BusEvent) -> None:
        """`interaction.fire.requested`/`interaction.fire.resolved` —
        self-contained facts published by the tool-approval and
        worker-creation-approval mutation sites (see `bind`'s docstring).
        Payload is already shaped like `UserInteraction`'s own fields
        (`interaction_ref`, `kind`, `request`, `state`, `response?`), so
        one generic handler covers both mechanisms and both transitions —
        unlike the session-bridge choice mechanism (`_on_ask_result_set`),
        which reuses a differently-shaped pre-existing fact."""
        payload = event.payload if isinstance(event.payload, dict) else {}
        interaction_ref = payload.get("interaction_ref")
        kind = payload.get("kind")
        state = payload.get("state")
        if not (
            isinstance(interaction_ref, str) and interaction_ref
            and isinstance(kind, str) and isinstance(state, str)
        ):
            return
        try:
            interaction = UserInteraction(
                interaction_ref=interaction_ref,
                kind=UserInteractionKind(kind),
                request=dict(payload.get("request") or {}),
                state=UserInteractionState(state),
                response=payload.get("response"),
            )
        except ValueError:
            return
        surface_id = event.root_id
        state_obj = self._ensure_seeded(surface_id)
        with state_obj.lock:
            snapshot = state_obj.projection.snapshot()
        state_obj.projection.broadcast(
            UserInteractionUpsert(
                cv=CONTRACT_VERSION, surface_id=surface_id, snapshot=snapshot,
                user_interaction=interaction,
            )
        )

    def _replay(self, state: _SurfaceState, cursor: SurfaceCursor, emit: Emit) -> None:
        with state.lock:
            implied_seq = state.render_seq_history.get(cursor.render_rev, 0)
            snapshot = state.projection.snapshot()
        all_rows = self._read_all_rows(cursor.surface_id)
        prompt_meta = _collect_prompt_meta(all_rows)
        segments = _segment_turns(all_rows)
        touched = {
            turn_id for turn_id, seg_rows, _seg_produced in segments
            if any(_row_seq(r) > implied_seq for r in seg_rows)
        }
        if not touched:
            return
        for turn_id, seg_rows, seg_produced in segments:
            if turn_id not in touched:
                continue
            finished, is_sidechain = _finish_normalize(
                cursor.surface_id, turn_id, seg_rows, seg_produced, prompt_meta,
            )
            # Same bound as `_build_turn_view`, for the SubAgentTurn family's
            # worker/sub_session/session panels: a resubscribing cursor must
            # not be flooded with raw WORKER_INTERACTION facts either — kept
            # excludes them (still reachable via children() once their own
            # container exists from the next open_session/children() call).
            kept, _panel_extra = _derive.build_subagent_turns_for_panels(
                finished, surface_id=cursor.surface_id, turn_id=turn_id, cv=CONTRACT_VERSION,
            )
            # Same bound as `_build_turn_view`: a resubscribing cursor must
            # not be flooded with raw sidechain descendants either — kept
            # excludes them (they're still reachable via children() once
            # their NATIVE_SUBAGENT_TURN exists from the next open_session/
            # children() call); non-sidechain nodes (including runtime_
            # change/failure) pass through unaffected.
            kept, _extra = _derive.build_subagent_turns(
                kept, is_sidechain, surface_id=cursor.surface_id, turn_id=turn_id, cv=CONTRACT_VERSION,
            )
            # Fill in any compaction node's `child_manifest` from its own
            # children, still present (flat) in `kept` — see
            # `derive.attach_compaction_manifests`'s docstring.
            kept = _derive.attach_compaction_manifests(kept)
            for n in kept:
                emit(NodeUpsert(cv=CONTRACT_VERSION, surface_id=cursor.surface_id, snapshot=snapshot, node=n))
