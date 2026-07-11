"""SessionManager — single owner of per-session state.

All session writes go through this object. It serializes mutations on a
per-root-tree RLock, holds a write-through in-memory cache of root
trees, and fans out typed change events to listeners.

**Tree-aware cache.** Schema v2 embeds forks inside their root file.
The cache stores roots in `_roots[root_id]`; any session id (root or
fork) resolves to its root via `_node_root_id[sid] = root_id`. Reads
return references into the live root tree, so mutating a fork dict in
memory updates the same object that gets persisted when we write the
root. Writes serialize on the per-root lock — siblings of the same
root cannot mutate concurrently, but different roots can.

Why this exists: the previous design had every writer call
`session_store.update_session(sid, whole_session)`, which read disk,
overlaid the passed dict, and wrote disk. Two concurrent writers each
carried their own stale snapshot, and the second writer's overlay
clobbered the first's mutations. Single owner + per-root lock + typed
mutations close that hole.
"""

from __future__ import annotations

import asyncio
import collections
import contextvars
import copy
import heapq
import json
import logging
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

import perf
import config_store
import messages_delta_compaction
import session_store
from event_bus import BusEvent, bus
from reasoning_effort import normalize_reasoning_effort

logger = logging.getLogger(__name__)
_NEGATIVE_NODE_ROOT_TTL_SECONDS = 5.0
def _new_reconcile_executor() -> ThreadPoolExecutor:
    return ThreadPoolExecutor(
        max_workers=2,
        thread_name_prefix="session-reconcile",
    )


_RECONCILE_EXECUTOR = _new_reconcile_executor()
_QUEUE_PROJECTION_EXECUTOR = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix="queue-projection-submit",
)
_queue_projection_cv = threading.Condition()
_queue_projection_pending: dict[str, dict] = {}
_queue_projection_worker_scheduled = False
_queue_projection_failure: Optional[BaseException] = None
_queue_projection_accepting = True

# Mirrors the frontend's BA_LINK_MARKER_RE (linkifyFilePaths.tsx). A session
# name is never allowed to carry raw copy-id marker syntax: every marker
# builder (frontend Copy id, team_messaging's FROM tag) re-embeds the stored
# name inside a new `[[ba-session:...]]`/`[[ba-event:...]]` marker, and a
# name that's already one of those renders as unreadable nested/percent-
# encoded garbage. Stripped once here, at the single write funnel used by
# both the user rename endpoint and the AI auto-title path.
_LINK_MARKER_RE = re.compile(r"\[\[(?:ba-session|ba-event):[^\]\n]*\]\]")
_SINCE_CACHE_MAX_BYTES = 32 * 1024 * 1024


def strip_link_marker_syntax(name: str) -> str:
    return _LINK_MARKER_RE.sub("", name or "").strip()


def _copy_jsonish(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _copy_jsonish(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_copy_jsonish(v) for v in value]
    return value


def _jsonish_byte_size(value: Any) -> int:
    if value is None or isinstance(value, bool):
        return 4
    if isinstance(value, (int, float)):
        return 8
    if isinstance(value, str):
        return len(value.encode("utf-8"))
    if isinstance(value, dict):
        return sum(
            _jsonish_byte_size(str(key)) + _jsonish_byte_size(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return sum(_jsonish_byte_size(item) for item in value)
    return len(str(value).encode("utf-8", errors="replace"))


async def shutdown_reconciles() -> None:
    started = time.perf_counter()
    manager._reconcile_accepting = False
    tasks = tuple(manager._in_flight_reconcile.values())
    for task in tasks:
        task.cancel()
    results = await asyncio.gather(*tasks, return_exceptions=True) if tasks else []
    await asyncio.to_thread(
        _RECONCILE_EXECUTOR.shutdown,
        wait=True,
        cancel_futures=True,
    )
    perf.record("shutdown.session_reconcile", (time.perf_counter() - started) * 1000)
    perf.record_count("shutdown.session_reconcile.cancelled", len(tasks))
    perf.record_count(
        "shutdown.session_reconcile.failed",
        sum(isinstance(result, Exception) for result in results),
    )


def reopen_reconciles() -> None:
    global _RECONCILE_EXECUTOR
    if manager._reconcile_accepting:
        return
    _RECONCILE_EXECUTOR = _new_reconcile_executor()
    manager._reconcile_accepting = True


def begin_queue_projection_shutdown() -> None:
    global _queue_projection_accepting
    with _queue_projection_cv:
        _queue_projection_accepting = False


def drain_queue_projection_submissions() -> None:
    with _queue_projection_cv:
        while _queue_projection_worker_scheduled or _queue_projection_pending:
            _queue_projection_cv.wait()
        if _queue_projection_failure is not None:
            raise RuntimeError("queue projection submission failed") from _queue_projection_failure


def shutdown_queue_projection_executor() -> None:
    _QUEUE_PROJECTION_EXECUTOR.shutdown(wait=True, cancel_futures=False)


def _submit_queue_projection_record(record: dict) -> None:
    global _queue_projection_failure, _queue_projection_worker_scheduled
    # project_session constructs this record from deep-copied user messages
    # and queued prompts. Ownership transfers here; callers do not reuse it.
    owned_record = record
    session_id = owned_record.get("id")
    if not isinstance(session_id, str) or not session_id:
        return
    with _queue_projection_cv:
        if not _queue_projection_accepting:
            error = RuntimeError("queue projection submission rejected during shutdown")
            _queue_projection_failure = _queue_projection_failure or error
            import session_queue_projection
            session_queue_projection.mark_dirty()
            logger.warning(str(error))
            _queue_projection_cv.notify_all()
            return
        _queue_projection_pending[session_id] = owned_record
        if _queue_projection_worker_scheduled:
            return
        _queue_projection_worker_scheduled = True
        try:
            _QUEUE_PROJECTION_EXECUTOR.submit(_drain_queue_projection_pending)
        except BaseException as exc:
            _queue_projection_worker_scheduled = False
            _queue_projection_failure = _queue_projection_failure or exc
            import session_queue_projection
            session_queue_projection.mark_dirty()
            _queue_projection_cv.notify_all()
            raise


def _drain_queue_projection_pending() -> None:
    global _queue_projection_failure, _queue_projection_worker_scheduled
    while True:
        with _queue_projection_cv:
            if not _queue_projection_pending:
                _queue_projection_worker_scheduled = False
                _queue_projection_cv.notify_all()
                return
            _, record = _queue_projection_pending.popitem()
        try:
            _upsert_queue_projection_record(record)
        except BaseException as exc:
            logger.exception("queue projection background submission failed")
            import session_queue_projection
            session_queue_projection.mark_dirty()
            with _queue_projection_cv:
                _queue_projection_failure = _queue_projection_failure or exc


def _upsert_queue_projection_record(record: dict) -> None:
    import session_queue_projection
    session_queue_projection.upsert_record_background(record)


# Draft-persist coalescer window. Bounds the worst-case data loss on
# backend crash for typed-but-unsent draft text. Reads of the in-memory
# state still see the latest mutation synchronously — only the disk
# write is deferred.
# DRAFT_FLUSH_DELAY moved to backend/draft_store.py.

# ── Per-root write_full debounce ──────────────────────────────────────
# Leading-edge debounce around `session_store.write_session_full` for
# the hot `_persist_root` path. First write of a burst fires
# immediately; subsequent writes within the window queue a scheduler
# deadline that tail-flushes the latest in-memory state once the window expires.
# Coalesces 10-50 writes/sec/session (token streaming) into ~20 writes/
# sec at most.
#
# INVARIANTs:
#   - `_persist_pending[rid]` holds the LIVE root dict ref (same object
#     producers mutate). Tail flush re-acquires `_lock_for_root(rid)`
#     before calling write_session_full, so `json.dump` runs serialized
#     against any concurrent producer's mutations — no torn JSON.
#   - `updated_at` is stamped at QUEUE-TIME (producer's per-root lock)
#     and the tail flush passes `bump_updated_at=False`. Otherwise the
#     50ms-late tail flush would write a wall-clock that misrepresents
#     when the mutation happened, distorting `list_sessions` sort order.
#   - Delete paths MUST call `_drop_pending_persist(rid)` while holding
#     `_lock_for_root(rid)` BEFORE `session_store.delete_session(...)`
#     — otherwise a queued tail flush would resurrect the just-deleted
#     session by re-creating its file via `os.replace`.
#   - `list_sessions` does NOT flush pending writes — accepting up to
#     PERSIST_DEBOUNCE_S of staleness on the summary index. The prior
#     flush-on-read pattern serialized through per-root locks during
#     write bursts and pushed /api/sessions to 11 s peak under load.
#     `flush_pending_persists` remains for on_shutdown + tests.
PERSIST_DEBOUNCE_S = 0.050
EXTERNAL_RELOAD_POLL_INTERVAL_S = 1.0

_persist_pending: dict[str, dict] = {}
_persist_deadlines: dict[str, float] = {}
_persist_deadline_heap: list[tuple[float, str]] = []
_persist_last_at: dict[str, float] = {}
_persist_inflight: set[str] = set()
_persist_state_lock = threading.Lock()
_persist_state_changed = threading.Condition(_persist_state_lock)
_persist_scheduler_started = False


def _arm_persist_deadline_unlocked(root_id: str, delay: float) -> None:
    global _persist_scheduler_started
    deadline = time.monotonic() + max(0.0, delay)
    _persist_deadlines[root_id] = deadline
    heapq.heappush(_persist_deadline_heap, (deadline, root_id))
    if not _persist_scheduler_started:
        _persist_scheduler_started = True
        t = threading.Thread(
            target=_persist_scheduler_loop,
            name="session-persist-scheduler",
            daemon=True,
        )
        t.start()
    _persist_state_changed.notify_all()


def _cancel_persist_deadline_unlocked(root_id: str) -> None:
    _persist_deadlines.pop(root_id, None)
    _persist_state_changed.notify_all()


def _persist_scheduler_loop() -> None:
    while True:
        due: list[str] = []
        with _persist_state_changed:
            while True:
                now = time.monotonic()
                while _persist_deadline_heap:
                    deadline, root_id = _persist_deadline_heap[0]
                    if _persist_deadlines.get(root_id) == deadline:
                        break
                    heapq.heappop(_persist_deadline_heap)
                if not _persist_deadline_heap:
                    _persist_state_changed.wait()
                    continue
                deadline, root_id = _persist_deadline_heap[0]
                wait_s = deadline - now
                if wait_s > 0:
                    _persist_state_changed.wait(timeout=wait_s)
                    continue
                heapq.heappop(_persist_deadline_heap)
                if _persist_deadlines.pop(root_id, None) == deadline:
                    due.append(root_id)
                break
        for root_id in due:
            manager._tail_persist(root_id)


Listener = Callable[[str, dict], None]


class IncompatibleOrchestrationMode(ValueError):
    """Raised when a session's `orchestration_mode` is incompatible with
    the chosen provider's capability flags. The single-chokepoint check
    lives in `session_manager.create` (catches HTTP `POST /api/sessions`,
    CLI, tests, and any future code path that mints a session). Mid-
    session mode changes (PATCH /api/sessions/{id}/selectors) MUST raise
    the same exception."""
    pass


class DelegateForkParentMissing(KeyError):
    """Raised by `create_delegate_fork` when the parent agent session is gone
    (race vs. delete/eviction, or a stale/unknown agent session id). Subclasses
    `KeyError` so the existing strict-mode contract is preserved for any caller
    that catches `KeyError`; the HTTP boundary (`/api/internal/ask-fork`) catches
    THIS type specifically and maps it to 409 so the race doesn't surface as a
    bare 500. Catching the specific type avoids masking unrelated `KeyError`s."""
    pass


@dataclass(frozen=True)
class SessionOwnerToken:
    sid: str
    root_id: str
    generation: int
    incarnation: str


def _validate_orchestration_mode_against_provider(
    *, orchestration_mode: str, provider_id: Optional[str],
) -> None:
    """Layer-2 capability gate. Layer 1 is the frontend (hides the option
    if no provider supports it); layer 3 is `Provider.start_run`'s last-
    line-of-defence `raise NotImplementedError`. This middle layer
    catches HTTP / CLI / direct-call mints before any disk write.

    Resolves the provider class statically by `kind` (cheap; no
    instantiation). When `provider_id` is None we resolve to the active
    provider's kind so the default-provider path is checked too. Unknown
    `provider_id` or any lookup failure is treated as "skip the check"
    — `start_run` will still raise if the mode is genuinely unsupported.
    """
    if orchestration_mode not in ("team", "manager"):
        return
    try:
        import config_store
        kind: Optional[str] = None
        if provider_id:
            rec = config_store.get_provider_with_key(provider_id)
            kind = (rec or {}).get("kind") if rec else None
        if not kind:
            active = config_store.get_default_provider()
            kind = (active or {}).get("kind")
        if not kind:
            return
        from provider import _resolve_class
        cls = _resolve_class(kind)
        if not getattr(cls, "supports_manager_mode", True):
            raise IncompatibleOrchestrationMode(
                f"Provider kind {kind!r} does not support team mode."
            )
    except IncompatibleOrchestrationMode:
        raise
    except Exception:
        # Lookup failure (defunct provider, unknown kind, …) — let
        # `start_run`'s last-line-of-defence handle it.
        logger.debug(
            "orchestration-mode capability check skipped",
            exc_info=True,
        )


class SessionManager:
    def __init__(self) -> None:
        # Root trees, keyed by root_id. Forks live inside their root.
        # OrderedDict for LRU: `move_to_end` marks recency on access,
        # `_enforce_root_cap` evicts the oldest UNPINNED roots beyond
        # `_roots_max`. Pinned roots (active turn / open WS subscriber /
        # live tailer / in-flight reconcile / batch / dirty draft /
        # pending persist) are always retained — the cap is enforced only
        # against evictable roots, so active sessions are never starved.
        self._roots: "collections.OrderedDict[str, dict]" = (
            collections.OrderedDict()
        )
        self._roots_max = 20
        self._root_file_fingerprints: dict[
            str, tuple[int, int, int, int, int]
        ] = {}
        self._root_file_checked_at: dict[str, float] = {}
        # Root ids whose cached tree has had events.jsonl replayed into
        # msg.events. Thin snapshot readers deliberately leave roots out
        # of this set so historical events stay on disk until expanded.
        self._event_hydrated_roots: set[str] = set()
        self._hydration_conditions: dict[str, threading.Condition] = {}
        self._hydration_in_flight: set[str] = set()
        # Any sid (root or fork) → its root_id. Maintained alongside _roots.
        self._node_root_id: dict[str, str] = {}
        self._node_root_missing_until: dict[str, float] = {}
        self._owner_generations: dict[str, int] = {}
        self._owner_revocation_callbacks: dict[str, set[Callable[[], None]]] = {}
        self._owner_operation_locks: dict[str, threading.RLock] = {}
        # (sid, change-kind) → last ERROR-log time for a dropped mutation
        # (see `_mutation_miss`); dedup only, never consulted for logic.
        self._mutation_miss_logged_at: dict[tuple, float] = {}
        # Per-node `kind`, populated in `_index_root` and DELIBERATELY
        # NOT cleared on LRU eviction (one short string per sid — tiny,
        # bounded by lifetime session count). Lets `recompute_state`'s
        # kind gate run with zero disk I/O for evicted roots. `kind` is
        # set once at node creation and never flipped, so a surviving
        # entry can't go stale.
        self._kind_by_sid: dict[str, Optional[str]] = {}
        # One lock per root tree — siblings serialize on the same lock.
        self._root_locks: dict[str, threading.RLock] = {}
        self._cache_guard = threading.Lock()
        self._listeners: list[Listener] = []
        # Active batch contexts, keyed by root_id (a batch covers all
        # mutations to nodes within one tree).
        self._batches: dict[str, dict] = {}
        # rid → thread ident currently inside `_load_root`. Backs the
        # same-thread re-entrancy guard so hydration's `get_ref` can't
        # recurse the load cycle (see `_load_root`).
        self._loading_roots: dict[str, int] = {}
        # ── Async reconcile coordination ────────────────────────────
        # Per-root dirty flag. Set by `event_ingester.ingest` when an
        # orphan event (msg_id=None for a finalized assistant msg) lands
        # on disk; consumed by readers via `consume_reconcile_dirty`.
        self._reconcile_dirty: dict[str, bool] = {}
        # Per-root single-flight task tracker. Mutated ONLY on the event
        # loop thread (add via schedule_*, remove via done_callback).
        self._in_flight_reconcile: dict[str, asyncio.Task] = {}
        self._reconcile_accepting = True
        # Bound at startup so cross-thread callers can schedule onto the
        # right loop.
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # Injected at startup: reconcile + emit can't live here without
        # circular imports (reconcile pulls from `orchs` which pulls
        # from session_manager; processing-event emit pulls from
        # coordinator).
        self._reconcile_fn: Optional[Callable[..., list]] = None
        self._emit_processing_fn: Optional[Callable[[str, str], None]] = None
        self._emit_stub_invalidated_fn: Optional[Callable[[list], None]] = None
        self._emit_reconciled_fn: Optional[Callable[[str], None]] = None
        # A10: injected by main.py so `set_selectors` can re-check
        # `coordinator.has_active_runs(sid)` UNDER the per-root lock
        # when a provider_id change is being persisted. Closes the
        # TOCTOU between the PATCH handler's pre-check and the actual
        # disk write — without the inside-lock check, a turn that
        # starts in the gap would see the FRESHLY-WRITTEN provider_id
        # at its own `provider_for_session` call. None when not bound
        # (CLI / tests bypass the gate; that's fine — they don't race).
        self._active_run_gate: Optional[Callable[[str], bool]] = None
        # A11: injected by main.py — `is_root_in_use(root_id, node_sids)`
        # returns True iff the orchestrator still holds a live reference
        # to the root (active turn for any of its sids, open WS
        # subscriber, or a live wire/owned tailer). Consulted by
        # `_is_pinned` before LRU eviction. None until bound → `_is_pinned`
        # fails CLOSED (every root treated as in-use → nothing evicted).
        self._pin_predicate: Optional[Callable[[str, set], bool]] = None
        # LRU cache for get_messages_since results. Keyed by sid.
        # Value: ((next_seq, event_max_seq), result). Invalidated when
        # the node's next_seq changes (new messages appended) OR when
        # the event journal's max seq for this node changes (late events
        # appended to existing messages without creating new ones).
        self._since_cache: collections.OrderedDict[str, tuple[tuple[int, int, int], dict]] = (
            collections.OrderedDict()
        )
        self._since_cache_bytes: dict[str, int] = {}
        self._since_cache_total_bytes = 0
        self._since_cache_max = 128
        self._window_cache: collections.OrderedDict[
            tuple[str, int, int, int, int, int, tuple[str, ...]],
            dict,
        ] = collections.OrderedDict()
        self._window_cache_max = 256
        self._tree_stub_cache: collections.OrderedDict[
            tuple[str, int, Optional[int], tuple, int],
            dict,
        ] = collections.OrderedDict()
        self._tree_stub_attached_cache: collections.OrderedDict[
            tuple[tuple[str, int, Optional[int], tuple, int], int],
            dict,
        ] = collections.OrderedDict()
        self._tree_stub_cache_max = 256
        self._todo_projection_cache: collections.OrderedDict[
            str,
            tuple[tuple[int, int] | None, dict[str, tuple[list, list]]],
        ] = collections.OrderedDict()
        self._todo_projection_cache_max = 64
        self._queued_prompt_counts_by_sid: dict[str, int] = {}
        # Per-root generation counter bumped after each reconcile.
        self._reconcile_gen: dict[str, int] = {}
        # Per-root seq cursor: highest seq that reconcile has processed.
        # Reconcile only reads events after this cursor — no full scan.
        self._reconcile_cursor: dict[str, int] = {}
        # Transient per-process marker for assistant messages currently
        # being reconciled by run_recovery. Lives in memory only — a
        # crash mid-recovery has no on-disk residue, and the next boot
        # re-marks the same messages when it picks the runs up again.
        # Read snapshots (`_stamp_recovering`) inject `isRecovering: true`
        # onto matching messages before serving REST responses.
        self._recovering_msg_ids: set[str] = set()
        # Draft persist coalescer lives in `backend/draft_store.py`.
        # sm hot paths (`_is_pinned`, `_persist_root`,
        # `_drop_root_memory`, the delete branch) call into DraftStore
        # via `get_active_coordinator().draft_store.X(rid)` — no hook
        # attrs stored on sm. DraftStore owns both behavior and
        # access path. Helper `_draft_store_or_none()` resolves the
        # active store with a try/except guard so a hook exception
        # never tears down sm's hot paths.
        # ── Per-session running flag + unread counter ─────────────────
        # Both transient (in-memory, lost on restart by design — running
        # is rebuilt by run_recovery via the same run_state_add hook that
        # feeds it live; unread is rebuilt lazily from msg.events on
        # first read for any session that has a persisted
        # `last_seen_event_uid`).
        #
        # INVARIANT: the SAME `_fire` spine that powers
        # `message_recovering_changed` carries `running_changed` /
        # `unread_changed` / `seen_advanced`. SessionWSBroadcaster maps
        # each to a global-allowlisted WS frame so home/sidebar/badge
        # consumers converge without polling. `kind != "user"` sessions
        # (delegate_fork, supervisor_worker, adv_sync_fork) are excluded
        # at the mutator boundary — workers don't contribute to a
        # user-facing session's unread, and they don't appear in the
        # sidebar so their "running" state never needs to surface.
        # Last value broadcast over WS for `running_changed`, per sid.
        # Liveness itself is computed live by the bound `_compute_is_running`
        # callback (coordinator-owned: walks `_run_state` + checks pid).
        # This dict only exists to dedupe the WS frame so we don't spam
        # `running_changed` on every recompute when nothing changed.
        self._last_broadcast_running: dict[str, bool] = {}
        # Injected at startup via `bind_running_check`.  Returns True
        # iff the sid currently has at least one alive run.
        self._compute_is_running: Optional[Callable[[str], bool]] = None
        # Last-broadcast monitoring state per sid (active/idle/blocked_on_user/
        # waiting_on_background/stopped). Mirrors `_last_broadcast_running`;
        # `recompute_monitoring` fires only on change. Computed live by the
        # bound `_compute_monitoring` (injected via `bind_monitoring_check`).
        self._last_broadcast_monitoring: dict[str, str] = {}
        self._compute_monitoring: Optional[Callable[[str], str]] = None
        self._project_key_cache: dict[str, tuple[str, str]] = {}
        self._unread_counts: dict[str, set[str]] = {}
        self._unread_counts_version = 0
        # Sessions whose `_unread_counts` has been hydrated from disk
        # via `_ensure_unread_loaded`. Lets `bump_unread` skip the
        # expensive walk for an unhydrated session — the first
        # `get_unread_count(sid)` will hydrate before any bump matters.
        self._unread_hydrated: set[str] = set()
        self._home_sessions_dir: Path | None = None
        session_store.register_root_writer_guard(self.write_root_locked)

    def write_root_locked(
        self, root_id: str, write_fn: Callable[[], None],
    ) -> None:
        """Guard for `session_store`'s unlocked bulk-walk writers (e.g.
        `_migrate_and_persist` via `iter_all_sessions`). Serializes
        `write_fn` under this root's `_lock_for_root` and skips it
        entirely when `root_id` is currently resident in `self._roots`.

        A resident root is the live authority — a bulk walker's
        `write_fn` closes over a plain disk snapshot taken WITHOUT the
        lock, so writing it while the root is resident would silently
        overwrite any live in-memory mutation (e.g. a turn's
        just-appended assistant message) that hasn't made it to disk
        yet. Skipping is safe: the resident copy already carries
        whatever the walker wanted to persist (or will, via the normal
        `_persist_root` path), and the walker retries on its next pass
        for any root that later evicts."""
        with self._lock_for_root(root_id):
            if root_id in self._roots:
                return
            write_fn()

    # ── Listeners ──────────────────────────────────────────────────

    def add_listener(self, fn: Listener) -> None:
        """Register a sync `(sid, change)` listener.

        DEPRECATED (A1b). The canonical notification spine is now the
        event bus — `_fire` publishes a `session.<kind>` BusEvent and
        production subscribers (`session_ws_broadcaster` via
        `event_bus_subscribers.bind_session_ws_broadcaster`) attach
        there. This alias is preserved for tests + a one-release
        backward-compat window. New code MUST subscribe via
        `bus.subscribe("session.*", handler, ...)`.

        Listeners registered here are still fanned out by `_fire`
        SYNCHRONOUSLY inside the per-root lock — same semantics as
        before A1b — so tests that rely on the legacy timing keep
        working. The DeprecationWarning surfaces in `-W default`
        runs so the next sweep can remove the surviving callers."""
        import warnings
        warnings.warn(
            "session_manager.add_listener is deprecated; "
            "subscribe to `session.*` on the event bus instead.",
            DeprecationWarning, stacklevel=2,
        )
        self._listeners.append(fn)

    @perf.timed_fn("session.fire")
    def _fire(self, sid: str, change: dict) -> None:
        """Fan a session mutation event out to subscribers.

        A1b: dual path during the deprecation window. Bus subscribers
        (the production path) receive `session.<kind>` BusEvents
        scheduled onto the bound loop via `create_task`; legacy
        `add_listener`-registered handlers (test code only, today)
        fire synchronously inside the per-root lock as before.
        Both paths observe the SAME enriched change dict (the INVARIANT
        on line 667 holds — every mutator enriches under the lock
        before calling `_fire`)."""
        # Bus path — production. Skipped if no loop bound (e.g. some
        # unit-test contexts that drive session_manager without a
        # running event loop). The bus publish is fire-and-forget;
        # subscribers run on the event loop's next tick AFTER the
        # per-root lock releases — same as today's listener-then-
        # `create_task` pattern in `session_ws_broadcaster.on_change`.
        if self._loop is not None and not self._loop.is_closed():
            kind = change.get("kind") or "unknown"
            event_type = f"session.{kind}"
            # Resolve root_id; falling back to `sid` (which IS a root
            # for top-level changes; for fork mutations the lock is
            # held on the root, and sid IS the fork id — root_id is
            # discoverable via `_node_root_id` but enrichment already
            # happened, so just use the discovered value or fall back).
            root_id = self._node_root_id.get(sid, sid)
            ev = BusEvent(
                type=event_type,
                root_id=root_id,
                sid=sid,
                payload=dict(change),
                persist=False,
            )
            try:
                # `run_coroutine_threadsafe` is the thread-safe primitive
                # — `_fire` can be called from worker threads (e.g.
                # `to_thread`-wrapped cleanup subscribers from A15 that
                # call `session_manager.delete`). `loop.create_task`
                # would corrupt the loop's task list from those threads.
                # We discard the returned Future; subscribers run
                # fire-and-forget after the lock releases.
                asyncio.run_coroutine_threadsafe(
                    bus.publish(ev), self._loop,
                )
            except RuntimeError:
                # Loop torn down mid-fire (shutdown race). Listener
                # fan-out below still runs.
                pass
        # Legacy listener fan-out — kept for the deprecation window.
        for fn in list(self._listeners):
            try:
                fn(sid, change)
            except Exception:
                logger.exception("session listener raised: %s", change.get("kind"))

    # ── Async reconcile coordination ───────────────────────────────
    # The cost-shaping rule: reconcile of events.jsonl → render-tree
    # is potentially O(N) (full file scan + per-event apply). It is
    # NEVER allowed to run inline on REST/WS read paths. Readers call
    # `schedule_reconcile_if_needed`, which is idempotent + single-
    # flight + delayed-progress (no UI events under 0.3s; emits
    # `session_processing_started/finished` for slow reconciles).
    #
    # Lock ordering invariant: `_async_reconcile_with_progress`
    # acquires `_lock_for_root(rid)` around BOTH `{emit started}` and
    # `{emit finished}`. WS subscribe's catch-up `{check in-flight;
    # emit started}` runs under the SAME lock. This serializes the
    # catch-up emit against the timer-driven emits, closing the
    # "subscriber observes in-flight=True then finished fires before
    # catch-up started → stuck badge" race.

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Capture the event loop so cross-thread `mark_reconcile_dirty`
        can schedule onto it. Call once at backend startup."""
        self._loop = loop

    def bind_reconcile_fn(self, fn: Callable[..., list]) -> None:
        """Wire the sync reconcile (defined in `main.py` to dodge a
        circular import: reconcile imports `orchs.get_strategy`, orchs
        imports session_manager). `fn(root_id, after_seq=cursor)` runs
        the incremental reconcile + zombie-reap against the live cached
        root. Invoked inside an `asyncio.to_thread` worker — MUST NOT
        hold this manager's per-root lock for the whole scan."""
        self._reconcile_fn = fn

    def bind_pin_predicate(
        self, fn: Callable[[str, set], bool],
    ) -> None:
        """Wire the orchestrator's `is_root_in_use(root_id, node_sids)`
        check (defined in main.py to dodge the circular import). LRU
        eviction calls it so it never drops a root the orchestrator still
        references. Call once at startup."""
        self._pin_predicate = fn

    def bind_processing_emitter(
        self, fn: Callable[[str, str], None],
    ) -> None:
        """Wire the WS event emitter. `fn(root_id, kind)` where kind ∈
        {'started','finished'}. Runs on the event loop thread inside
        the per-root lock."""
        self._emit_processing_fn = fn

    def bind_stub_invalidated_emitter(
        self, fn: Callable[[list], None],
    ) -> None:
        """Wire the `stub_invalidated` emitter. `fn(changes)` where each
        change is `{app_session_id, msg_id, stub}` for a non-latest
        historical msg whose stub went stale during reconcile. Runs on
        the event loop thread (the reconcile body runs in a worker)."""
        self._emit_stub_invalidated_fn = fn

    def bind_reconciled_emitter(
        self, fn: Callable[[str], None],
    ) -> None:
        """Wire the `session_reconciled` emitter. `fn(root_id)` fires
        on the event loop thread after every reconcile completes (fast
        or slow). The frontend uses it to silently refetch the session
        if the user is viewing it, so stale cache served on the initial
        GET is replaced with the reconciled state."""
        self._emit_reconciled_fn = fn

    def bind_active_run_gate(self, fn: Callable[[str], bool]) -> None:
        """Inject `coordinator.has_active_runs` so `set_selectors` can
        re-check it under the per-root lock when `provider_id` changes.
        Wired once at startup from `main.py`. A10 TOCTOU closure."""
        self._active_run_gate = fn

    def bind_running_check(self, fn: Callable[[str], bool]) -> None:
        """Inject the live-liveness computation. Source of truth lives
        on the coordinator (`_run_state` + per-entry pid liveness +
        `active_run_ids`). Wired once at startup from `main.py`.

        After binding, `is_running(sid)` returns a freshly-computed value
        and `recompute_state(sid)` can broadcast the `running_changed`
        projection. `is_running` is the cheap `_run_state` walk; the single
        state authority is `monitoring_state` (running == state != stopped)."""
        self._compute_is_running = fn

    def bind_monitoring_check(self, fn: Callable[[str], str]) -> None:
        """Inject the live monitoring-state computation (coordinator's
        `monitoring_state`) — the single source of truth for session state.
        After binding, `recompute_state(sid)` computes it once and broadcasts
        the `running_changed` + `monitoring_changed` deltas."""
        self._compute_monitoring = fn

    def monitoring_state(self, sid: str) -> str:
        """Live monitoring state as surfaced to the frontend badge. Workers
        return 'stopped' (no user-facing badge)."""
        if self._compute_monitoring is None:
            return "stopped"
        sess = self._cached(sid)
        if not self._is_user_kind(sess):
            return "stopped"
        return self._compute_monitoring(sid)

    def is_user_kind_sid(self, sid: str) -> bool:
        """Kind gate without event hydration — same light read
        `recompute_state` uses. True iff the sid is a user-facing session
        (workers never surface running/monitoring broadcasts)."""
        rid = self._root_id_for(sid)
        if rid is None:
            return False
        return self._is_user_kind(self._node_record_light(sid, rid))

    def broadcast_state_snapshot(self) -> tuple[dict[str, bool], dict[str, str]]:
        """Last WS-broadcast running/monitoring values per sid — what the
        frontend currently believes. Read-only diagnostic surface for the
        running-state discrepancy audit."""
        return (
            dict(self._last_broadcast_running),
            dict(self._last_broadcast_monitoring),
        )

    def recompute_state(self, sid: str) -> None:
        """Recompute a session's state and broadcast the deltas.

        There is ONE state — the monitoring state (active / idle /
        blocked_on_user / waiting_on_background / stopped). "Running" is just
        the projection `state != "stopped"`, NOT an independent flag. This
        computes the monitoring state ONCE and fires `running_changed` and/or
        `monitoring_changed`, each only when its value changed since the last
        broadcast. Replaces the old paired recompute_running +
        recompute_monitoring.

        Cheap for stopped sessions: `monitoring_state` short-circuits to
        "stopped" right after the same `_run_state` walk `is_running` does, so
        the all-sessions tick pays the approval/background refinement cost only
        for the few sessions that are actually live. Workers are dropped (no
        user-facing badge)."""
        if self._compute_monitoring is None and self._compute_is_running is None:
            return
        rid = self._root_id_for(sid)
        if rid is None:
            return
        # Kind gate WITHOUT event hydration. `monitoring_state` reads only
        # in-memory run-state — never the render tree — so the full
        # `_cached`→`_load_root` path here would cold-load + scan an
        # evicted root's whole events.jsonl (up to ~21s) ON THE CALLER'S
        # THREAD. `tick_running_state` runs this synchronously from async
        # REST handlers, so that scan froze the event loop and stalled
        # every concurrent request. The light read needs no lock — `kind`
        # is stable metadata.
        if not self._is_user_kind(self._node_record_light(sid, rid)):
            return
        with self._lock_for_root(rid):
            # Single computation. When the monitoring check is bound (always
            # in production) it is the authority and `running` is derived from
            # it. If only the running check is bound (some unit harnesses),
            # fall back to it and skip the monitoring delta.
            if self._compute_monitoring is not None:
                state = self._compute_monitoring(sid)
                running = state != "stopped"
            else:
                state = None
                running = bool(self._compute_is_running(sid))

            last_run = self._last_broadcast_running.get(sid)
            if not (last_run is not None and last_run == running):
                if running:
                    self._last_broadcast_running[sid] = True
                else:
                    self._last_broadcast_running.pop(sid, None)
                self._fire(sid, {"kind": "running_changed", "value": running})

            if state is not None and self._last_broadcast_monitoring.get(sid) != state:
                if state == "stopped":
                    self._last_broadcast_monitoring.pop(sid, None)
                else:
                    self._last_broadcast_monitoring[sid] = state
                self._fire(sid, {"kind": "monitoring_changed", "value": state})

    def mark_reconcile_dirty(self, root_id: str) -> None:
        """Signal that the in-memory cache may lag events.jsonl for
        `root_id` (e.g. orphan event landed for a finalized msg).
        Acquires this manager's per-root lock so the set happens-before
        any subsequent `consume_reconcile_dirty` under the same lock.
        Safe to call from any thread."""
        with self._lock_for_root(root_id):
            already = self._reconcile_dirty.get(root_id, False)
            self._reconcile_dirty[root_id] = True
            if not already:
                logger.info(
                    "reconcile-dirty armed: root=%s (hydrated=%s)",
                    root_id[:8], root_id in self._event_hydrated_roots,
                )

    def is_reconcile_dirty(self, root_id: str) -> bool:
        """Read-only peek at the dirty flag. Does NOT clear it.
        Used for diagnostic logging on read paths."""
        with self._lock_for_root(root_id):
            return self._reconcile_dirty.get(root_id, False)

    def consume_reconcile_dirty(self, root_id: str) -> bool:
        """Atomic read-and-clear of the dirty flag. Returns prior value.
        Race safety: if `mark_reconcile_dirty` fires AFTER this clears
        but BEFORE the subsequent reconcile reads events.jsonl, the
        flag re-arms and the NEXT consume picks up the just-added
        event. No orphan event is silently dropped."""
        with self._lock_for_root(root_id):
            was_dirty = self._reconcile_dirty.get(root_id, False)
            self._reconcile_dirty[root_id] = False
            return was_dirty

    def is_reconcile_in_flight(self, root_id: str) -> bool:
        """True iff a reconcile task is currently scheduled or
        running. Used by WS subscribe to send catch-up
        `session_processing_started` to a freshly-joined client."""
        t = self._in_flight_reconcile.get(root_id)
        return t is not None and not t.done()

    def latest_assistant_finalized(self, sid: str) -> bool:
        """True iff `sid` has an assistant message AND the most recent
        one is finalized (not streaming). Used by `event_ingester.ingest`
        to detect orphan events (msg_id=None landing after the
        orchestrator already finalized the turn) and arm the dirty flag.
        Returns False if no assistant msg exists OR the sid is unknown.

        Thin load (`hydrate_events=False`): this reads only message
        `role`/`isStreaming`, which come from the on-disk snapshot —
        event hydration populates `msg.events`, not the messages list,
        so the answer is identical without it. Avoiding hydration here
        matters because `event_ingester.ingest` calls this on the
        per-root shard thread for every orphan event, and a cold-load
        hydration scans the full events.jsonl (~seconds on the largest
        roots), stalling the shard and indirectly blocking the asyncio
        loop on queued writes for that root."""
        rid = self._root_id_for(sid)
        if rid is None:
            return False
        with self._lock_for_root(rid):
            sess = self._cached(sid, hydrate_events=False)
            if sess is None:
                return False
            for m in reversed(sess.get("messages") or []):
                if m.get("role") == "assistant":
                    return not m.get("isStreaming")
            return False

    def schedule_reconcile_if_needed(
        self, root_id: str,
    ) -> Optional[asyncio.Task]:
        """Spawn the async reconcile task, idempotent.

        Returns the existing in-flight task if one is already running.
        Otherwise: if `consume_reconcile_dirty` returns True (cold-
        load _load_root arms the flag, and `mark_reconcile_dirty`
        arms it for orphan events), spawns a new task. Else returns
        None (no work needed).

        Caller MUST run on the event loop thread (single-flight dict
        mutation is loop-only)."""
        if self._reconcile_fn is None:
            return None
        if not self._reconcile_accepting:
            perf.record_count("shutdown.session_reconcile.rejected", 1)
            return None
        existing = self._in_flight_reconcile.get(root_id)
        if existing is not None and not existing.done():
            return existing
        if not self.consume_reconcile_dirty(root_id):
            return None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = self._loop
            if loop is None or loop.is_closed():
                return None
        task = loop.create_task(self._async_reconcile_with_progress(root_id))
        self._in_flight_reconcile[root_id] = task
        task.add_done_callback(
            lambda _t, rid=root_id: self._in_flight_reconcile.pop(rid, None),
        )
        return task

    async def _async_reconcile_with_progress(self, root_id: str) -> None:
        """Run `_sync_reconcile` in a threadpool worker. Emits
        `session_processing_started` ONLY if reconcile takes >0.3s
        (avoids UI flash for fast cases); always emits the matching
        `finished` if `started` was emitted, even on exception."""
        queued_at = time.perf_counter()
        ctx = contextvars.copy_context()
        loop = asyncio.get_running_loop()

        def _run():
            perf.record(
                "session.reconcile.queue_wait",
                (time.perf_counter() - queued_at) * 1000,
            )
            return ctx.run(self._sync_reconcile, root_id)

        inner = loop.run_in_executor(
            _RECONCILE_EXECUTOR,
            _run,
        )
        reconcile_start = time.perf_counter()
        started_emitted = False
        changes: list = []
        try:
            try:
                # `shield` so `wait_for`'s timeout cancels the WAIT, not
                # the inner reconcile (which keeps running into the
                # outer `await inner` below).
                changes = await asyncio.wait_for(
                    asyncio.shield(inner), timeout=0.3,
                ) or []
            except asyncio.TimeoutError:
                started_emitted = True
                self._emit_processing("started", root_id)
                changes = await inner or []
        finally:
            perf.record(
                "session.reconcile.total",
                (time.perf_counter() - reconcile_start) * 1000,
            )
            if started_emitted:
                self._emit_processing("finished", root_id)
        logger.info(
            "reconcile completed: root=%s changes=%d started_emitted=%s",
            root_id[:8], len(changes), started_emitted,
        )
        # Emit stub-invalidations on the loop thread (reconcile ran in a
        # worker). Outside the lock — these are fire-and-forget pings.
        if changes and self._emit_stub_invalidated_fn is not None:
            try:
                self._emit_stub_invalidated_fn(changes)
            except Exception:
                logger.exception("stub_invalidated emit failed for %s", root_id)
        # Always notify frontend that reconcile completed so it can
        # silently refetch if the user is viewing this session — the
        # initial GET may have returned stale cache.
        if self._emit_reconciled_fn is not None:
            try:
                self._emit_reconciled_fn(root_id)
            except Exception:
                logger.exception("session_reconciled emit failed for %s", root_id)

    def _emit_processing(self, kind: str, root_id: str) -> None:
        if self._emit_processing_fn is None:
            return
        try:
            self._emit_processing_fn(root_id, kind)
        except Exception:
            logger.exception("processing emit failed: %s %s", kind, root_id)

    def _sync_reconcile(self, root_id: str) -> list:
        """Threadpool entrypoint for the injected reconcile. Passes the
        reconcile cursor so the reconcile body only reads events after
        the last processed seq — no full scan."""
        if self._reconcile_fn is None:
            return []
        cursor = self._reconcile_cursor.get(root_id, 0)
        gen_before = self._reconcile_gen.get(root_id, 0)
        start = time.perf_counter()
        try:
            changes = self._reconcile_fn(root_id, after_seq=cursor) or []
        except Exception:
            logger.exception("sync reconcile failed for %s", root_id)
            return []
        reconcile_ms = (time.perf_counter() - start) * 1000
        # Advance cursor to the current high-water mark.
        from event_journal import event_journal_reader
        cursor_start = time.perf_counter()
        new_cursor = event_journal_reader.current_seq(root_id)
        cursor_ms = (time.perf_counter() - cursor_start) * 1000
        if new_cursor is not None:
            self._reconcile_cursor[root_id] = new_cursor
        cursor_advanced = new_cursor is not None and new_cursor > cursor
        changed = cursor_advanced and bool(changes)
        if changed:
            self._reconcile_gen[root_id] = gen_before + 1
        logger.info(
            "_sync_reconcile %s: cursor=%d->%s gen=%d->%d changes=%d "
            "advanced=%s bumped=%s reconcile=%.1fms cursor=%.1fms",
            root_id[:8], cursor, new_cursor, gen_before,
            self._reconcile_gen.get(root_id, 0), len(changes),
            cursor_advanced, changed, reconcile_ms, cursor_ms,
        )
        return changes

    def reconcile_through(self, root_id: str, required_seq: int) -> None:
        """Synchronously project durable journal facts through a writer barrier."""
        from event_journal import event_journal_reader

        if self._reconcile_fn is None:
            raise RuntimeError("session reconcile function is not bound")
        event_journal_reader.read_through(root_id, required_seq)
        cursor = self._reconcile_cursor.get(root_id, 0)
        gen_before = self._reconcile_gen.get(root_id, 0)
        changes = self._reconcile_fn(root_id, after_seq=cursor) or []
        new_cursor = event_journal_reader.current_seq(root_id)
        if new_cursor is not None:
            self._reconcile_cursor[root_id] = new_cursor
        cursor_advanced = new_cursor is not None and new_cursor > cursor
        changed = cursor_advanced and bool(changes)
        if changed:
            self._reconcile_gen[root_id] = gen_before + 1
        logger.info(
            "reconcile_through %s: cursor=%d->%s gen=%d->%d required_seq=%d "
            "changes=%d advanced=%s bumped=%s",
            root_id[:8], cursor, new_cursor, gen_before,
            self._reconcile_gen.get(root_id, 0), required_seq, len(changes),
            cursor_advanced, changed,
        )

    def apply_journal_ownership_resolution(
        self,
        root_id: str,
        event_sid: str,
        msg_id: str,
        resolution_seq: int,
    ) -> bool:
        """Apply a late-resolved journal event to its exact live message."""
        from event_journal import event_journal_reader
        from orchs import ApplyEventCtx, get_strategy

        rows, _, _ = event_journal_reader.read_events(
            root_id, limit=999_999, msg_id_filter=msg_id,
        )
        rows = [
            row for row in rows
            if row.get("type") != "event_ownership_resolved"
        ]
        if not rows:
            return False
        rid = self._root_id_for(root_id)
        if rid is None:
            return False
        if not self.hydrate_root_prepared(rid):
            return False
        with self._lock_for_root(rid):
            root = self._load_root(root_id, hydrate_events=False)
            node = _find_message_node(root, msg_id) if root else None
            if node is None:
                return False
            msg = _find_message(node, msg_id)
            if msg is None:
                return False
            node_sid = str(node.get("id") or event_sid)
            strategy = get_strategy(node.get("orchestration_mode") or "team")
            events_list = strategy._events_list(msg)
            before = copy.deepcopy(events_list)
            ctx = ApplyEventCtx(root_id=root_id)
            for row in rows:
                strategy.apply_event(
                    app_session_id=node_sid,
                    msg=msg,
                    event={"type": row.get("type"), "data": row.get("data")},
                    ctx=ctx,
                    source_is_provider_stream=False,
                )
            order_by_uuid = {
                (row.get("data") or {}).get("uuid"): index
                for index, row in enumerate(rows)
                if isinstance(row.get("data"), dict)
                and (row.get("data") or {}).get("uuid")
            }
            events_list = strategy._events_list(msg)
            events_list.sort(
                key=lambda event: order_by_uuid.get(
                    (event.get("data") or {}).get("uuid"), len(order_by_uuid),
                ),
            )
            msg.pop("_uid_idx", None)
            # A reorder can leave len(events) unchanged, which would
            # otherwise look like "nothing happened" to
            # apply_written_journal_event's before_len+1 pure-append
            # check on its NEXT call — invalidate so that call
            # re-establishes a correct full-hash baseline instead of
            # incrementally folding onto a now-stale one.
            msg.pop(messages_delta_compaction.PRECOMPUTED_REVISION_KEY, None)
            changed = events_list != before
            if changed:
                self.refresh_message_content_from_events(
                    root_id, node_sid, msg_id,
                )
                self._fire(node_sid, {
                    "kind": "message_ownership_resolved",
                    "msg_id": msg_id,
                    "resolution_seq": resolution_seq,
                    "msg": copy.deepcopy(msg),
                })
            return changed

    def apply_written_journal_event(
        self,
        root_id: str,
        event_sid: str,
        msg_id: str,
        event_type: str,
        data: dict,
        seq: int,
    ) -> bool:
        """Project one written journal render event into the live tree."""
        from event_shape import project_content_snapshot
        from orchs import ApplyEventCtx, get_strategy

        rid = self._root_id_for(root_id)
        if rid is None:
            return False
        if not self.hydrate_root_prepared(rid):
            return False
        with self._lock_for_root(rid):
            root = self._load_root(root_id, hydrate_events=False)
            node = _find_message_node(root, msg_id) if root else None
            if node is None:
                return False
            msg = _find_message(node, msg_id)
            if msg is None:
                return False
            node_sid = str(node.get("id") or event_sid)
            strategy = get_strategy(node.get("orchestration_mode") or "team")
            before_events = strategy._events_list(msg)
            event_uuid = _event_uuid_safe({"type": event_type, "data": data})
            before_len = len(before_events)
            before_event = None
            if event_uuid:
                uid_idx = msg.get("_uid_idx")
                before_idx = uid_idx.get(event_uuid) if isinstance(uid_idx, dict) else None
                if before_idx is None:
                    for idx, event in enumerate(before_events):
                        if _event_uuid_safe(event) == event_uuid:
                            before_idx = idx
                            break
                if isinstance(before_idx, int) and 0 <= before_idx < before_len:
                    before_event = _copy_jsonish(before_events[before_idx])
            else:
                before_event = _copy_jsonish(before_events)
            ctx = ApplyEventCtx(root_id=root_id)
            strategy.apply_event(
                app_session_id=node_sid,
                msg=msg,
                event={"type": event_type, "data": data},
                ctx=ctx,
                source_is_provider_stream=False,
                write_journal=False,
            )
            after_events = strategy._events_list(msg)
            if not msg.get("isStreaming"):
                content = project_content_snapshot(after_events, msg.get("content"))
                if content != (msg.get("content") or ""):
                    msg["content"] = content
            if event_uuid:
                uid_idx = msg.get("_uid_idx")
                after_idx = uid_idx.get(event_uuid) if isinstance(uid_idx, dict) else None
                if after_idx is None:
                    for idx, event in enumerate(after_events):
                        if _event_uuid_safe(event) == event_uuid:
                            after_idx = idx
                            break
                after_event = (
                    after_events[after_idx]
                    if isinstance(after_idx, int) and 0 <= after_idx < len(after_events)
                    else None
                )
                changed = before_len != len(after_events) or before_event != after_event
            else:
                changed = before_event != after_events
            if changed:
                # Precompute the omitted-events revision HERE, where
                # before_len/after_events are the live, identity-stable
                # objects apply_event just mutated — the only place this
                # can be done incrementally and correctly. Recomputing a
                # full content hash for every compact delta on every
                # single streamed event was O(n) work called O(n) times
                # per message (O(n^2) over a turn), and was measured
                # causing multi-second-to-tens-of-seconds event-loop
                # stalls on long turns. A pure append (the dominant case)
                # folds the one new event into the cached prior revision;
                # anything else (a same-slot replace, e.g. Gemini
                # streaming re-emitting the same uuid) re-establishes a
                # fresh, correct full-hash baseline.
                if len(after_events) == before_len + 1:
                    prev_revision = msg.get(
                        messages_delta_compaction.PRECOMPUTED_REVISION_KEY, "",
                    )
                    if prev_revision or before_len == 0:
                        msg[messages_delta_compaction.PRECOMPUTED_REVISION_KEY] = (
                            messages_delta_compaction.fold_revision(
                                prev_revision, after_events[-1],
                            )
                        )
                    else:
                        msg[messages_delta_compaction.PRECOMPUTED_REVISION_KEY] = (
                            messages_delta_compaction.full_revision(after_events)
                        )
                else:
                    msg[messages_delta_compaction.PRECOMPUTED_REVISION_KEY] = (
                        messages_delta_compaction.full_revision(after_events)
                    )
                self._persist_root(rid, bump=True)
                delta = messages_delta_compaction.compact_message_delta_payload(msg)
                self._fire(node_sid, {
                    "kind": "journal_event_projected",
                    "msg_id": msg_id,
                    "seq": seq,
                    "delta": delta,
                })
            else:
                msg.pop(messages_delta_compaction.PRECOMPUTED_REVISION_KEY, None)
            return changed

    # ── Cache + lock ───────────────────────────────────────────────

    def _ensure_home_current(self) -> None:
        sessions_dir = session_store._sessions_dir()
        if self._home_sessions_dir == sessions_dir:
            return
        close_event_ingester = False
        with self._cache_guard:
            if self._home_sessions_dir == sessions_dir:
                return
            self._home_sessions_dir = sessions_dir
            self._clear_home_scoped_state()
            close_event_ingester = True
        if close_event_ingester:
            self._close_home_scoped_event_ingester()

    def _clear_home_scoped_state(self) -> None:
        callbacks = [
            callback
            for group in self._owner_revocation_callbacks.values()
            for callback in group
        ]
        self._owner_revocation_callbacks.clear()
        self._owner_generations.clear()
        self._owner_operation_locks.clear()
        for callback in callbacks:
            try:
                callback()
            except Exception:
                logger.exception("session owner home-switch callback failed")
        self._roots.clear()
        self._root_file_fingerprints.clear()
        self._root_file_checked_at.clear()
        self._event_hydrated_roots.clear()
        self._hydration_conditions.clear()
        self._hydration_in_flight.clear()
        self._node_root_id.clear()
        self._node_root_missing_until.clear()
        self._mutation_miss_logged_at.clear()
        self._kind_by_sid.clear()
        self._root_locks.clear()
        self._batches.clear()
        self._loading_roots.clear()
        self._reconcile_dirty.clear()
        self._in_flight_reconcile.clear()
        self._since_cache.clear()
        self._since_cache_bytes.clear()
        self._since_cache_total_bytes = 0
        self._window_cache.clear()
        self._tree_stub_cache.clear()
        self._tree_stub_attached_cache.clear()
        self._todo_projection_cache.clear()
        self._queued_prompt_counts_by_sid.clear()
        self._reconcile_gen.clear()
        self._reconcile_cursor.clear()
        self._recovering_msg_ids.clear()
        self._last_broadcast_running.clear()
        self._last_broadcast_monitoring.clear()
        self._project_key_cache.clear()
        self._unread_counts.clear()
        self._unread_counts_version += 1
        self._unread_hydrated.clear()
        with _persist_state_lock:
            _persist_pending.clear()
            _persist_deadlines.clear()
            _persist_deadline_heap.clear()
            _persist_last_at.clear()
            _persist_inflight.clear()
            _persist_state_changed.notify_all()

    def _close_home_scoped_event_ingester(self) -> None:
        try:
            from event_ingester import event_ingester
            event_ingester.close_all()
        except Exception:
            logger.exception("failed to close event ingester on state-home switch")

    def _root_id_for(self, sid: str) -> Optional[str]:
        self._ensure_home_current()
        rid = self._node_root_id.get(sid)
        if rid is not None:
            return rid
        rid = session_store._loaded_root_id_for(sid)
        if rid is not None:
            self._node_root_id[sid] = rid
            self._node_root_missing_until.pop(sid, None)
            return rid
        if session_store.session_file_fingerprint(sid) is not None:
            self._node_root_id[sid] = sid
            self._node_root_missing_until.pop(sid, None)
            return sid
        now = time.monotonic()
        if self._node_root_missing_until.get(sid, 0.0) > now:
            return None
        rid = session_store._resolve_root_id(sid)
        if rid is not None:
            self._node_root_id[sid] = rid
            self._node_root_missing_until.pop(sid, None)
        else:
            self._node_root_missing_until[sid] = (
                now + _NEGATIVE_NODE_ROOT_TTL_SECONDS
            )
        return rid

    def root_id_for(self, sid: str) -> Optional[str]:
        """Public: resolve any node id — a BA app_session_id, or a provider's
        native/agent session id (e.g. a claude transcript's id) — to its BA root
        session id. Returns None when the id belongs to no BA session (a raw
        native transcript never run through Better Agent)."""
        return self._root_id_for(sid)

    def claim_owner(self, sid: str) -> Optional[SessionOwnerToken]:
        rid = self._root_id_for(sid)
        if rid is None:
            return None
        with self._lock_for_root(rid):
            root = self._ensure_root_loaded(rid)
            if root is None or session_store._find_in_tree(root, sid) is None:
                return None
            generation = self._owner_generations.setdefault(sid, 1)
            node = session_store._find_in_tree(root, sid)
            assert node is not None
            incarnation = str(node.get("_owner_incarnation") or "")
            token = SessionOwnerToken(
                sid=sid,
                root_id=rid,
                generation=generation,
                incarnation=incarnation,
            )
            return token

    def run_if_owner(
        self, token: SessionOwnerToken, callback: Callable[[], Any],
    ) -> tuple[bool, Any]:
        with self._cache_guard:
            operation_lock = self._owner_operation_locks.setdefault(
                token.sid, threading.RLock(),
            )
        with operation_lock:
            with self._lock_for_root(token.root_id):
                if self._owner_generations.get(token.sid) != token.generation:
                    return False, None
                root = self._ensure_root_loaded(token.root_id)
                if root is None or session_store._find_in_tree(root, token.sid) is None:
                    return False, None
            return True, callback()

    def subscribe_owner_revoked(
        self, token: SessionOwnerToken, callback: Callable[[], None],
    ) -> Callable[[], None]:
        invoke_now = False
        with self._lock_for_root(token.root_id):
            if self._owner_generations.get(token.sid) != token.generation:
                invoke_now = True
            else:
                callbacks = self._owner_revocation_callbacks.setdefault(token.sid, set())
                callbacks.add(callback)
        if invoke_now:
            callback()
            return lambda: None

        def unsubscribe() -> None:
            with self._lock_for_root(token.root_id):
                current = self._owner_revocation_callbacks.get(token.sid)
                if current is not None:
                    current.discard(callback)
                    if not current:
                        self._owner_revocation_callbacks.pop(token.sid, None)
        return unsubscribe

    def _revoke_owner_locked(self, sid: str) -> tuple[Callable[[], None], ...]:
        generation = self._owner_generations.get(sid, 1)
        self._owner_generations[sid] = generation + 1
        return tuple(self._owner_revocation_callbacks.pop(sid, ()))

    @staticmethod
    def _invoke_owner_revocations(
        sid: str, callbacks: Iterable[Callable[[], None]],
    ) -> None:
        for callback in callbacks:
            try:
                callback()
            except Exception:
                logger.exception("session owner revocation callback failed for %s", sid)

    def _lock_for_root(self, root_id: str) -> threading.RLock:
        self._ensure_home_current()
        with self._cache_guard:
            lock = self._root_locks.get(root_id)
            if lock is None:
                lock = threading.RLock()
                self._root_locks[root_id] = lock
            return lock

    def _index_root(self, root: dict) -> None:
        """Populate `_node_root_id` for every node in the tree rooted at
        `root`. Safe to call repeatedly — overwrites with the same
        value if already present."""
        rid = root["id"]
        self._node_root_id[rid] = rid
        self._node_root_missing_until.pop(rid, None)
        self._owner_generations.setdefault(rid, 1)
        self._kind_by_sid[rid] = root.get("kind")
        for fork in session_store._walk_forks(root):
            self._node_root_id[fork["id"]] = rid
            self._node_root_missing_until.pop(fork["id"], None)
            self._owner_generations.setdefault(fork["id"], 1)
            self._kind_by_sid[fork["id"]] = fork.get("kind")

    def _ensure_root_loaded(self, rid: str) -> Optional[dict]:
        """Return the live in-memory root for `rid`, loading it from disk
        into `_roots` on a cache miss WITHOUT event-hydration or
        reconcile (fork/delete operate on session-level tree fields, not
        events.jsonl). Prefers the live `_roots` ref when present so a
        structural mutation lands on the authoritative copy. None if the
        root file is absent."""
        root = self._roots.get(rid)
        if root is None:
            root = session_store.get_root_tree(rid)
            if root is not None:
                self._roots[rid] = root
                self._root_file_fingerprints[rid] = (
                    session_store.session_file_fingerprint(rid) or (0, 0)
                )
                self._root_file_checked_at[rid] = time.monotonic()
                self._index_root(root)
        return root

    def _cached_root_is_stale(self, rid: str) -> bool:
        now = time.monotonic()
        last_checked = self._root_file_checked_at.get(rid, 0.0)
        if now - last_checked < EXTERNAL_RELOAD_POLL_INTERVAL_S:
            return False
        self._root_file_checked_at[rid] = now
        current = session_store.session_file_fingerprint(rid)
        if current is None:
            return False
        previous = self._root_file_fingerprints.get(rid)
        if previous is None:
            self._root_file_fingerprints[rid] = current
            return False
        if current == previous:
            return False
        with _persist_state_lock:
            has_pending_local_write = rid in _persist_pending
        return not has_pending_local_write

    def _note_root_file_written(self, rid: str) -> None:
        current = session_store.session_file_fingerprint(rid)
        if current is not None:
            self._root_file_fingerprints[rid] = current
            self._root_file_checked_at[rid] = time.monotonic()

    def _drop_cached_root_for_reload(self, rid: str, cached_root: dict) -> None:
        self._roots.pop(rid, None)
        self._event_hydrated_roots.discard(rid)
        self._drop_since_cache_entry(rid)
        self._drop_window_cache_for_sids({rid})
        self._drop_tree_stub_attached_cache_for_root(rid)
        for node in session_store._walk_forks(cached_root):
            node_sid = node.get("id")
            if node_sid:
                self._drop_since_cache_entry(node_sid)
                self._drop_window_cache_for_sids({node_sid})

    def _load_root(
        self, any_sid: str, *, hydrate_events: bool = True,
    ) -> Optional[dict]:
        """Resolve `any_sid` to its root and ensure the root tree is in
        cache. Returns the cached root reference.

        On cold cache miss, the disk-loaded tree may lag events.jsonl
        (orphan events written after the prior backend's orchestrator
        finalized a turn but before the next read). We arm the dirty
        flag so the first `schedule_reconcile_if_needed` call from
        any reader spawns the async recovery reconcile."""
        rid = self._root_id_for(any_sid)
        if rid is None:
            return None
        # Same-thread re-entrancy guard. Hydration
        # (`render_tree_hydrate.hydrate_msg_events_from_jsonl`) calls
        # `session_manager.get_ref`, which re-enters `_load_root`. For a
        # root whose on-disk fingerprint is racing the cache (an
        # actively-written session), the re-entrant call saw
        # `_cached_root_is_stale` True → `_drop_cached_root_for_reload`
        # → cold reload → hydrate again; the drop discards
        # `_event_hydrated_roots` and the phantom-batch guard only
        # covers the warm branch (not the cold path's unconditional
        # hydrate), so the cycle repeated until RecursionError — which
        # is swallowed inside `_hydrate_cached_root_events`, leaving
        # `msg.events` empty (the empty "No output" assistant boxes).
        # Short-circuit to the resident ref; the outer call owns the
        # disk load + hydrate.
        if self._loading_roots.get(rid) == threading.get_ident():
            return self._roots.get(rid)
        self._loading_roots[rid] = threading.get_ident()
        try:
            return self._load_root_impl(rid, hydrate_events=hydrate_events)
        finally:
            if self._loading_roots.get(rid) == threading.get_ident():
                self._loading_roots.pop(rid, None)

    def _load_root_impl(
        self, rid: str, *, hydrate_events: bool = True,
    ) -> Optional[dict]:
        cached = self._roots.get(rid)
        if cached is not None:
            if self._cached_root_is_stale(rid):
                self._drop_cached_root_for_reload(rid, cached)
                cached = None
            else:
                # `_cached_root_is_stale` above does a disk fingerprint check
                # (I/O, so the GIL can drop), and a concurrent thread — LRU-cap
                # eviction, `_drop_cached_root_for_reload`, or an explicit
                # `reload_root_from_disk`/`delete` — can evict `rid` between the
                # `get` above and here. `OrderedDict.move_to_end` raises
                # `KeyError` on an absent key, which would 500 the caller (seen
                # on the /api/sessions sidebar-list path). There is nothing to
                # bump if the entry was concurrently evicted; fall through and
                # return the still-valid `cached` tree (the next reader
                # cold-loads a fresh copy).
                try:
                    self._roots.move_to_end(rid)   # LRU: mark most-recently-used
                except KeyError:
                    pass
                batch_ctx = self._batches.get(rid)
                hydrating = bool(batch_ctx and batch_ctx.get("_phantom"))
                if (
                    hydrate_events
                    and rid not in self._event_hydrated_roots
                    and not hydrating
                ):
                    self._reconcile_dirty[rid] = True
                elif (
                    hydrate_events
                    and rid in self._event_hydrated_roots
                    and self._reconcile_dirty.get(rid)
                ):
                    logger.debug(
                        "_load_root: skipping hydration on dirty root=%s "
                        "(orphan events not yet applied to in-memory root)",
                        rid[:8],
                    )
                return cached
        self._event_hydrated_roots.discard(rid)
        # Drain any pending tail-flush for this root BEFORE reading
        # disk. Otherwise a debounced write whose live ref still sits
        # in `_persist_pending[rid]` would leave disk one revision
        # behind — cold-load would observe stale session-level state
        # (e.g. `last_seen_event_uid` set by mark_seen but not yet
        # written), then hydrate event lists from events.jsonl (which
        # IS durable). Result: snapshot vs events.jsonl disagree on
        # what the user has acked.
        with _persist_state_lock:
            pending = _persist_pending.pop(rid, None)
            _cancel_persist_deadline_unlocked(rid)
        drain_failed = False
        if pending is not None:
            try:
                session_store.write_session_full(
                    pending,
                    bump_updated_at=False,
                    preserve_projection_fields=True,
                )
                self._note_root_file_written(rid)
            except Exception:
                drain_failed = True
                logger.exception(
                    "_load_root: pre-flush of pending persist failed for %s",
                    rid,
                )
        root = session_store.get_root_tree(rid)
        if root is None:
            # Re-queue the drained pending state so it isn't silently
            # lost. The next reader/writer can retry the flush; the
            # in-memory ref is still authoritative. Belt-and-suspenders
            # against a torn write or disk swallow between drain and
            # re-read.
            if pending is not None:
                with _persist_state_lock:
                    _persist_pending.setdefault(rid, pending)
                logger.error(
                    "_load_root: get_root_tree(%s) returned None after "
                    "drain — pending state re-queued (drain_failed=%s).",
                    rid, drain_failed,
                )
            return None
        # `isStreaming` is no longer a persisted field (stripped on
        # write by `session_store.write_session_full`). Loaded sessions
        # therefore have no flag; the runner-registration hook
        # (`coordinator.run_state_add`) is the only writer at runtime.
        # Upgrade path: pre-refactor sessions may still have a baked-in
        # `isStreaming: True` on the last assistant msg. We strip it
        # and stamp `stopped_at` so the Retry button appears — recovery
        # will clear `stopped_at` later for any subprocess it
        # rehydrates as alive (`run_recovery._apply_integration_sync`).
        _strip_legacy_isstreaming_on_load(root)
        self._roots[rid] = root
        self._root_file_fingerprints[rid] = (
            session_store.session_file_fingerprint(rid) or (0, 0)
        )
        self._root_file_checked_at[rid] = time.monotonic()
        # Mirror the warm-branch guard above (see comment at the cached
        # branch): `session_file_fingerprint` (line ~1070) does disk I/O,
        # dropping the GIL, and a concurrent thread can evict `rid`
        # (`_drop_cached_root_for_reload` from a stale-on-reload check,
        # `_enforce_root_cap`, or an explicit reload/delete) between the
        # insert at 1068 and here. `OrderedDict.move_to_end` raises
        # `KeyError` on an absent key, which 500s the caller (seen on the
        # /api/sessions sidebar-list path). For a freshly-inserted key the
        # entry is already at the end, so there is nothing to bump when the
        # entry was concurrently evicted — fall through; `root` is still
        # valid and the next reader cold-loads a fresh copy.
        try:
            self._roots.move_to_end(rid)
        except KeyError:
            pass
        self._index_root(root)
        self._enforce_root_cap(keep_rid=rid)
        if not hydrate_events:
            self._reconcile_dirty[rid] = True
            return root
        self._reconcile_dirty[rid] = True
        return root

    # ── LRU eviction ───────────────────────────────────────────────

    def trim_resident_roots(self, *, keep_rid: str) -> None:
        """Public hook for bulk producers (native import) to bound the
        resident-root cache. `create` inserts without enforcing the cap and
        nothing cold-loads during a tight import loop, so `_roots` would grow
        to every imported session and OOM. Calling this after each import
        LRU-evicts back to the cap, keeping import RAM O(1)."""
        self._enforce_root_cap(keep_rid=keep_rid)

    def _enforce_root_cap(self, *, keep_rid: str) -> None:
        """LRU-evict resident root trees beyond `_roots_max`, oldest
        first, skipping `keep_rid` and any pinned root. Each victim is
        torn down only while its per-root lock is held — acquired
        NON-BLOCKING, so a busy lock (root in use) is simply skipped,
        which also makes the whole pass deadlock-free regardless of lock
        order. Best-effort: under heavy churn it may evict fewer than the
        surplus; the next load re-runs it."""
        if len(self._roots) <= self._roots_max:
            return
        for victim in self._snapshot_oldest_roots(exclude=keep_rid):
            if len(self._roots) <= self._roots_max:
                return
            lock = self._lock_for_root(victim)
            if not lock.acquire(blocking=False):
                continue
            try:
                cached = self._roots.get(victim)
                if cached is None:
                    continue
                node_sids = {victim} | {
                    f["id"] for f in session_store._walk_forks(cached)
                }
                if self._is_pinned(victim, node_sids):
                    continue
                self._drop_root_memory(victim, cached)
            finally:
                lock.release()

    def _snapshot_oldest_roots(self, *, exclude: str) -> list:
        """Oldest-first snapshot of resident root_ids. `_roots` has no
        global structural lock (per-root locks guard each entry's
        contents; single dict ops are GIL-atomic), so a concurrent
        insert/pop can race this iteration — retry on the resulting
        RuntimeError, then give up (the cap is best-effort and the next
        load retries)."""
        for _ in range(16):
            try:
                return [rid for rid in self._roots if rid != exclude]
            except RuntimeError:
                continue
        return []

    def _is_pinned(self, rid: str, node_sids: set) -> bool:
        """True iff the root is in use and must NOT be evicted. Checks the
        in-process signals owned here, then the injected orchestrator
        predicate. Fails CLOSED: an unbound predicate or any error counts
        as pinned, so doubt never evicts live state."""
        if (
            rid in self._batches
            or rid in self._in_flight_reconcile
            or self._reconcile_dirty.get(rid, False)
            or rid in _persist_pending
        ):
            return True
        try:
            ds = self._draft_store_or_none()
        except Exception:
            # Resolver failed (import error, init-ordering race). Per
            # `_is_pinned`'s fail-CLOSED contract, treat as pinned.
            logger.exception(
                "draft store resolution failed for %s — treating as pinned",
                rid,
            )
            return True
        if ds is not None:
            try:
                if ds.is_dirty(rid):
                    return True
            except Exception:
                logger.exception(
                    "draft is_dirty failed for %s — treating as pinned", rid,
                )
                return True
        pred = self._pin_predicate
        if pred is None:
            return True
        try:
            return bool(pred(rid, node_sids))
        except Exception:
            logger.exception(
                "pin predicate failed for %s — treating as pinned", rid,
            )
            return True

    def _drop_root_memory(self, rid: str, cached_root: dict) -> None:
        """Authoritative teardown of an evicted root's in-memory
        footprint: the hydrated tree, the index/display state keyed off
        it, AND the root's `event_ingester` per-root state (dedup sets,
        seq offsets, open fd) via `event_ingester.close(rid)`.

        Closing the ingester is safe ONLY because eviction runs solely
        when `_is_pinned(rid, ...)` is False — no active turn, no live
        tailer, no in-flight/dirty reconcile — so no writer is in flight.
        A stray late ingest (e.g. an owned tailer mid-teardown) re-seeds
        from disk via `_ensure_open`, which REBUILDS `_seq_offsets`
        (event_ingester.py:193) — so no duplicate events.jsonl row; and
        `_reconcile_dirty`/`_reconcile_cursor` survive (below) so a late
        orphan still triggers a delta reconcile. close() takes only the
        ingester lock, AFTER this method's SM per-root lock — the
        documented safe order (never ingester→SM).

        Deliberately LEAVES:
          • `_reconcile_dirty` / `_reconcile_cursor` — a pending reconcile
            for this root must survive (it cold-reloads on next access);
          • `_root_locks[rid]` — a future load re-creates the same lock
            identity; popping it risks a lock-identity race.
        Caller MUST hold `_lock_for_root(rid)`."""
        self._roots.pop(rid, None)
        self._event_hydrated_roots.discard(rid)
        self._node_root_id.pop(rid, None)
        self._drop_since_cache_entry(rid)
        self._drop_window_cache_for_sids({rid})
        self._drop_tree_stub_attached_cache_for_root(rid)
        try:
            ds = self._draft_store_or_none()
        except Exception:
            logger.exception(
                "draft store resolution failed in drop for %s", rid,
            )
            ds = None
        if ds is not None:
            try:
                ds.note_root_dropped(rid)
            except Exception:
                logger.exception("draft note_root_dropped failed for %s", rid)
        self._last_broadcast_running.pop(rid, None)
        self._unread_counts.pop(rid, None)
        self._unread_counts_version += 1
        self._unread_hydrated.discard(rid)
        for f in session_store._walk_forks(cached_root):
            fid = f.get("id")
            if not fid:
                continue
            self._node_root_id.pop(fid, None)
            self._drop_since_cache_entry(fid)
            self._drop_window_cache_for_sids({fid})
            self._last_broadcast_running.pop(fid, None)
            self._unread_counts.pop(fid, None)
            self._unread_counts_version += 1
            self._unread_hydrated.discard(fid)
        # Reclaim the co-resident event_ingester per-root state (dedup
        # sets / seq offsets / open fd). Local import: event_ingester
        # imports session_manager (circular). Safe — see docstring.
        from event_ingester import event_ingester
        event_ingester.close(rid)

    def _event_journal_fingerprint(self, root_id: str) -> tuple[int, int] | None:
        from event_ingester import event_ingester

        path = event_ingester._events_path(root_id)
        try:
            st = path.stat()
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def _apply_cached_todo_projection(
        self,
        root: dict,
        projected: dict[str, tuple[list, list]],
    ) -> None:
        for node in [root, *session_store._walk_forks(root)]:
            sid = node.get("id")
            todos, tasks = projected.get(sid, ([], []))
            node["current_todos"] = copy.deepcopy(todos)
            node["current_tasks"] = copy.deepcopy(tasks)

    def _cache_todo_projection(
        self,
        root_id: str,
        fingerprint: tuple[int, int] | None,
        projected: dict[str, tuple[list, list]],
    ) -> None:
        self._todo_projection_cache[root_id] = (
            fingerprint,
            {
                sid: (copy.deepcopy(todos), copy.deepcopy(tasks))
                for sid, (todos, tasks) in projected.items()
            },
        )
        self._todo_projection_cache.move_to_end(root_id)
        while len(self._todo_projection_cache) > self._todo_projection_cache_max:
            self._todo_projection_cache.popitem(last=False)

    def _hydrate_cached_root_events(self, rid: str, root: dict) -> None:
        self.hydrate_root_prepared(rid)

    @staticmethod
    def _hydration_topology(root: dict) -> tuple[str, ...]:
        return tuple(sorted({
            str(node.get("id"))
            for node in (root, *session_store._walk_forks(root))
            if node.get("id")
        }))

    def hydrate_root_prepared(
        self, rid: str, *, after_seq: int = 0,
        on_historical_change: Optional[Callable[[str, str, dict], None]] = None,
    ) -> bool:
        from render_tree_hydrate import (
            apply_prepared_hydration,
            decode_prepared_hydration,
            hydration_decode_apply_slot,
            prepare_hydration,
            validate_prepared_ownership,
        )

        with self._cache_guard:
            condition = self._hydration_conditions.setdefault(
                rid, threading.Condition(self._cache_guard),
            )
            while rid in self._hydration_in_flight:
                condition.wait()
                if after_seq == 0 and rid in self._event_hydrated_roots:
                    return True
            self._hydration_in_flight.add(rid)
        success = False
        try:
            for _attempt in range(3):
                with self._lock_for_root(rid):
                    root = self._ensure_root_loaded(rid)
                    if root is None:
                        return False
                    captured_root = root
                    topology = self._hydration_topology(root)
                from event_ingester import event_ingester
                if not event_ingester._events_path(rid).exists():
                    with self._lock_for_root(rid):
                        current = self._roots.get(rid)
                        if (
                            current is captured_root
                            and self._hydration_topology(current) == topology
                        ):
                            if after_seq == 0:
                                self._event_hydrated_roots.add(rid)
                            success = True
                            return True
                    continue
                slot = hydration_decode_apply_slot()
                slot.__enter__()
                try:
                    prepared = prepare_hydration(rid, topology, after_seq=after_seq)
                    decoded = decode_prepared_hydration(prepared)
                    if decoded is None:
                        continue
                    ownership_validated = validate_prepared_ownership(prepared)
                    if not ownership_validated:
                        continue
                    with self._lock_for_root(rid):
                        current = self._roots.get(rid)
                        if (
                            current is not captured_root
                            or self._hydration_topology(current) != topology
                        ):
                            continue
                        was_batched = rid in self._batches
                        if not was_batched:
                            self._batches[rid] = {
                                "bump_updated_at": False, "_phantom": True,
                            }
                        try:
                            success = apply_prepared_hydration(
                                current, prepared, decoded,
                                on_historical_change=on_historical_change,
                                ownership_validated=True,
                            )
                            if not success:
                                continue
                            if after_seq == 0:
                                self._derive_current_todos_from_events_jsonl(current, rid)
                                self._event_hydrated_roots.add(rid)
                            return True
                        finally:
                            if not was_batched:
                                self._batches.pop(rid, None)
                finally:
                    slot.__exit__(None, None, None)
            return False
        except Exception:
            logger.exception("prepared hydration failed for %s", rid)
            return False
        finally:
            if not success:
                self._reconcile_dirty[rid] = True
            with self._cache_guard:
                self._hydration_in_flight.discard(rid)
                condition.notify_all()

    def _derive_current_todos_from_events_jsonl(
        self, root: dict, root_id: str,
    ) -> None:
        """Walk every node's events.jsonl rows in seq order through
        the todos AND tasks extractors and stamp `current_todos` and
        `current_tasks` on each node.

        SRP: the only authoritative backfill path for both fields.
        Reads events.jsonl ONCE (no sid_filter) and buckets rows per
        node-sid, then walks each bucket through both extractors.

        Idempotent: the extractors are pure on `(event, current)`, and
        we always start from `[]`. Same row sequence → same final
        lists.
        """
        from event_journal import event_journal_reader
        import session_local_projection
        from orchs.base import _normalize_for_render

        # Collect all node sids in the tree (root + forks).
        node_sids: set[str] = set()
        def _collect(node: dict) -> None:
            sid = node.get("id")
            if sid:
                node_sids.add(sid)
            for f in node.get("forks", []):
                _collect(f)
        _collect(root)
        if not node_sids:
            return

        fingerprint = self._event_journal_fingerprint(root_id)
        cached = self._todo_projection_cache.get(root_id)
        if cached is not None and cached[0] == fingerprint:
            perf.record_count("session.hydrate_todos.cache_hit")
            self._todo_projection_cache.move_to_end(root_id)
            self._apply_cached_todo_projection(root, cached[1])
            return
        perf.record_count("session.hydrate_todos.cache_miss")

        # Single read — no sid_filter — then bucket per sid.
        try:
            with perf.timed("session.hydrate_todos.read_events"):
                all_rows, _, _ = event_journal_reader.read_events(
                    root_id, limit=200_000,
                )
        except Exception:
            return
        perf.record_count("session.hydrate_todos.rows", len(all_rows))

        def _payload_may_project(value: object) -> bool:
            if isinstance(value, str):
                return (
                    "TodoWrite" in value
                    or "TaskCreate" in value
                    or "TaskUpdate" in value
                    or "tool_result" in value
                    or "ALL_TASKS__DONE" in value
                )
            if isinstance(value, dict):
                block_type = value.get("type")
                if block_type == "tool_result":
                    return True
                if block_type == "tool_use" and value.get("name") in (
                    "TodoWrite",
                    "TaskCreate",
                    "TaskUpdate",
                ):
                    return True
                return any(_payload_may_project(child) for child in value.values())
            if isinstance(value, list):
                return any(_payload_may_project(child) for child in value)
            return False

        buckets: dict[str, list] = {sid: [] for sid in node_sids}
        for row in all_rows:
            sid = row.get("sid")
            if sid in buckets and _payload_may_project(row.get("data")):
                buckets[sid].append(row)

        projected_by_sid: dict[str, tuple[list, list]] = {}

        def _apply(node: dict) -> None:
            sid = node.get("id")
            if sid:
                rows = sorted(buckets.get(sid, []), key=lambda r: r.get("seq", 0))
                current_todos: list = []
                current_tasks: list = []
                with perf.timed("session.hydrate_todos.project_node"):
                    for row in rows:
                        etype = row.get("type")
                        if etype not in ("agent_message", "manager_event"):
                            continue
                        event = {"type": etype, "data": row.get("data") or {}}
                        normalized = _normalize_for_render(event)
                        fields = session_local_projection.project_event_fields(
                            normalized,
                            current_todos=current_todos,
                            current_tasks=current_tasks,
                        )
                        if "current_todos" in fields:
                            current_todos = list(fields.get("current_todos") or [])
                        if "current_tasks" in fields:
                            current_tasks = list(fields.get("current_tasks") or [])
                node["current_todos"] = current_todos
                node["current_tasks"] = current_tasks
                projected_by_sid[sid] = (current_todos, current_tasks)
            for f in node.get("forks", []):
                _apply(f)

        _apply(root)
        self._cache_todo_projection(root_id, fingerprint, projected_by_sid)

    def _cached(self, sid: str, *, hydrate_events: bool = False) -> Optional[dict]:
        """Return the live record for `sid` (a node within a cached root
        tree). Mutations to the returned dict propagate to the next
        persist call for the root."""
        root = self._load_root(sid, hydrate_events=hydrate_events)
        if root is None:
            return None
        return session_store._find_in_tree(root, sid)

    def _node_record_light(self, sid: str, rid: str) -> Optional[dict]:
        """A `{"kind": ...}`-bearing record for `sid`'s READ-ONLY kind
        gate, with ZERO disk I/O on the hot path and NO side effects.

        Resident root → the live in-memory node. Evicted root → the
        eviction-surviving `_kind_by_sid` cache (O(1), no disk). Only on
        a genuine cache miss (a node never indexed this process) does it
        fall back to `read_node_kind_record`, a pure side-effect-free
        disk read — NOT `get_root_tree`, which would migrate-persist and
        draft-seed (disk WRITES) on the loop thread. Never mutate the
        returned dict; for an evicted root it is not the cached tree."""
        root = self._roots.get(rid)
        if root is not None:
            return session_store._find_in_tree(root, sid)
        if sid in self._kind_by_sid:
            return {"kind": self._kind_by_sid[sid]}
        return session_store.read_node_kind_record(rid, sid)

    @staticmethod
    def _exchange_window(
        all_msgs: list[dict],
        exchange_count: int,
        msg_before_seq: Optional[int],
    ) -> list[dict]:
        """Return the message window for the last *exchange_count* user→assistant
        exchanges. An exchange starts at each ``role == "user"`` message and
        includes all subsequent messages until the next user message.

        Falls back to the last ``exchange_count * 2`` raw messages when the
        pool contains no user messages (assistant-only / recovery sessions),
        so pagination still limits initial load size."""
        pool = all_msgs
        if msg_before_seq is not None:
            pool = [m for m in all_msgs if (m.get("seq") or 0) < msg_before_seq]
        if not pool:
            return []
        # Walk backwards counting user messages to find the window start.
        user_count = 0
        start = len(pool)
        for i in range(len(pool) - 1, -1, -1):
            if pool[i].get("role") == "user":
                user_count += 1
                if user_count >= exchange_count:
                    start = i
                    break
        if user_count < exchange_count:
            if user_count == 0:
                # No user messages — fall back to raw count to limit load.
                return pool[-(exchange_count * 2):]
            start = 0
        return pool[start:]

    @staticmethod
    def _include_leading_assistant_initiator(
        all_msgs: list[dict],
        window: list[dict],
    ) -> list[dict]:
        if not window or window[0].get("role") != "assistant":
            return window
        first_seq = window[0].get("seq")
        if first_seq is None:
            return window
        initiator = None
        for msg in reversed(all_msgs):
            seq = msg.get("seq")
            if seq is None or seq >= first_seq:
                continue
            if msg.get("role") == "user":
                initiator = msg
                break
            if msg.get("role") == "assistant":
                break
        if initiator is None:
            return window
        return [initiator, *window]

    @staticmethod
    def _trim_tree_messages(
        tree: dict,
        msg_limit: int,
        msg_before_seq: Optional[int],
        exchange_count: Optional[int] = None,
    ) -> None:
        """Walk every node in *tree* and replace its ``messages`` with the
        paginated window. Stamps ``pagination`` metadata on each node.

        When *exchange_count* is set, pages by user→assistant exchanges
        instead of raw message count."""
        def _visit(node: dict) -> None:
            all_msgs = node.get("messages") or []
            total = len(all_msgs)

            if exchange_count is not None:
                window = SessionManager._exchange_window(
                    all_msgs, exchange_count, msg_before_seq,
                )
            elif msg_before_seq is None:
                window = all_msgs[-msg_limit:]
            else:
                older = [
                    m for m in all_msgs
                    if (m.get("seq") or 0) < msg_before_seq
                ]
                window = older[-msg_limit:]

            oldest_seq = None
            if window:
                seqs = [m.get("seq") for m in window if m.get("seq") is not None]
                if seqs:
                    oldest_seq = min(seqs)
            has_older = False
            if oldest_seq is not None:
                has_older = any(
                    (m.get("seq") or 0) < oldest_seq for m in all_msgs
                )

            node["messages"] = window
            node["pagination"] = {
                "total_messages": total,
                "oldest_loaded_seq": oldest_seq,
                "has_older": has_older,
            }

            for fork in node.get("forks") or []:
                _visit(fork)

        _visit(tree)

    @staticmethod
    def _native_only_tree(tree: dict) -> bool:
        stack = [tree]
        while stack:
            node = stack.pop()
            if (node.get("orchestration_mode") or "team") != "native":
                return False
            for m in node.get("messages") or []:
                if m.get("workers"):
                    return False
            stack.extend(node.get("forks") or [])
        return True

    @staticmethod
    def _event_ref(
        root_id: str, node_sid: str, msg_id: str, summary: dict,
    ) -> dict:
        return {
            "root_id": root_id,
            "sid": node_sid,
            "msg_id": msg_id,
            "seq_start": summary.get("seq_start"),
            "seq_end": summary.get("seq_end"),
            "byte_start": summary.get("byte_start"),
            "byte_end": summary.get("byte_end"),
        }

    def _native_event_summaries(
        self,
        root_id: str,
        node_sid: str,
        msg_ids: Optional[set[str]] = None,
    ) -> dict[str, dict]:
        from event_journal import event_journal_reader
        import render_stub
        return event_journal_reader.message_event_summaries(
            root_id,
            sid_filter=node_sid,
            msg_ids=msg_ids,
            tail=render_stub.STUB_TAIL,
        )

    @staticmethod
    def _merge_render_event(
        owner: dict, event: dict, *, replace_existing: bool = True,
    ) -> None:
        from orchs.base import _event_uuid

        events = owner.setdefault("events", [])
        ev_uuid = _event_uuid(event)
        if ev_uuid:
            for index, existing in enumerate(events):
                if _event_uuid(existing) == ev_uuid:
                    if replace_existing:
                        events[index] = event
                    return
        events.append(event)

    def _route_frontend_events_to_message_copy(
        self, msg: dict, events: list[dict],
    ) -> None:
        msg["events"] = []
        workers = {
            worker.get("delegation_id"): worker
            for worker in (msg.get("workers") or [])
            if isinstance(worker, dict) and worker.get("delegation_id")
        }
        live_worker_event_owners = {
            delegation_id
            for delegation_id, worker in workers.items()
            if worker.get("events")
        }
        for event in events:
            if not isinstance(event, dict):
                continue
            if event.get("type") != "worker_event":
                self._merge_render_event(msg, event)
                continue
            data = event.get("data") if isinstance(event.get("data"), dict) else {}
            worker = workers.get(data.get("delegation_id"))
            inner = data.get("event")
            if worker is None or not isinstance(inner, dict) or not inner:
                continue
            replace_existing = data.get("delegation_id") not in live_worker_event_owners
            self._merge_render_event(
                worker, inner, replace_existing=replace_existing,
            )

    def _hydrate_native_message_copy(
        self, root_id: str, node_sid: str, msg: dict,
    ) -> None:
        msg_id = msg.get("id")
        if not msg_id:
            return
        from event_journal import event_journal_reader
        import render_stub

        events = event_journal_reader.read_ws_events(
            root_id, sid_filter=node_sid, msg_id_filter=msg_id,
        )
        self._route_frontend_events_to_message_copy(msg, events)
        msg.pop("stub", None)
        summary = self._native_event_summaries(
            root_id, node_sid, {msg_id},
        ).get(msg_id)
        if summary:
            msg["event_ref"] = self._event_ref(root_id, node_sid, msg_id, summary)
        if not msg.get("isStreaming"):
            content = render_stub.message_output_text(msg)
            if content != (msg.get("content") or ""):
                msg["content"] = content

    # ── Reads ──────────────────────────────────────────────────────

    def _stamp_recovering_tree(self, node: Optional[dict]) -> None:
        """Walk a deep-copied tree/session/message-list and inject
        `isRecovering: true` on any message whose id is in the
        transient recovering set. Cheap no-op when the set is empty
        (the common case). Must be called on COPIED data — never on a
        live cache reference."""
        if not self._recovering_msg_ids or node is None:
            return
        recovering = self._recovering_msg_ids
        def visit(n: dict) -> None:
            for m in n.get("messages") or []:
                if m.get("id") in recovering:
                    m["isRecovering"] = True
            for f in n.get("forks") or []:
                visit(f)
        visit(node)

    def get(self, sid: str) -> Optional[dict]:
        """Return a deep copy of the live session, or None if unknown."""
        rid = self._root_id_for(sid)
        if rid is None:
            return None
        with self._lock_for_root(rid):
            root = self._load_root(sid, hydrate_events=False)
            if root is None:
                return None
            s = session_store._find_in_tree(root, sid)
            if s is None:
                return None
            out = copy.deepcopy(s)
            self._stamp_recovering_tree(out)
            return out

    def exists(self, sid: str) -> bool:
        rid = self._node_root_id.get(sid)
        if rid is not None:
            root = self._roots.get(rid)
            if root is not None:
                return session_store._find_in_tree(root, sid) is not None
        rid = session_store._loaded_root_id_for(sid)
        if rid is not None:
            self._node_root_id[sid] = rid
            return True
        if session_store.session_file_fingerprint(sid) is not None:
            self._node_root_id[sid] = sid
            self._node_root_missing_until.pop(sid, None)
            return True
        rid = session_store._resolve_root_id(sid)
        if rid is None:
            return False
        self._node_root_id[sid] = rid
        return True

    def get_field(self, sid: str, field: str) -> Any:
        """Read a single session-level field without deepcopy. Returns
        the field value (which may be a mutable list/dict from the live
        cache — caller MUST NOT mutate it), or None if session not found."""
        rid = self._root_id_for(sid)
        if rid is None:
            return None
        with self._lock_for_root(rid):
            root = self._load_root(sid, hydrate_events=False)
            if root is None:
                return None
            node = session_store._find_in_tree(root, sid)
            if node is None:
                return None
            return node.get(field)

    def get_fields(self, sid: str, fields: set[str] | tuple[str, ...] | list[str]) -> dict:
        rid = self._root_id_for(sid)
        if rid is None:
            return {}
        with self._lock_for_root(rid):
            root = self._load_root(sid, hydrate_events=False)
            if root is None:
                return {}
            node = session_store._find_in_tree(root, sid)
            if node is None:
                return {}
            return {field: copy.deepcopy(node.get(field)) for field in fields}

    def get_fields_many(
        self,
        sids: list[str] | tuple[str, ...],
        fields: set[str] | tuple[str, ...] | list[str],
    ) -> dict[str, dict]:
        by_root: dict[str, list[str]] = {}
        for sid in sids:
            rid = self._root_id_for(sid)
            if rid is not None:
                by_root.setdefault(rid, []).append(sid)
        out: dict[str, dict] = {}
        for rid, root_sids in by_root.items():
            with self._lock_for_root(rid):
                root = self._load_root(rid, hydrate_events=False)
                if root is None:
                    continue
                for sid in root_sids:
                    node = session_store._find_in_tree(root, sid)
                    if node is None:
                        continue
                    out[sid] = {
                        field: copy.deepcopy(node.get(field))
                        for field in fields
                    }
        return out

    def get_file_ref_context(self, sid: str) -> tuple[str | None, str]:
        rid = self._root_id_for(sid)
        if rid is None:
            return None, "primary"
        with self._lock_for_root(rid):
            root = self._load_root(sid, hydrate_events=False)
            node = session_store._find_in_tree(root, sid) if root else None
            if node is None:
                return None, "primary"
            return node.get("cwd"), node.get("node_id") or "primary"

    def get_lite(self, sid: str) -> Optional[dict]:
        """Return a deep copy with `msg.events` and
        `msg.workers[*].events` STRIPPED (replaced with empty lists).
        Same metadata as `get()`, ~100× cheaper deepcopy for sessions
        whose events lists dominate the in-memory size.

        Use for callers that read only session-level fields (`cwd`,
        `provider_id`, `name`, `draft_input`, etc.) or message
        metadata (`id`, `role`, `content`, `seq`, `timestamp`) but
        NEVER touch the events lists. Callers that DO read events
        (REST snapshot, WS messages_replay, content extraction)
        must keep using `get()` / `get_root_tree_paginated()`.

        Measured impact: `_ref_ctx_for_root` (called by `event_ingester`
        on every live ingest) was 200-800 ms on heavy sessions
        (`session_manager.get()` deepcopy of 13 MB hydrated tree).
        After switching to `get_lite()`: ~1 ms. Same for the REST
        `_require_session` gate.
        """
        rid = self._root_id_for(sid)
        if rid is None:
            return None
        with self._lock_for_root(rid):
            s = self._cached(sid, hydrate_events=False)
            if s is None:
                return None
            # Strip events from the live tree, snapshot, restore. Same
            # pattern as `session_store._strip_volatile_from_tree`'s
            # write path — atomic against torn deepcopy because the
            # per-root lock is held across the whole strip + copy +
            # restore. `_uid_idx` is popped too (same volatile field
            # rule). Worker-panel events ARE walked too — manager-mode
            # sessions can carry panel events that contribute to the
            # cache size, and consistency with the volatile-strip
            # invariant matters more than the minor walk cost.
            popped: list[tuple[dict, list]] = []
            popped_idx: list[tuple[dict, dict]] = []
            popped_anchor_cache: list[tuple[dict, dict]] = []
            stack = [s]
            while stack:
                node = stack.pop()
                for m in node.get("messages") or []:
                    ev = m.get("events")
                    if isinstance(ev, list) and ev:
                        popped.append((m, ev))
                        m["events"] = []
                    idx = m.pop("_uid_idx", None)
                    if isinstance(idx, dict):
                        popped_idx.append((m, idx))
                    anchor_cache = m.pop("_panel_anchor_cache", None)
                    if isinstance(anchor_cache, dict):
                        popped_anchor_cache.append((m, anchor_cache))
                    for w in m.get("workers") or []:
                        if not isinstance(w, dict):
                            continue
                        wev = w.get("events")
                        if isinstance(wev, list) and wev:
                            popped.append((w, wev))
                            w["events"] = []
                        widx = w.pop("_uid_idx", None)
                        if isinstance(widx, dict):
                            popped_idx.append((w, widx))
                for f in node.get("forks") or []:
                    stack.append(f)
            try:
                out = copy.deepcopy(s)
            finally:
                for owner, ev in popped:
                    owner["events"] = ev
                for owner, idx in popped_idx:
                    owner["_uid_idx"] = idx
                for owner, anchor_cache in popped_anchor_cache:
                    owner["_panel_anchor_cache"] = anchor_cache
            self._stamp_recovering_tree(out)
            return out

    def get_project_key(self, sid: str) -> tuple[str, str]:
        """Lightweight `(cwd, node_id)` accessor — avoids the full
        `copy.deepcopy` that `get()` does on the entire session tree.

        Hot path: called by `SessionWSBroadcaster._project_key_for` on
        EVERY `running_changed` / `unread_changed` / `seen_advanced`
        event so the WS frame can carry the per-project routing key.
        Under sustained ingest a single 12 MB session's deepcopy
        burns multi-millisecond on the event loop thread — a
        deepcopy storm in place of the previous `/api/projects`
        refetch storm. This accessor reads just the two fields
        under the per-root lock; no clone.

        INVARIANT: returns `("", node_id)` when the session is
        sidebar-hidden (`working_mode.should_hide_from_sidebar`) so
        the frontend can use `cwd === ""` as the "skip aggregate"
        signal — matching `_project_aggregates`'s filter
        (main.py:761) byte-for-byte. Returns `("", "primary")` when
        the sid is unknown (race vs. delete)."""
        import working_mode as _wm
        rid = self._root_id_for(sid)
        if rid is None:
            return self._project_key_cache.get(sid, ("", "primary"))
        lock = self._lock_for_root(rid)
        if not lock.acquire(blocking=False):
            return self._project_key_cache.get(sid, ("", "primary"))
        try:
            root = self._load_root(sid, hydrate_events=False)
            s = session_store._find_in_tree(root, sid) if root else None
            if s is None:
                return self._project_key_cache.get(sid, ("", "primary"))
            node_id = s.get("node_id") or "primary"
            if _wm.should_hide_from_sidebar(s):
                key = ("", node_id)
            else:
                key = (s.get("cwd") or "", node_id)
            self._project_key_cache[sid] = key
            return key
        finally:
            lock.release()

    def get_root_tree(self, sid: str) -> Optional[dict]:
        """Return a deep copy of the FULL root tree containing `sid`.
        Used by the API to hand the frontend the entire tree of a
        session in one shot."""
        rid = self._root_id_for(sid)
        if rid is None:
            return None
        if not self.hydrate_root_prepared(rid):
            return None
        with self._lock_for_root(rid):
            root = self._load_root(sid, hydrate_events=False)
            if root is None:
                return None
            out = copy.deepcopy(root)
            self._stamp_recovering_tree(out)
            return out

    def subtree_ids(self, sid: str) -> set[str]:
        """Set of session ids that `delete(sid)` would remove: `sid`
        plus every descendant fork. Empty if `sid` is unknown. Used by
        the delete handler to find which run dirs to reap."""
        rid = self._root_id_for(sid)
        if rid is None:
            return set()
        with self._lock_for_root(rid):
            root = self._load_root(sid, hydrate_events=False)
            if root is None:
                return set()
            node = session_store._find_in_tree(root, sid)
            if node is None:
                return set()
            return {sid, *(f["id"] for f in session_store._walk_forks(node) if f.get("id"))}

    def get_root_tree_paginated(
        self,
        sid: str,
        *,
        msg_limit: int = 50,
        msg_before_seq: Optional[int] = None,
        exchange_count: Optional[int] = None,
    ) -> Optional[dict]:
        """Return a deep copy of the root tree with messages trimmed to a
        window. Each node gets a ``pagination`` dict stamped on it.

        Trims messages BEFORE deepcopy to avoid copying the full message
        history (which can be multi-MB for long sessions).

        * ``exchange_count=N``: page by user→assistant exchanges (N pairs).
        * ``msg_before_seq=None`` (default): return the LAST ``msg_limit``
          messages per node (the tail — what the user sees on session open).
        * ``msg_before_seq=N``: return up to ``msg_limit`` messages whose
          ``seq < N`` (scroll-up / older-message loading).
        """
        rid = self._root_id_for(sid)
        if rid is None:
            return None
        if not self.hydrate_root_prepared(rid):
            return None
        with self._lock_for_root(rid):
            root = self._load_root(sid, hydrate_events=False)
            if root is None:
                return None
            # Snapshot messages per node so trim doesn't corrupt the cache.
            snapshots: list[tuple[dict, list]] = []
            def _snapshot(node: dict) -> None:
                msgs = node.get("messages")
                if msgs is not None:
                    snapshots.append((node, msgs))
                for f in node.get("forks") or []:
                    _snapshot(f)
            _snapshot(root)

            self._trim_tree_messages(
                root, msg_limit, msg_before_seq,
                exchange_count=exchange_count,
            )
            tree = copy.deepcopy(root)

            # Restore cache.
            for node, msgs in snapshots:
                node["messages"] = msgs
            self._stamp_recovering_tree(tree)
            return tree

    def get_root_tree_stubbed(
        self,
        sid: str,
        *,
        msg_limit: int = 50,
        exchange_count: Optional[int] = None,
    ) -> Optional[dict]:
        """Return a deep copy of the root tree with stubbed messages per node.
        Delegates to get_messages_since for each node to reuse the LRU cache."""
        return self.get_messages_since(
            sid, since_seq=0, limit=msg_limit,
            exchange_count=exchange_count, tree=True,
        )

    def get_root_tree_stubbed_with_cache_key(
        self,
        sid: str,
        *,
        msg_limit: int = 50,
        exchange_count: Optional[int] = None,
        known_root_id: Optional[str] = None,
    ) -> Optional[tuple[dict, tuple]]:
        rid = known_root_id or self._root_id_for(sid)
        if rid is None:
            return None
        with self._lock_for_root(rid):
            root = (
                self._load_root_impl(rid, hydrate_events=False)
                if known_root_id
                else self._load_root(sid, hydrate_events=False)
            )
            if root is None:
                return None
            return self._build_stubbed_tree(
                root, rid, msg_limit, exchange_count,
                return_cache_key=True,
            )

    def root_tree_stub_cache_key(
        self,
        sid: str,
        *,
        msg_limit: int = 50,
        exchange_count: Optional[int] = None,
    ) -> Optional[tuple]:
        rid = self._root_id_for(sid)
        if rid is None:
            return None
        with self._lock_for_root(rid):
            root = self._load_root(sid, hydrate_events=False)
            if root is None:
                return None
            return self._tree_stub_cache_key(
                root, rid, msg_limit, exchange_count,
            )

    def root_tree_stub_cache_key_for_root(
        self,
        root_id: str,
        *,
        msg_limit: int = 50,
        exchange_count: Optional[int] = None,
    ) -> Optional[tuple]:
        with self._lock_for_root(root_id):
            root = self._load_root_impl(root_id, hydrate_events=False)
            if root is None:
                return None
            return self._tree_stub_cache_key(
                root, root_id, msg_limit, exchange_count,
            )

    def get_message_full(
        self, node_sid: str, msg_id: str,
    ) -> Optional[dict]:
        """Deepcopy of a single message WITH full events — the lazy-expand
        fetch target. Native sessions read the message's events from
        events.jsonl on demand; manager sessions keep the historical
        hydrated-render-tree path."""
        rid = self._root_id_for(node_sid)
        if rid is None:
            return None
        with self._lock_for_root(rid):
            initial_root = self._load_root(node_sid, hydrate_events=False)
            if initial_root is None:
                return None
            native_only = self._native_only_tree(initial_root)
        if not native_only and not self.hydrate_root_prepared(rid):
            return None
        with self._lock_for_root(rid):
            root = self._load_root(node_sid, hydrate_events=False)
            if root is None:
                return None
            node = session_store._find_in_tree(root, node_sid)
            if node is None:
                return None
            for m in node.get("messages") or []:
                if m.get("id") == msg_id:
                    out = copy.deepcopy(m)
                    if native_only and out.get("role") == "assistant":
                        self._hydrate_native_message_copy(rid, node_sid, out)
                    if (
                        self._recovering_msg_ids
                        and msg_id in self._recovering_msg_ids
                    ):
                        out["isRecovering"] = True
                    return out
            return None

    def get_messages_since(
        self,
        node_sid: str,
        since_seq: int = 0,
        limit: int = 50,
        *,
        exchange_count: Optional[int] = None,
        tree: bool = False,
    ) -> Optional[dict]:
        """Return stubbed messages for a session node, LRU-cached by next_seq.

        Single method for both WS delta replay and REST full-tree loads:
        - tree=False (default): returns ``{messages, next_seq}`` for one
          node. WS uses this with since_seq > 0 for delta replay.
        - tree=True: returns the full root tree (root + forks) with
          per-node paginated stubbed messages and pagination metadata.
          REST uses this.

        Cache is invalidated when the node's next_seq changes.
        """
        rid = self._root_id_for(node_sid)
        if rid is None:
            return None
        lock = self._lock_for_root(rid)
        lock_wait_start = time.perf_counter()
        lock.acquire()
        lock_wait_ms = (time.perf_counter() - lock_wait_start) * 1000
        if lock_wait_ms >= 20:
            logger.info(
                "get_messages_since %s: lock_wait=%.1fms tree=%s since=%s",
                node_sid[:8], lock_wait_ms, tree, since_seq,
            )
        perf.record("session.get_messages_since.lock_wait", lock_wait_ms)
        try:
            load_start = time.perf_counter()
            root = self._load_root(node_sid, hydrate_events=False)
            load_ms = (time.perf_counter() - load_start) * 1000
            perf.record("session.get_messages_since.load_root", load_ms)
            if load_ms >= 20:
                logger.info(
                    "get_messages_since %s: load_root=%.1fms tree=%s",
                    node_sid[:8], load_ms, tree,
                )
            if root is None:
                return None

            if tree:
                tree_start = time.perf_counter()
                out = self._build_stubbed_tree(
                    root, rid, limit, exchange_count,
                )
                perf.record(
                    "session.get_messages_since.build_tree",
                    (time.perf_counter() - tree_start) * 1000,
                )
                return out

            # Single-node path.
            node = session_store._find_in_tree(root, node_sid)
            if node is None:
                return None
            if since_seq > 0:
                snapshot_start = time.perf_counter()
                delta = self._get_cached_messages_window(
                    node_sid, rid, node, since_seq=since_seq, limit=limit,
                )
                snapshot_ms = (time.perf_counter() - snapshot_start) * 1000
                perf.record("session.get_messages_since.delta_window", snapshot_ms)
                if snapshot_ms >= 20:
                    logger.info(
                        "get_messages_since %s: delta_window=%.1fms since=%s",
                        node_sid[:8], snapshot_ms, since_seq,
                    )
                return delta
            snapshot_start = time.perf_counter()
            snapshot = self._get_cached_snapshot(node_sid, rid, node)
            snapshot_ms = (time.perf_counter() - snapshot_start) * 1000
            perf.record("session.get_messages_since.snapshot", snapshot_ms)
            if snapshot_ms >= 20:
                logger.info(
                    "get_messages_since %s: snapshot=%.1fms",
                    node_sid[:8], snapshot_ms,
                )
            if snapshot is None:
                return None
            all_msgs = snapshot["messages"]
            next_seq = snapshot["next_seq"]
            delta = [m for m in all_msgs if (m.get("seq") or 0) >= since_seq]
            delta = delta[-limit:]
            delta = self._include_leading_assistant_initiator(all_msgs, delta)
            return {"messages": delta, "next_seq": next_seq}
        finally:
            lock.release()

    def _get_cached_snapshot(
        self, node_sid: str, rid: str, node: dict,
    ) -> Optional[dict]:
        """Get or compute the LRU-cached stubbed snapshot for one node.
        Caller MUST hold the per-root lock.

        Cache key is (next_seq, render_event_max_seq, reconcile_gen) so
        UI/audit rows in events.jsonl do not force message snapshot
        rebuilds, while async reconcile projection still invalidates stale
        pre-reconcile snapshots."""
        from event_ingester import event_ingester
        cur_seq = node.get("next_seq") or 0
        gen = self._reconcile_gen.get(rid, 0)
        cached = self._since_cache.get(node_sid)
        event_max_seq: int | None = None
        seq_ms = 0.0
        if cached is not None:
            seq_start = time.perf_counter()
            event_max_seq = event_ingester.render_seq_for_sid(rid, node_sid)
            seq_ms = (time.perf_counter() - seq_start) * 1000
            perf.record("session.get_cached_snapshot.render_seq", seq_ms)
            cache_key = (cur_seq, event_max_seq, gen)
            if cached[0] == cache_key:
                self._since_cache.move_to_end(node_sid)
                logger.debug(
                    "_since_cache HIT %s key=%s",
                    node_sid[:8], cache_key,
                )
                return cached[1]
        old_key = cached[0] if cached is not None else None
        if cached is not None:
            logger.info(
                "_since_cache MISS %s old_key=%s (gen=%d render_max=%d render_seq=%.1fms)",
                node_sid[:8], old_key, gen, event_max_seq or 0, seq_ms,
            )
        start = time.perf_counter()
        snapshot = self._compute_messages_snapshot(node_sid, rid, node)
        if snapshot is None:
            return None
        event_max_seq = int(snapshot.pop("_render_max_seq", 0) or 0)
        cache_key = (cur_seq, event_max_seq, gen)
        snapshot_bytes = _jsonish_byte_size(snapshot)
        perf.record("session.since_cache.snapshot_bytes", snapshot_bytes)
        elapsed_ms = (time.perf_counter() - start) * 1000
        if elapsed_ms >= 20:
            logger.info(
                "_since_cache BUILD %s: %.1fms msgs=%d bytes=%d next_seq=%s key=%s",
                node_sid[:8], elapsed_ms,
                len(snapshot.get("messages") or []),
                snapshot_bytes,
                snapshot.get("next_seq"),
                cache_key,
            )
        self._remember_since_cache_snapshot(
            node_sid, cache_key, snapshot, snapshot_bytes,
        )
        return snapshot

    def _drop_window_cache_for_sids(self, sids: set[str]) -> None:
        if not sids:
            return
        for key in list(self._window_cache):
            if key[0] in sids:
                self._window_cache.pop(key, None)

    def _get_cached_messages_window(
        self,
        node_sid: str,
        rid: str,
        node: dict,
        *,
        since_seq: int,
        limit: int,
    ) -> Optional[dict]:
        from event_ingester import event_ingester

        cur_seq = int(node.get("next_seq") or 0)
        render_seq = event_ingester.render_seq_for_sid(rid, node_sid)
        gen = int(self._reconcile_gen.get(rid, 0))
        recovering_key = tuple(sorted(self._recovering_msg_ids))
        cache_key = (
            node_sid,
            int(since_seq),
            int(limit),
            cur_seq,
            int(render_seq),
            gen,
            recovering_key,
        )
        cached = self._window_cache.get(cache_key)
        if cached is not None:
            perf.record("session.window_cache.hit", 1.0)
            self._window_cache.move_to_end(cache_key)
            return _copy_jsonish(cached)
        perf.record("session.window_cache.miss", 1.0)
        delta = self._compute_messages_window(
            node_sid,
            rid,
            node,
            since_seq=since_seq,
            limit=limit,
        )
        if delta is None:
            return None
        self._window_cache[cache_key] = _copy_jsonish(delta)
        if len(self._window_cache) > self._window_cache_max:
            self._window_cache.popitem(last=False)
        return delta

    def _tree_stub_cache_key(
        self,
        root: dict,
        rid: str,
        msg_limit: int,
        exchange_count: Optional[int],
    ) -> tuple[str, int, Optional[int], tuple]:
        from event_ingester import event_ingester

        node_keys = []
        render_seq_by_sid = event_ingester.render_seq_by_sid(rid)

        def _visit(node: dict) -> None:
            node_sid = str(node.get("id") or "")
            if node_sid:
                node_keys.append((
                    node_sid,
                    int(node.get("next_seq") or 0),
                    node.get("updated_at"),
                    node.get("draft_input") or "",
                    tuple(
                        item.get("id")
                        for item in (node.get("queued_prompts") or [])
                        if isinstance(item, dict)
                    ),
                    bool(node.get("is_running")),
                    bool(node.get("right_panel_open")),
                    int(render_seq_by_sid.get(node_sid, 0)),
                    int(self._reconcile_gen.get(rid, 0)),
                ))
            for child in node.get("forks") or []:
                if isinstance(child, dict):
                    _visit(child)

        _visit(root)
        recovering_key = tuple(sorted(self._recovering_msg_ids))
        return (
            rid,
            msg_limit,
            exchange_count,
            tuple(node_keys) + (recovering_key,),
        )

    def _drop_tree_stub_attached_cache_for_root(self, rid: str) -> None:
        for key in list(self._tree_stub_attached_cache):
            tree_key = key[0]
            if tree_key and tree_key[0] == rid:
                self._tree_stub_attached_cache.pop(key, None)

    def _drop_since_cache_entry(self, node_sid: str) -> None:
        cached = self._since_cache.pop(node_sid, None)
        if cached is None:
            self._since_cache_bytes.pop(node_sid, None)
            return
        bytes_used = self._since_cache_bytes.pop(node_sid, 0)
        self._since_cache_total_bytes = max(
            0, self._since_cache_total_bytes - bytes_used,
        )

    def _remember_since_cache_snapshot(
        self,
        node_sid: str,
        cache_key: tuple[int, int, int],
        snapshot: dict,
        snapshot_bytes: int,
    ) -> None:
        self._drop_since_cache_entry(node_sid)
        if snapshot_bytes > _SINCE_CACHE_MAX_BYTES:
            perf.record_count("session.since_cache.skip_oversize", 1)
            perf.record("session.since_cache.bytes", self._since_cache_total_bytes)
            return
        self._since_cache[node_sid] = (cache_key, snapshot)
        self._since_cache_bytes[node_sid] = snapshot_bytes
        self._since_cache_total_bytes += snapshot_bytes
        self._since_cache.move_to_end(node_sid)
        evicted = 0
        while (
            len(self._since_cache) > self._since_cache_max
            or self._since_cache_total_bytes > _SINCE_CACHE_MAX_BYTES
        ):
            old_sid = next(iter(self._since_cache))
            self._drop_since_cache_entry(old_sid)
            evicted += 1
        if evicted:
            perf.record_count("session.since_cache.evicted_entries", evicted)
        perf.record("session.since_cache.bytes", self._since_cache_total_bytes)

    def _build_stubbed_tree(
        self,
        root: dict,
        rid: str,
        msg_limit: int,
        exchange_count: Optional[int],
        *,
        return_cache_key: bool = False,
    ) -> Optional[dict] | Optional[tuple[dict, tuple]]:
        """Build a full tree copy with per-node stubbed messages from cache.
        Caller MUST hold the per-root lock."""
        cache_key = self._tree_stub_cache_key(
            root, rid, msg_limit, exchange_count,
        )
        root_events_version = self._root_events_version_for_tree(rid)
        attached_cache_key = (cache_key, root_events_version)
        attached_cached = self._tree_stub_attached_cache.get(attached_cache_key)
        if attached_cached is not None:
            perf.record("session.stubbed_tree_attached_cache.hit", 1.0)
            self._tree_stub_attached_cache.move_to_end(attached_cache_key)
            tree = _copy_jsonish(attached_cached)
            return (tree, cache_key) if return_cache_key else tree
        perf.record("session.stubbed_tree_attached_cache.miss", 1.0)
        cached = self._tree_stub_cache.get(cache_key)
        if cached is not None:
            perf.record("session.stubbed_tree_cache.hit", 1.0)
            self._tree_stub_cache.move_to_end(cache_key)
            tree = _copy_jsonish(cached)
            self._attach_root_events_to_stubbed_tree(tree, rid)
            self._cache_attached_stubbed_tree(attached_cache_key, tree)
            return (tree, cache_key) if return_cache_key else tree
        perf.record("session.stubbed_tree_cache.miss", 1.0)

        def _copy_node(node: dict) -> dict:
            out = {k: v for k, v in node.items() if k != "messages"}
            out["messages"] = []
            out["forks"] = [
                _copy_node(f) for f in node.get("forks") or []
            ]
            return out
        tree = _copy_node(root)
        attached = 0

        def _attach(node_src: dict, node_dst: dict) -> None:
            nonlocal attached
            node_sid = node_src.get("id")
            if node_sid:
                attached += 1
                snapshot = self._get_cached_snapshot(
                    node_sid, rid, node_src,
                )
                if snapshot is not None:
                    all_msgs = snapshot["messages"]
                    total = len(all_msgs)
                    if exchange_count is not None:
                        window = self._exchange_window(
                            all_msgs, exchange_count, None,
                        )
                    else:
                        window = all_msgs[-msg_limit:]
                    oldest_seq = None
                    if window:
                        seqs = [
                            m.get("seq") for m in window
                            if m.get("seq") is not None
                        ]
                        if seqs:
                            oldest_seq = min(seqs)
                    has_older = False
                    if oldest_seq is not None:
                        has_older = any(
                            (m.get("seq") or 0) < oldest_seq
                            for m in all_msgs
                        )
                    node_dst["messages"] = window
                    node_dst["pagination"] = {
                        "total_messages": total,
                        "oldest_loaded_seq": oldest_seq,
                        "has_older": has_older,
                    }
                    node_dst["next_seq"] = snapshot["next_seq"]
            for f_src, f_dst in zip(
                node_src.get("forks") or [],
                node_dst.get("forks") or [],
            ):
                _attach(f_src, f_dst)
        _attach(root, tree)
        self._stamp_recovering_tree(tree)
        self._tree_stub_cache[cache_key] = _copy_jsonish(tree)
        if len(self._tree_stub_cache) > self._tree_stub_cache_max:
            self._tree_stub_cache.popitem(last=False)
        self._attach_root_events_to_stubbed_tree(tree, rid)
        self._cache_attached_stubbed_tree(attached_cache_key, tree)
        return (tree, cache_key) if return_cache_key else tree

    def _root_events_version_for_tree(self, rid: str) -> int:
        from event_ingester import event_ingester

        return event_ingester.root_events_version(rid)

    def _cache_attached_stubbed_tree(self, key: tuple, tree: dict) -> None:
        self._tree_stub_attached_cache[key] = _copy_jsonish(tree)
        if len(self._tree_stub_attached_cache) > self._tree_stub_cache_max:
            self._tree_stub_attached_cache.popitem(last=False)

    def _attach_root_events_to_stubbed_tree(self, tree: dict, rid: str) -> None:
        from event_ingester import event_ingester

        root_events_start = time.perf_counter()
        root_events_by_sid = event_ingester.root_events_by_sid(rid)
        root_events_ms = (time.perf_counter() - root_events_start) * 1000
        attached = 0

        def _visit(node: dict) -> None:
            nonlocal attached
            node_sid = node.get("id")
            if isinstance(node_sid, str):
                root_events = root_events_by_sid.get(node_sid) or []
                if root_events:
                    node["root_events"] = root_events
                    attached += 1
                else:
                    node.pop("root_events", None)
            for child in node.get("forks") or []:
                if isinstance(child, dict):
                    _visit(child)

        _visit(tree)
        if root_events_ms >= 20:
            logger.info(
                "stubbed_tree %s: root_events_sids=%d attached=%d root_events=%.1fms",
                rid[:8], len(root_events_by_sid), attached, root_events_ms,
            )

    def _compute_messages_snapshot(
        self, node_sid: str, rid: str, node: dict,
    ) -> Optional[dict]:
        """Build a full stubbed message snapshot for a session node.
        Called only on cache miss. Caller MUST hold the per-root lock.

        One JSONL summary scan gives byte offsets for every message.
        Completed assistant messages get stubbed from summaries.
        Streaming messages get events via byte-range seek.
        """
        use_journal_summaries = True
        all_msgs = node.get("messages") or []
        next_seq = node.get("next_seq") or 0
        if not all_msgs:
            return {"messages": [], "next_seq": next_seq}

        import render_stub
        summaries_start = time.perf_counter()
        summaries = (
            self._native_event_summaries(rid, node_sid)
            if use_journal_summaries else {}
        )
        from event_ingester import event_ingester
        render_max_seq = event_ingester.render_seq_for_sid(rid, node_sid)
        summaries_ms = (time.perf_counter() - summaries_start) * 1000
        perf.record("session.compute_snapshot.summaries", summaries_ms)

        copied = []
        from event_journal import event_journal_reader

        def _copy_assistant_for_snapshot(m: dict, *, is_streaming: bool) -> dict:
            out = {
                k: v for k, v in m.items()
                if k not in ("events", "_uid_idx", messages_delta_compaction.PRECOMPUTED_REVISION_KEY)
            }
            out["events"] = []
            workers = []
            for worker in m.get("workers") or []:
                if not isinstance(worker, dict):
                    continue
                wc = {
                    k: v for k, v in worker.items()
                    if k not in ("events", "_uid_idx")
                }
                wc["events"] = (
                    copy.deepcopy(worker.get("events") or [])
                    if is_streaming else []
                )
                workers.append(wc)
            out["workers"] = workers
            return out

        copy_start = time.perf_counter()
        for m in all_msgs:
            is_streaming = bool(m.get("isStreaming"))
            if use_journal_summaries and m.get("role") == "assistant":
                copied.append(_copy_assistant_for_snapshot(m, is_streaming=is_streaming))
            elif m.get("role") != "assistant" or is_streaming:
                copied.append(copy.deepcopy(m))
            else:
                mc = {**m}
                mc["workers"] = [
                    {**w} for w in (m.get("workers") or [])
                ]
                copied.append(mc)
        copy_ms = (time.perf_counter() - copy_start) * 1000
        perf.record("session.compute_snapshot.copy_messages", copy_ms)

        hydrate_start = time.perf_counter()
        for m in copied:
            if m.get("role") != "assistant":
                continue
            msg_id = m.get("id")
            if not msg_id:
                continue
            is_streaming = bool(m.get("isStreaming"))
            if use_journal_summaries:
                summary = summaries.get(msg_id, {})
                if is_streaming and summary:
                    journal_events = event_journal_reader.read_frontend_events(
                        rid,
                        fork_id=node_sid if node_sid != rid else None,
                        message_id=msg_id,
                        summary=summary,
                    )
                    self._route_frontend_events_to_message_copy(m, journal_events)
                    m["event_ref"] = self._event_ref(
                        rid, node_sid, msg_id, summary,
                    )
                elif is_streaming:
                    m["events"] = []
                else:
                    m["events"] = []
                    m["stub"] = {
                        "event_count": summary.get("event_count", 0),
                        "last_events": _copy_jsonish(
                            summary.get("last_events") or []
                        ),
                    }
                    if summary:
                        m["event_ref"] = self._event_ref(
                            rid, node_sid, msg_id, summary,
                        )
            elif not is_streaming:
                render_stub.stub_message_inplace(m)
        hydrate_ms = (time.perf_counter() - hydrate_start) * 1000
        perf.record("session.compute_snapshot.hydrate_events", hydrate_ms)

        if self._recovering_msg_ids:
            recovering_start = time.perf_counter()
            for m in copied:
                if m.get("id") in self._recovering_msg_ids:
                    m["isRecovering"] = True
            perf.record(
                "session.compute_snapshot.recovering_stamp",
                (time.perf_counter() - recovering_start) * 1000,
            )
        if summaries_ms >= 20 or copy_ms >= 20 or hydrate_ms >= 20:
            logger.info(
                "compute_snapshot %s: summaries=%.1fms copy=%.1fms hydrate=%.1fms msgs=%d render_max=%d",
                node_sid[:8], summaries_ms, copy_ms, hydrate_ms,
                len(all_msgs), render_max_seq,
            )
        return {
            "messages": copied,
            "next_seq": next_seq,
            "_render_max_seq": render_max_seq,
        }

    def _compute_messages_window(
        self,
        node_sid: str,
        rid: str,
        node: dict,
        *,
        since_seq: int,
        limit: int,
    ) -> Optional[dict]:
        all_msgs = node.get("messages") or []
        next_seq = node.get("next_seq") or 0
        if not all_msgs:
            return {"messages": [], "next_seq": next_seq}

        window = [
            m for m in all_msgs
            if (m.get("seq") or 0) >= since_seq
        ][-limit:]
        if not window:
            return {"messages": [], "next_seq": next_seq}

        import render_stub
        from event_journal import event_journal_reader

        summary_ids = {
            str(m.get("id") or "")
            for m in window
            if m.get("role") == "assistant" and m.get("id")
        }
        summaries = self._native_event_summaries(
            rid, node_sid, summary_ids,
        )
        copied = []

        for m in window:
            is_streaming = bool(m.get("isStreaming"))
            if m.get("role") == "assistant":
                out = {
                    k: v for k, v in m.items()
                    if k not in ("events", "_uid_idx", messages_delta_compaction.PRECOMPUTED_REVISION_KEY)
                }
                out["events"] = []
                workers = []
                for worker in m.get("workers") or []:
                    if not isinstance(worker, dict):
                        continue
                    wc = {
                        k: v for k, v in worker.items()
                        if k not in ("events", "_uid_idx")
                    }
                    wc["events"] = (
                        copy.deepcopy(worker.get("events") or [])
                        if is_streaming else []
                    )
                    workers.append(wc)
                out["workers"] = workers
                copied.append(out)
            elif is_streaming:
                copied.append(copy.deepcopy(m))
            else:
                copied.append(copy.deepcopy(m))

        for m in copied:
            if m.get("role") != "assistant":
                continue
            msg_id = m.get("id")
            if not msg_id:
                continue
            is_streaming = bool(m.get("isStreaming"))
            summary = summaries.get(msg_id, {})
            if is_streaming and summary:
                journal_events = event_journal_reader.read_frontend_events(
                    rid,
                    fork_id=node_sid if node_sid != rid else None,
                    message_id=msg_id,
                    summary=summary,
                )
                self._route_frontend_events_to_message_copy(m, journal_events)
                m["event_ref"] = self._event_ref(rid, node_sid, msg_id, summary)
            elif is_streaming:
                m["events"] = []
            else:
                m["events"] = []
                m["stub"] = {
                    "event_count": summary.get("event_count", 0),
                    "last_events": _copy_jsonish(summary.get("last_events") or []),
                }
                if summary:
                    m["event_ref"] = self._event_ref(rid, node_sid, msg_id, summary)

        if self._recovering_msg_ids:
            for m in copied:
                if m.get("id") in self._recovering_msg_ids:
                    m["isRecovering"] = True
        return {"messages": copied, "next_seq": next_seq}

    def get_messages_before(
        self,
        node_sid: str,
        before_seq: int,
        limit: int = 50,
        exchange_count: Optional[int] = None,
    ) -> Optional[dict]:
        """Load older messages for a specific node without deep-copying
        the whole tree. Returns ``{messages, has_older, oldest_loaded_seq,
        total_messages}``.

        When *exchange_count* is set, pages by user→assistant exchanges
        instead of raw message count."""
        rid = self._root_id_for(node_sid)
        if rid is None:
            return None
        with self._lock_for_root(rid):
            root = self._load_root(node_sid, hydrate_events=False)
            if root is None:
                return None
            node = session_store._find_in_tree(root, node_sid)
            if node is None:
                return None
            all_msgs = node.get("messages") or []
            total = len(all_msgs)
            if exchange_count is not None:
                older = self._exchange_window(
                    all_msgs, exchange_count, before_seq,
                )
            else:
                older = [
                    m for m in all_msgs
                    if (m.get("seq") or 0) < before_seq
                ]
                older = older[-limit:]
            oldest_seq = None
            if older:
                seqs = [m.get("seq") for m in older if m.get("seq") is not None]
                if seqs:
                    oldest_seq = min(seqs)
            has_older = False
            if oldest_seq is not None:
                has_older = any(
                    (m.get("seq") or 0) < oldest_seq for m in all_msgs
                )
            copied = copy.deepcopy(older)
            # Older messages are never the latest turn → always stubbed
            # for lazy event fetch. Stubs the already-copied msgs in place
            # (no live mutation). Full events load on expand.
            summary_ids = {
                str(m.get("id") or "")
                for m in older
                if m.get("role") == "assistant" and m.get("id")
            }
            summaries = self._native_event_summaries(
                rid, node_sid, summary_ids,
            )
            for m in copied:
                if m.get("role") == "assistant" and not m.get("isStreaming"):
                    msg_id = m.get("id")
                    summary = summaries.get(msg_id or "", {})
                    m["events"] = []
                    m["stub"] = {
                        "event_count": summary.get("event_count", 0),
                        "last_events": _copy_jsonish(
                            summary.get("last_events") or []
                        ),
                    }
                    if msg_id and summary:
                        m["event_ref"] = self._event_ref(
                            rid, node_sid, msg_id, summary,
                        )
            if self._recovering_msg_ids:
                for m in copied:
                    if m.get("id") in self._recovering_msg_ids:
                        m["isRecovering"] = True
            return {
                "messages": copied,
                "has_older": has_older,
                "oldest_loaded_seq": oldest_seq,
                "total_messages": total,
            }

    def get_ref(self, sid: str) -> Optional[dict]:
        """Return the live cached node reference. Caller MUST NOT mutate."""
        rid = self._root_id_for(sid)
        if rid is None:
            return None
        with self._lock_for_root(rid):
            return self._cached(sid)

    @contextmanager
    def live_tree(self, sid: str):
        """Lease the live root tree while holding its owner lock."""
        rid = self._root_id_for(sid)
        if rid is None:
            yield None
            return
        with self._lock_for_root(rid):
            root = self._ensure_root_loaded(rid)
            if root is None or session_store._find_in_tree(root, sid) is None:
                yield None
                return
            yield root

    def set_updated_at(self, sid: str, value: str) -> None:
        """Set the root's `updated_at` to an explicit value (NOT a bump).

        Used by run_recovery's re-ingestion to repair `updated_at` to the
        session's real last-activity timestamp, instead of leaving a stale
        or spuriously-bumped value. Must run inside the caller's
        `bump_updated_at=False` batch so the authoritative value survives
        the coalesced persist (a bump would overwrite it with wall-clock)."""
        if not value:
            return
        rid = self._root_id_for(sid)
        if rid is None:
            return
        with self._lock_for_root(rid):
            root = self._cached(sid)
            if root is not None:
                root["updated_at"] = value

    def list(self) -> list[dict]:
        """Return the sidebar summary of every root session."""
        return session_store.list_sessions()

    def ordered_summary_ids(self, sort_by: str) -> list[str]:
        return session_store.ordered_session_summary_ids(sort_by)

    def iter_all(self):
        """Yield every session record (root + every embedded fork,
        depth-first). Returns deep copies so callers can iterate without
        racing concurrent mutations. Used by session_watcher and
        run_recovery to walk the whole session universe regardless of
        nesting."""
        for node in session_store.iter_all_sessions():
            yield copy.deepcopy(node)

    def has_any_queued_prompts(self) -> bool:
        return any(count > 0 for count in self._queued_prompt_counts_by_sid.values())

    def queued_prompt_count(self, sid: str) -> int:
        """Queued-prompt count from the queue projection — cheap read that
        avoids hydrating the session root just to learn the queue is empty."""
        return self._queued_prompt_counts_by_sid.get(sid, 0)

    def rebuild_queued_prompt_counts(self) -> None:
        import session_queue_projection

        self._queued_prompt_counts_by_sid = session_queue_projection.queued_counts()

    def _set_queued_prompt_count(self, sid: str, queued_count: int) -> None:
        if queued_count:
            self._queued_prompt_counts_by_sid[sid] = queued_count
            return
        self._queued_prompt_counts_by_sid.pop(sid, None)

    @staticmethod
    def _project_queue_record(session: dict) -> Optional[dict]:
        import session_queue_projection

        return session_queue_projection.project_session(session)

    @staticmethod
    def _upsert_queue_record(record: Optional[dict]) -> None:
        if record is None:
            return
        import session_queue_projection

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            session_queue_projection.upsert_record(record)
            return
        _submit_queue_projection_record(record)

    def _queue_projection_enricher(
        self, holder: dict[str, Optional[dict]], *, include_queued_prompts: bool,
    ) -> Callable[[dict], dict]:
        def _enrich(session: dict) -> dict:
            holder["record"] = self._project_queue_record(session)
            if include_queued_prompts:
                return {"queued_prompts": list(session.get("queued_prompts") or [])}
            return {}

        return _enrich

    # ── Batch ──────────────────────────────────────────────────────

    @contextmanager
    def batch(self, sid: str, *, bump_updated_at: bool = True):
        """Hold the per-root lock across multiple typed mutations and
        defer the disk persist to a single write at exit. Listeners
        still fire per-mutation; only the disk write is coalesced.
        Re-entrant: nested batches on the same root are no-ops at the
        inner level (outermost owns the persist).
        """
        rid = self._root_id_for(sid)
        if rid is None:
            raise KeyError(sid)
        with self._lock_for_root(rid):
            sess = self._cached(sid)
            if sess is None:
                raise KeyError(sid)
            if rid in self._batches:
                yield
                return
            ctx = {"bump_updated_at": bump_updated_at}
            self._batches[rid] = ctx
            try:
                yield
                self._persist_root(rid, bump=ctx["bump_updated_at"])
            finally:
                self._batches.pop(rid, None)

    @contextmanager
    def message_batch(
        self,
        sid: str,
        msg_id: str,
        *,
        bump_updated_at: bool = True,
        hydrate_events: bool = True,
    ):
        rid = self._root_id_for(sid)
        if rid is None:
            raise KeyError(sid)
        if hydrate_events and not self.hydrate_root_prepared(rid):
            raise RuntimeError(f"failed to hydrate {rid}")
        with self._lock_for_root(rid):
            root = self._load_root(sid, hydrate_events=False)
            node = _find_message_node(root, msg_id) if root else None
            if node is None:
                raise KeyError(msg_id)
            msg = _find_message(node, msg_id)
            if msg is None:
                raise KeyError(msg_id)
            if rid in self._batches:
                yield node, msg
                return
            ctx = {"bump_updated_at": bump_updated_at}
            self._batches[rid] = ctx
            try:
                yield node, msg
                self._persist_root(rid, bump=ctx["bump_updated_at"])
            finally:
                self._batches.pop(rid, None)

    def reload_root_from_disk(self, root_id: str) -> None:
        """Evict the in-memory root for `root_id` and discard any pending
        debounced persist so the next access cold-loads the current
        on-disk `<root_id>.json`.

        Used by re-digest rollback (`redigest_backup.RedigestBackup`)
        after the on-disk file has been restored from a backup: the live
        in-memory root is half-mutated and `_persist_pending` may hold
        that mutated state — `_load_root`'s cold path would flush it
        over the restored file, silently undoing the rollback. Discarding
        the pending persist here closes that trap.

        Caller must NOT already hold `_lock_for_root(root_id)`."""
        with self._lock_for_root(root_id):
            cached = self._roots.pop(root_id, None)
            with _persist_state_lock:
                _persist_pending.pop(root_id, None)
                _cancel_persist_deadline_unlocked(root_id)
            if cached is not None:
                self._drop_cached_root_for_reload(root_id, cached)
            self._root_file_checked_at[root_id] = 0.0

    def _mutation_miss(
        self, sid: str, kind: Optional[str], branch: str, *, strict: bool,
    ) -> None:
        """A state mutation could not find its target session.

        Silent-swallow of a mutator miss is what let a turn run against a
        phantom assistant message (turn-loss incident): every miss is now
        loud. Logs ERROR (deduped per (sid, kind) with a short TTL so a
        deleted-while-active session doesn't emit one line per mutation)
        and, when `strict`, raises KeyError — the same contract as
        `batch()`/`message_batch()`.

        The rid-miss branch distinguishes a poisoned negative root cache
        from a fresh resolver miss: the two need different follow-ups
        (cache-invalidation bug vs genuinely missing session file).
        """
        if branch == "rid-miss":
            neg_until = self._node_root_missing_until.get(sid, 0.0)
            remaining = neg_until - time.monotonic()
            if remaining > 0:
                detail = (
                    f"root resolve failed: negative-cache hit "
                    f"(ttl_remaining={remaining:.1f}s)"
                )
            else:
                detail = "root resolve failed: fresh resolver miss"
        else:
            loaded = self._roots.get(self._node_root_id.get(sid, "")) is not None
            detail = (
                "node lookup failed: root loaded but sid not in tree"
                if loaded else "node lookup failed: root load returned None"
            )
        msg = (
            f"session mutation dropped: sid={sid} kind={kind} "
            f"branch={branch} — {detail}"
        )
        now = time.monotonic()
        key = (sid, kind)
        if now - self._mutation_miss_logged_at.get(key, 0.0) > 60.0:
            self._mutation_miss_logged_at[key] = now
            logger.error(msg)
        if strict:
            raise KeyError(msg)

    def _run(
        self,
        sid: str,
        mutate: Callable[[dict], None],
        change: dict,
        *,
        bump_updated_at: bool = True,
        enrich: Optional[Callable[[dict], dict]] = None,
        hydrate_events: bool = True,
        strict: bool = False,
    ) -> Optional[dict]:
        """Mutate `sid`'s session, persist, fire listener.

        `enrich`, when set, is called with the post-mutation session
        snapshot and returns a dict of extra fields merged into the
        `change` dict before firing. Use it to ship full post-mutation
        collections (e.g. inline_tags after a tag_added) so listeners
        don't reach back into the singleton to load the full list.

        INVARIANT: enrich runs INSIDE the per-root lock, before _fire,
        so the post-state it captures is exactly the state the
        listener sees when it inspects the session — no race.

        `strict=True` raises KeyError instead of returning None when the
        target session cannot be found (state-critical mutators — the
        turn path's message appends). Default stays lenient: many
        fire-and-forget mutators legitimately race session deletion.
        Both modes log the miss (deduped) via `_mutation_miss`.
        """
        rid = self._root_id_for(sid)
        if rid is None:
            self._mutation_miss(sid, change.get("kind"), "rid-miss", strict=strict)
            return None
        with self._lock_for_root(rid):
            sess = self._cached(sid, hydrate_events=hydrate_events)
            if sess is None:
                self._mutation_miss(sid, change.get("kind"), "node-miss", strict=strict)
                return None
            mutate(sess)
            batch_ctx = self._batches.get(rid)
            if batch_ctx is None:
                self._persist_root(rid, bump=bump_updated_at)
            if enrich is not None:
                change = {**change, **enrich(sess)}
            # Phantom batch (set by `_load_root` during hydration)
            # suppresses both persist (above) AND listener fan-out
            # (here). Hydrate re-applies events already on disk in
            # events.jsonl — every mutator listener would re-broadcast
            # them, causing thousands of redundant WS frames per
            # cold-load. The frontend's `messages_replay` on
            # (re)subscribe already carries the rehydrated tree.
            if not (batch_ctx and batch_ctx.get("_phantom")):
                self._fire(sid, change)
            return sess

    @perf.timed_fn("session.persist_root")
    def _persist_root(self, root_id: str, *, bump: bool) -> None:
        """Leading-edge debounce wrapper around `write_session_full`.
        Caller MUST hold `_lock_for_root(root_id)`. See the
        PERSIST_DEBOUNCE_S block at module top for the contract.

        INVARIANT: every persist captures the latest in-memory state
        of the root tree — when the write is deferred, the live dict
        ref sitting in `_persist_pending[rid]` IS the same object the
        producer keeps mutating; the tail flush writes whatever's
        there at fire time.

        INVARIANT: `updated_at` is stamped HERE, under the caller's
        per-root lock, so it reflects the mutation moment — not the
        50ms-later wall-clock when the tail flush happens to fire.
        Downstream `write_session_full` calls always pass
        `bump_updated_at=False`.

        Both the leading-edge and trailing-edge writes are submitted
        to a thread pool so json.dump never blocks the caller's
        thread (which may be the asyncio event loop)."""
        root = self._roots.get(root_id)
        if root is None:
            return
        if bump:
            root["updated_at"] = datetime.now().isoformat()
        now = time.monotonic()
        with _persist_state_lock:
            _persist_pending[root_id] = root
            if root_id in _persist_inflight:
                return
            last = _persist_last_at.get(root_id, 0.0)
            if root_id in _persist_deadlines:
                delay = max(0.0, PERSIST_DEBOUNCE_S - (now - last))
                _arm_persist_deadline_unlocked(root_id, delay)
                return
            if now - last >= PERSIST_DEBOUNCE_S:
                # Leading edge: queue a zero-delay scheduler dispatch that re-acquires
                # the root lock before writing. The caller's lock is
                # still held → the timer thread blocks on
                # _lock_for_root until the caller returns, then writes
                # the latest state. This avoids running json.dump on
                # the event loop for large (13MB+) session trees.
                _arm_persist_deadline_unlocked(root_id, 0.0)
            else:
                # Inside window: queue the live ref + (re)arm tail deadline.
                delay = PERSIST_DEBOUNCE_S - (now - last)
                _arm_persist_deadline_unlocked(root_id, delay)
        # Drafts no longer live in the tree, so a tree persist does NOT
        # capture them. Flush the small draft sidecar synchronously when
        # this root has a pending draft (any mutator's persist also
        # durably commits the typed-but-unsent draft), then notify
        # DraftStore so its scheduled coalescer no-ops via its own check.
        # Defense in depth: a misbehaving DraftStore must never tear
        # down the persist path. Both calls are wrapped + logged.
        with perf.timed("session.persist_root.draft_store"):
            try:
                ds = self._draft_store_or_none()
            except Exception:
                logger.exception(
                    "draft store resolution failed in _persist_root for %s",
                    root_id,
                )
                ds = None
        with perf.timed("session.persist_root.draft_dirty"):
            try:
                dirty = ds is not None and ds.is_dirty(root_id)
            except Exception:
                logger.exception(
                    "draft is_dirty raised in _persist_root for %s", root_id,
                )
                dirty = False
        if dirty:
            with perf.timed("session.persist_root.draft_write"):
                session_store.write_drafts(
                    root_id, session_store.collect_tree_drafts(root),
                )
                try:
                    ds.note_root_persisted(root_id)
                except Exception:
                    logger.exception(
                        "draft note_root_persisted failed for %s", root_id,
                    )

    def _tail_persist(self, root_id: str) -> None:
        """Scheduler callback. Copies the root under its lock, then writes
        outside the lock so summary refresh and filesystem work cannot
        block live readers of the root tree."""
        sess = None
        root_lock = self._lock_for_root(root_id)
        lock_wait_started = time.perf_counter()
        root_lock.acquire()
        lock_acquired_at = time.perf_counter()
        try:
            with perf.timed("session.tail_persist.lock_copy"):
                with perf.timed("session.tail_persist.state"):
                    with _persist_state_lock:
                        if root_id in _persist_inflight:
                            return
                        pending = _persist_pending.pop(root_id, None)
                        _cancel_persist_deadline_unlocked(root_id)
                        if pending is None:
                            return
                        _persist_inflight.add(root_id)
                        _persist_last_at[root_id] = time.monotonic()
                with perf.timed("session.tail_persist.copy"):
                    sess = session_store.copy_persistable_tree(pending)
        finally:
            lock_released_at = time.perf_counter()
            root_lock.release()
            perf.record(
                "session.tail_persist.root_lock_wait",
                (lock_acquired_at - lock_wait_started) * 1000.0,
            )
            perf.record(
                "session.tail_persist.root_lock_held",
                (lock_released_at - lock_acquired_at) * 1000.0,
            )
        # bump=False — `updated_at` was set at queue time under
        # the caller's lock.
        try:
            with perf.timed("session.tail_persist.write_full"):
                session_store.write_session_full(
                    sess,
                    bump_updated_at=False,
                    preserve_projection_fields=True,
                    already_persistable=True,
                )
            self._note_root_file_written(root_id)
        except Exception:
            logger.exception(
                "_tail_persist: write_session_full failed for %s", root_id,
            )
        finally:
            with _persist_state_changed:
                _persist_inflight.discard(root_id)
                if root_id in _persist_pending and root_id not in _persist_deadlines:
                    last = _persist_last_at.get(root_id, 0.0)
                    delay = max(0.0, PERSIST_DEBOUNCE_S - (time.monotonic() - last))
                    _arm_persist_deadline_unlocked(root_id, delay)
                _persist_state_changed.notify_all()

    def _drop_pending_persist(self, root_id: str) -> None:
        """Cancel any queued tail flush + drop the pending entry for
        `root_id`. Caller MUST hold `_lock_for_root(root_id)`.

        After this returns:
          - `_persist_pending[rid]` is gone, so even a scheduler dispatch
            that already started executing will find `sess is None`
            inside its inner check and return without writing.
          - Even if a queued callback is blocked on `_lock_for_root`
            waiting for the caller to release, it sees the popped
            state once it acquires the lock — and returns harmlessly.
        Use this from every delete path to prevent the queued write
        from resurrecting the just-deleted session."""
        with _persist_state_lock:
            _persist_pending.pop(root_id, None)
            _cancel_persist_deadline_unlocked(root_id)
            _persist_last_at.pop(root_id, None)

    def flush_pending_persists(self) -> None:
        """Best-effort drain of pending tail flushes. Called from:
          - `main.on_shutdown` (after `drain_pending_drafts`) so a
            clean stop persists everything queued.

        Iterates the keys at snapshot time and blocks on each root lock.
        This method is the explicit durability barrier used by shutdown
        and tests, so returning while a pending tree write remains would
        violate its contract."""
        with _persist_state_lock:
            rids = set(_persist_pending.keys()) | set(_persist_inflight)
        for rid in rids:
            while True:
                lock = self._lock_for_root(rid)
                lock.acquire()
                try:
                    with _persist_state_changed:
                        while rid in _persist_inflight:
                            _persist_state_changed.wait(timeout=1.0)
                        sess = _persist_pending.pop(rid, None)
                        _cancel_persist_deadline_unlocked(rid)
                        if sess is None:
                            break
                        _persist_last_at[rid] = time.monotonic()
                    try:
                        session_store.write_session_full(
                            sess,
                            bump_updated_at=False,
                            preserve_projection_fields=True,
                        )
                        self._note_root_file_written(rid)
                    except Exception:
                        logger.exception(
                            "flush_pending_persists: failed for %s", rid,
                        )
                finally:
                    lock.release()
                with _persist_state_changed:
                    if rid not in _persist_pending and rid not in _persist_inflight:
                        break

    def flush_root_persist(self, root_id: str) -> None:
        """Durability barrier for one root without draining unrelated roots."""
        while True:
            wait_started = time.perf_counter()
            with _persist_state_changed:
                while root_id in _persist_inflight:
                    _persist_state_changed.wait(timeout=1.0)
            perf.record(
                "session.flush_root.inflight_wait",
                (time.perf_counter() - wait_started) * 1000.0,
            )
            lock = self._lock_for_root(root_id)
            lock_started = time.perf_counter()
            lock.acquire()
            perf.record(
                "session.flush_root.root_lock_wait",
                (time.perf_counter() - lock_started) * 1000.0,
            )
            try:
                with _persist_state_changed:
                    if root_id in _persist_inflight:
                        continue
                    pending = _persist_pending.pop(root_id, None)
                    _cancel_persist_deadline_unlocked(root_id)
                    if pending is None:
                        return
                    _persist_inflight.add(root_id)
                    _persist_last_at[root_id] = time.monotonic()
                sess = session_store.copy_persistable_tree(pending)
            finally:
                lock.release()
            try:
                with perf.timed("session.flush_root.write"):
                    session_store.write_session_full(
                        sess,
                        bump_updated_at=False,
                        preserve_projection_fields=True,
                        already_persistable=True,
                    )
                self._note_root_file_written(root_id)
            except Exception:
                with _persist_state_changed:
                    _persist_pending.setdefault(root_id, sess)
                raise
            finally:
                with _persist_state_changed:
                    _persist_inflight.discard(root_id)
                    _persist_state_changed.notify_all()

    # ── Draft persist coalescer ────────────────────────────────────
    # Moved to `backend/draft_store.py`. sm hot paths resolve the
    # active store via `_draft_store_or_none()` on each call rather
    # than storing callable refs — DraftStore owns both the behavior
    # AND the access path.

    def _draft_store_or_none(self):
        """Resolve the active DraftStore.

        Returns the store if a coordinator is bound and exposes one,
        `None` if no coordinator is bound (test harnesses exercising
        sm in isolation). Raises on an unexpected resolution failure
        (e.g. orchestrator import error, coord-bound-but-missing-
        attr race) so callers can fail-closed instead of silently
        treating the root as unpinned-by-drafts.
        """
        from orchestrator import get_active_coordinator
        coord = get_active_coordinator()
        if coord is None:
            return None
        ds = getattr(coord, "draft_store", None)
        if ds is None:
            # Coord is set but `draft_store` attr is missing — this
            # should only ever happen inside `Coordinator.__init__`'s
            # tiny ordering window. Raise so the caller fails closed.
            raise RuntimeError(
                "coordinator bound but `draft_store` attr unset — "
                "likely Coordinator.__init__ ordering bug",
            )
        return ds

    def drain_pending_drafts(self) -> None:
        """Thin facade: routes to `coordinator.draft_store.drain_pending_drafts`.

        Kept on sm because `main.on_shutdown` and `test_draft_sync.py`
        already call it via `session_manager.drain_pending_drafts()`.
        The actual machinery lives in `DraftStore`. Requires a
        coordinator to be bound."""
        from orchestrator import get_active_coordinator
        get_active_coordinator().draft_store.drain_pending_drafts()

    # ── Lifecycle ──────────────────────────────────────────────────

    def create(
        self,
        *,
        name: str = "",
        model: Optional[str] = None,
        cwd: str = "",
        orchestration_mode: str = "team",
        source: str = "web",
        provider_id: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        permission: Optional[dict] = None,
        browser_harness_enabled: bool = True,
        browser_harness_headless: bool = True,
        node_id: str = "primary",
        worker_creation_policy: str = "ask",
        bare_config: bool = False,
        user_initiated: bool = False,
        capability_contexts: Optional[list[dict]] = None,
        disallowed_tools: Optional[list[str]] = None,
        disabled_builtin_extensions: Optional[list[str]] = None,
        storage_scope: Optional[dict] = None,
        id: Optional[str] = None,
        created_at: Optional[str] = None,
    ) -> dict:
        # bare_config marks a TestApe-isolated session: empty system prompt
        # (no skills / CLAUDE.md / injected instructions) and orchestration_mode
        # honored at the runner level. Routed entirely off this session field.
        #
        # `user_initiated` records whether the user is aware of having
        # created this session (see session_store user-initiation taxonomy).
        # Defaults to False — fail-closed so a caller that forgets it never
        # surfaces a hidden helper session as user-facing.
        _validate_orchestration_mode_against_provider(
            orchestration_mode=orchestration_mode, provider_id=provider_id,
        )
        sess = session_store.create_session(
            name=name, model=model, cwd=cwd,
            orchestration_mode=orchestration_mode, source=source,
            provider_id=provider_id,
            reasoning_effort=reasoning_effort,
            permission=permission,
            browser_harness_enabled=browser_harness_enabled,
            browser_harness_headless=browser_harness_headless,
            node_id=node_id,
            worker_creation_policy=worker_creation_policy,
            bare_config=bare_config,
            user_initiated=user_initiated,
            disallowed_tools=disallowed_tools,
            disabled_builtin_extensions=disabled_builtin_extensions,
            storage_scope=storage_scope,
            id=id,
            created_at=created_at,
        )
        sess["capability_contexts"] = list(capability_contexts or [])
        self._ensure_project_for_session(sess)
        rid = sess["id"]
        with self._lock_for_root(rid):
            self._roots[rid] = sess
            self._note_root_file_written(rid)
            self._node_root_id[rid] = rid
            self._node_root_missing_until.pop(rid, None)
            self._owner_generations[rid] = self._owner_generations.get(rid, 0) + 1
        self._fire(rid, {"kind": "created", "session": copy.deepcopy(sess)})
        return copy.deepcopy(sess)

    def _ensure_project_for_session(self, sess: dict) -> None:
        cwd = sess.get("cwd")
        if not session_store.should_auto_register_project(sess):
            return
        try:
            import project_store
            project_store.add_project(
                cwd,
                node_id=sess.get("node_id") or "primary",
            )
        except Exception:
            logger.warning(
                "auto add_project failed for session %s",
                sess.get("id"),
                exc_info=True,
            )

    def create_delegate_fork(
        self,
        *,
        parent_agent_session_id: str,
        caller_agent_session_id: str,
        parent_agent_sid_at_fork: str,
        parent_line_count_at_fork: int,
        orchestration_mode: str,
    ) -> dict:
        """Create an internal-only delegate fork Better Agent session under the
        given target Better Agent session. Used by ask(run_mode="fork") delegations
        — the resulting Better Agent session is the per-(caller, target) thread.

        Fires a `delegate_fork_created` listener event which the WS
        broadcaster intentionally ignores (the fork is internal; clients
        don't render it). When the runner emits `session_discovered`
        the caller should `set_agent_sid(fork_id, mode, sid)` to wire
        up the underlying claude jsonl.
        """
        rid = self._root_id_for(parent_agent_session_id)
        if rid is None:
            raise DelegateForkParentMissing(parent_agent_session_id)
        with self._lock_for_root(rid):
            # Mutate the live in-memory root directly; session_manager
            # owns the single persist (delegate forks don't bump
            # updated_at — they're internal).
            cached_root = self._ensure_root_loaded(rid)
            if cached_root is None:
                raise DelegateForkParentMissing(parent_agent_session_id)
            child = session_store.create_delegate_fork(
                cached_root,
                parent_agent_session_id=parent_agent_session_id,
                caller_agent_session_id=caller_agent_session_id,
                parent_agent_sid_at_fork=parent_agent_sid_at_fork,
                parent_line_count_at_fork=parent_line_count_at_fork,
                orchestration_mode=orchestration_mode,
            )
            self._index_root(cached_root)
            session_store.write_session_full(cached_root, bump_updated_at=False)
            self._note_root_file_written(rid)
            self._fire(
                child["id"],
                {
                    "kind": "delegate_fork_created",
                    "session": copy.deepcopy(child),
                    "parent_session_id": parent_agent_session_id,
                    "caller_agent_session_id": caller_agent_session_id,
                },
            )
        return copy.deepcopy(child)

    def create_sub_session(
        self,
        *,
        parent_session_id: str,
        name: str,
        model: Optional[str] = None,
        provider_id: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        permission: Optional[dict] = None,
        cwd: str = "",
        node_id: Optional[str] = None,
        disallowed_tools: Optional[list[str]] = None,
        disabled_builtin_extensions: Optional[list[str]] = None,
    ) -> dict:
        rid = self._root_id_for(parent_session_id)
        if rid is None:
            raise KeyError(parent_session_id)
        _validate_orchestration_mode_against_provider(
            orchestration_mode="native", provider_id=provider_id,
        )
        with self._lock_for_root(rid):
            cached_root = self._ensure_root_loaded(rid)
            if cached_root is None:
                raise KeyError(parent_session_id)
            child = session_store.create_sub_session(
                cached_root,
                parent_session_id=parent_session_id,
                name=name,
                model=model,
                provider_id=provider_id,
                reasoning_effort=reasoning_effort,
                permission=permission,
                cwd=cwd,
                node_id=node_id,
                disallowed_tools=disallowed_tools,
                disabled_builtin_extensions=disabled_builtin_extensions,
            )
            self._index_root(cached_root)
            session_store.write_session_full(cached_root, bump_updated_at=False)
            self._note_root_file_written(rid)
            self._fire(
                child["id"],
                {
                    "kind": "sub_session_created",
                    "session": copy.deepcopy(child),
                    "parent_session_id": parent_session_id,
                },
            )
        return copy.deepcopy(child)

    def fork(
        self,
        parent_sid: str,
        name: Optional[str] = None,
        *,
        kind: Optional[str] = None,
        user_initiated: Optional[bool] = None,
    ) -> dict:
        """Fork `parent_sid` (root or embedded fork). The new fork is
        appended to the parent's `forks` array within the same root
        tree, then the whole root is persisted.

        `kind` overrides the default `"user"` discriminator on the new
        fork. Used by orchs.adv_sync to mark its forks as
        `"adv_sync_fork"` *before* the `forked` broadcast fires so the
        frontend's first view of the fork already carries the right
        kind (avoids a post-create kind flip race).

        `user_initiated` overrides the inherited default for agent-driven
        forks (e.g. session-bridge ask/run fork) that should remain
        non-user-aware even when they branch from a user session."""
        rid = self._root_id_for(parent_sid)
        if rid is None:
            raise KeyError(parent_sid)
        # Reject the fork up-front when the parent's provider has no
        # CLI-level fork primitive (gemini-cli 0.42 — see issue
        # google-gemini/gemini-cli#22563). Without this, the BC fork
        # record would be created and only the FIRST TURN would fail
        # with NotImplementedError — leaving a half-broken fork on
        # disk that confuses the user. Fail HERE, before the fork
        # record exists, so the user sees one clean error.
        parent_session = self.get(parent_sid)
        if parent_session:
            provider_id = parent_session.get("provider_id")
            if provider_id:
                # Reject up-front when the parent's provider doesn't
                # support fork. Hits gemini today via
                # `GeminiProvider.supports_fork = False`. Capability is
                # the source of truth — no per-kind isinstance checks
                # anywhere else. INVARIANT: this fires BEFORE any disk
                # write so we never leave a half-broken fork on disk
                # that explodes on its first turn.
                from provider import get_provider as _get_provider_instance
                try:
                    prov_inst = _get_provider_instance(provider_id)
                except KeyError:
                    raise ValueError(
                        f"Cannot fork: parent session's provider "
                        f"{provider_id} was deleted."
                    )
                if not prov_inst.supports_fork:
                    raise ValueError(
                        f"{prov_inst.KIND} provider does not support fork."
                    )
        with self._lock_for_root(rid):
            # Mutate the live in-memory root directly; session_manager
            # owns the single persist.
            cached_root = self._ensure_root_loaded(rid)
            if cached_root is None:
                raise KeyError(parent_sid)
            child = session_store.fork_session(cached_root, parent_sid, name=name)
            if kind is not None:
                child["kind"] = kind
                # A non-"user" kind (e.g. adv_sync_fork) is an internal
                # fork the user did not ask for — never user-facing.
                if kind != "user":
                    child["user_initiated"] = False
            if user_initiated is not None:
                child["user_initiated"] = bool(user_initiated)
            # The new fork node is now live inside cached_root; register
            # its id→root mapping, then persist the tree exactly once.
            # Synchronous (not debounced): fork durability is part of the
            # contract — the caller gets a fork already on disk.
            self._index_root(cached_root)
            session_store.write_session_full(cached_root, bump_updated_at=True)
            self._note_root_file_written(rid)
            # Fire INSIDE the lock for ordering parity with `_run` —
            # otherwise a `forked` frame could broadcast before an
            # earlier mutation's listener (which fires while still
            # holding the lock) finishes broadcasting.
            self._fire(
                child["id"],
                {
                    "kind": "forked",
                    "session": copy.deepcopy(child),
                    "parent_session_id": parent_sid,
                },
            )
        return copy.deepcopy(child)

    @staticmethod
    def _delete_subtree_sids(root: dict, sid: str) -> tuple[str, ...]:
        node = session_store._find_in_tree(root, sid)
        if node is None:
            return ()
        return tuple(sorted({
            sid,
            *(
                str(fork["id"])
                for fork in session_store._walk_forks(node)
                if fork.get("id")
            ),
        }))

    @staticmethod
    def _deletion_evidence_path(sid: str) -> Path:
        return session_store._sessions_dir() / ".owner-deletions" / f"{sid}.json"

    def _commit_deletion_evidence_locked(
        self,
        sids: Iterable[str],
        root_id: str,
        incarnations: dict[str, str],
    ) -> list[Path]:
        written: list[Path] = []
        try:
            for deleted_sid in sids:
                generation = self._owner_generations.get(deleted_sid, 1) + 1
                path = self._deletion_evidence_path(deleted_sid)
                path.parent.mkdir(parents=True, exist_ok=True)
                temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
                payload = json.dumps({
                    "sid": deleted_sid,
                    "root_id": root_id,
                    "generation": generation,
                    "incarnation": incarnations.get(deleted_sid, ""),
                }, separators=(",", ":"))
                with temp.open("w", encoding="utf-8") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp, path)
                dir_fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
                written.append(path)
            return written
        except Exception:
            for path in written:
                try:
                    path.unlink()
                except OSError:
                    pass
            raise

    def owner_deletion_committed(self, token: SessionOwnerToken) -> bool:
        try:
            data = json.loads(
                self._deletion_evidence_path(token.sid).read_text(encoding="utf-8")
            )
            return (
                data.get("root_id") == token.root_id
                and int(data.get("generation")) > token.generation
                and str(data.get("incarnation") or "") == token.incarnation
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return False

    def delete(self, sid: str) -> bool:
        """Delete only after every owner in the target subtree is quiescent."""
        while True:
            rid = self._root_id_for(sid)
            if rid is None:
                return False
            with self._lock_for_root(rid):
                root = self._ensure_root_loaded(rid)
                if root is None:
                    return False
                expected_sids = self._delete_subtree_sids(root, sid)
            if not expected_sids:
                return False
            with self._cache_guard:
                operation_locks = [
                    self._owner_operation_locks.setdefault(
                        owner_sid, threading.RLock(),
                    )
                    for owner_sid in expected_sids
                ]
            with ExitStack() as stack:
                for operation_lock in operation_locks:
                    stack.enter_context(operation_lock)
                with self._lock_for_root(rid):
                    current_root = self._ensure_root_loaded(rid)
                    if current_root is None:
                        return False
                    if self._delete_subtree_sids(current_root, sid) != expected_sids:
                        continue
                    ok, revocations = self._delete_with_locked_subtree(sid, rid)
            for revoked_sid, callbacks in revocations:
                self._invoke_owner_revocations(revoked_sid, callbacks)
            return ok

    def _delete_with_locked_subtree(
        self, sid: str, rid: str,
    ) -> tuple[bool, list[tuple[str, tuple[Callable[[], None], ...]]]]:
        """Delete a session. If `sid` is a root, the whole tree (including
        all embedded forks) is dropped. If `sid` is a fork, it (and its
        descendants) is spliced out of the parent and the root is
        re-persisted."""
        revocations: list[tuple[str, tuple[Callable[[], None], ...]]] = []
        with self._lock_for_root(rid):
            cached_root = self._ensure_root_loaded(rid)
            if cached_root is None:
                return False, []
            deleted_sids = list(self._delete_subtree_sids(cached_root, sid))
            if not deleted_sids:
                return False, []
            original_root = copy.deepcopy(cached_root)
            deleted_incarnations = {
                deleted_sid: str(
                    (session_store._find_in_tree(cached_root, deleted_sid) or {}).get(
                        "_owner_incarnation"
                    ) or ""
                )
                for deleted_sid in deleted_sids
            }
            updated_root = copy.deepcopy(cached_root)
            self._drop_pending_persist(rid)
            evidence_paths: list[Path] = []
            try:
                if sid == rid:
                    if not session_store.delete_session(sid):
                        return False, []
                else:
                    if not session_store.splice_fork(updated_root, sid):
                        return False, []
                    session_store.write_session_full(updated_root, bump_updated_at=True)
                evidence_paths = self._commit_deletion_evidence_locked(
                    deleted_sids,
                    rid,
                    deleted_incarnations,
                )
            except Exception:
                logger.exception("session deletion persistence failed for %s", sid)
                try:
                    session_store.write_session_full(original_root, bump_updated_at=False)
                except Exception:
                    logger.exception("session deletion rollback failed for %s", sid)
                for path in evidence_paths:
                    try:
                        path.unlink()
                    except OSError:
                        pass
                return False, []

            if sid == rid:
                self._roots.pop(rid, None)
                self._root_file_fingerprints.pop(rid, None)
                self._root_file_checked_at.pop(rid, None)
            else:
                self._roots[rid] = updated_root
                self._index_root(updated_root)
                self._note_root_file_written(rid)
            for deleted_sid in deleted_sids:
                self._node_root_id.pop(deleted_sid, None)
                self._kind_by_sid.pop(deleted_sid, None)
                self._last_broadcast_running.pop(deleted_sid, None)
                self._unread_counts.pop(deleted_sid, None)
                self._unread_hydrated.discard(deleted_sid)
                revocations.append((
                    deleted_sid, self._revoke_owner_locked(deleted_sid),
                ))
            self._unread_counts_version += 1
            try:
                import session_queue_projection
                session_queue_projection.delete_records(deleted_sids)
            except Exception:
                logger.exception("queue projection delete failed for %s", sid)
            self._fire(sid, {"kind": "deleted"})
        return True, revocations

    # ── Top-level metadata patches ─────────────────────────────────

    def rename(self, sid: str, name: str, *, force: bool = False) -> Optional[dict]:
        # `name_locked` sessions (e.g. the assistant singleton) refuse rename
        # from every path — AI auto-title, first-prompt auto-name, and the user
        # rename endpoint. Fail closed: return the unchanged session so callers
        # that branch on truthiness don't mistake a refusal for not-found.
        # `force` is the internal-only escape hatch for the owner to restore the
        # canonical name on a locked session that drifted before the lock existed.
        existing = self.get_lite(sid)
        if existing is not None and existing.get("name_locked") and not force:
            return existing
        name = strip_link_marker_syntax(name)
        return self._run(
            sid,
            lambda s: s.__setitem__("name", name),
            {"kind": "renamed", "name": name},
        )

    def set_name_locked(self, sid: str, locked: bool) -> Optional[dict]:
        """Mark a session's name as immutable. The rename gate (`rename`) and
        the user rename endpoint both honor this field — it is the single
        source of truth for name immutability."""
        return self._run(
            sid,
            lambda s: s.__setitem__("name_locked", bool(locked)),
            {"kind": "name_locked", "name_locked": bool(locked)},
        )

    def set_all_projects(self, sid: str, value: bool) -> Optional[dict]:
        """Mark a session as visible in every project regardless of its cwd
        (e.g. the assistant singleton). `session_matches_project` is the single
        membership check that honors this flag."""
        return self._run(
            sid,
            lambda s: s.__setitem__("all_projects", bool(value)),
            {"kind": "all_projects_set", "all_projects": bool(value)},
        )

    def set_capability_contexts(
        self,
        sid: str,
        capability_contexts: list[dict],
    ) -> Optional[dict]:
        return self._run(
            sid,
            lambda s: s.__setitem__("capability_contexts", list(capability_contexts)),
            {
                "kind": "capability_contexts_set",
                "capability_contexts": list(capability_contexts),
            },
        )

    def set_disallowed_tools(
        self,
        sid: str,
        disallowed_tools: list[str],
    ) -> Optional[dict]:
        tools = list(dict.fromkeys(str(tool).strip() for tool in disallowed_tools if str(tool).strip()))
        return self._run(
            sid,
            lambda s: s.__setitem__("disallowed_tools", tools),
            {
                "kind": "disallowed_tools_set",
                "disallowed_tools": tools,
            },
        )

    def set_disabled_builtin_extensions(
        self,
        sid: str,
        disabled_builtin_extensions: list[str],
    ) -> Optional[dict]:
        extensions = list(dict.fromkeys(
            str(item).strip()
            for item in disabled_builtin_extensions
            if str(item or "").strip()
        ))
        return self._run(
            sid,
            lambda s: s.__setitem__("disabled_builtin_extensions", extensions),
            {
                "kind": "disabled_builtin_extensions_set",
                "disabled_builtin_extensions": extensions,
            },
        )

    def set_origin(self, sid: str, *, source: str, user_initiated: bool) -> Optional[dict]:
        if source not in session_store._VALID_SESSION_SOURCES:
            raise ValueError(f"Invalid session source: {source}")
        return self._run(
            sid,
            lambda s: (
                s.__setitem__("source", source),
                s.__setitem__("user_initiated", bool(user_initiated)),
            ),
            {
                "kind": "origin_set",
                "source": source,
                "user_initiated": bool(user_initiated),
            },
        )

    def set_selectors(
        self,
        sid: str,
        *,
        model: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        permission: Optional[dict] = None,
        cwd: Optional[str] = None,
        provider_id: Optional[str] = None,
        client_id: Optional[str] = None,
    ) -> Optional[dict]:
        """Patch mutable per-session selectors.

        `orchestration_mode` is deliberately NOT a selector — it's
        frozen at session creation (`session_manager.create` validates
        against provider capability; the PATCH route at `main.py`
        returns 409 if a client sends one). If a future caller needs
        to change a session's mode it must delete + recreate so the
        capability gate re-runs.

        `provider_id` IS mutable even while a turn is in flight. The
        current run keeps using the provider instance that already owns
        it; the changed selector is picked up lazily by the next prompt,
        which starts a continuation if the provider/model differs from
        the last active provider subprocess.

        When we accept a provider change, we re-validate the
        session's existing `orchestration_mode` against the NEW
        provider's capability (e.g. switching Claude→Gemini on a
        manager-mode session must fail loudly here, not silently on
        the next turn).

        Empty / whitespace `provider_id` is rejected at the API
        boundary so a direct Python caller (CLI, tests) can't
        accidentally persist a broken provider reference that all
        future lookups would skip with a "missing provider" warning."""
        if reasoning_effort is not None:
            if not isinstance(reasoning_effort, str):
                raise ValueError("reasoning_effort must be a string")
            if reasoning_effort.strip():
                normalized_effort = normalize_reasoning_effort(reasoning_effort)
                if normalized_effort is None:
                    raise ValueError("invalid reasoning_effort")
                reasoning_effort = normalized_effort
            else:
                reasoning_effort = ""
        if permission is not None:
            if not isinstance(permission, dict):
                raise ValueError("permission must be a dict")
            existing = self.get(sid) or {}
            perm_provider = provider_id or existing.get("provider_id")
            permission = session_store._session_permission(permission, perm_provider)
        if provider_id is not None:
            if not isinstance(provider_id, str) or not provider_id.strip():
                raise ValueError(
                    "provider_id must be a non-empty string"
                )
            existing = self.get(sid) or {}
            mode = existing.get("orchestration_mode") or "team"
            _validate_orchestration_mode_against_provider(
                orchestration_mode=mode, provider_id=provider_id,
            )
        def _do(s: dict) -> None:
            # Inside the per-root lock.
            pass
            if model is not None:
                s["model"] = model
            if reasoning_effort is not None:
                s["reasoning_effort"] = reasoning_effort
            if permission is not None:
                s["permission"] = permission
            if cwd is not None:
                s["cwd"] = cwd
            if provider_id is not None:
                s["provider_id"] = provider_id
        return self._run(
            sid, _do,
            {
                "kind": "selectors_set",
                "model": model,
                "reasoning_effort": reasoning_effort,
                "permission": permission,
                "cwd": cwd,
                "provider_id": provider_id,
                "client_id": client_id,
            },
        )

    def set_agent_sid(
        self,
        sid: str,
        mode: str,
        agent_sid: Optional[str],
        *,
        provider_id: Optional[str] = None,
        model: Optional[str] = None,
        bump_updated_at: bool = True,
    ) -> Optional[dict]:
        field = session_store._agent_sid_field_for_mode(mode)
        def _do(s: dict) -> None:
            s[field] = agent_sid
            if agent_sid is not None:
                p_id = provider_id or s.get("provider_id")
                m_val = model or s.get("model")
                if field == "supervisor_agent_session_id":
                    s["last_active_supervisor_provider_id"] = p_id
                    s["last_active_supervisor_model"] = m_val
                else:
                    s["last_active_provider_id"] = p_id
                    s["last_active_model"] = m_val
        sess = self._run(
            sid,
            _do,
            {"kind": "agent_sid_set", "mode": mode, "agent_sid": agent_sid},
            bump_updated_at=bump_updated_at,
        )
        # Bus signal — Orchestrator subscribes to this to acquire a
        # tailer for the new sid on every already-connected WS callback.
        # `persist=False` because the session record on disk is the
        # durable source of truth for agent_sid; this event is purely
        # a backend-internal "new sid exists" hook with no WS consumer.
        # Scheduled outside the per-root lock on the running loop;
        # silently dropped when called from a sync-only context
        # (which currently never happens for set_agent_sid in
        # production paths — listed in `cluster_C_inventory`).
        if sess is not None and agent_sid:
            root_id = self._root_id_for(sid)
            if root_id:
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = None
                if loop is not None:
                    loop.create_task(
                        bus.publish(BusEvent(
                            type="session.agent_sid_set",
                            root_id=root_id,
                            sid=sid,
                            payload={"mode": mode, "agent_sid": agent_sid},
                            persist=False,
                        )),
                        name=f"sm-agent-sid-{sid[:8]}",
                    )
        return sess

    def clear_forked_from(self, sid: str) -> Optional[dict]:
        return self._run(
            sid,
            lambda s: s.__setitem__("forked_from_agent_sid", None),
            {"kind": "forked_from_cleared"},
        )

    def set_forked_from(self, sid: str, agent_sid: str) -> Optional[dict]:
        """Stamp `forked_from_agent_sid` on an existing session so its
        next turn-1 spawn passes `--fork-session <agent_sid>`. Used by
        the prompt-engineering flow to make an INDEPENDENT root session
        that still carries a parent's claude conversation context — i.e.
        an alternative to session_store.fork_session() when you want the
        fork-on-turn-1 mechanic without embedding the child inside the
        parent's `forks` tree.
        """
        return self._run(
            sid,
            lambda s: s.__setitem__("forked_from_agent_sid", agent_sid),
            {"kind": "forked_from_set", "agent_sid": agent_sid},
        )

    def clear_forked_from_supervisor(self, sid: str) -> Optional[dict]:
        """One-shot clear of `forked_from_supervisor_agent_sid` after the
        supervisor's first post-separate verdict completes. Independent of
        `clear_forked_from` (which targets the native/manager marker)
        so the two sid fields stay locally scoped."""
        return self._run(
            sid,
            lambda s: s.__setitem__(
                "forked_from_supervisor_agent_sid", None,
            ),
            {"kind": "forked_from_supervisor_cleared"},
        )

    def set_forked_from_supervisor(
        self, sid: str, agent_sid: str,
    ) -> Optional[dict]:
        """Stamp `forked_from_supervisor_agent_sid` so the NEXT supervisor
        verdict on this session passes `--fork-session <agent_sid>`. Used
        by the separate-supervisor flow to re-back the supervisor with a
        fork of its previous claude session after the original was
        graduated into a standalone native Better Agent session."""
        return self._run(
            sid,
            lambda s: s.__setitem__(
                "forked_from_supervisor_agent_sid", agent_sid,
            ),
            {
                "kind": "forked_from_supervisor_set",
                "agent_sid": agent_sid,
            },
        )

    def set_fork_closed(self, sid: str, value: bool) -> Optional[dict]:
        return self._run(
            sid,
            lambda s: s.__setitem__("fork_closed", bool(value)),
            {"kind": "fork_closed_set", "value": bool(value)},
        )

    # ── Queued prompts ──────────────────────────────────────────────

    def admit_queued_prompt(self, sid: str, prompt: dict) -> dict:
        client_id = prompt.get("client_id")
        projection_record: Optional[dict] = None
        result: dict = {
            "session": None,
            "admitted": False,
            "existing_user_message": None,
            "existing_queued_prompt": None,
        }

        rid = self._root_id_for(sid)
        if rid is None:
            return result

        with self._lock_for_root(rid):
            sess = self._cached(sid)
            if sess is None:
                return result

            if isinstance(client_id, str) and client_id:
                for existing_msg in sess.get("messages") or []:
                    if (
                        existing_msg.get("role") == "user"
                        and existing_msg.get("client_id") == client_id
                    ):
                        result["session"] = sess
                        result["existing_user_message"] = existing_msg
                        projection_record = self._project_queue_record(sess)
                        break
                if result["session"] is None:
                    for existing_prompt in sess.get("queued_prompts") or []:
                        if existing_prompt.get("client_id") == client_id:
                            result["session"] = sess
                            result["existing_queued_prompt"] = existing_prompt
                            projection_record = self._project_queue_record(sess)
                            break

            if result["session"] is None:
                q = sess.setdefault("queued_prompts", [])
                q[:] = [p for p in q if p.get("id") != prompt.get("id")]
                q.append(prompt)
                batch_ctx = self._batches.get(rid)
                if batch_ctx is None:
                    self._persist_root(rid, bump=True)
                change = {"kind": "queued_prompts_updated"}
                if not (batch_ctx and batch_ctx.get("_phantom")):
                    self._fire(sid, change)
                result["session"] = sess
                result["admitted"] = True
                projection_record = self._project_queue_record(sess)

        queued_len = len((projection_record or {}).get("queued_prompts") or [])
        self._set_queued_prompt_count(sid, queued_len)
        self._upsert_queue_record(projection_record)
        logger.debug(
            "queue-diag admit_queued_prompt sid=%s qp_id=%s client_id=%s "
            "admitted=%s -> queue_len=%d",
            sid, prompt.get("id"), prompt.get("client_id"),
            result["admitted"], queued_len,
        )
        return result

    def add_queued_prompt(self, sid: str, prompt: dict) -> Optional[dict]:
        admission = self.admit_queued_prompt(sid, prompt)
        return admission.get("session")

    def update_queued_prompt(
        self, sid: str, queued_id: str, updates: dict,
    ) -> Optional[dict]:
        projection: dict[str, Optional[dict]] = {}

        def _do(s: dict) -> None:
            for prompt in s.setdefault("queued_prompts", []):
                if prompt.get("id") == queued_id:
                    prompt.update(updates)
                    break

        result = self._run(
            sid,
            _do,
            {"kind": "queued_prompts_updated"},
            enrich=self._queue_projection_enricher(
                projection, include_queued_prompts=True,
            ),
        )
        if result is not None:
            queued_len = len((projection.get("record") or {}).get("queued_prompts") or [])
            self._set_queued_prompt_count(sid, queued_len)
            self._upsert_queue_record(projection.get("record"))
            logger.debug(
                "queue-diag update_queued_prompt sid=%s qp_id=%s keys=%s "
                "-> queue_len=%d",
                sid, queued_id, sorted(updates.keys()),
                queued_len,
            )
        return result

    def remove_queued_prompt(
        self, sid: str, queued_id: Optional[str],
    ) -> Optional[dict]:
        projection: dict[str, Optional[dict]] = {}

        def _do(s: dict) -> None:
            if queued_id is None:
                s["queued_prompts"] = []
                return
            s["queued_prompts"] = [
                p for p in s.get("queued_prompts", [])
                if p.get("id") != queued_id
            ]

        result = self._run(
            sid,
            _do,
            {"kind": "queued_prompts_updated"},
            enrich=self._queue_projection_enricher(
                projection, include_queued_prompts=True,
            ),
        )
        if result is not None:
            queued_len = len((projection.get("record") or {}).get("queued_prompts") or [])
            self._set_queued_prompt_count(sid, queued_len)
            self._upsert_queue_record(projection.get("record"))
            logger.debug(
                "queue-diag remove_queued_prompt sid=%s qp_id=%s "
                "-> queue_len=%d",
                sid, queued_id, queued_len,
            )
        return result

    def remove_queued_prompt_by_client_id(
        self, sid: str, client_id: str,
    ) -> Optional[dict]:
        projection: dict[str, Optional[dict]] = {}

        def _do(s: dict) -> None:
            s["queued_prompts"] = [
                p for p in s.get("queued_prompts", [])
                if p.get("client_id") != client_id
            ]

        result = self._run(
            sid,
            _do,
            {"kind": "queued_prompts_updated"},
            enrich=self._queue_projection_enricher(
                projection, include_queued_prompts=True,
            ),
        )
        if result is not None:
            queued_len = len((projection.get("record") or {}).get("queued_prompts") or [])
            self._set_queued_prompt_count(sid, queued_len)
            self._upsert_queue_record(projection.get("record"))
            logger.debug(
                "queue-diag remove_queued_prompt_by_client_id sid=%s "
                "client_id=%s -> queue_len=%d",
                sid, client_id, queued_len,
            )
        return result

    # ── Messages ───────────────────────────────────────────────────

    def append_user_msg(
        self, sid: str, msg: dict, *, strict: bool = False,
    ) -> Optional[dict]:
        client_id = msg.get("client_id")
        if isinstance(client_id, str) and client_id:
            rid = self._root_id_for(sid)
            if rid is None:
                self._mutation_miss(
                    sid, "user_msg_appended", "rid-miss", strict=strict,
                )
                return None
            projection_record: Optional[dict] = None
            with self._lock_for_root(rid):
                sess = self._cached(sid)
                if sess is None:
                    self._mutation_miss(
                        sid, "user_msg_appended", "node-miss", strict=strict,
                    )
                    return None
                for existing_msg in sess.get("messages") or []:
                    if (
                        existing_msg.get("role") == "user"
                        and existing_msg.get("client_id") == client_id
                    ):
                        return existing_msg
                session_store.assign_message_seq(sess, msg)
                sess["messages"].append(msg)
                batch_ctx = self._batches.get(rid)
                if batch_ctx is None:
                    self._persist_root(rid, bump=True)
                change = {"kind": "user_msg_appended", "msg": msg}
                if not (batch_ctx and batch_ctx.get("_phantom")):
                    self._fire(sid, change)
                projection_record = self._project_queue_record(sess)
            self._upsert_queue_record(projection_record)
            return msg

        projection: dict[str, Optional[dict]] = {}

        def _do(s: dict) -> None:
            session_store.assign_message_seq(s, msg)
            s["messages"].append(msg)

        result = self._run(
            sid,
            _do,
            {"kind": "user_msg_appended", "msg": msg},
            enrich=self._queue_projection_enricher(
                projection, include_queued_prompts=False,
            ),
            strict=strict,
        )
        if result is None:
            return None
        self._upsert_queue_record(projection.get("record"))
        return msg

    def append_assistant_msg(
        self, sid: str, msg: dict, *, strict: bool = False,
    ) -> Optional[dict]:
        def _do(s: dict) -> None:
            session_store.assign_message_seq(s, msg)
            s["messages"].append(msg)
        if self._run(
            sid, _do, {"kind": "assistant_msg_appended", "msg": msg},
            strict=strict,
        ) is None:
            return None
        return msg

    def remove_assistant_msg(self, sid: str, msg_id: str) -> Optional[dict]:
        def _do(s: dict) -> None:
            s["messages"] = [
                m for m in s.get("messages", []) if m.get("id") != msg_id
            ]
        return self._run(
            sid, _do, {"kind": "assistant_msg_removed", "msg_id": msg_id},
        )

    def truncate_messages(self, sid: str, keep_count: int) -> Optional[dict]:
        def _do(s: dict) -> None:
            s["messages"] = s.get("messages", [])[:keep_count]
        return self._run(
            sid, _do,
            {"kind": "messages_truncated", "keep_count": keep_count},
        )

    # ── Assistant-msg field updates ────────────────────────────────

    def append_native_event(
        self, sid: str, msg_id: str, event: dict,
    ) -> Optional[dict]:
        def _do(s: dict) -> None:
            m = _find_message(s, msg_id)
            if m is None:
                return
            evs = m.setdefault("events", [])
            # Maintain `_uid_idx` on `m` so apply_event's dedup stays
            # O(1). See `orchs/base.py:_uid_idx_for`.
            from orchs.base import _event_uuid
            uid_idx = m.get("_uid_idx")
            if isinstance(uid_idx, dict):
                u = _event_uuid(event)
                if u and u not in uid_idx:
                    uid_idx[u] = len(evs)
            evs.append(event)
            from render_stub import invalidate_panel_anchor_cache
            invalidate_panel_anchor_cache(m)
        return self._run(
            sid, _do,
            {"kind": "native_event_appended", "msg_id": msg_id, "event": event},
            hydrate_events=False,
        )

    def replace_native_event(
        self, sid: str, msg_id: str, event: dict, uuid: str,
    ) -> Optional[dict]:
        def _do(s: dict) -> None:
            m = _find_message(s, msg_id)
            if m is None:
                return
            evs = m.setdefault("events", [])
            # O(1) replace via cached uid_idx on `m`. Falls back to
            # linear scan if cache absent.
            from orchs.base import _event_uuid
            uid_idx = m.get("_uid_idx")
            if isinstance(uid_idx, dict) and uuid in uid_idx:
                evs[uid_idx[uuid]] = event
                from render_stub import invalidate_panel_anchor_cache
                invalidate_panel_anchor_cache(m)
                return
            for i, existing in enumerate(evs):
                existing_uuid = (
                    existing.get("uuid") or
                    (existing.get("data") or {}).get("uuid") or
                    ((existing.get("data") or {}).get("event") or {}).get("data", {}).get("uuid")
                )
                if existing_uuid == uuid:
                    evs[i] = event
                    from render_stub import invalidate_panel_anchor_cache
                    invalidate_panel_anchor_cache(m)
                    return
            # Not found — append, maintain uid_idx.
            if isinstance(uid_idx, dict):
                u = _event_uuid(event)
                if u and u not in uid_idx:
                    uid_idx[u] = len(evs)
            evs.append(event)
            from render_stub import invalidate_panel_anchor_cache
            invalidate_panel_anchor_cache(m)
        return self._run(
            sid, _do,
            {"kind": "native_event_replaced", "msg_id": msg_id, "event": event, "uuid": uuid},
            hydrate_events=False,
        )

    def set_agent_sid_on_msg(
        self, sid: str, msg_id: str, agent_sid: Optional[str],
    ) -> Optional[dict]:
        def _do(s: dict) -> None:
            m = _find_message(s, msg_id)
            if m is None:
                return
            m["agent_session_id"] = agent_sid
        return self._run(
            sid, _do,
            {"kind": "agent_sid_on_msg_set", "msg_id": msg_id, "agent_sid": agent_sid},
        )

    def snapshot_workers(
        self, sid: str, msg_id: str, workers_list: Iterable[dict],
    ) -> Optional[dict]:
        snap: list[dict] = []
        by_delegation: dict[str, dict] = {}
        for worker in workers_list:
            delegation_id = str(worker.get("delegation_id") or "")
            if not delegation_id:
                snap.append(worker)
                continue
            existing = by_delegation.get(delegation_id)
            if existing is None:
                by_delegation[delegation_id] = worker
                snap.append(worker)
                continue
            existing.update(worker)
        def _do(s: dict) -> None:
            m = _find_message(s, msg_id)
            if m is None:
                return
            m["workers"] = snap
        return self._run(
            sid, _do,
            {"kind": "workers_snapshot", "msg_id": msg_id, "workers": snap},
            hydrate_events=False,
        )

    def upsert_worker_panel(
        self, sid: str, msg_id: str, panel: dict, *, reset_events: bool = False,
    ) -> Optional[dict]:
        delegation_id = str(panel.get("delegation_id") or "")
        if not delegation_id:
            return None
        def _do(s: dict) -> None:
            m = _find_message(s, msg_id)
            if m is None:
                return
            workers = m.setdefault("workers", [])
            existing = next(
                (p for p in workers if p.get("delegation_id") == delegation_id),
                None,
            )
            if existing is None:
                workers.append(panel)
                return
            events = existing.get("events")
            existing.update(panel)
            if reset_events:
                existing["events"] = []
                existing.pop("_uid_idx", None)
                from render_stub import invalidate_panel_anchor_cache
                invalidate_panel_anchor_cache(existing)
            elif events and not panel.get("events"):
                existing["events"] = events
        return self._run(
            sid, _do,
            {"kind": "worker_panel_upserted", "msg_id": msg_id, "panel": panel},
            hydrate_events=False,
        )

    def update_worker_panel(
        self, sid: str, msg_id: str, delegation_id: str, fields: dict,
    ) -> Optional[dict]:
        if not delegation_id:
            return None
        def _do(s: dict) -> None:
            m = _find_message(s, msg_id)
            if m is None:
                return
            panel = next(
                (p for p in m.get("workers") or []
                 if p.get("delegation_id") == delegation_id),
                None,
            )
            if panel is not None:
                panel.update(fields)
        return self._run(
            sid, _do,
            {
                "kind": "worker_panel_updated",
                "msg_id": msg_id,
                "delegation_id": delegation_id,
                "fields": fields,
            },
            hydrate_events=False,
        )

    def apply_worker_panel_event(
        self,
        sid: str,
        msg_id: str,
        delegation_id: str,
        inner_event: dict,
    ) -> Optional[dict]:
        """Append (or replace) an event on the worker panel matching
        `delegation_id`. The INNER agent_message dict — not the outer
        `worker_event` wrapper — is what lives in panel.events.

        Idempotent on the inner event's uuid: a re-apply with identical
        data is a no-op. Same uuid + different data replaces the
        existing entry in place (supports streaming updates / Gemini
        cumulative snapshots).

        Sole writer for `msg.workers[i].events`. The previous direct
        mutation at `orchs/manager/_delegation.py:545` is gone — every
        worker event flows through `apply_event(... worker_event)` →
        this mutator, mirroring the rule that the primary `msg.events`
        only mutates via `apply_event`.

        No-op when the msg or the panel can't be resolved (replay
        ordering glitch, ghost delegation_id, etc.) — never crashes the
        caller, never pollutes the primary `msg.events`.
        """
        # Resolve the inner uuid OUTSIDE _do so the dedup decision and
        # the mutation are atomic under the per-root lock.
        from orchs.base import _event_uuid, _uid_idx_for
        ev_uuid = _event_uuid(inner_event)
        def _do(s: dict) -> None:
            m = _find_message(s, msg_id)
            if m is None:
                return
            workers = m.get("workers") or []
            panel = next(
                (p for p in workers
                 if p.get("delegation_id") == delegation_id),
                None,
            )
            if panel is None:
                return
            evs = panel.setdefault("events", [])
            uid_idx = _uid_idx_for(panel, evs)
            if ev_uuid:
                existing_idx = uid_idx.get(ev_uuid)
                if existing_idx is not None and existing_idx >= len(evs):
                    panel.pop("_uid_idx", None)
                    uid_idx = _uid_idx_for(panel, evs)
                    existing_idx = uid_idx.get(ev_uuid)
                if existing_idx is not None:
                    if evs[existing_idx] == inner_event:
                        return
                    evs[existing_idx] = inner_event
                    from render_stub import message_output_text
                    content = message_output_text(m)
                    if content:
                        m["content"] = content
                    return
                uid_idx[ev_uuid] = len(evs)
            evs.append(inner_event)
            from render_stub import message_output_text
            content = message_output_text(m)
            if content:
                m["content"] = content
        return self._run(
            sid, _do,
            {
                "kind": "worker_panel_event",
                "msg_id": msg_id,
                "delegation_id": delegation_id,
                "event": inner_event,
            },
            hydrate_events=False,
        )

    def update_running_content(
        self, sid: str, msg_id: str, content: str,
    ) -> Optional[dict]:
        def _do(s: dict) -> None:
            m = _find_message(s, msg_id)
            if m is None:
                return
            m["content"] = content
            m["_content_dirty"] = False
        return self._run(
            sid, _do,
            {"kind": "running_content_updated", "msg_id": msg_id, "content": content},
        )

    def refresh_message_content_from_events(
        self, root_id: str, sid: str, msg_id: str,
    ) -> Optional[dict]:
        """Project collapsed assistant content from message-owned journal rows."""
        from event_journal import event_journal_reader
        from event_shape import project_content_snapshot

        events = event_journal_reader.read_ws_events(
            root_id,
            sid_filter=sid,
            msg_id_filter=msg_id,
        )
        current = self.get(sid) or {}
        current_msg = _find_message(current, msg_id)
        content = project_content_snapshot(
            events, current_msg.get("content") if current_msg else "",
        )
        def _do(s: dict) -> None:
            m = _find_message(s, msg_id)
            if m is None or m.get("isStreaming"):
                return
            if content != (m.get("content") or ""):
                m["content"] = content
        return self._run(
            sid, _do,
            {
                "kind": "running_content_updated",
                "msg_id": msg_id,
                "content": content,
            },
        )

    def set_streaming(
        self, sid: str, msg_id: str, value: bool,
    ) -> Optional[dict]:
        def _do(s: dict) -> None:
            m = _find_message(s, msg_id)
            if m is None:
                return
            m["isStreaming"] = value
        return self._run(
            sid, _do,
            {"kind": "streaming_set", "msg_id": msg_id, "value": value},
        )

    # ── Per-session running flag + unread cursor ───────────────────

    @staticmethod
    def _is_user_kind(sess: Optional[dict]) -> bool:
        """A session contributes to the sidebar / aggregate metrics iff
        its `kind` is None or "user". Worker forks
        (`delegate_fork`, `supervisor_worker`, `adv_sync_fork`) are
        excluded — they never appear in the sidebar so neither their
        running state nor their unread count should leak into
        user-facing badges."""
        if sess is None:
            return False
        k = sess.get("kind")
        return k is None or k == "user"

    def _count_unread_from_disk(self, sess: dict) -> set[str]:
        """Count distinct assistant message IDs that have events AFTER
        the persisted `last_seen_event_uid`. Returns a set of msg_ids.
        Returns all assistant message IDs that have events when
        `last_seen_event_uid` is None (every event is unread).
        Idempotent: only run from `_ensure_unread_loaded` (under the
        per-root lock)."""
        marker = sess.get("last_seen_event_uid")
        messages = sess.get("messages") or []
        if not messages:
            return set()

        # If no marker, walk all and collect assistant msg_ids that have events.
        if marker is None:
            unread = set()
            for msg in messages:
                if msg.get("role") != "assistant":
                    continue
                mid = msg.get("id")
                if not mid:
                    continue
                if msg.get("events"):
                    unread.add(mid)
            return unread

        # Marker exists. Walk backwards until we find it.
        unread_msg_ids = set()
        found_marker = False

        for msg in reversed(messages):
            if msg.get("role") != "assistant":
                continue
            mid = msg.get("id")
            if not mid:
                continue

            # Collect all UUIDs in this message in order.
            msg_uuids = []
            for ev in msg.get("events") or []:
                u = _event_uuid_safe(ev)
                if u:
                    msg_uuids.append(u)

            if not msg_uuids:
                continue

            # Is the marker in this message?
            try:
                m_idx = msg_uuids.index(marker)
                # Found it! Any UUIDs after m_idx in this list are unread.
                if m_idx < len(msg_uuids) - 1:
                    unread_msg_ids.add(mid)
                found_marker = True
                break  # Stop walking history.
            except ValueError:
                # Marker not in this message; since we are walking backwards,
                # this entire message is "unread" (it came after the marker).
                unread_msg_ids.add(mid)

        if not found_marker:
            # Marker was stale/gone. The loop above collected everything,
            # which matches the "count all" fallback.
            pass

        return unread_msg_ids

    def _ensure_unread_loaded(self, sid: str) -> None:
        """Hydrate `_unread_counts[sid]` on first access by counting
        events after `last_seen_event_uid` on the live session record.
        Caller MUST hold the per-root lock for `sid`."""
        if sid in self._unread_hydrated:
            return
        sess = self._cached(sid)
        if sess is None:
            return
        if not self._is_user_kind(sess):
            # Worker forks are never surfaced — mark hydrated with 0
            # so bumps from `apply_event` on a worker fork (which the
            # mutator filters anyway) don't trigger a load attempt.
            self._unread_counts[sid] = set()
            self._unread_counts_version += 1
            self._unread_hydrated.add(sid)
            return
        self._unread_counts[sid] = self._count_unread_from_disk(sess)
        self._unread_counts_version += 1
        self._unread_hydrated.add(sid)

    def is_running(self, sid: str) -> bool:
        """Live liveness as surfaced to the frontend badge — delegates
        to the bound `_compute_is_running` (no internal cache; walks
        `_run_state[sid]` + checks pid liveness on each call).

        Workers (`kind != user`) ALWAYS return False — the user-facing
        sidebar/home badge never renders for them, so the answer the
        caller actually wants is "would this surface a badge?"
        Aggregate counters that include worker liveness should call
        `coordinator.is_running(sid)` directly."""
        if self._compute_is_running is None:
            return False
        sess = self._cached(sid)
        if not self._is_user_kind(sess):
            return False
        return bool(self._compute_is_running(sid))

    def get_unread_count(self, sid: str) -> int:
        """Hydrate-on-first-access then return the cached count.
        Safe to call from any thread (acquires the per-root lock)."""
        rid = self._root_id_for(sid)
        if rid is None:
            return 0
        with self._lock_for_root(rid):
            self._ensure_unread_loaded(sid)
            return len(self._unread_counts.get(sid, set()))

    def peek_unread_count(self, sid: str) -> Optional[int]:
        """Non-blocking peek — returns the cached count if already
        hydrated, else None. Used by `GET /api/sessions` to avoid the
        sidebar list triggering a `_load_root` deep-hydrate on every
        un-cached session at cold-load (was driving the endpoint to
        >50s wall-time on a user with ~2,300 sessions because
        `_ensure_unread_loaded` cold-misses → `_cached` →
        `_load_root` → `hydrate_msg_events_from_jsonl` +
        `_derive_current_todos_from_events_jsonl` per session, each
        reading the event journal).
        The frontend renders 0 for not-yet-known sessions and updates
        when later on-demand hydration or live turn events produce an
        `unread_changed` delta."""
        rid = self._root_id_for(sid)
        if rid is None:
            return 0
        # Per-root lock NOT held. `_unread_counts` / `_unread_hydrated`
        # are only written under `_lock_for_root`, so a stale snapshot
        # is tolerable — worst case we miss the just-hydrated value and
        # return None for one extra request cycle. `_root_id_for` may
        # take `_index_lock` on cache miss but that's fast after warmup.
        if sid in self._unread_hydrated:
            return len(self._unread_counts.get(sid, set()))
        return None

    def peek_unread_count_for_root(self, root_sid: str) -> Optional[int]:
        if root_sid in self._unread_hydrated:
            return len(self._unread_counts.get(root_sid, set()))
        return None

    def warm_unread(self, sid: str) -> None:
        """Hydrate `sid`'s unread count off the hot path and fire an
        `unread_changed` delta so open clients (sidebar + project/home
        aggregates) converge to the correct total. No-op if already
        Cold-loads the session tree (the expensive `events.jsonl`
        hydrate) under the per-root lock, so callers must keep it out
        of startup and list/search paths. Only fires when the count is
        non-zero — clients render 0 by default, so a 0-delta would be
        pure WS noise."""
        rid = self._root_id_for(sid)
        if rid is None:
            return
        with self._lock_for_root(rid):
            if sid in self._unread_hydrated:
                return
            self._ensure_unread_loaded(sid)
            cnt = len(self._unread_counts.get(sid, set()))
            if cnt > 0:
                self._fire(
                    sid,
                    {"kind": "unread_changed", "unread_count": cnt},
                )

    def mark_unread_clean_if_journal_seen(
        self, sid: str, last_seen_event_uid: Optional[str],
    ) -> bool:
        if not last_seen_event_uid:
            return False
        rid = self._root_id_for(sid)
        if rid is None:
            return False
        try:
            from event_ingester import event_ingester
            latest = event_ingester.latest_render_event_uid(
                rid, sid_filter=sid,
            )
        except Exception:
            logger.exception("unread fast-clean failed for %s", sid)
            return False
        if latest != last_seen_event_uid:
            return False
        with self._lock_for_root(rid):
            if sid in self._unread_hydrated:
                return True
            self._unread_counts[sid] = set()
            self._unread_counts_version += 1
            self._unread_hydrated.add(sid)
        return True

    def bump_unread(self, sid: str, msg_id: str) -> None:
        """Increment the unread counter and fire `unread_changed`.
        Called from `OrchestrationStrategy.apply_event`'s APPEND-new-UUID
        path (NOT the replace path — Gemini same-UUID streaming mutates
        in place and must not double-count). Worker forks dropped at
        the mutator boundary.

        First-bump-on-unhydrated session: `_count_unread_from_disk`
        already includes the just-appended event (apply_event mutates
        msg.events BEFORE calling here), so hydration sets the correct
        count and we must NOT also +1. Subsequent bumps increment
        normally."""
        rid = self._root_id_for(sid)
        if rid is None:
            return
        with self._lock_for_root(rid):
            sess = self._cached(sid)
            if not self._is_user_kind(sess):
                return
            if sid not in self._unread_hydrated:
                # First touch — hydrate; disk already reflects the
                # newly-appended event so no further increment.
                self._unread_counts[sid] = self._count_unread_from_disk(sess)
                self._unread_counts_version += 1
                self._unread_hydrated.add(sid)
                self._fire(
                    sid,
                    {
                        "kind": "unread_changed",
                        "unread_count": len(self._unread_counts[sid]),
                    },
                )
            else:
                prev_count = len(self._unread_counts[sid])
                self._unread_counts[sid].add(msg_id)
                new_count = len(self._unread_counts[sid])
                if new_count != prev_count:
                    self._unread_counts_version += 1
                    self._fire(
                        sid,
                        {
                            "kind": "unread_changed",
                            "unread_count": new_count,
                        },
                    )

    def mark_unread(self, sid: str) -> Optional[dict]:
        """Inverse of `mark_seen`: force the session into the "has new"
        state. Clears the persisted `last_seen_event_uid` watermark and
        recomputes the unread set from disk (every assistant event becomes
        unread), then fires `unread_changed` so every open client surfaces
        the badge. Persists, so the state survives a backend restart /
        re-hydration (disk recompute with a null watermark yields the same
        non-zero set). No-op for worker forks. Returns the session snapshot,
        or None if the session is missing / not user-kind."""
        rid = self._root_id_for(sid)
        if rid is None:
            return None
        with self._lock_for_root(rid):
            sess = self._cached(sid)
            if not self._is_user_kind(sess):
                return None
            sess["last_seen_event_uid"] = None
            self._unread_counts[sid] = self._count_unread_from_disk(sess)
            self._unread_counts_version += 1
            self._unread_hydrated.add(sid)
            session_store.write_seen_cursor(rid, sid, None)
            if sid == rid:
                session_store.update_seen_cursor_projection(sid, None)
            self._fire(
                sid,
                {
                    "kind": "unread_changed",
                    "unread_count": len(self._unread_counts[sid]),
                },
            )
            return copy.deepcopy(sess)

    def mark_seen(self, sid: str, uid: Optional[str]) -> Optional[dict]:
        """Persist `last_seen_event_uid` and zero the unread counter.
        Fires `seen_advanced` (broadcaster maps to `session_unread_changed`
        with `unread_count=0`) so every open client converges to the
        acked state. `uid=None` means "ack the current head, whatever it
        is" — caller can omit; the mutator stamps the latest known
        event uid found in `msg.events` walked in order."""
        rid = self._root_id_for(sid)
        if rid is None:
            return None
        with self._lock_for_root(rid):
            sess = self._cached(sid)
            if not self._is_user_kind(sess):
                return None
            resolved = uid
            if resolved is None:
                try:
                    from event_ingester import event_ingester
                    with perf.timed("session.mark_seen.latest_uid_journal"):
                        resolved = event_ingester.latest_render_event_uid(
                            rid,
                            sid_filter=sid,
                        )
                except Exception:
                    logger.exception("mark_seen latest uid lookup failed for %s", sid)
                    resolved = None
            if resolved is None:
                latest: Optional[str] = None
                with perf.timed("session.mark_seen.latest_uid_scan"):
                    for msg in sess.get("messages") or []:
                        if msg.get("role") != "assistant":
                            continue
                        for ev in msg.get("events") or []:
                            u = _event_uuid_safe(ev)
                            if u:
                                latest = u
                resolved = latest
            sess["last_seen_event_uid"] = resolved
            self._unread_counts[sid] = set()
            self._unread_counts_version += 1
            self._unread_hydrated.add(sid)
            session_store.write_seen_cursor(rid, sid, resolved)
            if sid == rid:
                session_store.update_seen_cursor_projection(sid, resolved)
            self._fire(
                sid,
                {
                    "kind": "seen_advanced",
                    "last_seen_event_uid": resolved,
                    "unread_count": 0,
                },
            )
            result = {"last_seen_event_uid": resolved}
        self._clear_view_markers(sid)
        return result

    def set_unseen_error(self, sid: str, text: str) -> None:
        """Mark this session's last turn as having ended in an unrecoverable
        error. Persisted on the session record as `unseen_error` so it
        survives a backend restart, and fired as `error_changed` so the
        sidebar renders the red error dot. Change-gated so repeat fires
        with the same text don't spam the WS bus.

        Lifecycle: the dot stays as long as the last turn errored, and is
        retired ONLY when the session resumes work (a new turn starts — see
        `turn_manager`). It is deliberately NOT tied to view/seen state."""
        rid = self._root_id_for(sid)
        if rid is None:
            return
        with self._lock_for_root(rid):
            sess = self._cached(sid)
            if sess is None or not self._is_user_kind(sess):
                return
            if sess.get("unseen_error") == text:
                return
            sess["unseen_error"] = text
            if rid not in self._batches:
                self._persist_root(rid, bump=False)
            self._fire(
                sid,
                {"kind": "error_changed", "has_error": True, "error": text},
            )

    def clear_unseen_error(self, sid: str) -> None:
        """Retire the unseen-error dot. Called when the session resumes work
        (a new turn starts). No-op + no fire when nothing was set."""
        rid = self._root_id_for(sid)
        if rid is None:
            return
        with self._lock_for_root(rid):
            sess = self._cached(sid)
            if sess is None or not self._is_user_kind(sess):
                return
            if not sess.get("unseen_error"):
                return
            sess["unseen_error"] = None
            if rid not in self._batches:
                self._persist_root(rid, bump=False)
            self._fire(sid, {"kind": "error_changed", "has_error": False})

    def has_unseen_error(self, sid: str) -> bool:
        """Cheap (stale-tolerant) read of whether this session currently
        has an unseen turn-error dot. For sidebar snapshot enrichment.

        The latest assistant message is the durable source of truth. The
        `unseen_error` flag is only used before any assistant turn exists."""
        rid = self._root_id_for(sid)
        if rid is None:
            return False
        sess = self._cached(sid)
        if not sess:
            return False
        return bool(session_store.current_turn_error(sess))

    def _clear_view_markers(self, sid: str) -> None:
        """On a view-ack, clear any marker on `sid` owned by an extension
        whose tag rule declares `clear_on == "view"`. Best-effort."""
        try:
            import extension_applied_config

            watch = extension_applied_config.tag_watch_rules()
        except Exception:
            return
        view_ext_ids = {
            r["extension_id"]
            for r in watch.values()
            if r.get("clear_on") == "view" and r.get("extension_id")
        }
        if not view_ext_ids:
            return
        present = session_store._markers_for_session(sid)
        for ext_id in present:
            if ext_id in view_ext_ids:
                self.clear_marker(sid, ext_id)

    def unread_counts_snapshot(self) -> dict[str, int]:
        """Return a copy of the unread counter map. Hydrates lazily —
        sessions never read so far will not appear; the home/sidebar
        path should call `get_unread_count(sid)` to force hydration."""
        return {sid: len(cnts) for sid, cnts in self._unread_counts.items()}

    def unread_counts_version(self) -> int:
        return self._unread_counts_version

    def agent_sid_field_for_mode(self, mode: str) -> str:
        """Expose the session_id field name for a given orchestration
        mode. Decouples callers from session_store schema details."""
        return session_store._agent_sid_field_for_mode(mode)

    def recover_running_adv_sync_overlays(self) -> int:
        """Walk every session on disk and flip any overlays stuck in
        'running' status to 'interrupted'. Called at startup before
        the coordinator starts any new driver tasks.
        """
        flipped = 0
        seen_roots: set[str] = set()
        for sess in session_store.iter_all_sessions():
            if sess.get("parent_session_id"):
                continue  # only persist on roots
            sid = sess.get("id")
            if not sid or sid in seen_roots:
                continue
            seen_roots.add(sid)
            overlays = sess.get("adv_sync_overlays") or []
            changed = False
            for ov in overlays:
                if ov.get("status") == "running":
                    ov["status"] = "interrupted"
                    ov["updated_at"] = datetime.now().isoformat()
                    changed = True
                    flipped += 1
            if changed:
                # Direct write-through since this runs BEFORE the
                # manager's in-memory cache is fully populated and
                # before any REST/WS readers are active.
                try:
                    session_store.write_session_full(sess, bump_updated_at=False)
                    self._note_root_file_written(sid)
                except Exception:
                    logger.exception(
                        "recover_running_adv_sync_overlays: write failed for %s", sid,
                    )
        return flipped

    def set_msg_recovering(self, sid: str, msg_id: str, value: bool) -> None:
        """Transient marker: this message is being reconciled by
        run_recovery right now. Lives in the in-memory set only; never
        touches disk. Read paths inject `isRecovering: true` on REST
        snapshots, and the listener fan-out emits a WS event so any
        open client flips the pill without a refetch."""
        rid = self._root_id_for(sid)
        if rid is None:
            return
        with self._lock_for_root(rid):
            if value:
                self._recovering_msg_ids.add(msg_id)
            else:
                self._recovering_msg_ids.discard(msg_id)
            self._fire(
                sid,
                {"kind": "msg_recovering_set", "msg_id": msg_id, "value": value},
            )

    def set_stopped_at(
        self, sid: str, msg_id: str, when: Optional[str],
    ) -> Optional[dict]:
        def _do(s: dict) -> None:
            m = _find_message(s, msg_id)
            if m is None:
                return
            if when is None:
                m.pop("stopped_at", None)
            else:
                m["stopped_at"] = when
        return self._run(
            sid, _do,
            {"kind": "stopped_at_set", "msg_id": msg_id, "when": when},
        )

    def set_completed_at(
        self, sid: str, msg_id: str, when: Optional[str],
    ) -> Optional[dict]:
        found = False

        def _do(s: dict) -> None:
            nonlocal found
            m = _find_message(s, msg_id)
            if m is None:
                return
            found = True
            if when is None:
                m.pop("completed_at", None)
            else:
                m["completed_at"] = when
        return self._run(
            sid, _do,
            {"kind": "completed_at_set", "msg_id": msg_id, "when": when},
            enrich=lambda session: {
                "msg": _copy_jsonish(_find_message(session, msg_id))
                if found
                else None,
            },
        )

    def set_assistant_error(
        self, sid: str, msg_id: str, text: str,
    ) -> Optional[dict]:
        def _do(s: dict) -> None:
            m = _find_message(s, msg_id)
            if m is None:
                return
            m["error"] = True
            m["errorText"] = text
        return self._run(
            sid, _do,
            {"kind": "assistant_error_set", "msg_id": msg_id, "text": text},
        )

    def set_msg_retrying_until(
        self, sid: str, msg_id: str, retry_at: Optional[str],
        *,
        error_text: Optional[str] = None,
    ) -> Optional[dict]:
        """Stamp `retrying_until=<iso>` on an assistant message while the
        orchestrator sleeps between a rate-limited attempt and the next
        re-spawn. `None` clears the marker. The matching WS event lets
        any open client render a 'Retrying in Ns…' pill that ticks down
        without needing the message to be re-streamed.

        When ``error_text`` is provided alongside a non-None ``retry_at``,
        the error reason is surfaced on the message so the user can see
        *why* the turn is retrying.  Clearing ``retry_at`` (``None``) also
        clears the error so the next attempt starts with a clean slate."""
        def _do(s: dict) -> None:
            m = _find_message(s, msg_id)
            if m is None:
                return
            if retry_at is None:
                m.pop("retrying_until", None)
                m.pop("error", None)
                m.pop("errorText", None)
            else:
                m["retrying_until"] = retry_at
                if error_text is not None:
                    m["error"] = True
                    m["errorText"] = error_text
        return self._run(
            sid, _do,
            {
                "kind": "msg_retrying_set",
                "msg_id": msg_id,
                "retry_at": retry_at,
                **({"error_text": error_text} if error_text is not None and retry_at is not None else {}),
            },
        )

    def set_msg_transient_attempt(
        self, sid: str, msg_id: str, attempt: Optional[int],
    ) -> Optional[dict]:
        """Stamp `transient_attempt` on an assistant message so transient-error
        retry state survives backend restarts. `None` clears the counter."""
        def _do(s: dict) -> None:
            m = _find_message(s, msg_id)
            if m is None:
                return
            if attempt is None:
                m.pop("transient_attempt", None)
            else:
                m["transient_attempt"] = attempt
        return self._run(
            sid, _do,
            {"kind": "msg_transient_attempt_set", "msg_id": msg_id, "attempt": attempt},
        )

    def record_auto_retry(
        self, sid: str, msg_id: str, count: int, kind: str,
    ) -> Optional[dict]:
        """Stamp `auto_retry={count, kind}` on an assistant message that
        succeeded only after one or more automatic retries (rate-limit /
        transient). Durable so the recovery stays visible across reloads;
        the WS event lets open clients badge the turn immediately."""
        def _do(s: dict) -> None:
            m = _find_message(s, msg_id)
            if m is None:
                return
            m["auto_retry"] = {"count": int(count), "kind": kind}
        return self._run(
            sid, _do,
            {
                "kind": "msg_auto_retry_set",
                "msg_id": msg_id,
                "auto_retry": {"count": int(count), "kind": kind},
            },
        )

    def set_msg_run_meta(
        self, sid: str, msg_id: str, run_meta: Optional[dict],
    ) -> Optional[dict]:
        """Stamp the per-turn `run_meta` (provider_id/model/effort actually
        used) on an assistant message. Re-applied each retry iteration so a
        mid-message selector switch (rate-limit 'continue on another
        provider', selector-change continuation) updates the badge to match
        the provider that runs the succeeding attempt, not the original one.
        `None` clears it."""
        def _do(s: dict) -> None:
            m = _find_message(s, msg_id)
            if m is None:
                return
            if run_meta:
                m["run_meta"] = run_meta
            else:
                m.pop("run_meta", None)
        return self._run(
            sid, _do,
            {"kind": "msg_run_meta_set", "msg_id": msg_id, "run_meta": run_meta},
        )

    def set_msg_continuation_active(
        self, sid: str, msg_id: str, chain_depth: Optional[int],
    ) -> Optional[dict]:
        """Stamp `continuation_active` on the in-flight assistant message
        while a fresh subprocess is starting for a context-window
        continuation. Frontend renders an inline banner. `None` clears it."""
        def _do(s: dict) -> None:
            m = _find_message(s, msg_id)
            if m is None:
                return
            if chain_depth is None:
                m.pop("continuation_active", None)
            else:
                m["continuation_active"] = chain_depth
        return self._run(
            sid, _do,
            {"kind": "msg_continuation_set", "msg_id": msg_id, "chain_depth": chain_depth},
        )

    def set_native_events(
        self, sid: str, msg_id: str, events: list,
    ) -> Optional[dict]:
        def _do(s: dict) -> None:
            m = _find_message(s, msg_id)
            if m is None:
                return
            m["events"] = events
            # Invalidate uid_idx — the list reference changed; any
            # cached map is stale (potentially same-length-different-
            # uuids, which the lazy len-check inside `_uid_idx_for`
            # WON'T catch). Next apply_event call rebuilds.
            m.pop("_uid_idx", None)
        return self._run(
            sid, _do,
            {"kind": "native_events_set", "msg_id": msg_id, "events": events},
        )

    def set_trace_id(
        self, sid: str, msg_id: str, trace_id: Optional[str],
    ) -> Optional[dict]:
        def _do(s: dict) -> None:
            m = _find_message(s, msg_id)
            if m is None:
                return
            m["trace_id"] = trace_id
        return self._run(
            sid, _do,
            {"kind": "trace_id_set", "msg_id": msg_id, "trace_id": trace_id},
        )

    def set_interrupted_by_msg_id(
        self, sid: str, msg_id: str, interrupted_by: Optional[str],
    ) -> Optional[dict]:
        def _do(s: dict) -> None:
            m = _find_message(s, msg_id)
            if m is None:
                return
            if interrupted_by is None:
                m.pop("interrupted_by_msg_id", None)
            else:
                m["interrupted_by_msg_id"] = interrupted_by
        return self._run(
            sid, _do,
            {"kind": "interrupted_by_set", "msg_id": msg_id,
             "interrupted_by_msg_id": interrupted_by},
        )

    def clear_recovered_flag(self, sid: str, msg_id: str) -> Optional[dict]:
        def _do(s: dict) -> None:
            m = _find_message(s, msg_id)
            if m is None:
                return
            m.pop("recovered", None)
        return self._run(
            sid, _do,
            {"kind": "recovered_flag_cleared", "msg_id": msg_id},
        )

    # ── User-msg field updates ─────────────────────────────────────

    def set_user_agent_uuid(
        self, sid: str, msg_id: str, claude_uuid: str,
    ) -> Optional[dict]:
        def _do(s: dict) -> None:
            m = _find_message(s, msg_id)
            if m is None:
                return
            m["agent_message_uuid"] = claude_uuid
        return self._run(
            sid, _do,
            {"kind": "user_claude_uuid_set", "msg_id": msg_id, "uuid": claude_uuid},
        )

    def mark_user_error(
        self, sid: str, msg_id: str, text: str,
    ) -> Optional[dict]:
        def _do(s: dict) -> None:
            m = _find_message(s, msg_id)
            if m is None:
                return
            m["status"] = "error"
            m["errorText"] = text
        def _enrich(s: dict) -> dict:
            m = _find_message(s, msg_id)
            return {"msg": copy.deepcopy(m) if m is not None else None}
        return self._run(
            sid, _do,
            {"kind": "user_msg_marked_error", "msg_id": msg_id, "text": text},
            enrich=_enrich,
        )

    # ── Watcher cursors ────────────────────────────────────────────

    def advance_processed_lines(
        self,
        sid: str,
        agent_sid: str,
        n: int,
        *,
        bump_updated_at: bool = True,
    ) -> Optional[dict]:
        def _do(s: dict) -> None:
            cursors = s.setdefault("processed_line_by_sid", {})
            cursors[agent_sid] = n
        return self._run(
            sid, _do,
            {"kind": "processed_lines_advanced", "agent_sid": agent_sid, "n": n},
            bump_updated_at=bump_updated_at,
        )

    # ── Inline tags ────────────────────────────────────────────────
    #
    # `client_id` propagates onto the change event so the broadcaster's
    # `originated_by` field on `session_metadata_updated` is populated;
    # the originating tab's useWebSocket then drops its own echo
    # instead of clobbering newer optimistic state.

    def add_tag(
        self, sid: str, tag: dict, *, client_id: Optional[str] = None,
    ) -> Optional[dict]:
        def _do(s: dict) -> None:
            s.setdefault("inline_tags", []).append(tag)
        return self._run(
            sid, _do,
            {"kind": "tag_added", "tag": tag, "client_id": client_id},
            enrich=lambda s: {"inline_tags": list(s.get("inline_tags") or [])},
        )

    def update_tag(
        self, sid: str, tag_id: str, updates: dict, *, client_id: Optional[str] = None,
    ) -> Optional[dict]:
        def _do(s: dict) -> None:
            for t in s.get("inline_tags", []):
                if t.get("id") == tag_id:
                    t.update(updates)
                    break
        return self._run(
            sid, _do,
            {"kind": "tag_updated", "tag_id": tag_id, "updates": updates, "client_id": client_id},
            enrich=lambda s: {"inline_tags": list(s.get("inline_tags") or [])},
        )

    def remove_tag(
        self, sid: str, tag_id: str, *, client_id: Optional[str] = None,
    ) -> Optional[dict]:
        def _do(s: dict) -> None:
            s["inline_tags"] = [
                t for t in s.get("inline_tags", []) if t.get("id") != tag_id
            ]
        return self._run(
            sid, _do,
            {"kind": "tag_removed", "tag_id": tag_id, "client_id": client_id},
            enrich=lambda s: {"inline_tags": list(s.get("inline_tags") or [])},
        )

    def clear_tags(
        self, sid: str, *, client_id: Optional[str] = None,
    ) -> Optional[dict]:
        def _do(s: dict) -> None:
            s["inline_tags"] = []
        return self._run(
            sid, _do,
            {"kind": "tags_cleared", "client_id": client_id},
            enrich=lambda s: {"inline_tags": list(s.get("inline_tags") or [])},
        )

    # ── Right panel (UI state) ─────────────────────────────────────
    #
    # Per-session right-panel visibility + active-tab persistence.
    # Mutated via PATCH /api/sessions/{sid}/right-panel; broadcast as
    # `session_metadata_updated` with patch keys `right_panel_open`
    # and/or `right_panel_active_tab`. `client_id` propagates so the
    # originating tab drops its own echo (same pattern as add_tag).

    def set_right_panel(
        self,
        sid: str,
        *,
        open: Optional[bool] = None,
        tab: Optional[str] = None,
        tab_set: bool = False,
        width: Optional[int] = None,
        mobile_height: Optional[int] = None,
        todos_dismissed: Optional[bool] = None,
        auto_opened_by: Optional[list[str]] = None,
        sidebar_minimized: Optional[bool] = None,
        client_id: Optional[str] = None,
    ) -> Optional[dict]:
        def _do(s: dict) -> None:
            if open is not None:
                s["right_panel_open"] = bool(open)
            if tab_set:
                s["right_panel_active_tab"] = tab
            if width is not None:
                s["right_panel_width"] = int(width)
            if mobile_height is not None:
                s["right_panel_mobile_height"] = int(mobile_height)
            if todos_dismissed is not None:
                s["right_panel_todos_dismissed"] = bool(todos_dismissed)
            if auto_opened_by is not None:
                s["right_panel_auto_opened_by"] = list(auto_opened_by)
            if sidebar_minimized is not None:
                s["sidebar_minimized"] = bool(sidebar_minimized)

        change: dict = {"kind": "right_panel_set", "client_id": client_id}
        if open is not None:
            change["right_panel_open"] = bool(open)
        if tab_set:
            change["right_panel_active_tab"] = tab
        if width is not None:
            change["right_panel_width"] = int(width)
        if mobile_height is not None:
            change["right_panel_mobile_height"] = int(mobile_height)
        if todos_dismissed is not None:
            change["right_panel_todos_dismissed"] = bool(todos_dismissed)
        if auto_opened_by is not None:
            change["right_panel_auto_opened_by"] = list(auto_opened_by)
        if sidebar_minimized is not None:
            change["sidebar_minimized"] = bool(sidebar_minimized)
        return self._run(sid, _do, change)

    # ── Adversarial-sync overlays ──────────────────────────────────
    #
    # Per-message text substitutions produced by the orchs.adv_sync
    # ping-pong loop. Same broadcast pattern as inline_tags: a single
    # "adv_sync_updated" change kind fires regardless of add/update;
    # the broadcaster ships the full post-mutation list. Overlay id is
    # the stable handle the driver/REST endpoints reference; status
    # transitions (running → converged/failed/stopped/interrupted) and
    # round counters arrive via update_adv_sync_overlay.

    def add_adv_sync_overlay(
        self, sid: str, overlay: dict, *, client_id: Optional[str] = None,
    ) -> Optional[dict]:
        def _do(s: dict) -> None:
            s.setdefault("adv_sync_overlays", []).append(overlay)
        return self._run(
            sid, _do,
            {"kind": "adv_sync_updated", "client_id": client_id},
            enrich=lambda s: {
                "adv_sync_overlays": list(s.get("adv_sync_overlays") or []),
            },
        )

    def update_adv_sync_overlay(
        self,
        sid: str,
        overlay_id: str,
        patch: dict,
        *,
        client_id: Optional[str] = None,
    ) -> Optional[dict]:
        def _do(s: dict) -> None:
            overlays = s.get("adv_sync_overlays") or []
            for ov in overlays:
                if ov.get("id") == overlay_id:
                    ov.update(patch)
                    ov["updated_at"] = datetime.now().isoformat()
                    return
        return self._run(
            sid, _do,
            {"kind": "adv_sync_updated", "client_id": client_id},
            enrich=lambda s: {
                "adv_sync_overlays": list(s.get("adv_sync_overlays") or []),
            },
        )

    # ── Open file panels ───────────────────────────────────────────
    #
    # Backend-owned set of file panels open in the session's tabbed/
    # split right-panel viewer. The LIST + the agent-/user-requested
    # focus/selection is persisted; the user's live scroll/selection
    # within a panel is frontend-transient (snapshotted at send time).
    # A single change kind ("open_panels_set") is fired regardless of
    # add/remove/set — the broadcaster ships the full post-mutation
    # list (same shape as inline_tags). Panels are de-duplicated by
    # `path`: re-opening an already-open file updates its focus/
    # selection in place instead of creating a duplicate tab.

    def add_open_file_panel(
        self, sid: str, panel: dict, *, client_id: Optional[str] = None,
    ) -> Optional[dict]:
        def _do(s: dict) -> None:
            panels = s.setdefault("open_file_panels", [])
            existing_index = next(
                (
                    idx
                    for idx, p in enumerate(panels)
                    if p.get("path") == panel.get("path")
                ),
                None,
            )
            if existing_index is not None:
                existing = panels.pop(existing_index)
                existing["focus"] = panel.get("focus")
                existing["selection"] = panel.get("selection")
                panels.append(existing)
            else:
                panels.append(panel)
        return self._run(
            sid, _do,
            {"kind": "open_panels_set", "client_id": client_id},
            enrich=lambda s: {
                "open_file_panels": list(s.get("open_file_panels") or []),
            },
        )

    def remove_open_file_panel(
        self, sid: str, panel_id: str, *, client_id: Optional[str] = None,
    ) -> Optional[dict]:
        def _do(s: dict) -> None:
            s["open_file_panels"] = [
                p for p in s.get("open_file_panels", [])
                if p.get("id") != panel_id
            ]
        return self._run(
            sid, _do,
            {"kind": "open_panels_set", "client_id": client_id},
            enrich=lambda s: {
                "open_file_panels": list(s.get("open_file_panels") or []),
            },
        )

    def set_open_file_panels(
        self, sid: str, panels: list, *, client_id: Optional[str] = None,
    ) -> Optional[dict]:
        def _do(s: dict) -> None:
            s["open_file_panels"] = list(panels)
        return self._run(
            sid, _do,
            {"kind": "open_panels_set", "client_id": client_id},
            enrich=lambda s: {
                "open_file_panels": list(s.get("open_file_panels") or []),
            },
        )

    def upsert_file_discussion(
        self,
        sid: str,
        discussion: dict,
        *,
        client_id: Optional[str] = None,
    ) -> Optional[dict]:
        def _do(s: dict) -> None:
            meta = dict(s.get("working_mode_meta") or {})
            discussions = list(meta.get("file_discussions") or [])
            discussion_id = discussion.get("id")
            replaced = False
            for idx, current in enumerate(discussions):
                if current.get("id") == discussion_id:
                    discussions[idx] = {**current, **discussion}
                    replaced = True
                    break
            if not replaced:
                discussions.append(discussion)
            meta["file_discussions"] = discussions
            s["working_mode_meta"] = meta

        return self._run(
            sid,
            _do,
            {"kind": "working_mode_marked", "mode": "file_editing", "client_id": client_id},
            enrich=lambda s: {"working_mode_meta": dict(s.get("working_mode_meta") or {})},
        )

    def patch_file_discussion(
        self,
        sid: str,
        discussion_id: str,
        patch: dict,
        *,
        client_id: Optional[str] = None,
    ) -> Optional[dict]:
        def _do(s: dict) -> None:
            meta = dict(s.get("working_mode_meta") or {})
            discussions = list(meta.get("file_discussions") or [])
            for discussion in discussions:
                if discussion.get("id") == discussion_id:
                    discussion.update(patch)
                    discussion["updated_at"] = datetime.now().isoformat()
                    break
            meta["file_discussions"] = discussions
            s["working_mode_meta"] = meta

        return self._run(
            sid,
            _do,
            {"kind": "working_mode_marked", "mode": "file_editing", "client_id": client_id},
            enrich=lambda s: {"working_mode_meta": dict(s.get("working_mode_meta") or {})},
        )

    def add_open_config_panel(
        self, sid: str, panel: dict, *, client_id: Optional[str] = None,
    ) -> Optional[dict]:
        def _do(s: dict) -> None:
            panels = s.setdefault("open_config_panels", [])
            key = (panel.get("capability_id"), panel.get("scope"), panel.get("cwd"))
            existing = next(
                (
                    p for p in panels
                    if (p.get("capability_id"), p.get("scope"), p.get("cwd")) == key
                ),
                None,
            )
            if existing is not None:
                existing["scope"] = panel.get("scope", existing.get("scope"))
                existing["cwd"] = panel.get("cwd", existing.get("cwd"))
            else:
                panels.append(panel)
        return self._run(
            sid, _do,
            {"kind": "open_config_panels_set", "client_id": client_id},
            enrich=lambda s: {
                "open_config_panels": list(s.get("open_config_panels") or []),
            },
        )

    def remove_open_config_panel(
        self, sid: str, panel_id: str, *, client_id: Optional[str] = None,
    ) -> Optional[dict]:
        def _do(s: dict) -> None:
            s["open_config_panels"] = [
                p for p in s.get("open_config_panels", [])
                if p.get("id") != panel_id
            ]
        return self._run(
            sid, _do,
            {"kind": "open_config_panels_set", "client_id": client_id},
            enrich=lambda s: {
                "open_config_panels": list(s.get("open_config_panels") or []),
            },
        )

    def set_open_config_panels(
        self, sid: str, panels: list, *, client_id: Optional[str] = None,
    ) -> Optional[dict]:
        def _do(s: dict) -> None:
            s["open_config_panels"] = list(panels)
        return self._run(
            sid, _do,
            {"kind": "open_config_panels_set", "client_id": client_id},
            enrich=lambda s: {
                "open_config_panels": list(s.get("open_config_panels") or []),
            },
        )

    # ── Draft ──────────────────────────────────────────────────────
    #
    # Draft persistence machinery (debounce, generation counter,
    # sidecar flush) lives in `backend/draft_store.py`. The methods
    # below stay on SessionManager because they touch the cached
    # session record + WS broadcast — those are sm's concerns. The
    # split is: sm owns IN-MEMORY mutate + WS fire; DraftStore owns
    # COALESCED disk write.

    def set_draft_inline(
        self,
        sid: str,
        text: str,
        seq: int,
        *,
        images: Optional[list] = None,
        bump_updated_at: bool = False,
        client_id: Optional[str] = None,
    ) -> Optional[dict]:
        """Pure in-memory draft mutate + WS `draft_set` fire — no flush
        wiring. Called by `DraftStore.set_draft`, which then arms the
        coalesced sidecar write. Returns the cached session dict so
        the REST handler can echo canonical state.
        """
        rid = self._root_id_for(sid)
        if rid is None:
            return None
        with self._lock_for_root(rid):
            sess = self._cached(sid)
            if sess is None:
                return None
            sess["draft_input"] = text
            sess["draft_input_seq"] = seq
            if images is not None:
                sess["draft_images"] = images
            change = {"kind": "draft_set", "text": text, "seq": seq,
                      "client_id": client_id}
            if images is not None:
                change["images"] = images
            self._fire(sid, change)
            return sess

    def set_draft(
        self,
        sid: str,
        text: str,
        seq: int,
        *,
        images: Optional[list] = None,
        bump_updated_at: bool = False,
        client_id: Optional[str] = None,
    ) -> Optional[dict]:
        """Thin facade: routes to `coordinator.draft_store.set_draft`.

        Kept on sm for back-compat with callers that hold an
        `sm`-typed reference. The actual debounce + sidecar write
        machinery lives in `DraftStore`. Callers (production + tests
        alike) MUST construct a Coordinator before driving any
        draft mutation through sm — there is no fallback path."""
        from orchestrator import get_active_coordinator
        return get_active_coordinator().draft_store.set_draft(
            sid, text, seq,
            images=images, bump_updated_at=bump_updated_at,
            client_id=client_id,
        )

    def is_batching(self, rid: str) -> bool:
        """Public batch-check for DraftStore's "is sm mid-batch for
        this root?" question. Inside a batch, the batch-exit persist
        flushes the sidecar — DraftStore skips its own arming.
        """
        return rid in self._batches

    def with_root_lock(self, rid: str, fn) -> None:
        """Acquire the per-root lock and invoke `fn()`. Public adapter
        over `_lock_for_root` so DraftStore can coordinate sidecar
        writes with the cached-tree lock without reaching into a
        private attribute."""
        with self._lock_for_root(rid):
            fn()

    def get_root_ref(self, rid: str) -> Optional[dict]:
        """Public read of the cached root for DraftStore's sidecar
        collector. Returns None if not loaded."""
        return self._roots.get(rid)

    def set_msg_ask_result(
        self, sid: str, msg_id: str, result: Optional[dict],
    ) -> Optional[dict]:
        """Stamp the Ask MCP `propose_sessions` result onto the assistant
        message that produced it (the turn that called the tool). Each ask
        turn owns its own picker payload — so previous turns keep their
        proposal and the picker can render inline per turn. Pure
        UI-driving msg metadata, outside `msg.events` and the convergence
        invariant (same class as `retrying_until`)."""
        def _do(s: dict) -> None:
            m = _find_message(s, msg_id)
            if m is None:
                return
            m["ask_result"] = result
        return self._run(
            sid, _do,
            {"kind": "msg_ask_result_set", "msg_id": msg_id,
             "ask_result": result},
        )

    def set_msg_ask_choice(
        self, sid: str, msg_id: str, chosen_session_id: Optional[str],
    ) -> Optional[dict]:
        """Record which session the user CHOSE from a turn's picker. Stamped
        on the producing assistant message so the chosen row stays
        highlighted across reloads / other tabs / previous turns. `None`
        clears the choice. Pure UI-driving msg metadata."""
        def _do(s: dict) -> None:
            m = _find_message(s, msg_id)
            if m is None:
                return
            if chosen_session_id is None:
                m.pop("chosen_session_id", None)
            else:
                m["chosen_session_id"] = chosen_session_id
        return self._run(
            sid, _do,
            {"kind": "msg_ask_choice_set", "msg_id": msg_id,
             "chosen_session_id": chosen_session_id},
        )

    # ── Notes ──────────────────────────────────────────────────────

    def add_note(self, sid: str, text: str, *, client_id: Optional[str] = None) -> Optional[dict]:
        """Append a new note to the session's notes list."""
        note = {
            "id": str(uuid.uuid4()),
            "text": text,
            "created_at": datetime.now().isoformat(),
        }
        def _do(s: dict) -> None:
            s.setdefault("notes", []).append(note)
        return self._run(
            sid, _do,
            {"kind": "notes_updated", "client_id": client_id},
            enrich=lambda s: {"notes": list(s.get("notes", []))},
        )

    def remove_note(self, sid: str, note_id: str, *, client_id: Optional[str] = None) -> Optional[dict]:
        """Remove a note by id."""
        def _do(s: dict) -> None:
            notes = s.setdefault("notes", [])
            s["notes"] = [n for n in notes if n.get("id") != note_id]
        return self._run(
            sid, _do,
            {"kind": "notes_updated", "client_id": client_id},
            enrich=lambda s: {"notes": list(s.get("notes", []))},
        )

    def update_note(self, sid: str, note_id: str, text: str, *, client_id: Optional[str] = None) -> Optional[dict]:
        """Update the text of an existing note."""
        def _do(s: dict) -> None:
            for n in s.setdefault("notes", []):
                if n.get("id") == note_id:
                    n["text"] = text
                    break
        return self._run(
            sid, _do,
            {"kind": "notes_updated", "client_id": client_id},
            enrich=lambda s: {"notes": list(s.get("notes", []))},
        )

    # ── Cross-provider current_todos ──────────────────────────────
    #
    # Single source of truth for the "Todos" right-panel tab.
    # Populated by the Todos extension's session-event hook when a
    # provider todo/task tool event lands in the render tree.

    def get_current_todos_snapshot(self, sid: str) -> list:
        """Return a shallow copy of the session's `current_todos` list.

        Hot-path read for extension session-event projection. Caller is
        free to mutate the returned list, but the items inside are still
        the same dicts held by the session.
        """
        rid = self._root_id_for(sid)
        if rid is None:
            return []
        with self._lock_for_root(rid):
            sess = self._cached(sid, hydrate_events=False)
            if sess is None:
                return []
            return list(sess.get("current_todos") or [])

    def set_current_todos(
        self, sid: str, todos: list, *, client_id: Optional[str] = None,
    ) -> Optional[dict]:
        """Replace the session's `current_todos` list and broadcast.

        Idempotent: if the post-state equals the pre-state, `_do` is a
        no-op and `_fire` still runs (matches the `add_note` /
        `update_note` contract — the caller's equality precheck in
        `apply_event` is what suppresses redundant fires under
        recovery replay).
        """
        next_todos = list(todos)
        rid = self._root_id_for(sid)

        def _do(s: dict) -> None:
            s["current_todos"] = next_todos

        result = self._run(
            sid, _do,
            {"kind": "todos_updated", "client_id": client_id},
            enrich=lambda s: {"current_todos": list(s.get("current_todos") or [])},
        )
        if rid is not None:
            self._todo_projection_cache.pop(rid, None)
        replaced = session_store._replace_summary_projection_field(sid, "current_todos", next_todos)
        if not replaced and rid is not None:
            root = self.get(rid)
            if root is not None:
                session_store._upsert_summary(root)
        return result

    # ── Cross-provider current_tasks (TaskCreate / TaskUpdate) ─────

    def get_current_tasks_snapshot(self, sid: str) -> list:
        """Return a shallow copy of the session's `current_tasks` list."""
        rid = self._root_id_for(sid)
        if rid is None:
            return []
        with self._lock_for_root(rid):
            sess = self._cached(sid, hydrate_events=False)
            if sess is None:
                return []
            return list(sess.get("current_tasks") or [])

    def set_current_tasks(
        self, sid: str, tasks: list, *, client_id: Optional[str] = None,
    ) -> Optional[dict]:
        """Replace the session's `current_tasks` list and broadcast."""
        next_tasks = list(tasks)
        rid = self._root_id_for(sid)

        def _do(s: dict) -> None:
            s["current_tasks"] = next_tasks

        result = self._run(
            sid, _do,
            {"kind": "tasks_updated", "client_id": client_id},
            enrich=lambda s: {"current_tasks": list(s.get("current_tasks") or [])},
        )
        if rid is not None:
            self._todo_projection_cache.pop(rid, None)
        replaced = session_store._replace_summary_projection_field(sid, "current_tasks", next_tasks)
        if not replaced and rid is not None:
            root = self.get(rid)
            if root is not None:
                session_store._upsert_summary(root)
        return result

    def apply_provenance_from_event(
        self,
        sid: str,
        normalized: dict,
        *,
        backend_msg_id: Optional[str] = None,
    ) -> bool:
        """Append provenance rows (tool + WHY) for this event and ping any
        open Details panel. Caller MUST gate on live=True — recovery replay
        (live=False) must not re-append (provenance_store also dedups by
        tool_use id as a second guard). Returns True iff rows were written."""
        from stores import provenance_store
        try:
            written = provenance_store.record_from_event(
                sid,
                normalized,
                backend_msg_id=backend_msg_id,
            )
        except Exception:
            logger.debug("provenance record failed sid=%s", sid, exc_info=True)
            return False
        if written:
            self._fire(sid, {"kind": "provenance_changed"})
        return bool(written)

    # ── Token usage ────────────────────────────────────────────────

    def add_session_token_usage(
        self, sid: str, usage: Optional[dict],
    ) -> Optional[dict]:
        keys = (
            "input_tokens",
            "output_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        )
        # Optional cache-write TTL split — summed/stored only when the
        # turn's usage carries it, never zero-filled (absence means the
        # provider didn't report the split).
        breakdown_keys = (
            "cache_creation_5m_tokens",
            "cache_creation_1h_tokens",
        )
        def _do(s: dict) -> None:
            t = s.setdefault("token_usage_total", {k: 0 for k in keys})
            for k in keys:
                t[k] = int(t.get(k, 0)) + int((usage or {}).get(k) or 0)
            for k in breakdown_keys:
                if isinstance(usage, dict) and k in usage:
                    t[k] = int(t.get(k, 0)) + int(usage.get(k) or 0)
            # Store the last turn's usage separately — used for context
            # fill bar (not cumulative).
            if usage:
                s["token_usage_last"] = {k: int(usage.get(k) or 0) for k in keys}
                for k in breakdown_keys:
                    if k in usage:
                        s["token_usage_last"][k] = int(usage.get(k) or 0)
        return self._run(
            sid, _do,
            {"kind": "session_token_usage_added", "usage": usage},
        )

    def set_context_window(
        self, sid: str, context_window: int,
    ) -> Optional[dict]:
        def _do(s: dict) -> None:
            s["context_window"] = context_window
        return self._run(
            sid, _do,
            {"kind": "context_window_set", "context_window": context_window},
        )

    def set_context_tokens(
        self, sid: str, context_tokens: int,
    ) -> Optional[dict]:
        def _do(s: dict) -> None:
            s["context_tokens"] = context_tokens
        return self._run(
            sid, _do,
            {"kind": "context_tokens_set", "context_tokens": context_tokens},
        )

    def set_continuation_chain(
        self, sid: str, continuation_chain: list[str],
    ) -> Optional[dict]:
        chain = [
            item.strip()
            for item in continuation_chain
            if isinstance(item, str) and item.strip()
        ]
        def _do(s: dict) -> None:
            s["continuation_chain"] = chain
        return self._run(
            sid, _do,
            {"kind": "continuation_chain_set", "continuation_chain": chain},
        )

    def set_continuation_requested(
        self, sid: str, prompt: str, *,
        reason: str = "agent_requested",
        when: str = "next_turn",
        origin: str = "agent",
    ) -> Optional[dict]:
        """Agent-requested continuation flag. Read+cleared by the turn loop.

        `when="next_turn"`: the current turn completes normally; the success
        path starts a fresh provider subprocess under the SAME session with
        `prompt`.
        `when="now"`: the in-flight run is aborted and the cancel path starts
        the continuation immediately.

        Lives on the session record so it survives the tool-call → turn-end
        gap (and the cancel-drain gap for `when="now"`)."""
        requested = {
            "prompt": str(prompt or ""),
            "reason": str(reason or "agent_requested"),
            "when": str(when or "next_turn"),
            "origin": "user" if str(origin or "").strip().lower() == "user" else "agent",
        }
        def _do(s: dict) -> None:
            s["continuation_requested"] = requested
        return self._run(
            sid, _do,
            {"kind": "continuation_requested_set", "continuation_requested": requested},
        )

    def pop_continuation_requested(self, sid: str) -> Optional[dict]:
        """Atomically read+clear the agent-requested continuation flag."""
        holder: list[Optional[dict]] = [None]
        def _do(s: dict) -> None:
            holder[0] = s.pop("continuation_requested", None)
        self._run(sid, _do, {"kind": "continuation_requested_cleared"})
        return holder[0]

    # ── Supervisor toggle ──────────────────────────────────────────

    def set_supervisor_enabled(
        self, sid: str, value: bool, *, custom_prompt: str = None,
    ) -> Optional[dict]:
        def _do(s: dict) -> None:
            s["supervisor_enabled"] = value
            if custom_prompt is not None:
                s["supervisor_custom_prompt"] = custom_prompt
        change: dict = {"kind": "supervisor_enabled_set", "value": value}
        if custom_prompt is not None:
            change["supervisor_custom_prompt"] = custom_prompt
        return self._run(sid, _do, change)

    def set_agent_rename_allowed(self, sid: str, value: bool) -> Optional[dict]:
        return self._run(
            sid,
            lambda s: s.__setitem__("agent_rename_allowed", bool(value)),
            {"kind": "agent_rename_allowed_set", "value": bool(value)},
            bump_updated_at=False,
        )

    def set_pinned(self, sid: str, value: bool) -> Optional[dict]:
        def _do(s: dict) -> None:
            s["pinned"] = bool(value)
        return self._run(
            sid, _do, {"kind": "pinned_set", "value": bool(value)},
            bump_updated_at=False,
        )

    def unpin_others(self, keep_sid: str) -> Optional[list[str]]:
        keep = self.get_ref(keep_sid)
        if keep is None:
            return None
        unpinned_ids: list[str] = []
        for session in self.list():
            sid = session.get("id")
            if not sid or sid == keep_sid or not session.get("pinned"):
                continue
            updated = self.set_pinned(sid, False)
            if updated:
                unpinned_ids.append(sid)
        return unpinned_ids

    def set_topbar_pinned(self, sid: str, value: bool) -> Optional[dict]:
        pinned = bool(value)
        pinned_at = datetime.now().isoformat() if pinned else None

        def _do(s: dict) -> None:
            s["topbar_pinned"] = pinned
            s["topbar_pinned_at"] = pinned_at
        return self._run(
            sid, _do, {
                "kind": "topbar_pinned_set",
                "value": pinned,
                "topbar_pinned_at": pinned_at,
            },
            bump_updated_at=False,
        )

    def set_marker(self, sid: str, extension_id: str, marker: dict) -> Optional[dict]:
        """Set an extension's attention marker on a session. Markers live in
        the session_store marker map, persisted to `attention_markers.json`
        so they survive backend restarts. Fires `marker_set` per sid."""
        rid = self._root_id_for(sid)
        if rid is None:
            return None
        with self._lock_for_root(rid):
            sess = self._cached(sid)
            if sess is None:
                return None
            # Change-gate: live ingest re-detects the same tag on every
            # streaming delta of one turn — only write + broadcast when the
            # marker actually changes so the WS isn't spammed per delta.
            if session_store._markers_for_session(sid).get(extension_id) == marker:
                return sess
            session_store.set_marker_projection(sid, extension_id, marker)
            self._fire(
                sid,
                {"kind": "marker_set", "extension_id": extension_id, "marker": dict(marker)},
            )
            return sess

    def clear_marker(self, sid: str, extension_id: str) -> Optional[dict]:
        """Clear one extension's marker on a session. Fires `marker_cleared`."""
        rid = self._root_id_for(sid)
        if rid is None:
            return None
        with self._lock_for_root(rid):
            sess = self._cached(sid)
            if sess is None:
                return None
            if extension_id not in session_store._markers_for_session(sid):
                return sess
            session_store.set_marker_projection(sid, extension_id, None)
            self._fire(sid, {"kind": "marker_cleared", "extension_id": extension_id})
            return sess

    def clear_markers_for_extension(self, extension_id: str) -> None:
        """Drop one extension's markers from every session, firing
        `marker_cleared` per affected sid. Used on disable/uninstall."""
        for sid in session_store.markers_for_extension_purge(extension_id):
            self._fire(sid, {"kind": "marker_cleared", "extension_id": extension_id})

    def set_last_opened_at(
        self,
        sid: str,
        at: str,
        *,
        return_session: bool = True,
    ) -> Optional[dict]:
        """Stamp when the user last opened this session's chat view on a
        client. Does NOT bump `updated_at` — opening is not a content
        change; it only feeds the "last opened" sort."""
        rid = self._root_id_for(sid)
        if rid is None:
            return None
        with self._lock_for_root(rid):
            sess = self._cached(sid)
            if sess is None:
                return None
            if sess.get("last_opened_at") == at:
                return _copy_jsonish(sess) if return_session else {"id": sid, "last_opened_at": at}
            sess["last_opened_at"] = at
            session_store.write_last_opened(rid, sid, at)
            if sid == rid:
                session_store.update_last_opened_projection(sid, at)
            self._fire(sid, {"kind": "last_opened_set", "at": at})
            return _copy_jsonish(sess) if return_session else {"id": sid, "last_opened_at": at}

    def set_archived(self, sid: str, value: bool) -> Optional[dict]:
        def _do(s: dict) -> None:
            s["archived"] = bool(value)
        return self._run(
            sid, _do, {"kind": "archived_set", "value": bool(value)},
        )

    def set_moved_to(self, sid: str, target_session_id: str) -> Optional[dict]:
        def _do(s: dict) -> None:
            s["moved_to_session_id"] = target_session_id
        return self._run(
            sid, _do,
            {"kind": "moved_to_set", "target_session_id": target_session_id},
        )

    def set_moved_from(self, sid: str, source_session_id: str) -> Optional[dict]:
        def _do(s: dict) -> None:
            s["moved_from_session_id"] = source_session_id
        return self._run(
            sid, _do,
            {"kind": "moved_from_set", "source_session_id": source_session_id},
        )

    # Summary fields carried from the source session by move-to-project after
    # `session_migrate.migrate_session_content` copies the render tree. Markers
    # are excluded — they live in the global attention_markers store keyed by
    # sid, not in the summary projection.
    _MIGRATED_SUMMARY_FIELDS = (
        "name",
        "message_count",
        "first_prompt",
        "last_user_prompt_at",
        "last_seen_event_uid",
        "current_tasks",
        "current_todos",
    )

    def apply_migrated_fields(self, sid: str, source: dict) -> Optional[dict]:
        def _do(s: dict) -> None:
            for key in self._MIGRATED_SUMMARY_FIELDS:
                if key in source:
                    s[key] = source[key]
        return self._run(sid, _do, {"kind": "migrated_fields_applied"})

    def set_worker_eligible(self, sid: str, value: bool) -> Optional[dict]:
        def _do(s: dict) -> None:
            s["worker_eligible"] = bool(value)
        return self._run(
            sid, _do, {"kind": "worker_eligible_set", "value": bool(value)},
        )

    def set_worker_creation_policy(self, sid: str, policy: str) -> Optional[dict]:
        if policy not in ("ask", "approve", "deny"):
            raise ValueError("worker_creation_policy must be ask, approve, or deny")
        def _do(s: dict) -> None:
            s["worker_creation_policy"] = policy
        return self._run(
            sid, _do, {"kind": "worker_creation_policy_set", "policy": policy},
        )

    def set_bare_config(self, sid: str, bare: bool) -> Optional[dict]:
        def _do(s: dict) -> None:
            s["bare_config"] = bool(bare)
        return self._run(
            sid, _do, {"kind": "bare_config_set", "bare_config": bool(bare)},
        )

    def add_active_capability(self, sid: str, capability_id: str) -> Optional[dict]:
        cid = str(capability_id or "").strip()
        if not cid:
            return self.get(sid)

        def _do(s: dict) -> None:
            current = [
                str(x) for x in (s.get("active_capability_ids") or []) if str(x or "").strip()
            ]
            if cid not in current:
                current.append(cid)
            s["active_capability_ids"] = current

        return self._run(
            sid, _do, {"kind": "active_capability_added", "capability_id": cid},
        )

    def remove_active_capability(self, sid: str, capability_id: str) -> Optional[dict]:
        cid = str(capability_id or "").strip()

        def _do(s: dict) -> None:
            current = [
                str(x) for x in (s.get("active_capability_ids") or []) if str(x or "").strip()
            ]
            s["active_capability_ids"] = [x for x in current if x != cid]

        return self._run(
            sid, _do, {"kind": "active_capability_removed", "capability_id": cid},
        )

    def set_backend_url(self, sid: str, backend_url: str) -> Optional[dict]:
        def _do(s: dict) -> None:
            s["backend_url"] = backend_url
        return self._run(
            sid, _do, {"kind": "backend_url_set", "backend_url": backend_url},
        )

    def mark_supervisor_bootstrap_received(self, sid: str) -> Optional[dict]:
        """Set `supervisor_bootstrap_received = True` after a successful
        supervisor verdict turn. Idempotent — calling twice is a no-op
        round-trip. Drives the compact-vs-full preamble gate in
        `_verdict._choose_verdict_prompt`.
        """
        def _do(s: dict) -> None:
            s["supervisor_bootstrap_received"] = True
        return self._run(
            sid, _do, {"kind": "supervisor_bootstrap_received_set"},
        )

    def separate_supervisor(self, sid: str) -> dict:
        """Graduate session ``sid``'s supervisor sidecar into a standalone
        native Better Agent session, and re-back the original supervisor as a fork
        of the graduated session.

        Pre-conditions (enforced inside the per-root lock to close the
        TOCTOU against a concurrent turn):
          * ``supervisor_enabled`` is True on ``sid``
          * ``supervisor_agent_session_id`` is set
          * ``_active_run_gate(sid)`` is False (no in-flight / queued turn)

        Post-state on ``sid`` (the X side):
          * ``supervisor_agent_session_id = None``
          * ``forked_from_supervisor_agent_sid = <old supervisor sid S1>``
            — the next supervisor verdict will pass ``--fork-session S1``
            via the fork-first-turn branch in ``orchestrator.run_turn``.
          * ``supervisor_bootstrap_received = False`` — the fork's first
            verdict re-injects the full preamble.

        Post-state on the NEW root Y:
          * Standalone root, ``orchestration_mode = "native"``.
          * ``native_agent_session_id = S1`` — Y owns the original
            supervisor claude session outright; ``--resume S1`` on its
            first turn picks up the full claude-side history.
          * ``processed_line_by_sid = {S1: N}`` where N is the line count
            of S1's claude jsonl at separation time — Y's Owned-tailer
            skips the historical entries so they aren't re-ingested.
          * ``messages`` pre-populated with the supervisor-sourced
            messages from X (the prompts injected into the supervisor
            and its verdict responses), with fresh ids, ``source``
            stripped, and ``seq`` reassigned.

        Fires:
          * ``supervisor_separated`` event on X (frontend patches X's
            supervisor fields).
          * ``created`` event on Y (frontend adds Y to the sidebar).

        Returns the new session Y (deep-copied snapshot).
        """
        # Cheap outer read for early validation + cwd/s1 capture. The
        # authoritative re-check happens inside the per-root lock below.
        x_snap = self.get(sid)
        if x_snap is None:
            raise KeyError(sid)
        if not x_snap.get("supervisor_enabled"):
            raise ValueError("supervisor not enabled")
        s1 = x_snap.get("supervisor_agent_session_id")
        if not s1:
            raise ValueError("supervisor session not yet created")
        cwd = x_snap.get("cwd") or ""

        # Baseline the future tailer cursor: count lines in S1's claude
        # jsonl NOW so Y's Owned-tailer (when Y takes its first turn)
        # skips the historical supervisor entries. Computed outside the
        # X-root lock — file IO shouldn't hold the lock. Safe because X
        # is gated idle, so S1.jsonl is not being appended.
        from orchs.jsonl_helpers import compute_jsonl_path
        baseline_n = 0
        jp = compute_jsonl_path(cwd, s1)
        if jp is not None and jp.exists():
            with open(jp, "rb") as f:
                baseline_n = sum(1 for _ in f)

        # Acquire X's root lock for atomic re-validate + snapshot + mutate.
        rid = self._root_id_for(sid)
        if rid is None:
            raise KeyError(sid)
        with self._lock_for_root(rid):
            x_live = self._cached(sid)
            if x_live is None:
                raise KeyError(sid)
            # Re-check: another thread may have toggled supervisor off
            # or nulled the sid between the outer read and now.
            if not x_live.get("supervisor_enabled"):
                raise ValueError("supervisor not enabled")
            if x_live.get("supervisor_agent_session_id") != s1:
                raise ValueError(
                    "supervisor session changed concurrently"
                )
            # TOCTOU gate: if a turn slipped between the REST handler's
            # has_active_runs() pre-check and here, reject.
            if (
                self._active_run_gate is not None
                and self._active_run_gate(sid)
            ):
                raise ValueError(
                    "cannot separate supervisor while a turn is "
                    "queued or in flight"
                )
            # Snapshot supervisor messages atomically with the mutation.
            supervisor_msgs = copy.deepcopy([
                m for m in (x_live.get("messages") or [])
                if m.get("source") == "supervisor"
            ])
            # Mutate X.
            x_live["supervisor_agent_session_id"] = None
            x_live["forked_from_supervisor_agent_sid"] = s1
            x_live["supervisor_bootstrap_received"] = False
            self._persist_root(rid, bump=True)
            # Fire under the lock so listeners see the post-mutation
            # state (mirrors the `_run` enrich-then-fire pattern).
            self._fire(sid, {
                "kind": "supervisor_separated",
                "old_supervisor_sid": s1,
            })

        # Create Y as a brand-new root in native mode. Inherits cwd /
        # model / provider / browser-harness / node from X so file refs in
        # the copied messages stay valid (same cwd).
        y = session_store.create_session(
            name=f"{x_snap.get('name') or 'session'} (supervisor)",
            model=x_snap.get("model") or config_store.default_session_model(),
            cwd=cwd,
            orchestration_mode="native",
            source=x_snap.get("source") or "web",
            provider_id=x_snap.get("provider_id"),
            reasoning_effort=x_snap.get("reasoning_effort"),
            browser_harness_enabled=bool(x_snap.get("browser_harness_enabled")),
            browser_harness_headless=bool(x_snap.get("browser_harness_headless")),
            node_id=x_snap.get("node_id") or "primary",
            # Separating the supervisor is an explicit user action; Y
            # inherits X's user-awareness.
            user_initiated=bool(x_snap.get("user_initiated", True)),
        )
        # Take ownership of S1 outright + baseline its tailer cursor.
        y["agent_session_id"] = s1
        y["processed_line_by_sid"] = {s1: baseline_n}
        # Append the snapshotted supervisor messages (fresh ids, fresh
        # seq, `source` stripped — they ARE the conversation on Y, not
        # supervisor-sourced sidecar entries).
        for src in supervisor_msgs:
            src["id"] = str(uuid.uuid4())
            src.pop("source", None)
            session_store.assign_message_seq(y, src)
            y["messages"].append(src)
        session_store.write_session_full(y, bump_updated_at=True)

        # Register in cache so subsequent get/mutate calls land.
        yid = y["id"]
        with self._lock_for_root(yid):
            self._roots[yid] = y
            self._note_root_file_written(yid)
            self._node_root_id[yid] = yid
        # Fire created so the WS broadcaster announces Y to every tab.
        self._fire(yid, {
            "kind": "created", "session": copy.deepcopy(y),
        })
        return copy.deepcopy(y)

    def set_pending_supervisor_verdict(
        self, sid: str, verdict: str, instructions: str,
    ) -> Optional[dict]:
        """Save a CONTINUE/FIX verdict that was interrupted before the
        primary could act on it. Replayed on the next user-prompted turn
        by ``replay_pending_verdict`` in the supervisor verdict loop."""
        def _do(s: dict) -> None:
            s["pending_supervisor_verdict"] = {
                "verdict": verdict,
                "instructions": instructions,
            }
        return self._run(
            sid, _do, {"kind": "pending_supervisor_verdict_set"},
        )

    def clear_pending_supervisor_verdict(self, sid: str) -> Optional[dict]:
        """Clear the pending verdict after successful replay."""
        def _do(s: dict) -> None:
            s["pending_supervisor_verdict"] = None
        return self._run(
            sid, _do, {"kind": "pending_supervisor_verdict_cleared"},
        )

    def apply_session_field(self, sid: str, field: str, value: Any) -> Optional[dict]:
        """Scoped session-record mutation routed to the existing tested setters.
        The caller (the /api/internal/session-field endpoint) has already
        verified ``field`` is in the acting extension's declared allowlist, so
        this only handles the known mutable fields. Raises ValueError for
        anything else (defense-in-depth)."""
        if field == "supervisor_enabled":
            return self.set_supervisor_enabled(sid, bool(value))
        if field == "pending_supervisor_verdict":
            v = value if isinstance(value, dict) else {}
            return self.set_pending_supervisor_verdict(
                sid, str(v.get("verdict") or ""), str(v.get("instructions") or ""),
            )
        if field == "clear_pending_supervisor_verdict":
            return self.clear_pending_supervisor_verdict(sid)
        if field == "current_todos":
            return self.set_current_todos(sid, value if isinstance(value, list) else [])
        if field == "current_tasks":
            return self.set_current_tasks(sid, value if isinstance(value, list) else [])
        raise ValueError(f"unsupported session field: {field}")


def _strip_legacy_isstreaming_on_load(root: dict) -> None:
    """Walk every node in the loaded tree and strip any baked-in
    `isStreaming` field. New writes never persist the flag (see
    `session_store.write_session_full`); this function exists for the
    one-time upgrade from a pre-refactor on-disk shape, where a
    crashed backend could leave `isStreaming: True` lingering on the
    last assistant msg.

    Side effect: if a stripped value was `True` AND `stopped_at` is
    absent, stamp `stopped_at` so the Retry button shows. Recovery's
    `_apply_integration_sync` clears `stopped_at` for any rehydrated
    alive subprocess, so this stamp is benign when the runner is in
    fact still alive — recovery overrides on its own pass."""
    now = datetime.now().isoformat()
    stack: list[dict] = [root]
    while stack:
        node = stack.pop()
        for m in node.get("messages", []):
            if "isStreaming" not in m:
                continue
            was_streaming = bool(m.pop("isStreaming"))
            if (
                was_streaming
                and m.get("role") == "assistant"
                and not m.get("stopped_at")
            ):
                m["stopped_at"] = now
        for f in node.get("forks", []) or []:
            stack.append(f)


def _event_uuid_safe(event: Any) -> Optional[str]:
    """Extract the durable UUID of one stored event. Mirrors the
    canonical `orchs.base._event_uuid` logic but as a local helper so
    session_manager doesn't import orchs (would be circular). Walks the
    same shape: top-level `uuid`, then `data.uuid`, then nested
    `data.event.data.uuid` (worker_event wrapper).
    """
    if not isinstance(event, dict):
        return None
    u = event.get("uuid")
    if isinstance(u, str) and u:
        return u
    data = event.get("data")
    if isinstance(data, dict):
        u = data.get("uuid")
        if isinstance(u, str) and u:
            return u
        inner = data.get("event")
        if isinstance(inner, dict):
            inner_data = inner.get("data")
            if isinstance(inner_data, dict):
                u = inner_data.get("uuid")
                if isinstance(u, str) and u:
                    return u
    return None


def _find_message(session: dict, msg_id: str) -> Optional[dict]:
    """Find a message by id anywhere in this session's subtree (its own
    messages OR any nested fork's messages). Msg ids are unique within
    a root tree.

    Walking forks here is load-bearing for supervisor mode: a worker
    turn runs inside `_run_turn(app_session_id=supervisor_id,
    persist_to=worker_fork_id)`. `_apply_event_to_assistant_msg`
    forwards events to `_append_event(app_session_id=supervisor_id, ...)`
    which calls us with the WORKER's msg_id — but the worker msg lives
    on the fork, not the supervisor's top-level `messages`. Without
    fork-walking, the event is silently dropped and the worker's
    assistant bubble renders empty ("No output")."""
    for m in session.get("messages") or []:
        if m.get("id") == msg_id:
            return m
    for fork in session.get("forks") or []:
        if isinstance(fork, dict):
            found = _find_message(fork, msg_id)
            if found is not None:
                return found
    return None


def _find_message_node(session: dict, msg_id: str) -> Optional[dict]:
    """Return the session/fork node that directly owns a unique message id."""
    if any(m.get("id") == msg_id for m in session.get("messages") or []):
        return session
    for fork in session.get("forks") or []:
        if isinstance(fork, dict):
            found = _find_message_node(fork, msg_id)
            if found is not None:
                return found
    return None


def session_matches_project(record: dict, project_path: str | None) -> bool:
    """Canonical project-membership check for a session or summary dict.

    A session belongs to a project when its cwd equals the project path, or
    when it carries the `all_projects` flag (visible in every project, e.g.
    the assistant singleton). Every project_path filter — backend list/facet
    paths and the frontend mirror in useSession.ts — must follow this rule."""
    if not project_path:
        return True
    if record.get("all_projects"):
        return True
    return record.get("cwd") == project_path


# Module-level singleton — every backend caller imports `manager` from here.
manager = SessionManager()
