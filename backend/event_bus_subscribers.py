"""Standard subscribers wired into `event_bus` at startup.

Today:
  - Persistence subscriber (priority 10) that funnels every persistent
    bus event into the event journal writer. From there `BetterAgentJsonlTailer`
    (which tails `events.jsonl` for any subscribed root) handles live
    WS fan-out and catch-up replay automatically — so persistence + WS
    broadcast are the same single side-effect.
  - Session content projection subscriber: turns written journal rows
    back into SessionManager-owned render-tree state.
  - Session worker-fanout projection subscriber: owns worker/fork cleanup
    for `session.worker_fanout_required` facts.

When/if other transports are added (metrics, traces, third-party
webhooks), they register here with appropriately higher priority so
they never run before persistence.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable

from event_bus import BusEvent, bus, register_event_schema
from event_journal import (
    EVENT_JOURNAL_EVENT,
    EVENT_JOURNAL_WRITTEN,
    EventJournalWriter,
    RENDER_EVENT_TYPES,
    event_journal_writer,
)
from session_manager import manager as session_manager
import perf

logger = logging.getLogger(__name__)


_JOURNAL_SUBSCRIBER_PRIORITY = 10  # MUST run before WS-facing subscribers
_SESSION_PROJECTION_PRIORITY = 20  # after journal write, before WS
_SESSION_LIFECYCLE_PROJECTION_PRIORITY = 6  # after lifecycle tree priority 5
_SESSION_PROJECTION_SHARDS = 8
_SESSION_PROJECTION_MAX_PENDING = 256
_SESSION_PROJECTION_DRAIN_CHUNK = 128


@dataclass(frozen=True)
class SessionProjectionCommand:
    root_id: str
    sid: str
    msg_id: str
    event_type: str
    source: str
    seq: int


@dataclass
class _ProjectionRootState:
    cursor: int
    target: int
    queued_at: float
    active: bool = False
    parked: bool = False
    expected_applicability: dict[int, bool] | None = None
    # Durable fold watermark snapshotted ONCE when this root transitions
    # idle → active (see `SessionProjectionDrainer._activate_locked`), not
    # re-read per row. Every row folded in this pass compares its seq
    # against this SAME value, so a cold-start catch-up of N backlog rows
    # judges all N against the one pre-restart watermark (all fire), while
    # a live steady-state single-row pass judges that one row against the
    # watermark from just before it (fires, then the watermark advances
    # past it — idempotent on any later re-fold).
    fold_start_watermark: int = 0


def _projection_command_is_applicable(command: SessionProjectionCommand) -> bool:
    return (
        command.event_type == "event_ownership_resolved"
        or command.event_type in RENDER_EVENT_TYPES
    )


class SessionProjectionDrainer:
    """Coalesces journal acknowledgements into ordered per-root drains."""

    def __init__(
        self,
        apply_row: Callable[[str, dict, int], None],
        read_rows: Callable[[str, int, int], list[dict]],
        mark_dirty: Callable[[str, BaseException], None],
        *,
        shards: int,
        max_active_roots: int,
        chunk_size: int,
        get_watermark: Callable[[str], int] = lambda _root_id: 0,
    ) -> None:
        self._apply_row = apply_row
        self._read_rows = read_rows
        self._mark_dirty = mark_dirty
        self._get_watermark = get_watermark
        self._max_active_roots = max_active_roots
        self._chunk_size = chunk_size
        self._lock = threading.Condition(threading.RLock())
        self._states: dict[str, _ProjectionRootState] = {}
        self._active_roots = 0
        # Roots whose target cursor advanced past the current capacity limit.
        # They are never dropped: `_promote_parked_locked` pops one every time
        # a slot frees, so backpressure delays a root's drain but never loses
        # it (no read/query is involved in that hand-off, only this deque).
        self._parked_roots: deque[str] = deque()
        self._accepting = True
        self._executors = tuple(
            ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix=f"session-projection-{i}",
            )
            for i in range(shards)
        )
        perf.register_queue("session_projection.active_roots", self.active_root_count)
        perf.register_queue("session_projection.pending_rows", self.pending_row_count)
        perf.register_queue("session_projection.parked_roots", self.parked_root_count)

    def submit(self, command: SessionProjectionCommand) -> bool:
        with self._lock:
            if not self._accepting:
                self._mark_dirty(
                    command.root_id,
                    RuntimeError("session projection is closed"),
                )
                perf.record_count("session_projection.errors")
                return False
            state = self._states.get(command.root_id)
            if state is None:
                state = _ProjectionRootState(
                    cursor=command.seq - 1,
                    target=command.seq,
                    queued_at=time.perf_counter(),
                    expected_applicability={},
                )
                self._states[command.root_id] = state
            else:
                assert state.expected_applicability is not None
                expected = _projection_command_is_applicable(command)
                prior_expected = state.expected_applicability.get(command.seq)
                if prior_expected is not None and prior_expected != expected:
                    perf.record_count(
                        "session_projection.command_expectation_conflict",
                    )
                    self._report_dirty(
                        command.root_id,
                        RuntimeError(
                            "session projection conflicting command "
                            f"applicability root={command.root_id} "
                            f"seq={command.seq}",
                        ),
                    )
                else:
                    state.expected_applicability[command.seq] = expected
                if command.seq <= state.target:
                    perf.record_count("session_projection.coalesced")
                    if state.active:
                        return True
                else:
                    perf.record_count(
                        "session_projection.coalesced",
                        command.seq - state.target,
                    )
                    state.target = command.seq
            assert state.expected_applicability is not None
            state.expected_applicability.setdefault(
                command.seq,
                _projection_command_is_applicable(command),
            )
            perf.record_count("session_projection.highwater", state.target)
            self._ensure_draining_locked(command.root_id, state)
            return True

    def require(self, root_id: str, seq: int) -> None:
        """Raise a root's fold target to a durable journal seq, with no
        command behind it. Must hold nothing; takes `self._lock`.

        `submit` is driven by the asynchronous `EVENT_JOURNAL_WRITTEN`
        hop, which lands strictly AFTER a synchronous journal write
        returns to its caller (`EventJournalWriter._schedule_written`
        hands the publish to the bound loop fire-and-forget). A caller
        that just journaled rows and now wants them folded therefore
        cannot rely on the command having arrived — `barrier` would find
        no state for the root and return having waited for nothing. This
        is the seq-addressed entry point that closes that window: the
        drainer reads rows from the durable journal by seq anyway, so a
        target raised directly from the writer's high-water mark folds
        exactly the same rows the pending commands would have. Those
        commands still arrive and coalesce into a no-op.
        """
        if seq <= 0:
            return
        with self._lock:
            if not self._accepting:
                return
            state = self._states.get(root_id)
            if state is None:
                # No fold pass has ever run for this root in this process,
                # so the durable fold watermark is the only record of how
                # far it got. Seeding `cursor` from `seq - 1` the way
                # `submit` does would silently declare every earlier row
                # already folded — `submit` gets away with it because its
                # commands arrive one per row from seq 1 up; a target
                # pulled straight from the writer's high-water mark skips
                # to the end.
                #
                # The watermark reads 0 for a root that is not resident,
                # so an evicted root re-folds its whole journal here. That
                # is the same cold-start re-fold the drainer already does
                # after a restart (idempotent by uuid dedup, with side
                # effects re-armed against a 0 watermark) — deliberately
                # preferred over seeding the cursor too high, which would
                # silently drop unfolded rows.
                folded_through = self._get_watermark(root_id)
                if seq <= folded_through:
                    return
                state = _ProjectionRootState(
                    cursor=folded_through,
                    target=seq,
                    queued_at=time.perf_counter(),
                    expected_applicability={},
                )
                self._states[root_id] = state
            elif seq > state.target:
                state.target = seq
            if state.cursor >= state.target:
                return
            perf.record_count("session_projection.highwater", state.target)
            self._ensure_draining_locked(root_id, state)

    def _ensure_draining_locked(
        self, root_id: str, state: _ProjectionRootState,
    ) -> None:
        """Make sure a drain pass is scheduled for `root_id`. Must hold
        `self._lock`."""
        if state.active:
            return
        if state.parked:
            # Already queued for a slot; state.target is already the
            # highwater, so the parked entry will fold to it once
            # promoted — no need to enqueue a second time.
            return
        if self._active_roots >= self._max_active_roots:
            state.parked = True
            self._parked_roots.append(root_id)
            perf.record_count("session_projection.parked")
            return
        self._activate_locked(root_id, state)

    def _activate_locked(
        self, root_id: str, state: _ProjectionRootState,
    ) -> None:
        """Flip a root's state to active and snapshot the fold watermark
        for the fold pass this activation starts. Must hold `self._lock`.

        Snapshotting HERE (once per idle→active transition) rather than
        per-row is what makes a cold-start catch-up of N backlog rows
        judge all N against the SAME pre-restart watermark — re-reading
        per row would compare later rows against an already-advanced
        value and they'd never fire their side effects.
        """
        state.active = True
        state.queued_at = time.perf_counter()
        state.fold_start_watermark = self._get_watermark(root_id)
        self._active_roots += 1
        self._executor(root_id).submit(self._drain_chunk, root_id)

    def _executor(self, root_id: str) -> ThreadPoolExecutor:
        return self._executors[self._shard(root_id)]

    def _shard(self, root_id: str) -> int:
        return hash(root_id) % len(self._executors)

    def _drain_chunk(self, root_id: str) -> None:
        with self._lock:
            state = self._states[root_id]
            after_seq = state.cursor
            target = state.target
            queued_at = state.queued_at
            # Snapshotted once at activation (`_activate_locked`), fixed
            # for this entire pass — including across chunk-boundary
            # resubmissions below (a >chunk_size backlog spans multiple
            # `_drain_chunk` invocations but stays one fold pass; the
            # watermark must NOT be re-read on each resubmission).
            fold_start_watermark = state.fold_start_watermark
        perf.record(
            "session_projection.age",
            (time.perf_counter() - queued_at) * 1000.0,
        )
        perf.record_lag(f"session_projection.shard{self._shard(root_id)}", queued_at)
        try:
            rows = self._read_rows(root_id, after_seq, self._chunk_size)
            if not rows and after_seq < target:
                raise RuntimeError(f"durable journal row after {root_id}:{after_seq} is unavailable")
            advanced = after_seq
            for row in rows:
                seq = int(row.get("seq") or 0)
                if seq <= advanced:
                    continue
                if seq > target:
                    break
                if seq != advanced + 1:
                    raise RuntimeError(
                        f"durable journal gap for {root_id}: "
                        f"expected {advanced + 1}, got {seq}",
                    )
                row_applicable = _projection_row_is_applicable(row)
                with self._lock:
                    state = self._states[root_id]
                    assert state.expected_applicability is not None
                    command_applicable = state.expected_applicability.pop(seq, None)
                if (
                    command_applicable is not None
                    and command_applicable != row_applicable
                ):
                    direction = (
                        "command_applicable_row_skipped"
                        if command_applicable
                        else "command_skipped_row_applicable"
                    )
                    perf.record_count(
                        f"session_projection.command_row_mismatch.{direction}",
                    )
                    self._report_dirty(
                        root_id,
                        RuntimeError(
                            f"session projection command/journal mismatch "
                            f"root={root_id} seq={seq} direction={direction}",
                        ),
                    )
                if row_applicable:
                    self._apply_row(root_id, row, fold_start_watermark)
                    perf.record_count("session_projection.applied")
                else:
                    perf.record_count("session_projection.skipped")
                advanced = seq
                with self._lock:
                    self._states[root_id].cursor = advanced
            with self._lock:
                state = self._states[root_id]
                if state.cursor < state.target:
                    if state.cursor == after_seq:
                        raise RuntimeError(
                            f"session projection made no progress for "
                            f"{root_id}:{state.cursor}->{state.target}",
                        )
                    state.queued_at = time.perf_counter()
                    self._executor(root_id).submit(self._drain_chunk, root_id)
                    return
                state.active = False
                self._active_roots -= 1
                self._promote_parked_locked()
                self._lock.notify_all()
        except Exception as exc:
            with self._lock:
                state = self._states[root_id]
                state.active = False
                self._active_roots -= 1
                # Deliberately do NOT re-park this root on exception: the
                # failure may be a permanent condition (e.g. a durable
                # journal gap that will never fill in — see
                # test_missing_journal_gap_dirties_without_advancing_past_gap).
                # Re-parking unconditionally here would busy-retry that
                # permanent case forever on one shard's thread. Instead:
                # the freed slot is still handed to any OTHER root already
                # waiting in `_parked_roots` (a real slot-free event), and
                # THIS root resumes normally the next time a genuinely new
                # row is submitted for it (`submit()` sees `state.active`
                # is False and drains again from `state.cursor`) — i.e. the
                # ordinary push-driven path, no polling involved.
                self._promote_parked_locked()
                self._lock.notify_all()
            self._report_dirty(root_id, exc)

    def _promote_parked_locked(self) -> None:
        """Activate parked roots into freed slots. Must hold `self._lock`.

        O(1) per promotion: one `deque.popleft` and dict lookups, no scan.
        """
        while self._parked_roots and self._active_roots < self._max_active_roots:
            root_id = self._parked_roots.popleft()
            state = self._states.get(root_id)
            if state is None or not state.parked:
                continue
            state.parked = False
            self._activate_locked(root_id, state)

    def barrier(self, root_id: str) -> None:
        with self._lock:
            self._lock.wait_for(
                lambda: not (
                    self._states.get(root_id)
                    and (self._states[root_id].active or self._states[root_id].parked)
                ),
            )

    def shutdown(self, *, wait: bool = True, timeout_s: float = 5.0) -> None:
        drained = True
        with self._lock:
            self._accepting = False
            if wait:
                drained = self._lock.wait_for(
                    lambda: self._active_roots == 0,
                    timeout=max(0.0, timeout_s),
                )
                if not drained:
                    perf.record_count("session_projection.shutdown_timeout")
                    active = [
                        root_id
                        for root_id, state in self._states.items()
                        if state.active or state.parked
                    ]
                    self._parked_roots.clear()
                else:
                    active = []
            else:
                active = []
        for root_id in active:
            self._report_dirty(
                root_id,
                RuntimeError("session projection shutdown timed out"),
            )
        for executor in self._executors:
            executor.shutdown(wait=wait and drained, cancel_futures=not drained)
        perf.unregister_queue("session_projection.active_roots")
        perf.unregister_queue("session_projection.pending_rows")
        perf.unregister_queue("session_projection.parked_roots")

    def active_root_count(self) -> int:
        with self._lock:
            return self._active_roots

    def parked_root_count(self) -> int:
        with self._lock:
            return len(self._parked_roots)

    def is_accepting(self) -> bool:
        with self._lock:
            return self._accepting

    def pending_row_count(self) -> int:
        with self._lock:
            return sum(
                max(0, state.target - state.cursor)
                for state in self._states.values()
            )

    def _report_dirty(self, root_id: str, exc: BaseException) -> None:
        perf.record_count("session_projection.errors")
        try:
            self._mark_dirty(root_id, exc)
        except Exception:
            logger.exception(
                "session projection dirty marker failed root=%s",
                root_id,
            )


def _projection_row_is_applicable(row: dict) -> bool:
    return (
        row.get("type") == "event_ownership_resolved"
        or row.get("type") in RENDER_EVENT_TYPES
    )


# Declare event schemas at MODULE LOAD time (not inside
# `register_default_subscribers`). `register_default_subscribers` runs
# only in `on_startup`, but other `bind_*` calls happen during
# `main.py` module load — earlier. Without module-load registration,
# producers that publish between module-load and on_startup would
# stamp `schema_version=1` (the unregistered default) instead of the
# real registered version. Today everything's v1 so the outcome is
# identical; a future v2 bump without this discipline would silently
# emit mixed stamps depending on startup timing.
register_event_schema("lifecycle.turn_complete", 1)
register_event_schema("lifecycle.turn_stopped", 1)
register_event_schema("lifecycle.turn_start", 1)
register_event_schema("lifecycle.admission_registered", 1)
register_event_schema("lifecycle.admission_starting", 1)
register_event_schema("lifecycle.admission_admitted", 1)
register_event_schema("lifecycle.admission_spawned", 1)
register_event_schema("lifecycle.admission_deferred", 1)
register_event_schema("lifecycle.admission_cancel_requested", 1)
register_event_schema("lifecycle.admission_cancelled", 1)
register_event_schema("lifecycle.admission_failed", 1)
register_event_schema("session.parent_deleted", 1)
register_event_schema("session.worker_fanout_required", 1)
register_event_schema("requirement_tags.refresh_requested", 1)
register_event_schema("requirement_tags.refreshed", 1)


async def _persist_to_event_journal(event: BusEvent) -> None:
    """Bus → EventJournalWriter.

    Honors `event.persist`: backend-internal notifications opt out by
    setting `persist=False` on the BusEvent. Skipped events still fan
    out to other subscribers — they just don't land on disk.
    """
    if not event.persist:
        return
    if event.type.startswith("event_journal."):
        return
    source = "event_bus"
    payload = event.payload
    explicit_source = payload.get("__source")
    if isinstance(explicit_source, str) and explicit_source:
        source = explicit_source
        payload = {k: v for k, v in payload.items() if k != "__source"}
    journal_payload = {
        "event_type": event.type,
        "data": payload,
        "source": source,
    }
    if event.msg_id:
        journal_payload["message_id"] = event.msg_id
    await bus.publish(BusEvent(
        type=EVENT_JOURNAL_EVENT,
        root_id=event.root_id,
        sid=event.sid,
        payload=journal_payload,
        run_id=event.run_id,
        persist=False,
    ))


# Orphan rows (msg_id=None) arrive for externally-driven sessions (native
# terminal sessions Better Agent didn't spawn, which never fire a BA
# `turn_complete` fact) whenever the row lands after this fold pass's view
# of the render tree. If the sid's latest assistant message is already
# finalized there is no in-flight message it could race with, so the row
# folds immediately; otherwise it's buffered here until finalization is
# observed, then flushed in order. Transient, in-memory only — like
# `session_manager._recovering_msg_ids` — a crash loses the buffer, but the
# fold just re-buffers/re-checks any not-yet-applied orphan rows the same
# way on the next boot. Keyed by (root_id, sid) since one root can have
# multiple sids (forks).
_ORPHAN_ROW_LOCK = threading.Lock()
_PENDING_ORPHAN_ROWS: dict[tuple[str, str], list[dict]] = {}


def _fold_orphan_row(root_id: str, sid: str, row: dict, fold_start_watermark: int) -> None:
    """Fold one orphan row onto the sid's latest (now-finalized) assistant
    message. Attention-marker detection does NOT run here: the raw
    pre-strip text a marker tag lives in never survives to the persisted
    journal row (`event_ingester`'s canonical-storage rewrite strips
    `<TAG>` wrappers at write time for every source) — for orphan rows
    that detection instead runs in `orchs.base.ingest_orphan`, on the raw
    event, before it ever reaches the ingester."""
    msg_id = session_manager.latest_assistant_msg_id(sid)
    if not msg_id:
        return
    event_type = str(row.get("type") or "unknown")
    data = row.get("data")
    data = data if isinstance(data, dict) else {}
    session_manager.apply_written_journal_event(
        root_id, sid, msg_id, event_type, data, int(row.get("seq") or 0),
        fold_start_watermark=fold_start_watermark,
    )


def _handle_orphan_row(root_id: str, sid: str, row: dict, fold_start_watermark: int) -> None:
    if session_manager.latest_assistant_finalized(sid):
        _fold_orphan_row(root_id, sid, row, fold_start_watermark)
        return
    with _ORPHAN_ROW_LOCK:
        _PENDING_ORPHAN_ROWS.setdefault((root_id, sid), []).append(row)


def _flush_pending_orphan_rows(root_id: str, sid: str, fold_start_watermark: int) -> None:
    """Push-driven: called right after every non-orphan row folds for this
    root/sid, so a buffered orphan is applied the instant the fold that
    finalizes the in-flight assistant message lands — no polling.

    A buffered orphan may end up flushed in a LATER fold pass than the one
    that read it off disk; `fold_start_watermark` here is whatever pass is
    CURRENTLY applying it (the caller's), which is correct — the
    seq-vs-watermark comparison is about when the row is actually folded
    into the render tree, not when it was read off disk."""
    key = (root_id, sid)
    with _ORPHAN_ROW_LOCK:
        rows = _PENDING_ORPHAN_ROWS.get(key)
        if not rows:
            return
        if not session_manager.latest_assistant_finalized(sid):
            return
        del _PENDING_ORPHAN_ROWS[key]
    for buffered_row in rows:
        _fold_orphan_row(root_id, sid, buffered_row, fold_start_watermark)


def _apply_session_projection_row(root_id: str, row: dict, fold_start_watermark: int) -> None:
    event_type = str(row.get("type") or "unknown")
    sid = str(row.get("sid") or root_id)
    msg_id = str(row.get("msg_id") or "")
    seq = int(row.get("seq") or 0)
    # Flush BEFORE processing this row too: a buffered orphan always has a
    # lower seq than whatever's being folded now, so if finalization was
    # already observed on an earlier row this pass, the buffered orphan
    # must land before this row's own content to preserve seq order.
    _flush_pending_orphan_rows(root_id, sid, fold_start_watermark)
    if event_type == "event_ownership_resolved":
        session_manager.apply_journal_ownership_resolution(
            root_id,
            sid,
            msg_id,
            seq,
        )
        _flush_pending_orphan_rows(root_id, sid, fold_start_watermark)
        return
    if not msg_id:
        _handle_orphan_row(root_id, sid, row, fold_start_watermark)
        return
    data = row.get("data")
    session_manager.apply_written_journal_event(
        root_id,
        sid,
        msg_id,
        event_type,
        data if isinstance(data, dict) else {},
        seq,
        fold_start_watermark=fold_start_watermark,
    )
    _flush_pending_orphan_rows(root_id, sid, fold_start_watermark)


def _read_session_projection_rows(
    root_id: str,
    after_seq: int,
    limit: int,
) -> list[dict]:
    from event_journal import event_journal_reader
    rows, _, _ = event_journal_reader.read_events(
        root_id,
        after_seq=after_seq,
        limit=limit,
    )
    return rows


def _log_session_projection_error(root_id: str, exc: BaseException) -> None:
    """Surface a stuck/failed projection root. Nothing re-reads this signal
    to trigger a repair — the fold is the only writer and it is already
    driven directly off durable journal writes (event-driven, not polled),
    so there is no "next read" to arm a retry for. Visibility only: the
    root self-heals on its next cold reload (full re-fold from
    events.jsonl), or an operator can act on the logged root_id."""
    logger.error(
        "session projection error root=%s: %r", root_id[:8], exc,
    )
    perf.record_count("session_projection.dirty_roots")


def _new_session_projection_dispatcher() -> SessionProjectionDrainer:
    return SessionProjectionDrainer(
        _apply_session_projection_row,
        _read_session_projection_rows,
        _log_session_projection_error,
        shards=_SESSION_PROJECTION_SHARDS,
        max_active_roots=_SESSION_PROJECTION_MAX_PENDING,
        chunk_size=_SESSION_PROJECTION_DRAIN_CHUNK,
        get_watermark=session_manager.get_fold_watermark,
    )


_SESSION_PROJECTION_DISPATCHER = _new_session_projection_dispatcher()


async def _refresh_session_content_projection(event: BusEvent) -> None:
    """Enqueue an ordered projection after its journal row is durable.

    `event.msg_id` is empty for orphan rows (msg_id=None events from
    externally-driven sessions) — those still need to reach the drainer
    so `_apply_session_projection_row`'s orphan-fold arm runs on them in
    seq order, so this no longer skips them.
    """
    payload = event.payload
    if payload.get("appended") is False or int(payload.get("seq") or 0) <= 0:
        return
    command = SessionProjectionCommand(
        root_id=str(event.root_id),
        sid=str(event.sid),
        msg_id=str(event.msg_id or ""),
        event_type=str(payload.get("event_type") or "unknown"),
        source=str(payload.get("source") or ""),
        seq=int(payload.get("seq") or 0),
    )
    _SESSION_PROJECTION_DISPATCHER.submit(command)


def shutdown_session_content_projection() -> None:
    bus.unsubscribe("session_content_projection")
    _SESSION_PROJECTION_DISPATCHER.shutdown(wait=True)


async def await_session_content_projection(root_id: str) -> None:
    """Block until every journal row durable for `root_id` has been
    folded into the render tree.

    Draining the writer first is what makes this deterministic rather
    than a race. The drainer is normally fed by `EVENT_JOURNAL_WRITTEN`,
    which a synchronous journal write publishes onto the bound loop
    fire-and-forget — so it reliably lands only AFTER the writing thread
    has moved on and called this. Barriering the drainer alone would
    then find no state for the root and return having waited for
    nothing. The writer barrier yields the durable high-water seq, and
    `require` targets the drain at it directly, so the fold covers every
    row on disk whether or not its command has arrived yet.
    """
    durable_seq = await event_journal_writer.barrier(root_id)
    _SESSION_PROJECTION_DISPATCHER.require(root_id, durable_seq)
    await asyncio.to_thread(_SESSION_PROJECTION_DISPATCHER.barrier, root_id)


def await_session_content_projection_sync(root_id: str) -> None:
    """Blocking counterpart of `await_session_content_projection` for
    callers with no running event loop (e.g. `native_import.py`'s
    background import thread)."""
    durable_seq = event_journal_writer.barrier_sync(root_id)
    _SESSION_PROJECTION_DISPATCHER.require(root_id, durable_seq)
    _SESSION_PROJECTION_DISPATCHER.barrier(root_id)


async def _refresh_session_search_projection(event: BusEvent) -> None:
    payload = event.payload
    if payload.get("appended") is False or int(payload.get("seq") or 0) <= 0:
        return
    data = payload.get("data")
    if not isinstance(data, dict):
        return
    entry = {
        "seq": payload.get("seq"),
        "sid": event.sid,
        "type": payload.get("event_type"),
        "data": data,
    }
    if event.msg_id:
        entry["msg_id"] = event.msg_id
    _enqueue_session_search_projection(event.root_id, entry)


def _enqueue_session_search_projection(root_id: str, entry: dict) -> None:
    import session_search_projection
    session_search_projection.note_event_written(root_id, entry)


async def _refresh_requirement_tags(event: BusEvent) -> None:
    await asyncio.to_thread(_refresh_requirement_tags_sync)


def _refresh_requirement_tags_sync() -> None:
    import extension_package_loader
    import extension_store
    try:
        try:
            extension_package_loader.ensure_package_importable(
                extension_store.extension_id_for_role('requirements'),
                "requirement_analysis",
            )
        except extension_package_loader.ExtensionPackageUnavailable:
            pass  # Extension not registered; try direct import (tests, standalone).
        from requirement_analysis.session_tags import tags_by_session
    except ModuleNotFoundError:
        return
    tags_by_session(blocking=False)


async def _apply_requirement_tags_projection(event: BusEvent) -> None:
    payload = event.payload if isinstance(event.payload, dict) else {}
    tags_by_session = payload.get("tags_by_session")
    if not isinstance(tags_by_session, dict):
        return
    import session_store
    await asyncio.to_thread(
        session_store.set_requirement_tags_projection,
        tags_by_session,
    )


def bind_event_journal_writer(
    writer: EventJournalWriter = event_journal_writer,
) -> None:
    writer.register(bus, priority=_JOURNAL_SUBSCRIBER_PRIORITY)
    logger.info("event_bus: registered event journal writer")


def bind_session_content_projection() -> None:
    global _SESSION_PROJECTION_DISPATCHER
    if not _SESSION_PROJECTION_DISPATCHER.is_accepting():
        _SESSION_PROJECTION_DISPATCHER = _new_session_projection_dispatcher()
    bus.unsubscribe("session_content_projection")
    bus.unsubscribe("session_search_projection")
    bus.subscribe(
        EVENT_JOURNAL_WRITTEN,
        _refresh_session_content_projection,
        priority=_SESSION_PROJECTION_PRIORITY,
        name="session_content_projection",
    )
    logger.info("event_bus: registered session content projection")


async def _refresh_session_lifecycle_projection(event: BusEvent) -> None:
    session_manager.recompute_state(event.sid)


def bind_session_lifecycle_projection() -> None:
    bus.unsubscribe("session_lifecycle_projection")
    bus.subscribe(
        "user_message_*",
        _refresh_session_lifecycle_projection,
        priority=_SESSION_LIFECYCLE_PROJECTION_PRIORITY,
        name="session_lifecycle_projection",
    )
    bus.subscribe(
        "lifecycle.*",
        _refresh_session_lifecycle_projection,
        priority=_SESSION_LIFECYCLE_PROJECTION_PRIORITY,
        name="session_lifecycle_projection",
    )
    logger.info("event_bus: registered session lifecycle projection")


def bind_requirement_tags_projection() -> None:
    bus.unsubscribe("requirement_tags_refresh")
    bus.unsubscribe("requirement_tags_projection")
    bus.subscribe(
        "requirement_tags.refresh_requested",
        _refresh_requirement_tags,
        priority=200,
        name="requirement_tags_refresh",
    )
    bus.subscribe(
        "requirement_tags.refreshed",
        _apply_requirement_tags_projection,
        priority=40,
        name="requirement_tags_projection",
    )
    logger.info("event_bus: registered requirement tags projection")


def register_default_subscribers() -> None:
    """Idempotent. Wires the standard subscribers; safe to call multiple
    times during startup or in tests — duplicate `name` registrations
    are pruned first."""
    bind_event_journal_writer()
    bind_session_content_projection()
    bind_session_lifecycle_projection()
    bind_requirement_tags_projection()
    bind_ask_delivery()
    bind_extension_job_delivery()
    bind_push_notifications()
    bus.unsubscribe("ingester_to_events_jsonl")
    bus.unsubscribe("event_journal_persistence_adapter")
    bus.subscribe(
        "*",
        _persist_to_event_journal,
        priority=_JOURNAL_SUBSCRIBER_PRIORITY,
        name="event_journal_persistence_adapter",
    )
    # (Event schema registrations live at module-load time above
    # so they're in place before any producer runs.)
    logger.info("event_bus: registered event journal persistence adapter")
    try:
        from hook_runner import bind_configured_hooks
        bind_configured_hooks()
    except Exception:
        logger.exception("event_bus: hook runner registration failed")
    bind_task_turn_end_triggers()


def bind_push_notifications() -> None:
    async def _on_turn_complete(event: BusEvent) -> None:
        if (event.payload or {}).get("reason") != "success":
            return
        import push_sender

        sid = event.sid

        # Capture the completed response before the next queued turn can
        # create another assistant message, then keep external delivery off
        # the terminal lifecycle lane.
        message_id = await asyncio.to_thread(
            session_manager.latest_assistant_msg_id,
            sid,
        )
        asyncio.get_running_loop().run_in_executor(
            None,
            functools.partial(
                push_sender.send_turn_completed_push,
                sid,
                message_id=message_id,
            ),
        )

    bus.unsubscribe("push_notification_turn_complete")
    bus.subscribe(
        "lifecycle.turn_complete",
        _on_turn_complete,
        priority=300,
        name="push_notification_turn_complete",
    )


def bind_ask_delivery() -> None:
    import ask_delivery

    bus.unsubscribe("ask_delivery_journal_receipt")
    bus.unsubscribe("ask_delivery_turn_complete")
    bus.unsubscribe("ask_delivery_turn_stopped")
    bus.unsubscribe("ask_delivery_target_turn_done")
    bus.unsubscribe("ask_delivery_target_turn_failed")
    bus.subscribe(
        EVENT_JOURNAL_WRITTEN,
        ask_delivery.on_journal_written,
        priority=30,
        name="ask_delivery_journal_receipt",
    )
    bus.subscribe(
        "lifecycle.turn_complete",
        ask_delivery.on_caller_terminal,
        priority=30,
        name="ask_delivery_turn_complete",
    )
    bus.subscribe(
        "lifecycle.turn_stopped",
        ask_delivery.on_caller_terminal,
        priority=30,
        name="ask_delivery_turn_stopped",
    )
    bus.subscribe(
        "user_message_done",
        ask_delivery.on_target_turn_terminal,
        priority=30,
        name="ask_delivery_target_turn_done",
    )
    bus.subscribe(
        "user_message_failed",
        ask_delivery.on_target_turn_terminal,
        priority=30,
        name="ask_delivery_target_turn_failed",
    )


def bind_extension_job_delivery() -> None:
    import extension_job_delivery

    bus.unsubscribe("extension_job_delivery_turn_complete")
    bus.unsubscribe("extension_job_delivery_turn_stopped")
    bus.subscribe(
        "lifecycle.turn_complete",
        extension_job_delivery.on_caller_terminal,
        priority=30,
        name="extension_job_delivery_turn_complete",
    )
    bus.subscribe(
        "lifecycle.turn_stopped",
        extension_job_delivery.on_caller_terminal,
        priority=30,
        name="extension_job_delivery_turn_stopped",
    )


def bind_session_ws_broadcaster(broadcaster) -> None:
    """Subscribe `session_ws_broadcaster.on_change` to `session.*` BusEvents.

    The broadcaster's `on_change(sid, change)` signature is unchanged:
    the bus subscriber unwraps `(event.sid, event.payload)` so the
    broadcaster itself doesn't need to know about the bus.

    Idempotent. Priority 50 (default, well after persistence at 10
    so events.jsonl writes always land first)."""
    async def _handler(event: BusEvent) -> None:
        # `event.payload` is the enriched change dict that
        # `session_manager._fire` published. Same shape the legacy
        # `add_listener` callers received.
        change = _session_change_from_event(event)
        if change is None:
            return
        try:
            broadcaster.on_change(event.sid, change)
        except Exception:
            logger.exception(
                "bind_session_ws_broadcaster: on_change raised "
                "for %s", event.type,
            )

    bus.unsubscribe("session_ws_broadcaster_on_change")
    bus.subscribe(
        "session.*",
        _handler,
        priority=50,
        name="session_ws_broadcaster_on_change",
    )
    logger.info(
        "event_bus: registered session_ws_broadcaster.on_change "
        "as bus subscriber on session.*",
    )


def unbind_session_ws_broadcaster() -> None:
    """Detach the WS projection before its scheduling owner shuts down."""
    bus.unsubscribe("session_ws_broadcaster_on_change")


def _session_change_from_event(event: BusEvent) -> dict | None:
    """Map a `session.*` BusEvent to the legacy `on_change(sid, change)`
    shape the WS broadcaster expects.

    `session_manager._fire` dual-publishes every mutation: `session.<kind>`
    (this mapper's input) and, separately, `session.fire.<kind>` — an
    additive owner-side fact namespace consumed by adapters (see
    `adapters/session_adapter.py`'s own `session.fire.*` subscription), not
    by the legacy broadcaster. Both match the `session.*` wildcard this
    mapper's caller subscribes with, so `session.fire.*` facts must be
    skipped here cleanly rather than mis-parsed as a `session.<kind>` event
    (which would strip only the `session.` prefix, leaving a bogus
    `fire.<kind>` kind that never matches the payload's real kind)."""
    if event.type.startswith("session.fire."):
        return None
    event_kind = event.type.removeprefix("session.")
    change = dict(event.payload)
    payload_kind = change.get("kind")
    if payload_kind is not None and payload_kind != event_kind:
        logger.error(
            "session event kind mismatch type=%s payload_kind=%r sid=%s",
            event.type,
            payload_kind,
            event.sid,
        )
    change["kind"] = event_kind
    return change


def bind_worker_fanout_cleanup(broadcast_workers_changed, cancel_session) -> None:
    """Project worker/fork invalidation facts into worker_store state."""
    def _project(
        session_id: str,
        *,
        caller_scope: bool,
        remove_worker: bool,
    ) -> list[str]:
        from stores import worker_store as _ws

        cleared = _ws.cleanup_session_fanout(
            session_id,
            caller_scope=caller_scope,
            remove_worker=remove_worker,
        )
        return list(cleared)

    async def _handler(event: BusEvent) -> None:
        payload = event.payload
        session_id = str(payload.get("session_id") or event.sid or "")
        if not session_id:
            logger.warning("worker_fanout_cleanup: missing session_id")
            return
        op_label = str(payload.get("op_label") or event.type)
        caller_scope = bool(payload.get("caller_scope"))
        remove_worker = bool(payload.get("remove_worker"))
        try:
            cleared = await asyncio.to_thread(
                _project,
                session_id,
                caller_scope=caller_scope,
                remove_worker=remove_worker,
            )
            if cleared:
                for fork_session_id in cleared:
                    # cancel_session BEFORE delete: this is a second,
                    # independent delegate-fork deletion path from
                    # _delete_session_tree's subtree-wide cancel (see
                    # commit 29d01d58e) — session.worker_fanout_required
                    # fires here for many reasons beyond session delete
                    # (worker close, other invalidation events), so a
                    # cleared fork can still have an active turn. Without
                    # this the fork's runner is orphaned exactly like the
                    # original incident: still streaming provider events
                    # against a root that's about to vanish.
                    try:
                        await cancel_session(fork_session_id)
                    except Exception:
                        logger.exception(
                            "cancel delegate-fork BC %s failed during %s",
                            fork_session_id, op_label,
                        )
                    try:
                        await asyncio.to_thread(
                            session_manager.delete, fork_session_id,
                        )
                    except Exception:
                        logger.exception(
                            "delete delegate-fork BC %s failed during %s",
                            fork_session_id, op_label,
                        )
            await broadcast_workers_changed(None)
        except Exception:
            logger.exception(
                str(payload.get("outer_log_msg") or "worker fan-out cleanup failed"),
            )

    bus.unsubscribe("worker_fanout_cleanup")
    bus.subscribe(
        "session.worker_fanout_required",
        _handler,
        priority=200,
        name="worker_fanout_cleanup",
    )
    logger.info("event_bus: registered worker fan-out cleanup subscriber")


def _last_assistant_text(sess: dict) -> str:
    """Concatenate finalized text-block text from the LAST assistant message.
    READ-ONLY — never mutates msg.events (convergence invariant)."""
    msgs = sess.get("messages") or []
    last = None
    for msg in msgs:
        if isinstance(msg, dict) and msg.get("role") == "assistant":
            last = msg
    if last is None:
        return ""
    parts: list[str] = []
    for ev in last.get("events") or []:
        if not isinstance(ev, dict):
            continue
        data = ev.get("data")
        if not isinstance(data, dict):
            continue
        message = data.get("message")
        blocks = message.get("content") if isinstance(message, dict) else None
        if not isinstance(blocks, list):
            continue
        for block in blocks:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
    return "\n".join(parts)


def _log_hook_task_exception(task: asyncio.Task, label: str) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error(
            "%s hook task raised: %r",
            label,
            exc,
            exc_info=(type(exc), exc, exc.__traceback__),
        )


def bind_post_turn_hooks() -> None:
    """Dispatch ``lifecycle.turn_complete`` to every installed extension that
    declares an ``entrypoints.hooks.post_turn`` backend path. Fire-and-forget,
    isolated errors — one failing hook never blocks another or the turn.

    Additive: subscribes to the existing turn-complete bus event the
    orchestrator already publishes, so it touches no turn-finalization path
    (no convergence-invariant risk). Each hook is a sandboxed backend-host
    invocation via ``invoke_extension_backend``."""
    import extension_backend_loader

    async def _on_turn_complete(event: BusEvent) -> None:
        try:
            import extension_store
            hooks = extension_store.post_turn_hooks()
            if not hooks:
                return
            from env_compat import get_env
            import json as _json
            base_url = get_env("BETTER_CLAUDE_BACKEND_URL", "http://localhost:8000")
            body = _json.dumps({
                "session_id": event.sid,
                "app_session_id": event.sid,
                "turn_type": event.type,
                "payload": event.payload or {},
            }).encode("utf-8")
            for ext_id, path in hooks:
                async def _invoke(eid: str = ext_id, p: str = path) -> None:
                    try:
                        await extension_backend_loader.invoke_extension_backend(
                            eid, p.lstrip("/"), method="POST", body_bytes=body, base_url=base_url,
                        )
                    except Exception:
                        logger.exception("post-turn hook %s failed", eid)
                t = asyncio.create_task(_invoke(), name=f"post-turn-{ext_id}-{event.sid[:8]}")
                t.add_done_callback(
                    lambda task: _log_hook_task_exception(task, "post-turn")
                )
        except Exception:
            logger.exception("post-turn hook dispatch failed for %s", event.sid)

    bus.unsubscribe("extension_post_turn_hooks")
    bus.subscribe(
        "lifecycle.turn_complete",
        _on_turn_complete,
        priority=300,
        name="extension_post_turn_hooks",
    )
    logger.info("event_bus: registered extension post-turn hooks subscriber")


async def persist_task_turn_end(event: BusEvent) -> int:
    from stores import task_trigger_store

    fields = session_manager.get_fields(
        event.sid,
        ("cwd", "node_id", "storage_scope"),
    )
    if not fields:
        return 0
    storage_scope = fields.get("storage_scope")
    if isinstance(storage_scope, dict) and storage_scope.get("kind") == "routine":
        return 0
    payload = event.payload if isinstance(event.payload, dict) else {}
    raw_provider_kind = payload.get("provider_kind")
    provider_kind = (
        raw_provider_kind.lower()
        if isinstance(raw_provider_kind, str) and raw_provider_kind
        else None
    )
    event_key = str(
        payload.get("trace_id")
        or event.run_id
        or f"{event.sid}:{event.seq}:{event.ts}"
    )
    return await asyncio.to_thread(
        task_trigger_store.enqueue_turn_end,
        event_type=event.type,
        event_key=event_key,
        root_id=event.root_id,
        session_id=event.sid,
        reason=payload.get("reason"),
        timestamp=event.ts,
        provider_kind=provider_kind,
        cwd=str(fields.get("cwd") or ""),
        node_id=str(fields.get("node_id") or "primary"),
    )


def bind_task_turn_end_triggers() -> None:
    async def _on_turn_end(event: BusEvent) -> None:
        await persist_task_turn_end(event)

    bus.unsubscribe("task_turn_end_triggers")
    bus.subscribe(
        "lifecycle.turn_*",
        _on_turn_end,
        priority=310,
        name="task_turn_end_triggers",
    )
    logger.info("event_bus: registered task turn-end triggers subscriber")


def bind_pre_turn_hooks() -> None:
    """Dispatch ``lifecycle.turn_start`` to every installed extension that
    declares an ``entrypoints.hooks.pre_turn`` backend path. Fire-and-forget,
    isolated errors — one failing hook never blocks another or the turn.

    Mirror of ``bind_post_turn_hooks``: subscribes to the existing
    turn-start bus event the orchestrator already publishes, so it touches no
    turn-execution path (no convergence-invariant risk). Each hook is a
    sandboxed backend-host invocation via ``invoke_extension_backend``. The
    body carries the turn context (session id + payload); hooks fetch whatever
    else they need (prompt, cwd) via core internal endpoints, exactly as
    post-turn hooks do."""
    import extension_backend_loader

    async def _on_turn_start(event: BusEvent) -> None:
        try:
            import extension_store
            hooks = extension_store.pre_turn_hooks()
            if not hooks:
                return
            from env_compat import get_env
            import json as _json
            base_url = get_env("BETTER_CLAUDE_BACKEND_URL", "http://localhost:8000")
            body = _json.dumps({
                "session_id": event.sid,
                "app_session_id": event.sid,
                "turn_type": event.type,
                "payload": event.payload or {},
            }).encode("utf-8")
            for ext_id, path in hooks:
                async def _invoke(eid: str = ext_id, p: str = path) -> None:
                    try:
                        await extension_backend_loader.invoke_extension_backend(
                            eid, p.lstrip("/"), method="POST", body_bytes=body, base_url=base_url,
                        )
                    except Exception:
                        logger.exception("pre-turn hook %s failed", eid)
                t = asyncio.create_task(_invoke(), name=f"pre-turn-{ext_id}-{event.sid[:8]}")
                t.add_done_callback(
                    lambda task: _log_hook_task_exception(task, "pre-turn")
                )
        except Exception:
            logger.exception("pre-turn hook dispatch failed for %s", event.sid)

    bus.unsubscribe("extension_pre_turn_hooks")
    bus.subscribe(
        "lifecycle.turn_start",
        _on_turn_start,
        priority=300,
        name="extension_pre_turn_hooks",
    )
    logger.info("event_bus: registered extension pre-turn hooks subscriber")
