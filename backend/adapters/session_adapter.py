"""Concrete SessionSurface implementation (ADR 0008).

Reads exclusively through `backend.adapters.store_access.store_access`;
live deltas come from the `session.fire.*` owner-side fact
(`session_manager._fire`, additive alongside its `session.<kind>`
WS-broadcaster event — see session_manager.py's `_fire` docstring),
`session.status_projected` (backend/session_status_projection.py:107-144,
the richer per-sid running/waiting_for_user/errored projection), and
`lifecycle.turn_*`. The fact/lifecycle handlers re-pull the affected
session record and re-map it — see `_refresh` — so a tick that doesn't
change the record's fields (the common case) never re-broadcasts. Rollup
state (`SessionSummary.state`) is instead tracked in a separate per-sid
`_RollupFlags` cache fed by the two fact sources — see `_derive_rollup`.

Every field this adapter cannot honestly derive from
`backend.adapters.store_access.StoreAccess`'s current surface is called
out inline as a `# gap:` comment rather than guessed at (CLAUDE.md:
"never invent"); the caller's report enumerates them.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from datetime import datetime

from backend.adapters.projection import BusBoundProjection
from backend.adapters.store_access import SessionRecord, store_access
from backend.event_bus import BusEvent
from backend.surface_contract.identity import (
    CONTRACT_VERSION,
    Emit,
    Ok,
    PageCursor,
    ProjectionResult,
    Rebuilding,
    SessionId,
    SessionSelectors,
    StaleCursor,
    Subscription,
)
from backend.surface_contract.intents import IntentRejected, SessionIntent, TransportAck
from backend.surface_contract.session_surface import (
    Project,
    SessionPage,
    SessionRemoved,
    SessionRollupState,
    SessionSummary,
    SessionSurface,
    SessionSummaryUpsert,
    SessionTreeNode,
)

_PAGE_SIZE = 50
# Opaque container id for the paginated session list — there is no single
# session this cursor belongs to, unlike chat surface cursors.
_LIST_SURFACE_ID = "sessions:list"


class _SubscriptionImpl:
    def __init__(self, close_fn) -> None:
        self._close_fn = close_fn

    def close(self) -> None:
        self._close_fn()


def _parse_epoch(value: str) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class _RollupFlags:
    """Per-sid live-signal cache, fed incrementally by `session.fire.*`
    (running_changed/error_changed) and fully by `session.status_projected`
    (backend/session_status_projection.py:107-144). Never persisted —
    rebuilt purely from bus facts observed since this adapter started, so
    a sid this process hasn't heard about yet is simply absent."""

    running: bool = False
    waiting_for_user: bool = False
    errored: bool = False


def _derive_rollup(flags: "_RollupFlags | None") -> SessionRollupState:
    # QUEUED / RECONNECTING / DETACHED have no source anywhere reachable
    # from session.fire.*/session.status_projected — conservatively IDLE
    # when nothing else is known, same priority order as
    # session_status.key_for (error > needs_decision > running > idle).
    if flags is None:
        return SessionRollupState.IDLE
    if flags.errored:
        return SessionRollupState.FAILED
    if flags.waiting_for_user:
        return SessionRollupState.AWAITING_APPROVAL
    if flags.running:
        return SessionRollupState.RUNNING
    return SessionRollupState.IDLE


def _map_summary(record: SessionRecord, state: SessionRollupState) -> SessionSummary:
    return SessionSummary(
        session_id=record.id,
        title=record.title,
        # gap: SessionRecord carries no project foreign key.
        project_ref=None,
        selectors=SessionSelectors(
            provider_id=record.provider_id,
            runtime_profile_id=record.runtime_profile_id,
            model=record.model,
            reasoning_effort=record.reasoning_effort,
            orchestration_mode=record.orchestration_mode,
            cwd=record.cwd,
        ),
        state=state,
        # gap: no attention-marker source on SessionRecord.
        attention_markers=(),
        opened_at=_parse_epoch(record.opened_at),
        last_activity_at=_parse_epoch(record.updated_at) or 0.0,
        archived=record.archived,
    )


def _map_tree_node(record: SessionRecord, state: SessionRollupState) -> SessionTreeNode:
    return SessionTreeNode(
        surface_id=record.id,
        parent_surface_id=None,
        kind="root",
        title=record.title,
        state=state,
    )


class SessionSurfaceAdapter(SessionSurface):
    def __init__(self) -> None:
        self._projection = BusBoundProjection()
        self._cache_lock = threading.Lock()
        self._last_summary: dict[SessionId, SessionSummary] = {}
        self._rollup_by_sid: dict[SessionId, _RollupFlags] = {}

    def bind(self) -> None:
        """Idempotent: `BusBoundProjection.bind` unsubscribes-then-
        resubscribes under deterministic per-pattern names."""
        self._projection.bind(
            [
                ("session.fire.*", self._on_session_fire),
                ("session.status_projected", self._on_status_projected),
                ("lifecycle.turn_*", self._on_lifecycle),
            ]
        )

    # ---- internals: rollup cache ----------------------------------------

    def _rollup_for(self, session_id: SessionId) -> SessionRollupState:
        with self._cache_lock:
            return _derive_rollup(self._rollup_by_sid.get(session_id))

    def _update_flags(self, session_id: SessionId, **changes: bool) -> None:
        with self._cache_lock:
            current = self._rollup_by_sid.get(session_id, _RollupFlags())
            self._rollup_by_sid[session_id] = replace(current, **changes)

    def _evict(self, session_id: SessionId) -> None:
        with self._cache_lock:
            known = session_id in self._last_summary or session_id in self._rollup_by_sid
            self._last_summary.pop(session_id, None)
            self._rollup_by_sid.pop(session_id, None)
        if known:
            self._projection.bump_render()
            self._projection.broadcast(
                SessionRemoved(cv=CONTRACT_VERSION, session_id=session_id)
            )

    # ---- read plane ------------------------------------------------

    def list_sessions(
        self, cursor: PageCursor | None, query: str | None
    ) -> ProjectionResult[SessionPage]:
        snapshot = self._projection.snapshot()
        if cursor is not None:
            if cursor.snapshot.incarnation != snapshot.incarnation:
                return StaleCursor()
            try:
                offset = int(cursor.token)
            except ValueError:
                return StaleCursor()
        else:
            offset = 0

        records = list(store_access.list_session_records())
        records.sort(key=lambda r: _parse_epoch(r.updated_at) or 0.0, reverse=True)
        if query:
            needle = query.lower()
            records = [r for r in records if needle in r.title.lower()]

        page = records[offset : offset + _PAGE_SIZE]
        next_cursor = None
        if offset + _PAGE_SIZE < len(records):
            next_cursor = PageCursor(
                surface_id=_LIST_SURFACE_ID, snapshot=snapshot, token=str(offset + _PAGE_SIZE),
            )
        summaries = tuple(_map_summary(r, self._rollup_for(r.id)) for r in page)
        return Ok(SessionPage(sessions=summaries, next_cursor=next_cursor), snapshot)

    def session_tree(
        self, session_id: SessionId
    ) -> ProjectionResult[tuple[SessionTreeNode, ...]]:
        snapshot = self._projection.snapshot()
        record = store_access.get_session_record(session_id)
        if record is None:
            return Rebuilding(retry_after_ms=None)
        # gap: no fork/parent-child source in store_access — always a
        # single root node, forks never surface here.
        return Ok((_map_tree_node(record, self._rollup_for(session_id)),), snapshot)

    def projects(self) -> ProjectionResult[tuple[Project, ...]]:
        snapshot = self._projection.snapshot()
        records = store_access.list_projects()
        # gap: ProjectRecord has neither an explicit ordering field nor a
        # session-count field, and SessionRecord has no project foreign
        # key to derive one from — list position and 0 rather than a
        # guessed/heuristic count.
        projects = tuple(
            Project(project_ref=r.path, name=r.name, order=i, session_count=0)
            for i, r in enumerate(records)
        )
        return Ok(projects, snapshot)

    def rearranger_state(self) -> ProjectionResult[dict[str, object]]:
        # gap: no rearranger-state source in store_access.
        return Rebuilding(retry_after_ms=None)

    # ---- live plane --------------------------------------------------

    def subscribe(self, emit: Emit) -> Subscription:
        self._projection.register(emit)
        return _SubscriptionImpl(lambda: self._projection.unregister(emit))

    # ---- command plane -------------------------------------------------

    def submit(self, intent: SessionIntent) -> TransportAck:
        return IntentRejected(
            intent_id=intent.intent_id,
            code="unsupported_contract_phase",
            message="command routing deferred; the legacy REST/WS path remains authoritative",
        )

    # ---- internals: live fact handlers ----------------------------------

    async def _on_session_fire(self, event: BusEvent) -> None:
        kind = event.payload.get("kind")
        if kind == "deleted":
            # gap: SessionFrame has no deletion/tombstone variant (would
            # need a new frame type added to
            # backend/surface_contract/session_surface.py, which is not in
            # this worktree's editable set — a live subscriber only learns
            # a deleted session is gone the next time it re-lists). The
            # cache-eviction side is real: `deleted_sids` carries the whole
            # removed subtree (session_manager.py's delete()), not just
            # `event.sid`, so every one of them is evicted here.
            deleted_sids = event.payload.get("deleted_sids") or [event.sid]
            for sid in deleted_sids:
                if isinstance(sid, str) and sid:
                    self._evict(sid)
            return
        if kind == "running_changed":
            self._update_flags(event.sid, running=bool(event.payload.get("value")))
        elif kind == "error_changed":
            self._update_flags(event.sid, errored=bool(event.payload.get("has_error")))
        await self._refresh(event.sid)

    async def _on_status_projected(self, event: BusEvent) -> None:
        payload = event.payload
        session_id = payload.get("session_id") or event.sid
        if not session_id:
            return
        if payload.get("deleted"):
            # Same tombstone limitation as `_on_session_fire`'s "deleted"
            # branch above — cache eviction only, no frame.
            self._evict(session_id)
            return
        self._update_flags(
            session_id,
            running=bool(payload.get("running")),
            waiting_for_user=bool(payload.get("waiting_for_user")),
            errored=bool(payload.get("errored")),
        )
        await self._refresh(session_id)

    async def _on_lifecycle(self, event: BusEvent) -> None:
        await self._refresh(event.sid)

    async def _refresh(self, session_id: str) -> None:
        if not session_id:
            return
        record = store_access.get_session_record(session_id)
        if record is None:
            # Defensive backstop only — real deletions are evicted
            # explicitly above (`_on_session_fire`'s "deleted" branch and
            # `_on_status_projected`'s `deleted` flag); this covers a
            # record vanishing without either fact reaching this adapter.
            self._evict(session_id)
            return
        summary = _map_summary(record, self._rollup_for(session_id))
        with self._cache_lock:
            if self._last_summary.get(session_id) == summary:
                return
            self._last_summary[session_id] = summary
        cv = self._projection.bump_render()
        self._projection.broadcast(SessionSummaryUpsert(cv=cv, summary=summary))
