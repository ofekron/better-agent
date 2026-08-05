"""Concrete RunsSurface implementation (ADR 0009).

Reads exclusively through `backend.adapters.store_access.store_access`;
live deltas are best-effort from `lifecycle.turn_*` facts (see `bind()`
and `_on_lifecycle` for the linkage heuristic and its limits).

Every field this adapter cannot honestly derive from `StoreAccess`'s
current surface is called out inline as a `# gap:` comment rather than
guessed at (CLAUDE.md: "never invent"); the caller's report enumerates
them.
"""

from __future__ import annotations

from backend.adapters.projection import BusBoundProjection
from backend.adapters.store_access import RunRecord, store_access
from backend.event_bus import BusEvent
from backend.surface_contract.identity import (
    Emit,
    Ok,
    PageCursor,
    ProjectionResult,
    Rebuilding,
    SessionId,
    StaleCursor,
    Subscription,
)
from backend.surface_contract.runs_surface import (
    DetailValueKind,
    RunDetail,
    RunDetailEntry,
    RunPhase,
    RunsSurface,
    RunSummary,
    RunSummaryUpsert,
    UsagePage,
)


class _SubscriptionImpl:
    def __init__(self, close_fn) -> None:
        self._close_fn = close_fn

    def close(self) -> None:
        self._close_fn()


def _phase(record: RunRecord) -> RunPhase:
    if record.success is True:
        return RunPhase.COMPLETED
    if record.success is False:
        return RunPhase.FAILED
    # gap: no orphan/reconciliation classification reaches store_access,
    # so UNKNOWN_AFTER_RECOVERY is unreachable — conservatively RUNNING
    # when `success` is absent (complete.json not yet written).
    return RunPhase.RUNNING


def _provider_id(record: RunRecord) -> str:
    # gap: RunRecord carries no provider_id of its own — the only
    # available cross-reference is the owning session's record.
    session = store_access.get_session_record(record.session_id)
    return (session.provider_id or "") if session is not None and session.provider_id else ""


def _map_summary(record: RunRecord) -> RunSummary:
    return RunSummary(
        run_id=record.run_id,
        session_id=record.session_id,
        # gap: no turn_id source on RunRecord.
        turn_id=None,
        provider_id=_provider_id(record),
        # gap: no runner source on RunRecord or joinable via the session.
        runner="",
        phase=_phase(record),
        started_at=record.started_at,
        # gap: no heartbeat source — `stalled` is never computed here,
        # per the surface contract's note that it's backend-computed.
        last_heartbeat_at=None,
        startup=None,
    )


class RunsSurfaceAdapter(RunsSurface):
    def __init__(self) -> None:
        self._projection = BusBoundProjection()

    def bind(self) -> None:
        self._projection.bind([("lifecycle.turn_*", self._on_lifecycle)])

    # ---- read plane ------------------------------------------------

    def list_runs(
        self, session_id: SessionId | None, cursor: PageCursor | None
    ) -> ProjectionResult[tuple[RunSummary, ...]]:
        snapshot = self._projection.snapshot()
        if cursor is not None and cursor.snapshot.incarnation != snapshot.incarnation:
            return StaleCursor()
        # gap: store_access.list_run_records() has no pagination source —
        # every matching run is returned in one page regardless of cursor.
        records = store_access.list_run_records()
        if session_id is not None:
            records = tuple(r for r in records if r.session_id == session_id)
        return Ok(tuple(_map_summary(r) for r in records), snapshot)

    def run_detail(self, run_id: str) -> ProjectionResult[RunDetail]:
        snapshot = self._projection.snapshot()
        record = next(
            (r for r in store_access.list_run_records() if r.run_id == run_id), None,
        )
        if record is None:
            # gap: the contract has no "not found" projection state —
            # Rebuilding is the closest honest signal for a missing run.
            return Rebuilding(retry_after_ms=None)
        entries = (
            RunDetailEntry(key="run.detail.run_id", value_kind=DetailValueKind.TEXT, value=record.run_id),
            RunDetailEntry(key="run.detail.session_id", value_kind=DetailValueKind.TEXT, value=record.session_id),
            RunDetailEntry(key="run.detail.started_at", value_kind=DetailValueKind.TEXT, value=record.started_at),
            RunDetailEntry(key="run.detail.success", value_kind=DetailValueKind.TEXT, value=record.success),
        )
        if record.error:
            entries += (
                RunDetailEntry(key="run.detail.error", value_kind=DetailValueKind.TEXT, value=record.error),
            )
        return Ok(RunDetail(summary=_map_summary(record), entries=entries), snapshot)

    def usage_analytics(self, cursor: PageCursor | None) -> ProjectionResult[UsagePage]:
        # gap: no aggregate usage source in scope.
        return Rebuilding(retry_after_ms=None)

    # ---- live plane --------------------------------------------------

    def subscribe(self, emit: Emit) -> Subscription:
        self._projection.register(emit)
        return _SubscriptionImpl(lambda: self._projection.unregister(emit))

    # ---- internals: live fact handlers ----------------------------------

    async def _on_lifecycle(self, event: BusEvent) -> None:
        session_id = event.sid
        if not session_id:
            return
        records = [r for r in store_access.list_run_records() if r.session_id == session_id]
        if not records:
            return
        # gap: no direct run<->turn index — best-effort linkage picks the
        # most recently started run for this session.
        record = max(records, key=lambda r: r.started_at)
        cv = self._projection.bump_render()
        self._projection.broadcast(RunSummaryUpsert(cv=cv, summary=_map_summary(record)))
