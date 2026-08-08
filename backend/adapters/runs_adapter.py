"""Concrete RunsSurface implementation (ADR 0009).

Reads exclusively through `backend.adapters.store_access.store_access`;
live deltas fold from TWO independent bus-fact families:

- `lifecycle.turn_*` (see `bind()` and `_on_lifecycle`) — authoritative
  run<->turn linkage, matched by exact `turn_id` (see `_on_lifecycle`'s
  docstring), no same-session-most-recent heuristic.
- `run.state.*` (see `_on_run_state_fact`) — `turn_manager.py`'s own
  `_run_state` mutation facts (startup-phase transitions, stalled
  detection, recovery classification, run kind/anchoring assignment, pid
  capture). Folded into `self._live`, a per-run_id, LIVE-ONLY overlay —
  present while this backend incarnation has observed the run's facts,
  honestly absent/default after a cold restart (never persisted, never
  stale; see `_LiveRunState`'s docstring for the persistence-honesty
  split against `RunRecord`'s disk-derived fields).

Every field this adapter cannot honestly derive from either source is
called out inline as a `# gap:` comment rather than guessed at (CLAUDE.md:
"never invent"); the caller's report enumerates them.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from backend.adapters.projection import BusBoundProjection
from backend.adapters.store_access import RunRecord, store_access
from backend.adapters.usage_analytics import aggregate_usage_rows
from backend.event_bus import BusEvent
from backend.surface_contract.identity import (
    Emit,
    Ok,
    PageCursor,
    ProjectionResult,
    Rebuilding,
    SessionId,
    SnapshotIdentity,
    StaleCursor,
    Subscription,
)
from backend.surface_contract.runs_surface import (
    DetailValueKind,
    RunDetail,
    RunDetailEntry,
    RunKind,
    RunPage,
    RunPhase,
    RunsSurface,
    RunSummary,
    RunSummaryUpsert,
    StartupProgress,
    UsagePage,
)

_LIST_SURFACE_ID = "runs:list"
_USAGE_SURFACE_ID = "runs:usage_analytics"
_PAGE_SIZE = 50

# Heartbeat staleness threshold for `RunPhase.STALLED`, applied to
# `RunRecord.last_heartbeat_at` (the `runner_alive` sentinel's mtime).
# Mirrors the numeric value `turn_manager.py`'s `_AUDIT_STALE_HEARTBEAT_S`
# already uses for its OWN audit of the same `runner_alive` signal
# ("refreshed every ~5s ... older than this while the pid is alive means
# the runner is stuck or the heartbeat writer died") — that constant lives
# in a module outside this adapter's reach (`turn_manager.py` is edited
# for run-state publication only, not general import), so the NUMBER is
# mirrored here rather than imported; a follow-up should hoist both to one
# shared constant (e.g. in `runs_dir.py`, already imported by both sides)
# once that's in scope for a cross-cutting edit.
_STALE_HEARTBEAT_S = 30.0

# `run.state.*` fact type strings — MUST match `turn_manager.py`'s
# `_RUN_STATE_FACT_*` constants exactly (string contract between producer
# and subscriber; `turn_manager.py` is out of this module's import
# boundary, so the strings are duplicated here rather than imported, same
# rationale as `_STALE_HEARTBEAT_S` above).
_FACT_ANCHOR_CHANGED = "run.state.anchor_changed"
_FACT_PID_CHANGED = "run.state.pid_changed"
_FACT_RETRYING = "run.state.retrying"
_FACT_REMOVED = "run.state.removed"
_FACT_STARTUP_PHASE_CHANGED = "run.state.startup_phase_changed"
_FACT_RECOVERY_CLASSIFIED = "run.state.recovery_classified"


@dataclass
class _LiveRunState:
    """One run's live-only overlay, folded in place from `run.state.*`
    facts (`_on_run_state_fact`) — never read from disk, never persisted.

    Persistence-honesty split (CLAUDE.md: never serve stale data as
    live): `RunRecord` (store_access.py) carries everything
    disk-reconstructable (run_id, session_id, turn_id, started_at,
    success, error, last_heartbeat_at via the `runner_alive` sentinel) and
    stays correct across a backend restart. Every field here has NO disk
    fallback — `turn_manager.py`'s `_run_state` is purely in-memory — so a
    cold restart genuinely loses this history; the adapter's own state
    (this dict) starts empty again post-restart, and `_map_summary` must
    render that as an honest absence (None / False), never a stale
    leftover value.
    """
    session_id: str
    kind: RunKind | None = None
    target_message_id: str | None = None
    delegation_id: str | None = None
    pid: int | None = None
    startup_phase: str | None = None
    stalled_at_raw: str | None = None  # turn_manager's ISO-8601 string
    recovered_at_startup: bool = False


def _decode_offset_cursor(
    cursor: PageCursor | None, snapshot: SnapshotIdentity,
) -> int | StaleCursor:
    """Shared offset-token cursor decode for this adapter's two paged
    reads (`list_runs`, `usage_analytics`) — one incarnation-check + one
    `int(token)` parse, factored once rather than duplicated per method."""
    if cursor is None:
        return 0
    if cursor.snapshot.incarnation != snapshot.incarnation:
        return StaleCursor()
    try:
        return int(cursor.token)
    except ValueError:
        return StaleCursor()


class _SubscriptionImpl:
    def __init__(self, close_fn) -> None:
        self._close_fn = close_fn

    def close(self) -> None:
        self._close_fn()


def _to_run_kind(value: object) -> RunKind | None:
    if not isinstance(value, str):
        return None
    try:
        return RunKind(value)
    except ValueError:
        return None


def _parse_iso_to_epoch(value: str | None) -> float | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _phase(record: RunRecord, live: _LiveRunState | None, *, now: float) -> RunPhase:
    if record.success is True:
        return RunPhase.COMPLETED
    if record.success is False:
        return RunPhase.FAILED
    if live is not None and live.startup_phase == "stalled":
        return RunPhase.STALLED
    if live is not None and live.recovered_at_startup:
        # This run was reattached at backend startup (`run.state.
        # recovery_classified`, sourced from `run_recovery.py`'s
        # startup path via `turn_manager.run_state_record_activity`'s
        # "recovered_run" activity marker — the one recovery-
        # classification signal reachable from turn_manager.py; see its
        # docstring). Sticky for the run's whole live overlay life, so a
        # recovered run is never reported as a plain, fully-confirmed
        # RUNNING within this backend incarnation — only heartbeat
        # freshness is independently re-verifiable post-restart.
        if record.last_heartbeat_at is None:
            # No `runner_alive` sentinel observed on THIS incarnation at
            # all yet — genuinely unknown whether the reattached process
            # is alive.
            return RunPhase.UNKNOWN_AFTER_RECOVERY
        if now - record.last_heartbeat_at > _STALE_HEARTBEAT_S:
            return RunPhase.STALLED
        return RunPhase.RECONNECTING
    # gap: COMPLETING has no reachable source. Tracing `run_turn`'s
    # finally block (turn_manager.py): `_publish_terminal_lifecycle` (E5's
    # RunRecord.success write) and `run_state_remove` (this adapter's live
    # overlay clears via `run.state.removed`) both fire in the SAME
    # synchronous finally block with no observable state in between —
    # there is no genuine "finalizing" window turn_manager.py currently
    # marks as a distinct fact-worthy state to source COMPLETING from.
    if record.last_heartbeat_at is None:
        return RunPhase.STARTING
    if now - record.last_heartbeat_at > _STALE_HEARTBEAT_S:
        return RunPhase.STALLED
    return RunPhase.RUNNING


def _provider_id(record: RunRecord) -> str:
    # gap: RunRecord carries no provider_id of its own — the only
    # available cross-reference is the owning session's record.
    session = store_access.get_session_record(record.session_id)
    return (session.provider_id or "") if session is not None and session.provider_id else ""


def _runner(provider_id: str) -> str:
    # gap: RunRecord carries no runner of its own, and a provider can have
    # more than one runtime profile — best available signal is the first
    # runtime profile configured for the run's provider (list order); a
    # provider with several runners stamps every one of its runs with the
    # same first-found runner.
    if not provider_id:
        return ""
    return next(
        (p.runner for p in store_access.list_runtime_profiles() if p.provider_id == provider_id),
        "",
    )


def _map_summary(
    record: RunRecord, live: _LiveRunState | None = None, *, now: float | None = None,
) -> RunSummary:
    provider_id = _provider_id(record)
    now = now if now is not None else time.time()
    startup_phase = live.startup_phase if live is not None else None
    return RunSummary(
        run_id=record.run_id,
        session_id=record.session_id,
        turn_id=record.turn_id,
        provider_id=provider_id,
        runner=_runner(provider_id),
        phase=_phase(record, live, now=now),
        started_at=record.started_at,
        last_heartbeat_at=record.last_heartbeat_at,
        # `startup_phase` is turn_manager.py's own opaque token
        # (awaiting_provider_start/awaiting_provider_ready/running/
        # stalled/unsupported) — matches this field's pre-existing
        # contract ("opaque provider-declared token; display resolves via
        # the provider descriptor's copy keys, never by branching on the
        # value"). `progress` has no reachable source (turn_manager.py
        # tracks phase, never a fractional progress within it).
        startup=StartupProgress(phase=startup_phase, progress=None) if startup_phase else None,
        kind=live.kind if live is not None else None,
        target_message_id=live.target_message_id if live is not None else None,
        delegation_id=live.delegation_id if live is not None else None,
        pid=live.pid if live is not None else None,
        stalled_at=_parse_iso_to_epoch(live.stalled_at_raw) if live is not None else None,
        recovered_at_startup=live.recovered_at_startup if live is not None else False,
    )


class RunsSurfaceAdapter(RunsSurface):
    def __init__(self) -> None:
        self._projection = BusBoundProjection()
        self._live: dict[str, _LiveRunState] = {}
        # Serializes the off-loop record scan + broadcast across both bus
        # handlers: without it two handlers' scans can resolve out of
        # dispatch order and a stale RunRecord snapshot would be broadcast
        # after a newer one.
        self._scan_broadcast_lock = asyncio.Lock()

    def bind(self) -> None:
        self._projection.bind([
            ("lifecycle.turn_*", self._on_lifecycle),
            ("run.state.*", self._on_run_state_fact),
        ])

    # ---- read plane ------------------------------------------------

    def list_runs(
        self, session_id: SessionId | None, cursor: PageCursor | None
    ) -> ProjectionResult[RunPage]:
        snapshot = self._projection.snapshot()
        offset = _decode_offset_cursor(cursor, snapshot)
        if isinstance(offset, StaleCursor):
            return offset

        records = list(store_access.list_run_records())
        if session_id is not None:
            records = [r for r in records if r.session_id == session_id]
        records.sort(key=lambda r: r.started_at, reverse=True)

        page = records[offset : offset + _PAGE_SIZE]
        next_cursor = None
        if offset + _PAGE_SIZE < len(records):
            next_cursor = PageCursor(
                surface_id=_LIST_SURFACE_ID, snapshot=snapshot, token=str(offset + _PAGE_SIZE),
            )
        now = time.time()
        summaries = tuple(_map_summary(r, self._live.get(r.run_id), now=now) for r in page)
        return Ok(RunPage(runs=summaries, next_cursor=next_cursor), snapshot)

    def run_detail(self, run_id: str) -> ProjectionResult[RunDetail]:
        snapshot = self._projection.snapshot()
        record = next(
            (r for r in store_access.list_run_records() if r.run_id == run_id), None,
        )
        if record is None:
            # gap: the contract has no "not found" projection state —
            # Rebuilding is the closest honest signal for a missing run.
            return Rebuilding(retry_after_ms=None)
        live = self._live.get(run_id)
        entries = (
            RunDetailEntry(key="run.detail.run_id", value_kind=DetailValueKind.TEXT, value=record.run_id),
            RunDetailEntry(key="run.detail.session_id", value_kind=DetailValueKind.TEXT, value=record.session_id),
            RunDetailEntry(key="run.detail.started_at", value_kind=DetailValueKind.TEXT, value=record.started_at),
            RunDetailEntry(key="run.detail.success", value_kind=DetailValueKind.TEXT, value=record.success),
        )
        if record.turn_id is not None:
            entries += (
                RunDetailEntry(key="run.detail.turn_id", value_kind=DetailValueKind.REF, value=record.turn_id),
            )
        provider_id = _provider_id(record)
        if provider_id:
            entries += (
                RunDetailEntry(key="run.detail.provider_id", value_kind=DetailValueKind.REF, value=provider_id),
            )
            runner = _runner(provider_id)
            if runner:
                entries += (
                    RunDetailEntry(key="run.detail.runner", value_kind=DetailValueKind.TEXT, value=runner),
                )
        now = time.time()
        if record.last_heartbeat_at is not None:
            entries += (
                RunDetailEntry(
                    key="run.detail.last_heartbeat_at",
                    value_kind=DetailValueKind.TEXT,
                    value=record.last_heartbeat_at,
                ),
                RunDetailEntry(
                    key="run.detail.heartbeat_silence_seconds",
                    value_kind=DetailValueKind.DURATION,
                    value=max(0.0, now - record.last_heartbeat_at),
                ),
            )
        if record.error:
            entries += (
                RunDetailEntry(key="run.detail.error", value_kind=DetailValueKind.TEXT, value=record.error),
            )
        # Live-only overlay fields (turn_manager.py's `run.state.*` facts,
        # folded into `self._live`) — absent, by construction, when this
        # backend incarnation never observed a fact for this run_id
        # (cold/historic run, or a restart since it last ran).
        if live is not None:
            if live.kind is not None:
                entries += (
                    RunDetailEntry(key="run.detail.kind", value_kind=DetailValueKind.TEXT, value=live.kind.value),
                )
            if live.target_message_id is not None:
                entries += (
                    RunDetailEntry(
                        key="run.detail.target_message_id",
                        value_kind=DetailValueKind.REF,
                        value=live.target_message_id,
                    ),
                )
            if live.delegation_id is not None:
                entries += (
                    RunDetailEntry(
                        key="run.detail.delegation_id",
                        value_kind=DetailValueKind.REF,
                        value=live.delegation_id,
                    ),
                )
            if live.pid is not None:
                entries += (
                    RunDetailEntry(key="run.detail.pid", value_kind=DetailValueKind.COUNT, value=live.pid),
                )
            if live.startup_phase is not None:
                entries += (
                    RunDetailEntry(
                        key="run.detail.startup_phase",
                        value_kind=DetailValueKind.TEXT,
                        value=live.startup_phase,
                    ),
                )
            stalled_at = _parse_iso_to_epoch(live.stalled_at_raw)
            if stalled_at is not None:
                entries += (
                    RunDetailEntry(key="run.detail.stalled_at", value_kind=DetailValueKind.TEXT, value=stalled_at),
                )
            if live.recovered_at_startup:
                entries += (
                    RunDetailEntry(
                        key="run.detail.recovered_at_startup",
                        value_kind=DetailValueKind.TEXT,
                        value=True,
                    ),
                )
            # Process tree: inherently on-demand/read-time (live OS state,
            # not a discrete bus-publishable event — a "process tree
            # changed" fact has no natural moment to fire on). Reuses the
            # EXACT helper `session_detail_api.py`'s legacy route calls
            # (`process_inspect.inspect_process_tree`, via
            # `store_access.inspect_process_tree` — adapters may only
            # reach it through that one sanctioned door) — one
            # implementation, not a second. NOTE: unlike the legacy
            # route's `await asyncio.to_thread(inspect_process_tree, ...)`
            # (`session_detail_api.py:2049`), this call is synchronous —
            # `RunsSurface.run_detail` is a plain (non-async) method and
            # `adapter_api.py`'s route calls it inline on the event loop
            # (not in this module's edit scope to change). This forks
            # `ps`/`pgrep` (blocking) for the duration of an on-demand,
            # rare (modal-open-triggered) call — flagged in the caller's
            # report as a follow-up for whoever owns `adapter_api.py`'s
            # `GET /runs/{run_id}` route (a one-line `asyncio.to_thread`
            # wrap would restore the non-blocking guarantee).
            if live.pid is not None:
                processes = store_access.inspect_process_tree(live.pid, run_id)
                entries += (
                    RunDetailEntry(
                        key="run.detail.process_tree",
                        value_kind=DetailValueKind.PROCESS_TREE,
                        value=processes,
                    ),
                )
        return Ok(RunDetail(summary=_map_summary(record, live, now=now), entries=entries), snapshot)

    def usage_analytics(self, cursor: PageCursor | None) -> ProjectionResult[UsagePage]:
        """E6: `{provider_id, model, period}`-keyed rows aggregated from
        `store_access.list_llm_call_records()` — see `backend.adapters.
        usage_analytics`'s module docstring for the source choice and the
        honest-absence (`REPORTED`/`UNREPORTED`) derivation. Pull-only per
        ADR 0009 ("no live analytics stream") — same offset-token cursor
        idiom `list_runs` uses, over the same `self._projection` snapshot
        (this adapter has exactly one incarnation, shared across both
        paged reads)."""
        snapshot = self._projection.snapshot()
        offset = _decode_offset_cursor(cursor, snapshot)
        if isinstance(offset, StaleCursor):
            return offset

        rows = aggregate_usage_rows(store_access.list_llm_call_records())
        page = rows[offset : offset + _PAGE_SIZE]
        next_cursor = None
        if offset + _PAGE_SIZE < len(rows):
            next_cursor = PageCursor(
                surface_id=_USAGE_SURFACE_ID, snapshot=snapshot, token=str(offset + _PAGE_SIZE),
            )
        return Ok(UsagePage(rows=tuple(page), next_cursor=next_cursor), snapshot)

    # ---- live plane --------------------------------------------------

    def subscribe(self, emit: Emit) -> Subscription:
        self._projection.register(emit)
        return _SubscriptionImpl(lambda: self._projection.unregister(emit))

    # ---- internals: live fact handlers ----------------------------------

    def _broadcast_summary(self, record: RunRecord) -> None:
        """Shared tail for both fact handlers below: bump the render
        revision and broadcast a fresh `RunSummaryUpsert` for `record`,
        folding in whatever live overlay `self._live` currently holds for
        its run_id (absent overlay -> honest defaults, see `_map_summary`).
        """
        cv = self._projection.bump_render()
        self._projection.broadcast(
            RunSummaryUpsert(cv=cv, summary=_map_summary(record, self._live.get(record.run_id))),
        )

    async def _on_lifecycle(self, event: BusEvent) -> None:
        session_id = event.sid
        if not session_id:
            return
        # Authoritative run<->turn linkage: `lifecycle.turn_start` /
        # `lifecycle.turn_complete` / `lifecycle.turn_stopped` (the only
        # event types matching this adapter's `lifecycle.turn_*` bind
        # pattern) all carry `execution_turn_id` in their payload
        # (`turn_manager.py`'s `_publish_turn_start_lifecycle` /
        # `_publish_terminal_lifecycle`, non-empty per
        # `_validate_lifecycle_identity`) — the SAME id that ends up naming
        # `runs/<run_id>/turns/<turn_id>/` on disk (see store_access's
        # `_turn_id_for_run_dir`), so an exact match against
        # `RunRecord.turn_id` replaces the previous same-session-most-
        # recent-started heuristic entirely; no fallback to it.
        turn_id = event.payload.get("execution_turn_id")
        if not isinstance(turn_id, str) or not turn_id:
            return
        # list_run_records_for_session walks only this session's own run
        # dirs (SQL-filtered) — sync filesystem work that must not run on
        # the event-loop thread, but O(this session's runs) rather than
        # O(every session the backend has ever run), which is what
        # list_run_records() would cost here on every turn_start/turn_
        # complete/turn_stopped fact for EVERY session in the backend.
        async with self._scan_broadcast_lock:
            records = await asyncio.to_thread(
                store_access.list_run_records_for_session, session_id,
            )
            record = next(
                (
                    r for r in records
                    if r.session_id == session_id and r.turn_id == turn_id
                ),
                None,
            )
            if record is None:
                # The run directory for this turn hasn't appeared on disk
                # yet (admission can race ahead of the runner creating
                # `turns/<turn_id>/`) — no push now, rather than
                # misattribute to a different run. The terminal lifecycle
                # event for this SAME turn fires later, by which point the
                # runner has always written its turn directory, so the live
                # feed catches up; a `list_runs()`/`run_detail()` pull
                # always sees the truth regardless.
                return
            self._broadcast_summary(record)

    async def _on_run_state_fact(self, event: BusEvent) -> None:
        """Folds one `run.state.*` fact (turn_manager.py — see that
        module's `_publish_run_state_fact`) into `self._live`, then
        broadcasts a fresh `RunSummaryUpsert` IF a `RunRecord` already
        exists on disk for this run_id (fail-closed, same rationale as
        `_on_lifecycle`: a run-state fact can arrive before the runner has
        written `runs/<run_id>/` — the overlay is still updated so a
        subsequent pull sees it once the record catches up)."""
        run_id = event.run_id or event.payload.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            return
        fact_type = event.type
        if fact_type == _FACT_REMOVED:
            removed = self._live.pop(run_id, None)
            session_id = (
                removed.session_id if removed is not None
                else event.sid or str(event.payload.get("session_id") or "")
            )
        else:
            payload = event.payload
            live = self._live.get(run_id)
            if live is None:
                session_id = event.sid or str(payload.get("session_id") or "")
                live = _LiveRunState(session_id=session_id)
                self._live[run_id] = live
            else:
                session_id = live.session_id
            if fact_type == _FACT_ANCHOR_CHANGED:
                if "kind" in payload:
                    live.kind = _to_run_kind(payload.get("kind"))
                if "target_message_id" in payload:
                    live.target_message_id = payload.get("target_message_id")
                if "delegation_id" in payload:
                    live.delegation_id = payload.get("delegation_id")
                if "pid" in payload:
                    live.pid = payload.get("pid")
            elif fact_type == _FACT_PID_CHANGED:
                live.pid = payload.get("pid")
            elif fact_type == _FACT_STARTUP_PHASE_CHANGED:
                live.startup_phase = payload.get("startup_phase")
                live.stalled_at_raw = payload.get("stalled_at")
            elif fact_type == _FACT_RECOVERY_CLASSIFIED:
                live.recovered_at_startup = True
            elif fact_type == _FACT_RETRYING:
                # No dedicated overlay field: `run_state_begin_retry`
                # (turn_manager.py) always pairs this with a
                # `run.state.pid_changed(pid=None)` fact on the same
                # transition, which already clears `live.pid` — the only
                # currently-projected effect of "retrying".
                pass

        if not session_id:
            return
        async with self._scan_broadcast_lock:
            records = await asyncio.to_thread(
                store_access.list_run_records_for_session, session_id,
            )
            record = next((r for r in records if r.run_id == run_id), None)
            if record is None:
                return
            self._broadcast_summary(record)
