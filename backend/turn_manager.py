"""TurnManager — turn-lifecycle authority.

Owns the per-session turn lifecycle (start, drive, retry, cancel,
recovery resume, finalize) and is the SOLE emitter of
`lifecycle.turn_*` facts on the backend event bus across every
terminal path: success-complete, cancel-stopped, error, recovery-
finalize, and worker-inner.

Coordinator (in `orchestrator.py`) delegates its core turn methods
(`run_turn`, `_drive_cli_run`, `cancel_turn`,
`_apply_event_to_assistant_msg`) through `self._ensure_tm()` which
returns this TurnManager instance.

Shared helpers (`_TRANSIENT_*`, `_is_rate_limit_attempt`,
`_is_transient_error`, `_is_stale_session_error`, `_append_todo_reminder`)
live in `backend/turn_helpers.py` — a neutral module both
`orchestrator.py` and `turn_manager.py` import from.

Worker-inner integration: `orchs/manager/_delegation.py`'s terminal
path calls `coordinator.turn_manager._publish_terminal_lifecycle(
"complete"|"stopped", reason="worker_inner")` directly — no separate
public hook on TurnManager.

Convergence-invariant contract: TurnManager owns turn LIFECYCLE
(framing, bus emits, retry/reuse, queue-drain). It does NOT own
render-tree mutation — `_apply_event_to_assistant_msg` is a thin
adapter that funnels through `OrchestrationStrategy.apply_event` in
`orchs/base.py`, the single mutation chokepoint shared with
`run_recovery._replay_and_apply`. The three live/offline/restore
ingestion paths still converge through that same funnel by
construction.
"""

import asyncio
import copy
import logging
import os
import random
import threading
import time as _time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Literal, Optional

from continuation import is_context_overflow_error
from continuation_flow import start_continuation_for
from event_bus import BusEvent, bus
from event_shape import (
    extract_output_text as _extract_output_text,
    extract_subagent_types as _extract_subagent_types,
    is_synthetic_event as _is_synthetic_event,
)
from capability_contexts import provider_capability_contexts
from runtime_skills import runtime_skill_contexts
from i18n import t
from provider import StreamEvent
from runs_dir import pid_alive as _pid_alive, salvage_complete_payload
from session_manager import manager as session_manager
from trace_collector import TraceCollector, extract_provider_result_token_usage
from turn_helpers import (
    _RATE_LIMIT_MAX_ATTEMPTS,
    _TRANSIENT_BASE_WAIT_S,
    _TRANSIENT_MAX_ATTEMPTS,
    _TRANSIENT_MAX_WAIT_S,
    _append_todo_reminder,
    _is_rate_limit_attempt,
    _is_stale_session_error,
    _is_transient_error,
)
from user_msg_lifecycle import emit_sent

logger = logging.getLogger(__name__)


# Bridged direct-WS framing types. Mirrors `_BRIDGE_EVENT_TYPES` in
# orchestrator.py — kept private here so this module is self-contained.
_BRIDGE_EVENT_TYPES = frozenset((
    "turn_complete", "turn_stopped", "turn_detached",
    "worker_creation_requested",
))


class _Cancelled(Exception):
    """Raised when a turn is cancelled by the user."""


# Streaming-eligible run kinds. Worker / adv-sync registrations
# pointing at the parent msg do NOT flip streaming.
_STREAMING_KINDS = frozenset({"manager", "native"})
_PIDLESS_RUN_STALE_AFTER_S = 30.0
_RECOVERED_CANCEL_ESCALATE_AFTER_S = 5.0
_CONTEXT_CONTINUATION_PREEMPT_RATIO = 0.90
_RATE_LIMIT_MIN_WAIT_S = 5.0
_RATE_LIMIT_FALLBACK_WAIT_S = 60.0


def _provider_capability_contexts(
    contexts: Optional[list[dict]],
    provider_kind: str,
) -> list[dict]:
    return provider_capability_contexts(contexts, provider_kind)


def _stamp_agent_type(mode: str, event_dict: dict) -> dict:
    """Stamp the orchestration mode on agent_message events.

    Manager-mode agent_message events get `agent_type: "manager"`.
    All other event types and native-mode events pass through unchanged.
    """
    if mode == "manager" and event_dict.get("type") == "agent_message":
        return {**event_dict, "agent_type": "manager"}
    return event_dict


def _rate_limit_wait_seconds(reset_dt: Optional[datetime]) -> float:
    if reset_dt is None:
        return _RATE_LIMIT_FALLBACK_WAIT_S
    reset_utc = (
        reset_dt.astimezone(timezone.utc)
        if reset_dt.tzinfo is not None
        else reset_dt.replace(tzinfo=timezone.utc)
    )
    return max(
        _RATE_LIMIT_MIN_WAIT_S,
        (reset_utc - datetime.now(timezone.utc)).total_seconds(),
    )


def _release_abandoned_queue(
    provider, run_id: str, queue, *, persist_to: str,
) -> None:
    """Hand the run queue back to the provider when the consumer loop
    exits for ANY reason (complete/error/cancel/timeout/exception) —
    the provider drains still-queued lines through the orphan funnel
    and gates future tailer dispatches off the dead queue. Providers
    without `release_queue` (no abandoned-queue handling yet) are
    skipped."""
    release = getattr(provider, "release_queue", None)
    if release is None:
        return
    try:
        release(run_id, queue, persist_to=persist_to)
    except Exception:
        logger.exception("release_queue failed for run %s", run_id)


class TurnManager:
    """Per-coordinator turn-lifecycle authority.

    Owns the turn-scoped state (cancel events, active run ids, in-flight
    assistant messages, in-flight worker panels, turn save callbacks,
    in-flight lifecycle msg ids, run_state registry, interrupted-by
    cross-refs) and the methods that mutate it (run_turn,
    _drive_cli_run, the run_state accessors).

    Coordinator owns everything else (per-session prompt queues, WS
    callbacks registry, providers, MCP delegation, sessions, etc.) and
    is reachable via `self._c` for non-turn collaborators
    (`_build_assistant_msg`, `_init_turn_messages`,
    `_finalize_turn_messages`, `_dispatch_messages_delta`,
    `broadcast_session`, `provider_for_session`, `internal_token`,
    static `_cancel_turn_fanout`, transient
    error classifiers).
    """

    # Exposed so `monitoring_state` filtering matches Coordinator's prior
    # behavior. Keep in sync if new kinds are introduced.
    _STREAMING_KINDS = _STREAMING_KINDS

    def __init__(self, coordinator) -> None:
        # Back-reference for non-turn collaborators that live on
        # Coordinator. Intentionally not a weakref — TurnManager and
        # Coordinator share lifetime by construction (one per process).
        # IMPORTANT: do NOT invoke any session_manager hot path
        # (`set_draft`, `_persist_root`, `_is_pinned`, etc.) from this
        # constructor. `Coordinator.__init__` constructs us BEFORE
        # `self.draft_store` is set; an sm hot path would call
        # `_draft_store_or_none()` which would RuntimeError on the
        # missing attr — see the ordering note in Coordinator.__init__.
        self._c = coordinator

        # ------------------------------------------------------------------
        # Turn-scoped state — moved from Coordinator.
        # ------------------------------------------------------------------
        self.cancel_events: dict[str, asyncio.Event] = {}
        self.active_run_ids: dict[str, list[str]] = {}
        self.current_assistant_msgs: dict[str, dict] = {}
        self.current_turn_workers: dict[str, list[dict]] = {}
        self._turn_save_callbacks: dict[str, Callable[[dict], Awaitable[None]]] = {}
        # `in_flight_lifecycle_msg_id` moved to UserPromptManager.
        # `_interrupted_by_msg_id` stays here — it's a turn-side
        # handoff (cancel_turn writes, run_turn pops, UPM consumes
        # via an explicit parameter passed at emit-time).
        self._interrupted_by_msg_id: dict[str, str] = {}
        # Pending cancel for the dequeue→cancel_events gap: cancel_turn
        # writes when no live turn exists but a prompt is in flight;
        # run_turn consumes right after registering its cancel_event.
        # Stale entries are popped by the processor (skip path, item
        # finally, loop exit) in orchestrator.py.
        self._pending_cancel: dict[str, object] = {}
        self._run_state: dict[str, list[dict]] = {}
        self._forced_context_overflow_once: set[str] = set()

        # Background tick: periodically prunes dead PIDs from _run_state
        # and publishes cached running/monitoring snapshots so read-only
        # endpoints never call os.kill(pid,0) on the event loop.
        self._cached_running: set[str] = set()
        self._cached_monitoring: dict[str, str] = {}
        self._cached_state_version = 0
        self._cache_lock = threading.Lock()
        self._bg_tick_started = False

    # ======================================================================
    # (ii) Single bus-emitter for lifecycle.turn_* facts.
    # ======================================================================
    async def _publish_turn_start_lifecycle(
        self,
        *,
        app_session_id: str,
        manager_session_id: Optional[str] = None,
    ) -> None:
        try:
            root_id = (
                session_manager._root_id_for(app_session_id)
                or app_session_id
            )
            payload: dict = {}
            if manager_session_id is not None:
                payload["manager_session_id"] = manager_session_id
            await bus.publish(BusEvent(
                type="lifecycle.turn_start",
                root_id=root_id,
                sid=app_session_id,
                payload=payload,
                persist=False,
            ))
        except Exception:
            logger.exception(
                "lifecycle turn_start bus publish failed sid=%s",
                app_session_id,
            )

    async def _publish_terminal_lifecycle(
        self,
        kind: Literal["complete", "stopped"],
        *,
        app_session_id: str,
        trace_id: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> None:
        """Sole emitter of `lifecycle.turn_complete` /
        `lifecycle.turn_stopped` on the bus.

        EVERY terminal path routes through here — success-complete,
        cancel-stopped, error, recovery-finalize, worker-inner. Today
        the error/recovery/worker paths in `Coordinator` emit only the
        direct WS framing and skip the bus; this helper closes that
        gap (the (ii) behavior change). The rearranger and any future
        `lifecycle.turn_*` subscriber observe every terminal.

        `reason` is an optional payload tag — "success" / "cancelled"
        / "error" / "recovery_finalize" / "worker_inner" — so
        subscribers can distinguish causes without resorting to two
        event types per cause.
        """
        try:
            root_id = (
                session_manager._root_id_for(app_session_id)
                or app_session_id
            )
            payload: dict = {}
            if trace_id is not None:
                payload["trace_id"] = trace_id
            if reason is not None:
                payload["reason"] = reason
            event_type = (
                "lifecycle.turn_complete" if kind == "complete"
                else "lifecycle.turn_stopped"
            )
            await bus.publish(BusEvent(
                type=event_type,
                root_id=root_id,
                sid=app_session_id,
                payload=payload,
                persist=False,
            ))
        except Exception:
            # The bus is fire-and-forget — a misbehaving subscriber must
            # never tear down a turn-finalize sequence. Log and move on.
            logger.exception(
                "lifecycle bus publish failed kind=%s sid=%s",
                kind, app_session_id,
            )

    # ======================================================================
    # The `user_message_*` lifecycle (5-state machine: queued / sent /
    # received / done / failed) is owned by `UserPromptManager`. See
    # backend/user_prompt_manager.py for the funnel and helpers.
    # TurnManager keeps `_interrupted_by_msg_id` (turn-side handoff,
    # written by `cancel_turn`, popped by `Coordinator.handle_prompt`
    # and passed in as a parameter at user-msg-done emit time).
    # ======================================================================

    def _pop_run_id(self, app_session_id: str, run_id: str) -> None:
        """Remove a single run_id from active_run_ids (retry cleanup)."""
        ids = self.active_run_ids.get(app_session_id)
        if ids and run_id in ids:
            ids.remove(run_id)
            if not ids:
                self.active_run_ids.pop(app_session_id, None)

    def force_context_overflow_once(self, app_session_id: str) -> None:
        self._forced_context_overflow_once.add(app_session_id)

    def _pop_forced_context_overflow_once(self, app_session_id: str) -> bool:
        if app_session_id not in self._forced_context_overflow_once:
            return False
        self._forced_context_overflow_once.remove(app_session_id)
        return True

    def request_immediate_continuation(
        self,
        app_session_id: str,
        prompt: str,
        *,
        reason: str = "agent_requested",
    ) -> bool:
        """Agent-requested IMMEDIATE continuation (`continue_in_fresh_context`
        with `when="now"`): abort the in-flight run and restart in a fresh
        provider subprocess under the same session.

        Sets the continuation flag with `when="now"`, then signals the current
        run to abort (cancel_event + fanout) WITHOUT the user-cancel
        side-effects (`_session_cancelled` / `_interrupted_by_msg_id`). The
        drive loop's cancel path detects the flag and starts the continuation
        instead of returning 'cancelled'. Returns False if no live turn is
        running to abort (caller falls back to next-turn semantics)."""
        session_manager.set_continuation_requested(
            app_session_id, prompt, reason=reason, when="now",
        )
        event = self.cancel_events.get(app_session_id)
        if not event:
            return False
        event.set()
        for run_id in self.active_run_ids.get(app_session_id, []):
            self._c._cancel_turn_fanout(run_id)
        logger.info(
            "continuation: agent-requested IMMEDIATE abort for %s",
            app_session_id[:8],
        )
        return True

    def _run_state_age_s(self, entry: dict) -> float:
        started_at = entry.get("started_at")
        if not isinstance(started_at, str):
            return _PIDLESS_RUN_STALE_AFTER_S
        try:
            return _time.time() - datetime.fromisoformat(started_at).timestamp()
        except ValueError:
            return _PIDLESS_RUN_STALE_AFTER_S

    async def _emit_attempt_terminal(
        self,
        *,
        ws_callback: Callable[[dict], Awaitable[None]],
        mode: Literal["native", "manager"],
        attempt_events: list[dict],
    ) -> None:
        for etype in ("complete", "error"):
            terminal = next(
                (e for e in attempt_events if e.get("type") == etype),
                None,
            )
            if terminal is not None:
                await ws_callback(_stamp_agent_type(mode, terminal))
                return

    # ======================================================================
    # Accessors.
    # ======================================================================
    def has_active_turn(self, app_session_id: str) -> bool:
        return app_session_id in self.cancel_events

    def has_active_runs(self, app_session_id: str) -> bool:
        """True iff a turn is in flight OR a queued prompt is pending.

        Mirrors `Coordinator.has_active_runs` — TurnManager owns the
        `active_run_ids` signal; the `_in_flight_prompts` /
        `_prompt_queues` signals stay on Coordinator (queue ownership).
        Reaches across to Coordinator for those two.
        """
        if self.active_run_ids.get(app_session_id):
            return True
        if getattr(self._c, "_in_flight_prompts", {}).get(app_session_id, 0) > 0:
            return True
        q = getattr(self._c, "_prompt_queues", {}).get(app_session_id)
        if q is not None and q.qsize() > 0:
            return True
        return False

    def _evict_stale_runs(self, app_session_id: str, mode: str) -> None:
        """Drop dead same-kind run_state leftovers before a new turn.

        Entries with a live pid (e.g. a recovered subprocess) are NEVER
        evicted — that would erase their kill levers and monitoring
        while the process keeps running and writing.
        """
        for r in list(self._run_state.get(app_session_id, [])):
            if r.get("kind") != mode:
                continue
            rid, pid = r["run_id"], r.get("pid")
            if pid and _pid_alive(pid):
                logger.error(
                    "_run_turn: run_state entry %s (pid %s) still ALIVE for "
                    "session %s — refusing to evict",
                    rid[:8], pid, app_session_id[:8],
                )
                continue
            logger.warning(
                "_run_turn: evicting stale run_state entry %s for session %s",
                rid[:8], app_session_id[:8],
            )
            self.run_state_remove(app_session_id, rid)

    async def wait_for_clear_runs(self, app_session_id: str) -> None:
        """Block until no active_run_ids remain for the session.

        Used by the prompt processor as a barrier before starting a
        turn: recovered runs registered by run_recovery (and any other
        externally-registered run) must finish — or be cancelled — before
        a new CLI subprocess may start on the same session. Fail-closed:
        waits indefinitely, logging while it waits.
        """
        waited = 0.0
        while self.active_run_ids.get(app_session_id):
            if waited == 0.0 or waited % 30 < 0.5:
                logger.info(
                    "processor barrier: session %s waiting on external runs %s",
                    app_session_id[:8],
                    [r[:8] for r in self.active_run_ids.get(app_session_id, [])],
                )
            await asyncio.sleep(0.5)
            waited += 0.5

    def get_turn_save_callback(self, app_session_id: str):
        return self._turn_save_callbacks.get(app_session_id)

    def get_in_flight_assistant_msg(self, app_session_id: str) -> Optional[dict]:
        msg = self.current_assistant_msgs.get(app_session_id)
        if msg is None:
            return None
        # `msg` is the SAME dict `apply_event` mutates — frequently from a
        # different thread (SDK callback / asyncio.to_thread) under
        # `session_manager._lock_for_root(root_id)`. An unguarded
        # `copy.deepcopy` walks the dict tree and races a concurrent key
        # change, raising 'RuntimeError: dictionary keys changed during
        # iteration'. Take the snapshot under that same per-root lock so no
        # thread can reshape the tree mid-copy.
        #
        # Lock-order safe: this acquires ONLY a session_manager root lock
        # for a READ-only deepcopy, and the callers (websocket_chat /
        # messages_replay / propose_sessions) hold no lock when invoking it.
        # The documented cross-subsystem order is event_ingester →
        # session_manager; since no event_ingester lock is taken here, that
        # order cannot invert.
        rid = session_manager._root_id_for(app_session_id)
        if rid is None:
            # No resolvable root ⇒ no `session_manager.batch(...)` can be
            # mutating the dict (batch raises KeyError without a root), so
            # the unguarded copy cannot race a mutation.
            return copy.deepcopy(msg)
        with session_manager._lock_for_root(rid):
            return copy.deepcopy(msg)

    def in_flight_event_count(self, app_session_id: str) -> int:
        """Manager-event count on the in-flight assistant message for
        `app_session_id`. Stamps a delegation panel's `insert_at`
        inline-position at worker_start so the panel renders where the
        delegation occurred instead of at the bottom. Cheap len() read —
        no deepcopy — safe under the single-threaded asyncio loop."""
        msg = self.current_assistant_msgs.get(app_session_id)
        if not msg:
            return 0
        return len(msg.get("events") or [])

    def in_flight_event_count_after_current_event(self, app_session_id: str) -> int:
        return self.in_flight_event_count(app_session_id) + 1

    # ======================================================================
    # run_state registry — relocated wholesale from Coordinator.
    # ======================================================================
    def _maybe_flip_streaming(
        self,
        app_session_id: str,
        target_message_id: Optional[str],
        value: bool,
        kind: Optional[str],
    ) -> None:
        if target_message_id is None:
            return
        if kind not in self._STREAMING_KINDS:
            return
        session_manager.set_streaming(app_session_id, target_message_id, value)

    def _dbg_runstate(self, app_session_id: str, label: str) -> None:
        """Diagnostic snapshot of _run_state + active_run_ids for a session.
        Grep `RUNSTATE_DBG` to trace why a 'Native Running' badge sticks."""
        runs = self._run_state.get(app_session_id) or []
        entries = [
            f"{(r.get('run_id') or '?')[:8]}|{r.get('kind')}|pid={r.get('pid')}"
            f"|tgt={(r.get('target_message_id') or '-')[:8]}"
            for r in runs
        ]
        logger.info(
            "RUNSTATE_DBG[%s] sid=%s n=%d active_ids=%s entries=%s",
            label, app_session_id[:8], len(runs),
            [str(x)[:8] for x in self.active_run_ids.get(app_session_id, [])],
            entries,
        )

    def run_state_add(
        self,
        app_session_id: str,
        *,
        run_id: str,
        kind: Literal["manager", "native", "worker"],
        target_message_id: Optional[str] = None,
        delegation_id: Optional[str] = None,
        pid: Optional[int] = None,
    ) -> dict:
        if kind != "worker" and target_message_id:
            current = self._run_state.get(app_session_id) or []
            self._run_state[app_session_id] = [
                r for r in current
                if not (
                    r.get("kind") == kind
                    and r.get("target_message_id") == target_message_id
                    and r.get("run_id") != run_id
                )
            ]
            if not self._run_state[app_session_id]:
                self._run_state.pop(app_session_id, None)
        now = datetime.now().isoformat()
        for entry in self._run_state.get(app_session_id) or []:
            if entry.get("run_id") != run_id:
                continue
            entry.update({
                "kind": kind,
                "target_message_id": target_message_id,
                "delegation_id": delegation_id,
                "pid": pid,
                "last_event_at": now,
            })
            self._maybe_flip_streaming(
                app_session_id, target_message_id, True, kind,
            )
            session_manager.recompute_state(app_session_id)
            self._dbg_runstate(app_session_id, f"add_existing:{run_id[:8]}:{kind}")
            return entry
        entry = {
            "run_id": run_id,
            "kind": kind,
            "target_message_id": target_message_id,
            "delegation_id": delegation_id,
            "pid": pid,
            "started_at": now,
            "last_event_at": now,
        }
        self._run_state.setdefault(app_session_id, []).append(entry)
        self._maybe_flip_streaming(
            app_session_id, target_message_id, True, kind,
        )
        session_manager.recompute_state(app_session_id)
        self._dbg_runstate(app_session_id, f"add:{run_id[:8]}:{kind}")
        return entry

    def run_state_remove(self, app_session_id: str, run_id: str) -> None:
        runs = self._run_state.get(app_session_id)
        if not runs:
            logger.info(
                "RUNSTATE_DBG[remove_noop:%s] sid=%s — no entries to remove",
                run_id[:8], app_session_id[:8],
            )
            return
        removed = [r for r in runs if r.get("run_id") == run_id]
        self._run_state[app_session_id] = [
            r for r in runs if r.get("run_id") != run_id
        ]
        if not self._run_state[app_session_id]:
            self._run_state.pop(app_session_id, None)
        for r in removed:
            self._maybe_flip_streaming(
                app_session_id,
                r.get("target_message_id"),
                False,
                r.get("kind"),
            )
        session_manager.recompute_state(app_session_id)
        self._dbg_runstate(
            app_session_id, f"remove:{run_id[:8]}:found={len(removed)}",
        )

    def is_running(self, sid: str) -> bool:
        runs = self._run_state.get(sid)
        if not runs:
            return False
        for r in runs:
            pid = r.get("pid")
            if pid is None:
                return True
            if _pid_alive(pid):
                return True
        return False

    def _has_pending_approval(self, sid: str) -> bool:
        try:
            from stores import pending_approvals
            return any(
                a.get("app_session_id") == sid
                for a in pending_approvals.list_pending()
            )
        except Exception:
            return False

    def _has_background_work(self, sid: str) -> bool:
        runs = self._run_state.get(sid)
        if not runs:
            return False
        try:
            from containment import containment
            c = containment()
        except Exception:
            return False
        for r in runs:
            run_id, pid = r.get("run_id"), r.get("pid")
            if run_id and pid and c.has_background_work(run_id, pid):
                return True
        return False

    def monitoring_state(self, sid: str) -> str:
        if not self.is_running(sid):
            return "stopped"
        if self.has_active_turn(sid) or self.has_active_runs(sid):
            return "active"
        if self._has_pending_approval(sid):
            return "blocked_on_user"
        if self._has_background_work(sid):
            return "waiting_on_background"
        return "idle"

    def _prune_dead_entries(self, sid: str) -> bool:
        runs = self._run_state.get(sid)
        if not runs:
            return False
        active_ids = set(self.active_run_ids.get(sid) or [])
        alive: list[dict] = []
        dropped: list[dict] = []
        for r in runs:
            pid = r.get("pid")
            run_id = r.get("run_id")
            if pid is None:
                if run_id in active_ids and sid in self.cancel_events:
                    alive.append(r)
                    continue
                if run_id in active_ids and r.get("retrying"):
                    alive.append(r)
                    continue
                if (
                    run_id in active_ids
                    and self._run_state_age_s(r) < _PIDLESS_RUN_STALE_AFTER_S
                ):
                    alive.append(r)
                else:
                    logger.warning(
                        "_prune_dead_entries: pidless orphan run %s on session %s — dropping",
                        (run_id or "?")[:8], sid[:8],
                    )
                    dropped.append(r)
                continue
            if _pid_alive(pid):
                alive.append(r)
            else:
                logger.warning(
                    "_prune_dead_entries: pid %s dead for run %s on session %s — dropping",
                    pid, (run_id or "?")[:8], sid[:8],
                )
                dropped.append(r)
        if len(alive) == len(runs):
            return False
        if alive:
            self._run_state[sid] = alive
        else:
            self._run_state.pop(sid, None)
        for r in dropped:
            run_id = r.get("run_id")
            if run_id:
                self._pop_run_id(sid, run_id)
            self._maybe_flip_streaming(
                sid,
                r.get("target_message_id"),
                False,
                r.get("kind"),
            )
        return True

    def tick_running_state(
        self, app_session_id: Optional[str] = None,
    ) -> None:
        sids = (
            [app_session_id]
            if app_session_id is not None
            else list(self._run_state.keys())
        )
        for sid in sids:
            try:
                self._prune_dead_entries(sid)
            except Exception:
                logger.warning(
                    "tick_running_state: prune failed for %s", sid[:8],
                    exc_info=True,
                )
        for sid in sids:
            try:
                session_manager.recompute_state(sid)
            except Exception:
                logger.warning(
                    "tick_running_state: recompute failed for %s", sid[:8],
                    exc_info=True,
                )

    # ── Background tick + cached state ────────────────────────────

    def start_background_tick(self) -> None:
        """Start a daemon thread that prunes dead PIDs and refreshes the
        cached running/monitoring state every 2 s. Called once from
        FastAPI startup. The initial tick runs synchronously before the
        thread starts so the cache is warm before the first request."""
        if self._bg_tick_started:
            return
        self._bg_tick_started = True
        # Initial synchronous tick so the cache is populated immediately.
        self._refresh_cache()
        t = threading.Thread(
            target=self._bg_tick_loop, daemon=True,
            name="tick-running-state",
        )
        t.start()

    def _bg_tick_loop(self) -> None:
        while True:
            try:
                _time.sleep(2.0)
                self._refresh_cache()
            except Exception:
                logger.exception("bg tick failed")

    def _refresh_cache(self) -> None:
        """Run tick_running_state then snapshot running/monitoring into cache."""
        self.tick_running_state()
        running: set[str] = set()
        monitoring: dict[str, str] = {}
        for sid in list(self._run_state.keys()):
            try:
                if self.is_running(sid):
                    running.add(sid)
                monitoring[sid] = self.monitoring_state(sid)
            except Exception:
                pass
        with self._cache_lock:
            if (
                self._cached_running != running
                or self._cached_monitoring != monitoring
            ):
                self._cached_state_version += 1
            self._cached_running = running
            self._cached_monitoring = monitoring

    def is_running_cached(self, sid: str) -> bool:
        """Read running state from background-tick cache. No PID probing,
        no disk reads, no lock acquisition. Safe to call from the event
        loop. Stale by up to 2 s — acceptable for sidebar badges."""
        with self._cache_lock:
            return sid in self._cached_running

    def monitoring_state_cached(self, sid: str) -> str:
        """Read monitoring state from background-tick cache. No PID
        probing, no pending-approval disk reads, no containment checks.
        Safe to call from the event loop."""
        with self._cache_lock:
            return self._cached_monitoring.get(sid, "stopped")

    def cached_state_snapshot(self) -> tuple[set[str], dict[str, str]]:
        with self._cache_lock:
            return set(self._cached_running), dict(self._cached_monitoring)

    def cached_state_version(self) -> int:
        with self._cache_lock:
            return self._cached_state_version

    def _run_state_touch(self, app_session_id: str) -> None:
        runs = self._run_state.get(app_session_id)
        if not runs:
            return
        now = datetime.now().isoformat()
        for r in runs:
            r["last_event_at"] = now

    def run_state_set_pid(
        self, app_session_id: str, run_id: str, pid: int,
    ) -> None:
        runs = self._run_state.get(app_session_id)
        if not runs:
            logger.info(
                "RUNSTATE_DBG[set_pid_noop:%s] sid=%s pid=%s — no entries",
                run_id[:8], app_session_id[:8], pid,
            )
            return
        for r in runs:
            if r.get("run_id") == run_id:
                r["pid"] = pid
                r.pop("retrying", None)
                self._dbg_runstate(
                    app_session_id, f"set_pid:{run_id[:8]}:{pid}",
                )
                return
        logger.info(
            "RUNSTATE_DBG[set_pid_nomatch:%s] sid=%s pid=%s — run_id absent",
            run_id[:8], app_session_id[:8], pid,
        )

    def run_state_clear_pid(self, app_session_id: str, run_id: str) -> None:
        runs = self._run_state.get(app_session_id)
        if not runs:
            return
        for r in runs:
            if r.get("run_id") == run_id:
                r.pop("pid", None)
                self._dbg_runstate(app_session_id, f"clear_pid:{run_id[:8]}")
                return

    def run_state_mark_retrying(self, app_session_id: str, run_id: str) -> None:
        runs = self._run_state.get(app_session_id)
        if not runs:
            return
        for r in runs:
            if r.get("run_id") == run_id:
                r["retrying"] = True
                self._dbg_runstate(app_session_id, f"retrying:{run_id[:8]}")
                return

    def _run_state_set_target(
        self, app_session_id: str, run_id: str, target_message_id: str,
    ) -> None:
        runs = self._run_state.get(app_session_id)
        if not runs:
            return
        for r in runs:
            if r.get("run_id") == run_id:
                prev = r.get("target_message_id")
                r["target_message_id"] = target_message_id
                if prev != target_message_id:
                    self._maybe_flip_streaming(
                        app_session_id,
                        target_message_id,
                        True,
                        r.get("kind"),
                    )
                return

    def get_run_state(self, app_session_id: str) -> list[dict]:
        pruned = self._prune_dead_entries(app_session_id)
        if pruned:
            session_manager.recompute_state(app_session_id)
        return copy.deepcopy(self._run_state.get(app_session_id, []))

    def get_all_run_states(self) -> dict[str, list[dict]]:
        for sid in list(self._run_state.keys()):
            if self._prune_dead_entries(sid):
                session_manager.recompute_state(sid)
        return copy.deepcopy(self._run_state)

    async def emit_run_state(self, app_session_id: str) -> None:
        snapshot = copy.deepcopy(self._run_state.get(app_session_id, []))
        logger.info(
            "RUNSTATE_DBG[emit] sid=%s runs=%s",
            app_session_id[:8],
            [
                f"{(r.get('run_id') or '?')[:8]}|{r.get('kind')}|pid={r.get('pid')}"
                for r in snapshot
            ],
        )
        await self._c.broadcast_session(
            app_session_id,
            "run_state",
            {"app_session_id": app_session_id, "runs": snapshot},
            source="orchestrator.run_state",
        )

    # ======================================================================
    # Cancellation — turn-scoped.
    # ======================================================================
    async def cancel_turn(
        self,
        app_session_id: str,
        *,
        interrupted_by_msg_id: Optional[str] = None,
    ) -> bool:
        """Cancel the current turn. Mirrors Coordinator.cancel_turn but
        owns the turn-scoped fields; reaches to Coordinator only for the
        session-cancelled flag and the static fanout helpers.

        When no live turn exists (no cancel_event), two fallbacks still
        make the cancel land:
        - fan out to any registered active_run_ids (recovered runs from
          run_recovery have run ids but no cancel_event);
        - if a dequeued prompt is mid-gap (in-flight counter > 0 but
          run_turn hasn't registered its cancel_event yet), park a
          pending cancel that run_turn consumes on registration.
        `_session_cancelled` is set only when a cancel actually lands,
        so a pure no-op cancel can't suppress the next turn's
        supervisor verdict.
        """
        event = self.cancel_events.get(app_session_id)
        if not event:
            landed = False
            for run_id in self.active_run_ids.get(app_session_id, []):
                self._c._cancel_turn_fanout(run_id)
                self._schedule_recovered_cancel_escalation(app_session_id, run_id)
                landed = True
            in_flight = getattr(self._c, "_in_flight_prompts", {}).get(
                app_session_id, 0,
            )
            if in_flight > 0:
                self._pending_cancel[app_session_id] = (
                    interrupted_by_msg_id or True
                )
                landed = True
            if landed:
                self._c._session_cancelled[app_session_id] = True
                logger.info(
                    "Cancelled (no live turn) for session %s", app_session_id,
                )
            return landed
        self._c._session_cancelled[app_session_id] = True
        if interrupted_by_msg_id:
            self._interrupted_by_msg_id[app_session_id] = interrupted_by_msg_id
        event.set()
        for run_id in self.active_run_ids.get(app_session_id, []):
            self._c._cancel_turn_fanout(run_id)
        logger.info("Cancelled turn for session %s", app_session_id)
        return True

    def _schedule_recovered_cancel_escalation(
        self, app_session_id: str, run_id: str,
    ) -> None:
        hard_cancel = getattr(self._c, "_cancel_recovered_run_fanout", None)
        if hard_cancel is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(
            self._escalate_recovered_cancel_after_grace(
                app_session_id, run_id, hard_cancel,
            ),
            name=f"recovered-cancel-{run_id[:8]}",
        )

    async def _escalate_recovered_cancel_after_grace(
        self,
        app_session_id: str,
        run_id: str,
        hard_cancel: Callable[[str], bool],
    ) -> None:
        await asyncio.sleep(_RECOVERED_CANCEL_ESCALATE_AFTER_S)
        if run_id not in self.active_run_ids.get(app_session_id, []):
            return
        runs = self._run_state.get(app_session_id) or []
        entry = next((r for r in runs if r.get("run_id") == run_id), None)
        if entry is None:
            return
        pid = entry.get("pid")
        if pid is not None and not _pid_alive(pid):
            self.run_state_remove(app_session_id, run_id)
            self._pop_run_id(app_session_id, run_id)
            return
        if hard_cancel(run_id):
            logger.warning(
                "Escalated recovered turn stop to hard cancel for session %s run %s",
                app_session_id, run_id,
            )

    # ======================================================================
    # Render-tree mutation adapter — pure dispatch into the convergence
    # funnel (`OrchestrationStrategy.apply_event` in orchs/base.py).
    # Lives here so the save_ws_callback closure inside run_turn can
    # reach it cheaply; the funnel itself is unchanged.
    # ======================================================================
    def _apply_event_to_assistant_msg(
        self,
        app_session_id: str,
        event_dict: dict,
        assistant_msg: dict,
        manager_sid_holder: dict,
        workers_list: list[dict],
        user_msg: Optional[dict] = None,
        run_id: Optional[str] = None,
        write_journal: Optional[bool] = None,
    ) -> None:
        from orchs import ApplyEventCtx, get_strategy
        root_id = session_manager._root_id_for(app_session_id)
        ctx = ApplyEventCtx(
            manager_sid_holder=manager_sid_holder,
            workers_list=workers_list,
            user_msg=user_msg,
            root_id=root_id,
            run_id=run_id,
        )
        get_strategy("native").apply_event(
            app_session_id=app_session_id,
            msg=assistant_msg,
            event=event_dict,
            ctx=ctx,
            source_is_provider_stream=True,
            write_journal=write_journal,
        )

    async def _publish_provider_stream_event(
        self,
        *,
        app_session_id: str,
        event_dict: dict,
        assistant_msg: dict,
        run_id: str,
    ) -> None:
        from event_journal import publish_event
        from orchs import get_strategy

        root_id = session_manager._root_id_for(app_session_id)
        if root_id is None:
            raise RuntimeError(f"cannot resolve root for session {app_session_id}")
        etype, data = get_strategy("native").prepare_provider_event_for_journal(
            app_session_id=app_session_id,
            event=event_dict,
        )
        await publish_event(
            session_id=root_id,
            context_id=app_session_id,
            event_type=etype,
            data=data,
            source="provider_stream",
            message_id=assistant_msg.get("id"),
            run_id=run_id,
        )

    # ======================================================================
    # The primary turn.
    # ======================================================================
    async def run_turn(
        self,
        *,
        session: dict,
        prompt: str,
        cli_prompt: str,
        app_session_id: str,
        model: str,
        cwd: str,
        ws_callback: Callable[[dict], Awaitable[None]],
        images: Optional[list],
        files: Optional[list] = None,
        trace_step_name: str,
        session_id_field: str,
        mode: Literal["native", "manager"],
        client_id: Optional[str] = None,
        supervised: bool = False,
        supervisor_agent_session_id: Optional[str] = None,
        worker_agent_session_id: Optional[str] = None,
        source: Optional[str] = None,
        persist_to: Optional[str] = None,
        user_initiated: bool = False,
        disallowed_tools: Optional[list[str]] = None,
        queue_item_id: Optional[str] = None,
        team_message: Optional[dict] = None,
        capability_contexts: Optional[list[dict]] = None,
        file_discussion_id: Optional[str] = None,
    ) -> None:
        """Behavior-identical to `Coordinator._run_turn` (today
        ~orchestrator.py:2506-3067) EXCEPT every terminal path now
        emits `lifecycle.turn_*` on the bus via
        `_publish_terminal_lifecycle` — the (ii) behavior change.

        `cli_prompt` is the exact text sent to the model and MUST be
        non-None. Callers that bypass `run_primary` (whose wrap fallback
        would otherwise supply it) are responsible for defaulting it —
        see the supervisor-direct branch in `handle_prompt`.
        """
        import traceback

        persist_to = persist_to or app_session_id
        lifecycle_msg_id = self._c.user_prompt_manager.get_in_flight_lifecycle_msg_id(
            app_session_id,
        )
        cancel_event = asyncio.Event()
        self.cancel_events[app_session_id] = cancel_event
        # A cancel may have landed in the dequeue→here gap; consume it
        # so the interrupt displaces this turn instead of being lost.
        pending = self._pending_cancel.pop(app_session_id, None)
        if pending is not None:
            self._c._session_cancelled[app_session_id] = True
            if isinstance(pending, str):
                self._interrupted_by_msg_id[app_session_id] = pending
            cancel_event.set()
        # Non-destructive: externally-registered run ids (recovery
        # retries) must survive this turn. Snapshot them so the finally
        # below restores instead of blanket-popping.
        foreign_run_ids = list(self.active_run_ids.get(app_session_id) or [])
        self.active_run_ids.setdefault(app_session_id, [])

        self.current_turn_workers[app_session_id] = []
        workers_list = self.current_turn_workers[app_session_id]

        self._evict_stale_runs(app_session_id, mode)

        turn_run_id = str(uuid.uuid4())
        self.active_run_ids.setdefault(app_session_id, []).append(turn_run_id)
        self.run_state_add(
            app_session_id,
            run_id=turn_run_id,
            kind=mode,
        )
        await self.emit_run_state(app_session_id)

        user_msg = self._c._init_turn_messages(
            session=session,
            app_session_id=persist_to,
            prompt=prompt,
            images=images,
            files=files,
            client_id=client_id,
            source=source,
            lifecycle_msg_id=lifecycle_msg_id,
            cli_prompt=cli_prompt,
            queue_item_id=queue_item_id,
            team_message=team_message,
            file_discussion_id=file_discussion_id,
        )
        if queue_item_id:
            self._c._forget_active_prompt_item(queue_item_id)

        await self._c.user_prompt_manager.notify_user_msg_persisted(
            ws_callback, persist_to, user_msg,
        )

        manager_sid_holder: dict[str, Optional[str]] = {
            "id": session.get(session_id_field)
        }

        new_msg = self._c._build_assistant_msg(
            session=session, app_session_id=app_session_id,
        )
        if source:
            new_msg["source"] = source
        if file_discussion_id:
            new_msg["file_discussion_id"] = file_discussion_id
        session_manager.append_assistant_msg(persist_to, new_msg)
        assistant_msg_holder: list[Optional[dict]] = [new_msg]
        self.current_assistant_msgs[app_session_id] = new_msg
        self._run_state_set_target(app_session_id, turn_run_id, new_msg["id"])
        try:
            from event_journal import publish_event
            root_id = session_manager._root_id_for(persist_to) or persist_to
            await publish_event(
                session_id=root_id,
                context_id=persist_to,
                event_type="turn_started",
                data={
                    "turn_id": turn_run_id,
                    "message_id": new_msg["id"],
                    "source_ts": datetime.now(timezone.utc).isoformat(),
                },
                source="orchestrator.turn",
                message_id=new_msg["id"],
                turn_id=turn_run_id,
                run_id=turn_run_id,
            )
        except Exception:
            logger.exception(
                "failed to persist turn ownership boundary for %s",
                app_session_id,
            )

        await self._c._dispatch_messages_delta(app_session_id, persist_to, new_msg)
        await self.emit_run_state(app_session_id)

        original_ws_callback = ws_callback

        async def save_ws_callback(event_dict: dict) -> None:
            if _is_synthetic_event(event_dict):
                return

            try:
                msg = assistant_msg_holder[0]
                if msg is not None:
                    await self._publish_provider_stream_event(
                        app_session_id=app_session_id,
                        event_dict=event_dict,
                        assistant_msg=msg,
                        run_id=turn_run_id,
                    )
                    with session_manager.batch(persist_to):
                        self._apply_event_to_assistant_msg(
                            app_session_id,
                            event_dict,
                            msg,
                            manager_sid_holder,
                            workers_list,
                            user_msg=user_msg,
                            run_id=turn_run_id,
                            write_journal=False,
                        )
                self._run_state_touch(app_session_id)
            except Exception as exc:
                from event_journal import EventJournalWriteError
                if isinstance(exc, EventJournalWriteError) and "writer is closed" in str(exc):
                    return
                logger.exception("provider stream journal publish failed")

            if event_dict.get("type") in _BRIDGE_EVENT_TYPES:
                try:
                    await original_ws_callback(event_dict)
                except Exception:
                    logger.warning(
                        "save_ws_callback bridge failed for %s",
                        event_dict.get("type"), exc_info=True,
                    )

        ws_callback = save_ws_callback
        self._turn_save_callbacks[app_session_id] = save_ws_callback

        trace = TraceCollector(session_id=app_session_id, user_prompt=prompt)
        trace.set_ws_callback(save_ws_callback)

        primary_result: dict = {}

        if (
            user_initiated
            and prompt and prompt.strip()
        ):
            cli_prompt = _append_todo_reminder(cli_prompt, session)

        try:
            step = trace.start_step(trace_step_name)
            step.input_prompt = cli_prompt

            await ws_callback({"type": "turn_start", "data": {
                "app_session_id": app_session_id,
                "manager_session_id": session.get(session_id_field),
            }})
            await self._publish_turn_start_lifecycle(
                app_session_id=app_session_id,
                manager_session_id=session.get(session_id_field),
            )

            # The session is resuming work, so the prior turn (even if it
            # errored) is no longer the "last" turn. Retire the error dot
            # up-front; if THIS turn errors it gets re-set below. Deliberately
            # NOT tied to view/seen state — the dot reflects "did the most
            # recent turn error?", nothing else.
            try:
                session_manager.clear_unseen_error(app_session_id)
            except Exception:
                logger.debug("clear_unseen_error at turn start failed", exc_info=True)

            current_sid = session.get(session_id_field)
            forked_from_field = (
                "forked_from_supervisor_agent_sid"
                if session_id_field == "supervisor_agent_session_id"
                else "forked_from_agent_sid"
            )
            forked_from_sid = session.get(forked_from_field)
            is_fork_first_turn = not current_sid and bool(forked_from_sid)
            resume_sid = current_sid or (forked_from_sid if is_fork_first_turn else None)
            primary_result = await self._drive_cli_run(
                prompt=cli_prompt,
                images=images,
                files=files,
                cwd=cwd,
                model=model,
                session_id=resume_sid,
                ws_callback=ws_callback,
                app_session_id=app_session_id,
                source=source,
                cancel_event=cancel_event,
                session_id_field=session_id_field,
                mode=mode,
                fork=is_fork_first_turn,
                supervised=supervised,
                supervisor_agent_session_id=supervisor_agent_session_id,
                worker_agent_session_id=worker_agent_session_id,
                primary_session_id=session.get("id"),
                user_initiated=user_initiated,
                turn_run_id=turn_run_id,
                disallowed_tools=disallowed_tools,
                capability_contexts=capability_contexts,
            )

            if cancel_event.is_set():
                raise _Cancelled()

            persist_id = session["id"]
            new_sid = primary_result.get("session_id")
            if is_fork_first_turn and new_sid and session_id_field != "supervisor_agent_session_id":
                try:
                    parent_lines = int(session.get("parent_line_count_at_fork") or 0)
                except (TypeError, ValueError):
                    parent_lines = 0
                if parent_lines > 0:
                    try:
                        session_manager.advance_processed_lines(
                            persist_id,
                            new_sid,
                            parent_lines,
                            bump_updated_at=False,
                        )
                    except Exception:
                        logger.exception("advance_processed_lines on first-turn fork failed")
            if (
                new_sid
                and new_sid != session.get(session_id_field)
                and session_id_field != "supervisor_agent_session_id"
            ):
                provider = self._c.provider_for_session(app_session_id)
                persist_mode = session.get("orchestration_mode") or mode
                with session_manager.batch(persist_id):
                    session_manager.set_agent_sid(
                        persist_id, persist_mode, new_sid,
                        provider_id=provider.id, model=model,
                    )
                    if session.get("forked_from_agent_sid"):
                        session_manager.clear_forked_from(persist_id)
                session = session_manager.get(persist_id) or session

            if (
                session_id_field == "supervisor_agent_session_id"
                and is_fork_first_turn
                and session.get("forked_from_supervisor_agent_sid")
            ):
                session_manager.clear_forked_from_supervisor(persist_id)
                session = session_manager.get(persist_id) or session

            step.raw_output = _extract_output_text(primary_result.get("events", []))
            step.token_usage = extract_provider_result_token_usage(primary_result)
            step.subagent_types = _extract_subagent_types(primary_result.get("events", []))
            if primary_result.get("error"):
                step.error = primary_result["error"]

            cw = primary_result.get("context_window")
            if cw:
                session_manager.set_context_window(persist_id, cw)
            context_tokens = primary_result.get("context_tokens")
            if context_tokens:
                session_manager.set_context_tokens(persist_id, context_tokens)

            turn_error = primary_result.get("error") or ""
            if (
                not primary_result.get("success")
                and turn_error
                and session.get(session_id_field)
                and _is_stale_session_error(turn_error)
            ):
                logger.warning(
                    "clearing stale %s=%s for session %s — "
                    "resume target not found by runner",
                    session_id_field,
                    session.get(session_id_field),
                    app_session_id,
                )
                persist_mode = session.get("orchestration_mode") or mode
                with session_manager.batch(persist_id):
                    session_manager.set_agent_sid(
                        persist_id, persist_mode, None,
                    )
                session = session_manager.get(persist_id) or session

            await trace.end_step(step)

            if lifecycle_msg_id:
                try:
                    from orchs import get_strategy
                    get_strategy(mode).record_turn_result(
                        lifecycle_msg_id,
                        role=mode,
                        success=bool(primary_result.get("success")),
                        token_usage=extract_provider_result_token_usage(primary_result),
                        error=primary_result.get("error"),
                        agent_sid=primary_result.get("session_id"),
                    )
                except Exception:
                    logger.debug(
                        "lifecycle: record_turn_result failed",
                        exc_info=True,
                    )

            # Turn-join: a sender turn is not complete while mssg work it
            # initiated is still running. Await those target turns before
            # emitting turn_complete (no-op when the sender fired none).
            await self._c.await_outstanding_mssg(app_session_id)

            await ws_callback({"type": "turn_complete", "data": {
                "app_session_id": persist_to,
                "success": primary_result.get("success", False),
                "session_id": primary_result.get("session_id"),
                "token_usage": extract_provider_result_token_usage(primary_result),
            }})

            trace.finalize()
            trace.save()

            workers = list(workers_list)
            workers_used = [w["worker_session_id"] for w in workers]
            finalized_msg = assistant_msg_holder[0]
            self._c._finalize_turn_messages(
                session=session,
                app_session_id=persist_to,
                user_msg=user_msg,
                assistant_msg=finalized_msg,
                primary_result=primary_result,
                workers=workers,
                stopped_at=None,
                trace_id=trace.trace_id,
            )

            if finalized_msg:
                await self._c._dispatch_messages_delta(
                    app_session_id,
                    persist_to,
                    finalized_msg,
                    omit_render_events=True,
                )

            await ws_callback({"type": "turn_complete", "data": {
                "app_session_id": persist_to,
                "workers_used": workers_used,
                "total_token_usage": session.get("token_usage_total", {}),
                "trace_id": trace.trace_id,
            }})

            if primary_result.get("success"):
                await self._publish_terminal_lifecycle(
                    "complete",
                    app_session_id=app_session_id,
                    trace_id=trace.trace_id,
                    reason="success",
                )
            else:
                await self._publish_terminal_lifecycle(
                    "stopped",
                    app_session_id=app_session_id,
                    trace_id=trace.trace_id,
                    reason="error",
                )

        except _Cancelled:
            logger.info("Turn cancelled for session %s", app_session_id)
            trace.finalize()
            trace.save()

            workers = list(workers_list)
            workers_used = [w["worker_session_id"] for w in workers]
            finalized_msg = assistant_msg_holder[0]
            interrupted_by = self._interrupted_by_msg_id.pop(app_session_id, None)
            self._c._finalize_turn_messages(
                session=session,
                app_session_id=persist_to,
                user_msg=user_msg,
                assistant_msg=finalized_msg,
                primary_result=primary_result,
                workers=workers,
                stopped_at=datetime.now().isoformat(),
                trace_id=trace.trace_id,
                interrupted_by_msg_id=interrupted_by,
            )

            if finalized_msg:
                await self._c._dispatch_messages_delta(
                    app_session_id,
                    persist_to,
                    finalized_msg,
                    omit_render_events=True,
                )

            await ws_callback({"type": "turn_stopped", "data": {
                "app_session_id": persist_to,
                "stopped_at": datetime.now().isoformat(),
                "workers_used": workers_used,
                "interrupted_by_msg_id": interrupted_by,
                "trace_id": trace.trace_id,
            }})

            # (ii): single bus emit — cancel terminal.
            await self._publish_terminal_lifecycle(
                "stopped",
                app_session_id=app_session_id,
                trace_id=trace.trace_id,
                reason="cancelled",
            )

        except asyncio.CancelledError:
            logger.info(
                "Turn task cancelled (likely shutdown) for session %s",
                app_session_id,
            )
            try:
                trace.finalize()
                trace.save()
            except Exception:
                logger.exception("Failed to save trace on task cancel")
            try:
                await ws_callback({"type": "turn_detached", "data": {
                    "app_session_id": persist_to,
                    "msg_id": assistant_msg_holder[0]["id"] if assistant_msg_holder[0] else None,
                    "trace_id": trace.trace_id,
                }})
            except Exception:
                logger.exception("Failed to emit turn_detached WS event")
            # NOTE: detached is NOT a terminal in the lifecycle sense —
            # the runner is still alive and a fresh backend will pick
            # it up via run_recovery. No lifecycle.turn_* emit.
            raise

        except Exception as e:
            logger.exception("turn failed for session %s", app_session_id)
            error_text = f"{type(e).__name__}: {e}\n\n{traceback.format_exc()}"
            try:
                trace.finalize()
                trace.save()
            except Exception:
                logger.exception("Failed to save trace during error handling")
            try:
                self._c._finalize_turn_messages(
                    session=session,
                    app_session_id=persist_to,
                    user_msg=user_msg,
                    assistant_msg=assistant_msg_holder[0],
                    primary_result=primary_result,
                    workers=list(workers_list),
                    stopped_at=None,
                    trace_id=trace.trace_id,
                    error_text=error_text,
                )
            except Exception:
                logger.exception("Failed to persist user message during error handling")
            await ws_callback({"type": "error", "data": {
                "app_session_id": persist_to, "error": error_text,
            }})
            # The error dot is set inside `_finalize_turn_messages` (called
            # just above with error_text) — the single chokepoint covering
            # both exception and non-exception failure paths.

            # (ii) NEW: error terminal now publishes lifecycle.turn_stopped.
            # Pre-cutover the error path emitted only the direct
            # `error` WS frame and skipped the bus, leaving the
            # rearranger and other lifecycle subscribers blind to error
            # terminals. Treat as "stopped" since the turn did not
            # complete successfully.
            await self._publish_terminal_lifecycle(
                "stopped",
                app_session_id=app_session_id,
                trace_id=trace.trace_id,
                reason="error",
            )

        finally:
            self.cancel_events.pop(app_session_id, None)
            # Keep foreign run ids (recovery retries registered outside
            # this turn) — drop only what this turn added.
            _remaining = [
                r for r in self.active_run_ids.get(app_session_id, [])
                if r in foreign_run_ids
            ]
            if _remaining:
                self.active_run_ids[app_session_id] = _remaining
            else:
                self.active_run_ids.pop(app_session_id, None)
            self.current_turn_workers.pop(app_session_id, None)
            self.current_assistant_msgs.pop(app_session_id, None)
            # Pre-existing latent bug in the original Coordinator._run_turn
            # (orchestrator.py:3057+): the cancel branch pops
            # `_interrupted_by_msg_id` but the success / error / detached
            # branches don't. A cancel followed by an unrelated exception
            # would leak the entry until the next cancel clobbers it.
            # Fixed here in the finally so every terminal path clears it.
            self._interrupted_by_msg_id.pop(app_session_id, None)
            self.run_state_remove(app_session_id, turn_run_id)
            try:
                await self.emit_run_state(app_session_id)
            except Exception:
                pass
            self._turn_save_callbacks.pop(app_session_id, None)

    # ======================================================================
    # CLI driver — spawn one runner.py and stream its events.
    # ======================================================================
    async def _drive_cli_run(
        self,
        *,
        prompt: str,
        images: Optional[list] = None,
        files: Optional[list] = None,
        cwd: str,
        model: str,
        session_id: Optional[str],
        ws_callback: Callable[[dict], Awaitable[None]],
        app_session_id: str,
        cancel_event: asyncio.Event,
        session_id_field: str,
        mode: Literal["native", "manager"],
        fork: bool = False,
        supervised: bool = False,
        supervisor_agent_session_id: Optional[str] = None,
        worker_agent_session_id: Optional[str] = None,
        primary_session_id: Optional[str] = None,
        user_initiated: bool = False,
        turn_run_id: str,
        source: Optional[str] = None,
        disallowed_tools: Optional[list[str]] = None,
        capability_contexts: Optional[list[dict]] = None,
    ) -> dict:
        loop = asyncio.get_running_loop()

        _session_rec = session_manager.get(primary_session_id or app_session_id)
        bt_enabled = bool((_session_rec or {}).get("browser_harness_enabled", False))
        reasoning_effort = (_session_rec or {}).get("reasoning_effort")

        backend_url: Optional[str] = None
        internal_token: Optional[str] = None
        if mode == "manager" or supervised or bt_enabled or user_initiated:
            from env_compat import get_env
            backend_url = (_session_rec or {}).get("backend_url") or get_env(
                "BETTER_CLAUDE_BACKEND_URL", "http://localhost:8000"
            )
            internal_token = self._c.internal_token

        provider = self._c.provider_for_session(primary_session_id or app_session_id)
        provider_kind = getattr(provider, "KIND", "")
        run_setting_sources: Optional[list[str]] = (
            [] if supervised else None
        )

        collected: list[dict] = []
        discovered_session_id: Optional[str] = None
        current_session_id = session_id
        # Read continuation_chain from session for the provider → runner.
        _session_rec_chain = (
            session_manager.get(primary_session_id or app_session_id) or {}
        ).get("continuation_chain") or []
        provider_run_config = (_session_rec or {}).get("provider_run_config") or None
        session_capability_contexts = (
            _session_rec or {}
        ).get("capability_contexts") or []
        runtime_capability_contexts = runtime_skill_contexts(
            cwd,
            bare_config=bool((_session_rec or {}).get("bare_config")),
        )
        run_capability_contexts = _provider_capability_contexts(
            [*session_capability_contexts, *(capability_contexts or [])],
            provider_kind,
        )
        run_capability_contexts = [
            *runtime_capability_contexts,
            *run_capability_contexts,
        ]
        transient_attempt = 0
        rate_limit_attempt = 0
        in_flight_msg = self.current_assistant_msgs.get(app_session_id)
        if in_flight_msg:
            transient_attempt = int(
                in_flight_msg.get("transient_attempt") or 0
            )
        continuation_active_msg_id: Optional[str] = None
        # Count of auto-retries (rate-limit / transient) that actually
        # fired for this turn. On a SUCCESSFUL terminal we stamp this on
        # the message so the user can tell a turn was auto-retried and
        # recovered, instead of it looking like a clean first-try run.
        auto_retry_count = 0
        auto_retry_kinds: set[str] = set()

        def _clear_continuation_active() -> None:
            nonlocal continuation_active_msg_id
            if not continuation_active_msg_id:
                return
            session_manager.set_msg_continuation_active(
                app_session_id,
                continuation_active_msg_id,
                None,
            )
            continuation_active_msg_id = None

        def _should_preempt_context_continuation() -> bool:
            import provider_manifest
            _spec = provider_manifest.spec_for(provider_kind)
            if not (_spec and _spec.context_continuation):
                return False
            if not current_session_id:
                return False
            import user_prefs
            if user_prefs.get_context_strategy() != "continuation":
                return False
            session = session_manager.get(primary_session_id or app_session_id) or {}
            tokens = session.get("context_tokens")
            window = session.get("context_window")
            if not isinstance(tokens, int) or not isinstance(window, int) or window <= 0:
                return False
            return tokens >= int(window * _CONTEXT_CONTINUATION_PREEMPT_RATIO)

        def _start_context_continuation(
            old_provider_sid: Optional[str], *, reason: str = "context_exceeded",
        ) -> int:
            nonlocal current_session_id, discovered_session_id, prompt
            nonlocal _session_rec_chain, continuation_active_msg_id
            continuation = start_continuation_for(
                session_manager=session_manager,
                app_session_id=primary_session_id or app_session_id,
                prompt=prompt,
                old_provider_sid=old_provider_sid,
                reason=reason,
            )
            _session_rec_chain = continuation.continuation_chain
            current_session_id = None
            discovered_session_id = None
            prompt = continuation.prompt

            _in_flight = self.current_assistant_msgs.get(app_session_id)
            _msg_id = _in_flight.get("id") if _in_flight else None
            if _msg_id:
                session_manager.set_msg_continuation_active(
                    app_session_id, _msg_id, continuation.chain_depth,
                )
                continuation_active_msg_id = _msg_id
            return continuation.chain_depth

        def _should_preempt_selector_change_continuation() -> bool:
            if not current_session_id:
                return False
            session_rec = session_manager.get(primary_session_id or app_session_id) or {}
            if session_id_field == "supervisor_agent_session_id":
                last_prov = session_rec.get("last_active_supervisor_provider_id")
                last_mod = session_rec.get("last_active_supervisor_model")
            else:
                last_prov = session_rec.get("last_active_provider_id")
                last_mod = session_rec.get("last_active_model")

            current_prov_id = session_rec.get("provider_id")
            current_model = session_rec.get("model")

            if last_prov is not None and current_prov_id != last_prov:
                return True
            if last_mod is not None and current_model != last_mod:
                return True
            return False

        def _refresh_provider_context() -> None:
            nonlocal _session_rec, reasoning_effort, provider, provider_kind
            nonlocal _session_rec_chain, provider_run_config
            nonlocal session_capability_contexts, runtime_capability_contexts
            nonlocal run_capability_contexts, model
            _session_rec = session_manager.get(primary_session_id or app_session_id) or {}
            reasoning_effort = _session_rec.get("reasoning_effort")
            session_model = _session_rec.get("model")
            if isinstance(session_model, str) and session_model.strip():
                model = session_model
            provider = self._c.provider_for_session(primary_session_id or app_session_id)
            provider_kind = getattr(provider, "KIND", "")
            _session_rec_chain = _session_rec.get("continuation_chain") or []
            provider_run_config = _session_rec.get("provider_run_config") or None
            session_capability_contexts = _session_rec.get("capability_contexts") or []
            runtime_capability_contexts = runtime_skill_contexts(
                cwd,
                bare_config=bool(_session_rec.get("bare_config")),
            )
            run_capability_contexts = _provider_capability_contexts(
                [*session_capability_contexts, *(capability_contexts or [])],
                provider_kind,
            )
            run_capability_contexts = [
                *runtime_capability_contexts,
                *run_capability_contexts,
            ]

        def _start_selector_change_continuation(old_provider_sid: Optional[str]) -> int:
            nonlocal current_session_id, discovered_session_id, prompt
            nonlocal _session_rec_chain, continuation_active_msg_id
            continuation = start_continuation_for(
                session_manager=session_manager,
                app_session_id=primary_session_id or app_session_id,
                prompt=prompt,
                old_provider_sid=old_provider_sid,
                reason="selector_changed",
            )
            _session_rec_chain = continuation.continuation_chain
            current_session_id = None
            discovered_session_id = None
            prompt = continuation.prompt

            _in_flight = self.current_assistant_msgs.get(app_session_id)
            _msg_id = _in_flight.get("id") if _in_flight else None
            if _msg_id:
                session_manager.set_msg_continuation_active(
                    app_session_id, _msg_id, continuation.chain_depth,
                )
                continuation_active_msg_id = _msg_id
            return continuation.chain_depth

        while True:
            _refresh_provider_context()
            if cancel_event.is_set():
                # Displaced before spawn (pending-cancel consumed by
                # run_turn, or cancel landed between retries) — don't
                # start a CLI subprocess for a turn that's already dead.
                return {
                    "success": False,
                    "session_id": discovered_session_id,
                    "events": collected,
                    "error": t("runner.cancelled"),
                    "token_usage": None,
                }
            if _should_preempt_context_continuation():
                old_provider_sid = current_session_id
                chain_depth = _start_context_continuation(old_provider_sid)
                logger.info(
                    "continuation: preempting native compaction for %s "
                    "(provider=%s chain depth %d, old sid %s)",
                    app_session_id[:8],
                    provider_kind or "unknown",
                    chain_depth,
                    (old_provider_sid or "none")[:8],
                )
                continue
            if _should_preempt_selector_change_continuation():
                old_provider_sid = current_session_id
                chain_depth = _start_selector_change_continuation(old_provider_sid)
                logger.info(
                    "continuation: preempting due to provider/model change for %s "
                    "(provider=%s chain depth %d, old sid %s)",
                    app_session_id[:8],
                    provider_kind or "unknown",
                    chain_depth,
                    (old_provider_sid or "none")[:8],
                )
                continue
            run_id = str(uuid.uuid4())
            queue: asyncio.Queue[StreamEvent] = asyncio.Queue()
            force_context_overflow = self._pop_forced_context_overflow_once(
                primary_session_id or app_session_id
            )

            attempt_events: list[dict] = []
            attempt_cancelled = False

            if force_context_overflow:
                attempt_events.append({
                    "type": "complete",
                    "data": {
                        "success": False,
                        "error": "context_window_exceeded",
                        "session_id": current_session_id,
                        "token_usage": None,
                    },
                })
            else:
                spawn_started = _time.monotonic()
                import startup_recovery_gate
                await startup_recovery_gate.wait_for_recovery_ready()
                target_message_id = (
                    self.current_assistant_msgs.get(app_session_id) or {}
                ).get("id")
                await asyncio.to_thread(
                    provider.start_run,
                    run_id=run_id,
                    prompt=prompt,
                    images=images,
                    files=files,
                    cwd=cwd,
                    loop=loop,
                    queue=queue,
                    model=model,
                    reasoning_effort=reasoning_effort,
                    session_id=current_session_id,
                    mode=mode,
                    app_session_id=app_session_id,
                    setting_sources=run_setting_sources,
                    backend_url=backend_url,
                    internal_token=internal_token,
                    fork=fork,
                    supervised=supervised,
                    supervisor_agent_session_id=supervisor_agent_session_id,
                    worker_agent_session_id=worker_agent_session_id,
                    browser_harness_enabled=bt_enabled,
                    open_file_panel_enabled=user_initiated,
                    working_mode=(_session_rec or {}).get("working_mode"),
                    continuation_chain=_session_rec_chain or None,
                    disallowed_tools=disallowed_tools,
                    provider_run_config=provider_run_config,
                    capability_contexts=run_capability_contexts,
                    target_message_id=target_message_id,
                    turn_run_id=turn_run_id,
                )
                spawn_elapsed = _time.monotonic() - spawn_started
                if spawn_elapsed > 2.0:
                    logger.warning(
                        "provider.start_run slow %.3fs provider=%s run=%s session=%s",
                        spawn_elapsed,
                        provider_kind or "unknown",
                        run_id[:8],
                        app_session_id[:8],
                    )

                lifecycle_msg_id = self._c.user_prompt_manager.get_in_flight_lifecycle_msg_id(
                    app_session_id,
                )
                if lifecycle_msg_id:
                    # Record delivery BEFORE the emit so a cancel landing
                    # in the emit gap still sees the prompt as sent.
                    self._c.user_prompt_manager.mark_sent(lifecycle_msg_id)
                    try:
                        await emit_sent(
                            app_session_id=app_session_id,
                            lifecycle_msg_id=lifecycle_msg_id,
                            run_id=run_id,
                            agent_sid=current_session_id,
                        )
                    except Exception:
                        logger.debug("lifecycle: emit_sent failed", exc_info=True)

                self.active_run_ids.setdefault(app_session_id, []).append(run_id)

                provider_rs = provider._runs.get(run_id)
                if provider_rs and provider_rs.popen.pid:
                    self.run_state_set_pid(
                        app_session_id, turn_run_id, provider_rs.popen.pid,
                    )
                    await self.emit_run_state(app_session_id)

                try:
                    while True:
                        get_task = asyncio.create_task(queue.get())
                        cancel_task = asyncio.create_task(cancel_event.wait())
                        try:
                            done, _ = await asyncio.wait(
                                [get_task, cancel_task],
                                return_when=asyncio.FIRST_COMPLETED,
                                timeout=15,
                            )
                        finally:
                            for _p in (get_task, cancel_task):
                                if not _p.done():
                                    _p.cancel()

                        if not done and provider.is_running(run_id):
                            continue
                        if not done and not provider.is_running(run_id):
                            logger.warning(
                                "runner dead but no complete event for %s — "
                                "synthesizing failure",
                                run_id,
                            )
                            captured_complete = False
                            try:
                                late = await asyncio.wait_for(queue.get(), timeout=1)
                                late_dict = {"type": late.type, "data": late.data}
                                if not _is_synthetic_event(late_dict):
                                    attempt_events.append(late_dict)
                                if late.type == "complete":
                                    captured_complete = True
                            except (asyncio.TimeoutError, Exception):
                                pass
                            if not captured_complete:
                                # The runner frequently succeeded and wrote
                                # complete.json just before exiting, but the
                                # provider's complete event lost the race
                                # with this dead-process check. Trust the
                                # on-disk authority instead of fabricating a
                                # false failure.
                                salvaged = salvage_complete_payload(run_id)
                                if salvaged is not None:
                                    logger.info(
                                        "turn_manager: salvaged on-disk "
                                        "complete.json for %s success=%s",
                                        run_id, salvaged.get("success"),
                                    )
                                    attempt_events.append({
                                        "type": "complete",
                                        "data": {
                                            "success": salvaged.get("success", False),
                                            "error": salvaged.get("error"),
                                            "session_id": discovered_session_id,
                                            "token_usage": salvaged.get("token_usage"),
                                        },
                                    })
                                else:
                                    attempt_events.append({
                                        "type": "complete",
                                        "data": {
                                            "success": False,
                                            "error": "runner exited without delivering a complete event",
                                            "session_id": discovered_session_id,
                                            "token_usage": None,
                                        },
                                    })
                            break

                        if cancel_task in done and get_task not in done:
                            provider.cancel_turn(run_id)
                            # Settle barrier: the runner drains the interrupted
                            # CLI's wind-down tail for up to 15s
                            # (runner._drain_until_result) and keeps emitting
                            # events (tool aborts, ResultMessage) while doing
                            # so. Consume that tail HERE — onto THIS turn's
                            # message via ws_callback — until a terminal event
                            # or the runner exits. Bailing early would leave the
                            # tail to the backup tailer's orphan ingest, which
                            # later seq-brackets it onto the NEXT turn's message
                            # (interleaved-turns bug).
                            _deadline = asyncio.get_running_loop().time() + 16
                            while True:
                                _remaining = _deadline - asyncio.get_running_loop().time()
                                if _remaining <= 0:
                                    logger.warning(
                                        "cancel drain timed out for run %s", run_id[:8],
                                    )
                                    break
                                try:
                                    event = await asyncio.wait_for(
                                        queue.get(), timeout=min(_remaining, 1.0),
                                    )
                                except asyncio.TimeoutError:
                                    if not provider.is_running(run_id):
                                        break
                                    continue
                                except Exception:
                                    break
                                event_dict = {"type": event.type, "data": event.data}
                                if not _is_synthetic_event(event_dict):
                                    attempt_events.append(event_dict)
                                    try:
                                        await ws_callback(
                                            _stamp_agent_type(mode, event_dict),
                                        )
                                    except Exception:
                                        logger.debug(
                                            "cancel drain ws_callback failed",
                                            exc_info=True,
                                        )
                                if event.type in ("complete", "error"):
                                    break
                            attempt_cancelled = True
                            break

                        event: StreamEvent = get_task.result()
                        event_dict = {"type": event.type, "data": event.data}
                        if not _is_synthetic_event(event_dict):
                            attempt_events.append(event_dict)
                            if event.type != "session_discovered":
                                _clear_continuation_active()

                        if event.type == "session_discovered":
                            sid = event.data.get("session_id")
                            if sid and sid != discovered_session_id:
                                discovered_session_id = sid
                                if session_id_field == "supervisor_agent_session_id":
                                    discovery_mode = "supervisor"
                                else:
                                    discovery_mode = (
                                        session_manager.get_field(
                                            primary_session_id or app_session_id,
                                            "orchestration_mode",
                                        )
                                        or "manager"
                                    )
                                session_manager.set_agent_sid(
                                    primary_session_id or app_session_id,
                                    discovery_mode, sid,
                                    provider_id=provider.id,
                                    model=model,
                                )

                        if event.type in ("complete", "error"):
                            break

                        await ws_callback(_stamp_agent_type(mode, event_dict))

                finally:
                    _release_abandoned_queue(
                        provider, run_id, queue,
                        persist_to=worker_agent_session_id or app_session_id,
                    )

            collected.extend(attempt_events)

            if attempt_cancelled:
                # Agent-requested IMMEDIATE continuation (`when="now"`): the
                # abort landed — restart in a fresh provider subprocess under
                # the SAME session with the queued prompt. Clear the cancel
                # signal so the next iteration proceeds. Only fires for
                # `when="now"`; a plain user-cancel with a stale next-turn
                # flag falls through to the cancelled return.
                requested = session_manager.pop_continuation_requested(
                    primary_session_id or app_session_id,
                )
                if requested and requested.get("when") == "now":
                    cancel_event.clear()
                    self._c._session_cancelled.pop(app_session_id, None)
                    prompt = requested.get("prompt") or ""
                    _chain_depth = _start_context_continuation(
                        discovered_session_id or current_session_id,
                        reason="agent_requested",
                    )
                    logger.info(
                        "continuation: agent-requested IMMEDIATE restart for "
                        "%s (chain depth %d)",
                        app_session_id[:8], _chain_depth,
                    )
                    self._pop_run_id(app_session_id, run_id)
                    continue
                _clear_continuation_active()
                return {
                    "success": False,
                    "session_id": discovered_session_id,
                    "events": collected,
                    "error": t("runner.cancelled"),
                    "token_usage": None,
                }

            complete = next(
                (e for e in attempt_events if e["type"] == "complete"), None,
            )
            complete_data = (complete.get("data") or {}) if complete else {}
            success = bool(complete and complete_data.get("success"))
            new_sid = (
                complete_data.get("session_id")
                or discovered_session_id
            )
            error = next(
                ((e.get("data") or {}).get("error")
                 for e in attempt_events if e["type"] == "error"),
                None,
            ) or complete_data.get("error")

            # ── Context-window overflow → continuation ────────────────
            # When context_strategy is "continuation" and the provider hits
            # its context limit, start a fresh provider subprocess under the
            # SAME Better Agent session. The old provider session ID is recorded in
            # continuation_chain so prior transcript context remains reachable.
            if (
                not success
                and is_context_overflow_error(error)
            ):
                import user_prefs
                if user_prefs.get_context_strategy() == "continuation":
                    old_provider_sid = new_sid or current_session_id
                    chain_depth = _start_context_continuation(old_provider_sid)

                    logger.info(
                        "continuation: fresh subprocess for %s "
                        "(provider=%s chain depth %d, old sid %s)",
                        app_session_id[:8],
                        provider_kind or "unknown",
                        chain_depth,
                        (old_provider_sid or "none")[:8],
                    )

                    self._pop_run_id(app_session_id, run_id)
                    continue  # Retry loop → fresh start_run with no session_id

            if (not success
                    and _is_rate_limit_attempt(error, attempt_events)
                    and rate_limit_attempt < _RATE_LIMIT_MAX_ATTEMPTS):
                rate_limit_attempt += 1
                in_flight = self.current_assistant_msgs.get(app_session_id)
                assistant_msg_id = in_flight.get("id") if in_flight else None
                reset_dt = provider.parse_rate_limit(error, attempt_events)
                wait_s = _rate_limit_wait_seconds(reset_dt)
                retry_at = (datetime.now(timezone.utc) + timedelta(seconds=wait_s)).isoformat()
                if assistant_msg_id:
                    session_manager.set_msg_retrying_until(
                        app_session_id, assistant_msg_id, retry_at,
                        error_text=error or "Rate limit exceeded",
                    )
                self.run_state_clear_pid(app_session_id, turn_run_id)
                self.run_state_mark_retrying(app_session_id, turn_run_id)
                await self.emit_run_state(app_session_id)
                try:
                    await asyncio.wait_for(cancel_event.wait(), timeout=wait_s)
                    if assistant_msg_id:
                        session_manager.set_msg_retrying_until(
                            app_session_id, assistant_msg_id, None,
                        )
                    requested = session_manager.pop_continuation_requested(
                        primary_session_id or app_session_id,
                    )
                    if requested and requested.get("when") == "now":
                        cancel_event.clear()
                        self._c._session_cancelled.pop(app_session_id, None)
                        prompt = requested.get("prompt") or ""
                        _chain_depth = _start_context_continuation(
                            new_sid or current_session_id,
                            reason=requested.get("reason") or "agent_requested",
                        )
                        logger.info(
                            "continuation: rate-limit provider switch restart for "
                            "%s (chain depth %d)",
                            app_session_id[:8], _chain_depth,
                        )
                        self._pop_run_id(app_session_id, run_id)
                        continue
                    _clear_continuation_active()
                    return {
                        "success": False,
                        "session_id": new_sid,
                        "events": collected,
                        "error": t("runner.cancelled"),
                        "token_usage": None,
                    }
                except asyncio.TimeoutError:
                    pass
                if assistant_msg_id:
                    session_manager.set_msg_retrying_until(
                        app_session_id, assistant_msg_id, None,
                    )
                self._pop_run_id(app_session_id, run_id)
                current_session_id = new_sid or current_session_id
                auto_retry_count += 1
                auto_retry_kinds.add("rate_limit")
                continue

            if not success and error and transient_attempt < _TRANSIENT_MAX_ATTEMPTS:
                if _is_transient_error(error, attempt_events):
                    transient_attempt += 1
                    wait_s = min(
                        _TRANSIENT_BASE_WAIT_S * (2 ** (transient_attempt - 1)),
                        _TRANSIENT_MAX_WAIT_S,
                    )
                    # ±25% jitter — same rationale as the rate-limit branch.
                    wait_s = min(_TRANSIENT_MAX_WAIT_S, wait_s * random.uniform(0.75, 1.25))
                    logger.warning(
                        "Transient error on attempt %d/%d for %s, retrying in %.0fs: %s",
                        transient_attempt, _TRANSIENT_MAX_ATTEMPTS,
                        app_session_id, wait_s, error[:200],
                    )
                    in_flight = self.current_assistant_msgs.get(app_session_id)
                    assistant_msg_id = in_flight.get("id") if in_flight else None
                    retry_at = (datetime.now(timezone.utc) + timedelta(seconds=wait_s)).isoformat()
                    if assistant_msg_id:
                        session_manager.set_msg_retrying_until(
                            app_session_id, assistant_msg_id, retry_at,
                            error_text=error or "Transient error, retrying",
                        )
                        session_manager.set_msg_transient_attempt(
                            app_session_id, assistant_msg_id, transient_attempt,
                        )
                    self.run_state_clear_pid(app_session_id, turn_run_id)
                    self.run_state_mark_retrying(app_session_id, turn_run_id)
                    await self.emit_run_state(app_session_id)
                    try:
                        await asyncio.wait_for(
                            cancel_event.wait(), timeout=wait_s,
                        )
                        if assistant_msg_id:
                            session_manager.set_msg_retrying_until(
                                app_session_id, assistant_msg_id, None,
                            )
                        _clear_continuation_active()
                        return {
                            "success": False,
                            "session_id": new_sid,
                            "events": collected,
                            "error": t("runner.cancelled"),
                            "token_usage": None,
                        }
                    except asyncio.TimeoutError:
                        pass
                    if assistant_msg_id:
                        session_manager.set_msg_retrying_until(
                            app_session_id, assistant_msg_id, None,
                        )
                    self._pop_run_id(app_session_id, run_id)
                    current_session_id = new_sid or current_session_id
                    auto_retry_count += 1
                    auto_retry_kinds.add("transient")
                    continue

            # Terminal attempt. If the turn ultimately SUCCEEDED after one
            # or more auto-retries, stamp a durable marker on the message
            # so the recovery is observable (and distinguishable from a
            # clean first-try turn) across reloads/tabs.
            if success and auto_retry_count > 0:
                in_flight = self.current_assistant_msgs.get(app_session_id)
                done_msg_id = in_flight.get("id") if in_flight else None
                if done_msg_id:
                    kind = (
                        next(iter(auto_retry_kinds))
                        if len(auto_retry_kinds) == 1
                        else "mixed"
                    )
                    session_manager.record_auto_retry(
                        app_session_id, done_msg_id, auto_retry_count, kind,
                    )

            # Agent-requested continuation: the agent called
            # `continue_in_fresh_context` during this turn. Honor it by
            # starting a fresh provider subprocess under the SAME Better
            # Agent session (continuation_chain extended) with the queued
            # prompt — same path as context-overflow, triggered by the agent.
            if success:
                requested = session_manager.pop_continuation_requested(
                    primary_session_id or app_session_id,
                )
                if requested:
                    # Clear any abort signal so the continuation iteration
                    # proceeds — relevant when a `when="now"` abort raced the
                    # run to a natural success and landed here instead.
                    cancel_event.clear()
                    self._c._session_cancelled.pop(app_session_id, None)
                    prompt = requested.get("prompt") or ""
                    _chain_depth = _start_context_continuation(
                        new_sid or current_session_id,
                        reason="agent_requested",
                    )
                    logger.info(
                        "continuation: agent-requested fresh subprocess for "
                        "%s (chain depth %d)",
                        app_session_id[:8], _chain_depth,
                    )
                    self._pop_run_id(app_session_id, run_id)
                    continue

            await self._emit_attempt_terminal(
                ws_callback=ws_callback,
                mode=mode,
                attempt_events=attempt_events,
            )
            _clear_continuation_active()
            return {
                "success": success,
                "session_id": new_sid,
                "events": collected,
                "error": error,
                "token_usage": complete_data.get("token_usage") or None,
                "context_window": complete_data.get("context_window"),
                "context_tokens": complete_data.get("context_tokens"),
                "sdk_output": complete_data.get("sdk_output") or None,
            }

    # ======================================================================
    # Worker-inner (`Coordinator.run_delegation` terminal) routing calls
    # `self._c.turn_manager._publish_terminal_lifecycle("complete", ...,
    # reason="worker_inner")` directly — no separate API surface needed.
    # ======================================================================
