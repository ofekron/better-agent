"""Concrete SessionSurface implementation (ADR 0008).

Reads exclusively through `backend.adapters.store_access.store_access`;
live deltas come from the `session.fire.*` owner-side fact
(`session_manager._fire`, additive alongside its `session.<kind>`
WS-broadcaster event — see session_manager.py's `_fire` docstring) and
`lifecycle.turn_*`. Both handlers just re-pull the affected session
record and re-map it — see `_refresh` — so a lifecycle tick that doesn't
change the record's fields (the common case) never re-broadcasts.

Every field this adapter cannot honestly derive from
`backend.adapters.store_access.StoreAccess`'s current surface is called
out inline as a `# gap:` comment rather than guessed at (CLAUDE.md:
"never invent"); the caller's report enumerates them.
"""

from __future__ import annotations

import threading
from datetime import datetime

from backend.adapters.projection import BusBoundProjection
from backend.adapters.store_access import SessionRecord, store_access
from backend.event_bus import BusEvent
from backend.surface_contract.identity import (
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


def _map_summary(record: SessionRecord) -> SessionSummary:
    return SessionSummary(
        session_id=record.id,
        title=record.title,
        # gap: SessionRecord carries no project foreign key.
        project_ref=None,
        selectors=SessionSelectors(
            provider_id=record.provider_id,
            # gap: SessionRecord carries none of these selector fields.
            runtime_profile_id=None,
            model=None,
            reasoning_effort=None,
            orchestration_mode=None,
            cwd=record.cwd,
        ),
        # gap: SessionRecord carries no running/failed/queued signal —
        # conservatively always IDLE rather than inventing one.
        state=SessionRollupState.IDLE,
        # gap: no attention-marker source on SessionRecord.
        attention_markers=(),
        opened_at=_parse_epoch(record.opened_at),
        last_activity_at=_parse_epoch(record.updated_at) or 0.0,
        archived=record.archived,
    )


def _map_tree_node(record: SessionRecord) -> SessionTreeNode:
    return SessionTreeNode(
        surface_id=record.id,
        parent_surface_id=None,
        kind="root",
        title=record.title,
        state=SessionRollupState.IDLE,
    )


class SessionSurfaceAdapter(SessionSurface):
    def __init__(self) -> None:
        self._projection = BusBoundProjection()
        self._cache_lock = threading.Lock()
        self._last_summary: dict[SessionId, SessionSummary] = {}

    def bind(self) -> None:
        """Idempotent: `BusBoundProjection.bind` unsubscribes-then-
        resubscribes under deterministic per-pattern names."""
        self._projection.bind(
            [
                ("session.fire.*", self._on_session_fire),
                ("lifecycle.turn_*", self._on_lifecycle),
            ]
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
        summaries = tuple(_map_summary(r) for r in page)
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
        return Ok((_map_tree_node(record),), snapshot)

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
        await self._refresh(event.sid)

    async def _on_lifecycle(self, event: BusEvent) -> None:
        await self._refresh(event.sid)

    async def _refresh(self, session_id: str) -> None:
        if not session_id:
            return
        record = store_access.get_session_record(session_id)
        if record is None:
            # gap: SessionFrame has no deletion/tombstone variant — a
            # deleted session silently drops out of the cache with no
            # frame broadcast to tell a live subscriber it is gone.
            with self._cache_lock:
                self._last_summary.pop(session_id, None)
            return
        summary = _map_summary(record)
        with self._cache_lock:
            if self._last_summary.get(session_id) == summary:
                return
            self._last_summary[session_id] = summary
        cv = self._projection.bump_render()
        self._projection.broadcast(SessionSummaryUpsert(cv=cv, summary=summary))
