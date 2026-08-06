"""Concrete ChatSurface implementation (ADR 0006).

Reads exclusively through `event_journal_reader.read_events` (the journal
is the only persistent source), normalizes/derives via
`backend.adapters.{normalize,derive}`, and pushes live deltas from
`event_journal.written` / `lifecycle.turn_*` bus facts. See
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
import threading
from collections import OrderedDict
from dataclasses import dataclass, field, replace

from backend.adapters import derive as _derive
from backend.adapters.normalize import (
    ParentLink,
    derive_link,
    enrich_typed_prompt_node,
    normalize_journal_row,
    pair_tool_results,
    resolve_parents,
    typed_prompt_node_id,
)
from backend.adapters.projection import BusBoundProjection, SurfaceProjection
from backend.event_bus import BusEvent
from backend.event_journal import EVENT_JOURNAL_WRITTEN, event_journal_reader
from backend.surface_contract.chat_surface import (
    ChatSurface,
    CompactSessionSnapshot,
    CompactTurn,
    OlderPage,
    SearchMatch,
)
from backend.surface_contract.frames import NodeUpsert, ResyncRequired, TerminalReason, TurnLifecycle, TurnPhase
from backend.surface_contract.identity import (
    CONTRACT_VERSION,
    Emit,
    Focus,
    NodeId,
    Ok,
    PageCursor,
    ProjectionResult,
    Rebuilding,
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
    SendPrompt,
    Stop,
    TransportAck,
)
from backend.surface_contract.nodes import (
    ContentStatus,
    Node,
    NodeKind,
    RUNTIME_CHANGED_KINDS,
    Sidecar,
)

_COMPACT_TURN_WINDOW = 5
_RENDER_HISTORY_CAP = 500
_TOOL_PREFIX = "tool:"
_RESULT_SUFFIX = ":result"


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


def _segment_turns(rows: list[dict]) -> list[tuple[str, list[dict]]]:
    """Group ordered rows into (turn_id, rows) segments. A row is a turn
    boundary iff it normalizes to a TYPED_PROMPT node; rows before the
    first boundary are dropped (no anchoring turn)."""
    segments: list[tuple[str, list[dict]]] = []
    current_rows: list[dict] | None = None
    current_turn_id: str | None = None
    for row in rows:
        provisional = normalize_journal_row(row, surface_id="_", turn_id="_", cv=CONTRACT_VERSION)
        prompt_node = next((n for n in provisional if n.kind == NodeKind.TYPED_PROMPT), None)
        if prompt_node is not None:
            if current_rows is not None:
                segments.append((current_turn_id, current_rows))
            current_turn_id = prompt_node.node_id
            current_rows = [row]
        elif current_rows is not None:
            current_rows.append(row)
    if current_rows is not None:
        segments.append((current_turn_id, current_rows))
    return segments


def _normalize_rows(
    surface_id: str, turn_id: str, rows: list[dict], prompt_meta: dict[str, dict] | None = None,
) -> list[Node]:
    raw_nodes: list[Node] = []
    links: dict[NodeId, ParentLink] = {}
    for row in rows:
        produced = normalize_journal_row(row, surface_id=surface_id, turn_id=turn_id, cv=CONTRACT_VERSION)
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
    return resolve_parents(raw_nodes, links)


def _build_turn_view(
    surface_id: str, turn_id: str, rows: list[dict], prompt_meta: dict[str, dict] | None = None,
) -> _TurnView:
    boundary_seq = _row_seq(rows[0])
    raw_nodes = _normalize_rows(surface_id, turn_id, rows, prompt_meta)

    runtime_change = next((n for n in raw_nodes if n.kind in RUNTIME_CHANGED_KINDS), None)
    body_source = [n for n in raw_nodes if n.kind not in RUNTIME_CHANGED_KINDS]

    derived = _derive.derive_turn(turn_id, body_source, surface_id=surface_id, cv=CONTRACT_VERSION)
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

    derived_body = derived["body"]
    for item in derived_body.items:
        if item.kind == NodeKind.EXPLANATION:
            exp = replace(item, parent_id=turn_node.node_id)
            index[exp.node_id] = exp
            for member in derived_body.membership[exp.node_id]:
                m = replace(member, parent_id=exp.node_id)
                index[m.node_id] = m
        else:
            pn = replace(item, parent_id=turn_node.node_id)
            index[pn.node_id] = pn

    return _TurnView(
        turn_id=turn_id, boundary_seq=boundary_seq, turn_node=turn_node, prompt=prompt,
        results=results, runtime_change=runtime_change, live_nodes=tuple(raw_nodes), index=index,
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
        """Idempotent: attaches the two live-plane bus subscriptions.
        Calling this again (e.g. after a reconnect) re-subscribes under
        the same deterministic names rather than duplicating."""
        self._binder.bind(
            [
                (EVENT_JOURNAL_WRITTEN, self._on_event_written),
                ("lifecycle.turn_start", self._on_lifecycle),
                ("lifecycle.turn_complete", self._on_lifecycle),
                ("lifecycle.turn_stopped", self._on_lifecycle),
            ]
        )

    # ---- read plane ------------------------------------------------

    def open_session(self, session_id: SessionId) -> ProjectionResult[CompactSessionSnapshot]:
        state = self._get_or_create(session_id)
        turns = self._all_turns(session_id)
        window = turns[-_COMPACT_TURN_WINDOW:]
        older_cursor = None
        if len(turns) > len(window):
            older_cursor = PageCursor(
                surface_id=session_id, snapshot=state.projection.snapshot(),
                token=str(window[0].boundary_seq),
            )
        live_nodes = window[-1].live_nodes if window else ()
        snapshot = CompactSessionSnapshot(
            session_id=session_id, surface_id=session_id, instruction_widget=None,
            turns=tuple(_to_compact(t) for t in window), live_turn_nodes=live_nodes,
            runs=(), older_cursor=older_cursor,
        )
        self._ensure_seeded(session_id, turns)
        return Ok(snapshot, state.projection.snapshot())

    def children(
        self, surface_id: SurfaceId, node_id: NodeId, at_render_rev: int
    ) -> ProjectionResult[tuple[Node, ...]]:
        state = self._get_or_create(surface_id)
        current = state.projection.snapshot()
        if at_render_rev != current.render_rev:
            return StaleCursor()
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
        eligible = [t for t in self._all_turns(cursor.surface_id) if t.boundary_seq < boundary]
        page = eligible[-_COMPACT_TURN_WINDOW:]
        next_cursor = None
        if len(eligible) > len(page):
            next_cursor = PageCursor(
                surface_id=cursor.surface_id, snapshot=current, token=str(page[0].boundary_seq),
            )
        return Ok(
            OlderPage(turns=tuple(_to_compact(t) for t in page), runs=(), older_cursor=next_cursor),
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
        # No sidecar source is reachable within the adapter import boundary
        # yet (no sidecar-content store/reader is on the allowlist).
        return Rebuilding(retry_after_ms=None)

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

    def _all_turns(self, surface_id: SurfaceId) -> list[_TurnView]:
        rows = self._read_all_rows(surface_id)
        prompt_meta = _collect_prompt_meta(rows)
        if prompt_meta:
            state = self._get_or_create(surface_id)
            with state.lock:
                state.prompt_meta.update(prompt_meta)
        return [
            _build_turn_view(surface_id, turn_id, seg_rows, prompt_meta)
            for turn_id, seg_rows in _segment_turns(rows)
        ]

    @staticmethod
    def _read_all_rows(root_id: str) -> list[dict]:
        rows: list[dict] = []
        after = 0
        while True:
            batch, _total, has_more = event_journal_reader.read_events(root_id, after_seq=after, limit=1000)
            if not batch:
                break
            rows.extend(batch)
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
            turns = self._all_turns(surface_id)
        seq = event_journal_reader.current_seq(surface_id) or 0
        pending: dict[str, Node] = {}
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
        with state.lock:
            if state.seeded:
                return state
            state.current_turn_id = turns[-1].turn_id if turns else None
            state.last_seq = seq
            state.pending_tool_uses = pending
            state.current_prompt_row = turns[-1].prompt_row if turns else None
            # render_rev 0 (never bumped) is what open_session's snapshot
            # hands back — a cursor presenting it has already seen
            # everything through `seq`, not literally "seq 0".
            state.render_seq_history.setdefault(0, seq)
            state.seeded = True
        return state

    # ---- internals: live fact handlers ----------------------------------

    async def _on_event_written(self, event: BusEvent) -> None:
        surface_id = event.root_id
        state = self._ensure_seeded(surface_id)
        with state.lock:
            after = state.last_seq
        rows, _total, _has_more = event_journal_reader.read_events(surface_id, after_seq=after, limit=1000)
        if not rows:
            return

        frames: list[Node] = []
        with state.lock:
            state.prompt_meta.update(_collect_prompt_meta(rows))
            turn_id = state.current_turn_id
            max_seq = state.last_seq
            for row in rows:
                max_seq = max(max_seq, _row_seq(row))
                if row.get("type") == "prompt_meta":
                    frames.extend(
                        self._late_prompt_meta_frames(state, row, turn_id, surface_id)
                    )
                    continue
                produced = normalize_journal_row(row, surface_id=surface_id, turn_id="_", cv=CONTRACT_VERSION)
                prompt_node = next((n for n in produced if n.kind == NodeKind.TYPED_PROMPT), None)
                if prompt_node is not None:
                    turn_id = prompt_node.node_id
                    state.pending_tool_uses = {}
                    state.current_prompt_row = row
                if turn_id is None:
                    continue  # pre-anchor row (no prompt seen yet): dropped
                row_data = row.get("data")
                row_data = row_data if isinstance(row_data, dict) else {}
                row_msg_id = row.get("msg_id")
                meta = state.prompt_meta.get(row_msg_id) if isinstance(row_msg_id, str) else None
                for n in produced:
                    n = replace(n, turn_id=turn_id)
                    if n.kind == NodeKind.TYPED_PROMPT:
                        n = enrich_typed_prompt_node(n, row_data=row_data, meta=meta)
                    frames.append(self._merge_live_node(state, n))
            state.current_turn_id = turn_id
            state.last_seq = max_seq
            render_rev = state.projection.bump_render()
            state.render_seq_history[render_rev] = max_seq
            while len(state.render_seq_history) > _RENDER_HISTORY_CAP:
                state.render_seq_history.popitem(last=False)
            snapshot = state.projection.snapshot()

        for n in frames:
            state.projection.broadcast(
                NodeUpsert(cv=CONTRACT_VERSION, surface_id=surface_id, snapshot=snapshot, node=n)
            )

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

        with state.lock:
            render_rev = state.projection.bump_render()
            state.render_seq_history[render_rev] = state.last_seq
            snapshot = state.projection.snapshot()

        state.projection.broadcast(
            TurnLifecycle(
                cv=CONTRACT_VERSION, surface_id=surface_id, snapshot=snapshot,
                turn_id=turn_id, phase=phase, reason=reason,
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
            turn_id for turn_id, seg_rows in segments
            if any(_row_seq(r) > implied_seq for r in seg_rows)
        }
        if not touched:
            return
        for turn_id, seg_rows in segments:
            if turn_id not in touched:
                continue
            for n in _normalize_rows(cursor.surface_id, turn_id, seg_rows, prompt_meta):
                emit(NodeUpsert(cv=CONTRACT_VERSION, surface_id=cursor.surface_id, snapshot=snapshot, node=n))
