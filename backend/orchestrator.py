"""Manager/worker coordinator.

One persistent "manager" Claude Code session per UI session. All user prompts
and follow-ups go to the same manager. The manager delegates work to "worker"
Claude Code sessions via a `delegate` MCP tool.

Both orchestration modes share the same durable detached-runner
architecture. The orchestrator asks the active `Provider` (today
`ClaudeProvider`) to spawn `runner.py` with an `input.json` payload.
The runner runs the claude SDK, which writes claude's own session
jsonl under `~/.claude/projects/<encoded-cwd>/<session_id>.jsonl`.
The provider tails that jsonl, translates each line into
`StreamEvent`s, and enqueues them for the orchestrator's
`_drive_cli_run` / `run_delegation` consumers.

  - Native mode  → runner.py in `mode="native"`, plain claude SDK
  - Manager mode → runner.py in `mode="manager"`, with in-process team MCP
    tools (`mssg`, `ask`, `create_worker`, plus the generic `delegate` +
    `create_session` handoff tools). `ask(run_mode="fork")` HTTP-POSTs back
    to `/api/internal/ask-fork` on this backend (the fork engine);
    `delegate` POSTs to `/api/internal/delegate` (detached handoff).
  - Workers      → runner.py in `mode="native"` (no MCP needed)

Because the runner writes directly to claude's own jsonl (not to a
custom events.jsonl), a backend restart can keep tailing the same file
and pick up where it left off.
"""

import asyncio
import base64
import copy
import json
import logging
import os
import re
import secrets
import traceback
import uuid
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable, Literal, Optional

from i18n import t
from provider import StreamEvent, ProviderSuspendedError, default_provider, get_provider, known_providers
from runs_dir import pid_alive as _pid_alive
from trace_collector import (
    TraceCollector,
    extract_provider_result_token_usage,
    extract_token_usage,
)
from session_manager import manager as session_manager
# user_msg_lifecycle emits routed through UserPromptManager — no
# direct import needed here. handle_prompt calls
# `self.user_prompt_manager.emit_user_msg_done/_failed`.

import perf
import time as _time
import virtual_session_prompt_handlers
from ws_serialization import dumps_ws_json

logger = logging.getLogger(__name__)


class SerializedGlobalEvent(dict):
    pass

_IMAGE_EXT_BY_MEDIA_TYPE = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
}


def _save_message_images(app_session_id: str, owner_id: str, images: list) -> list[dict]:
    img_dir = ba_home() / "sessions" / "images" / app_session_id
    img_dir.mkdir(parents=True, exist_ok=True)
    saved_images = []
    for i, img in enumerate(images):
        media_type = img.get("media_type")
        ext = _IMAGE_EXT_BY_MEDIA_TYPE.get(media_type)
        if ext is None:
            raise ValueError("unsupported image media_type")
        fname = f"{owner_id}_{i}.{ext}"
        (img_dir / fname).write_bytes(base64.b64decode(img["data"], validate=True))
        saved_images.append({"filename": fname, "media_type": media_type})
    return saved_images


def _message_file_metadata(files: list) -> list[dict]:
    metadata = []
    for f in files:
        if not isinstance(f, dict):
            raise ValueError("malformed file attachment")
        name = f.get("name")
        media_type = f.get("media_type")
        size = f.get("size")
        if not isinstance(name, str) or not name:
            raise ValueError("file attachment missing name")
        if not isinstance(media_type, str):
            raise ValueError("file attachment missing media_type")
        if not isinstance(size, int) or size < 0:
            raise ValueError("file attachment missing size")
        metadata.append({
            "name": name,
            "media_type": media_type,
            "size": size,
        })
    return metadata


# Safety cap for the turn-join wait (sender turn held open while its mssg
# targets finish). Targets run with their own timeouts; this only bounds the
# wait if a target hangs without completing, so a stuck worker can't hold the
# sender turn open forever.
_MSSG_TURN_JOIN_TIMEOUT = 6 * 60 * 60

# How long delegate_task waits for user approval (manual / always_new_approve).
_DELEGATE_TASK_APPROVAL_TIMEOUT = 24 * 60 * 60

# Event types bridged directly through save_ws_callback → original_ws_callback.
# Content events go only via the tailer to avoid double-delivery on the WS path.
_BRIDGE_EVENT_TYPES = frozenset((
    "turn_complete", "turn_stopped", "turn_detached",
    "worker_creation_requested",
))
_STEER_READY_RETRY_SECONDS = 1.0
_STEER_READY_RETRY_INTERVAL_SECONDS = 0.05


from paths import ba_home


def _cb_token(ws_callback) -> object:
    """Per-connection identity for WS subscription bookkeeping.

    Prefers the stable `_bc_conn_token` stamped on the callback by the WS
    handler over `id()`. CPython recycles a callback's `id()` (memory
    address) once the closure is GC'd, so a stale leaked subscription from
    a dead connection could collide with a fresh connection's callback and
    make `_subscribe_to_wire_tailer`'s dedup guard skip the new subscribe —
    starving the reconnected tab of live events. The token is unique per
    connection and never reused, so it can't collide. Falls back to `id()`
    for callbacks with no token (e.g. internal/test callers)."""
    return getattr(ws_callback, "_bc_conn_token", None) or id(ws_callback)


def _internal_token_path() -> Path:
    return ba_home() / "internal_token"


def _load_or_create_internal_token() -> str:
    """Read the persisted internal token, or mint+persist a new one.

    The token authenticates `runner.py` → the `/api/internal/*` loopback
    endpoints (ask-fork, delegate, mssg, ask, create-worker, create-session).
    Persisting across restarts keeps detached runners that survived a backend
    reload able to authenticate. Permissions 0600 so other local users can't
    read the secret.
    """
    try:
        token = _internal_token_path().read_text(encoding="utf-8").strip()
        if token:
            return token
    except FileNotFoundError:
        pass
    except OSError:
        logger.exception("Failed to read internal_token file; minting a new one")

    token = secrets.token_urlsafe(32)
    try:
        _internal_token_path().parent.mkdir(parents=True, exist_ok=True)
        _internal_token_path().write_text(token, encoding="utf-8")
        os.chmod(_internal_token_path(), 0o600)
    except OSError:
        logger.exception("Failed to persist internal_token; using in-memory only")
    return token


# Pure event-shape helpers — moved to event_shape.py. Re-exported
# under the old private names so internal call sites don't need
# updating. New code should import from `event_shape`.
from event_shape import (
    extract_output_text as _extract_output_text,
    extract_subagent_types as _extract_subagent_types,
    is_synthetic_event as _is_synthetic_event,
    project_content_snapshot as _project_content_snapshot,
    strip_synthetic_events as _strip_synthetic_events,
)
from event_bus import BusEvent, bus


# ============================================================================
# Coordinator
# ============================================================================

class _Cancelled(Exception):
    """Raised when a turn is cancelled by the user."""
    pass


# Error classifiers + retry caps live in `turn_helpers` so both
# `orchestrator.py` and `turn_manager.py` import from a neutral module.
from turn_helpers import (
    _TRANSIENT_MAX_ATTEMPTS,
    _TRANSIENT_BASE_WAIT_S,
    _TRANSIENT_MAX_WAIT_S,
    _is_stale_session_error,
    _is_rate_limit_attempt,
    _is_transient_error,
)


# Per-task handle to the running coordinator instance. Set by
# Coordinator.__init__ as the default for this process, but
# tests that instantiate a second coordinator can install their own
# via the ContextVar — no cross-test leak from a clobbering singleton.
# Strategies use `get_active_coordinator()` to read it from call
# sites that can't accept a coordinator through their signature
# (apply_event's user_message_received hook, etc.).
import contextvars as _contextvars

_active_coordinator_var: _contextvars.ContextVar[Optional["Coordinator"]] = (
    _contextvars.ContextVar("active_coordinator", default=None)
)


def get_active_coordinator() -> Optional["Coordinator"]:
    """Lookup the coordinator bound for the current task. Falls back
    to the process-wide default registered at the last `__init__`
    when no per-task value has been set. Returns None if no
    coordinator has been instantiated yet."""
    val = _active_coordinator_var.get()
    if val is not None:
        return val
    return _default_coordinator


# Process-wide fallback for code paths that run outside the task in
# which the coordinator was constructed (e.g. background threads that
# don't inherit the ContextVar). Tests can override via
# `_active_coordinator_var.set(coord)` in their own task scope.
_default_coordinator: Optional["Coordinator"] = None


# ── Open-todo reminder ────────────────────────────────────────────────
#
# Lightweight cli_prompt-only reminder injected at `run_turn` for
# every user-initiated primary turn. The reminder text is derived from
# `session.current_todos`; empty and all-completed lists leave the
# model-facing prompt unchanged.
#
# INJECTION SITE: `TurnManager.run_turn` is the single funnel
# that BOTH `orchs.native.handle_turn` AND `orchs.manager.handle_turn`
# call through with their final cli_prompt. Manager mode wraps the
# prompt via `build_wrapped_prompt` before that call, so injecting in
# `dispatch` would be silently dropped (the wrapper rebuilds from
# `prompt`, not `cli_prompt`). Injecting in `run_turn` catches both.
#
# SKIP CONDITIONS (gated at the call site):
#   - `user_initiated=False` — internal turns (verdict loops,
#     recovery replays, supervisor delegations) MUST NOT be nudged.
#   - `prompt` is whitespace-only — nudge-only turns are pure noise.
#
# Side-effect-free with respect to the persisted user message — only
# `cli_prompt` is mutated, so the visible history stays clean.

# Open-todo cli_prompt reminder lives in `turn_helpers`.
from turn_helpers import _append_todo_reminder


def build_semantic_alter_prompt(previous_prompt: str, replacement_prompt: str) -> str:
    payload = json.dumps(
        {
            "previous_prompt": previous_prompt,
            "replacement_prompt": replacement_prompt,
        },
        ensure_ascii=False,
    )
    return (
        "<user-alter-request>"
        "The previous user prompt was requested to be altered. "
        "Treat this replacement prompt as the user's intended prompt instead. "
        f"{payload}"
        "</user-alter-request>\n\n"
        f"{replacement_prompt}"
    )


class Coordinator:
    """Coordinates a persistent manager session and its worker delegations.

    All claude-CLI invocations route through the session's bound
    `Provider` (resolved via `self.provider_for_session(sid)`). The
    session record's `provider_id` is the source of truth; legacy
    sessions without one fall back to the currently-active provider.
    The orchestrator never reaches for the `claude` binary directly.
    """

    def __init__(self) -> None:
        # ORDERING: instantiate the manager triad BEFORE registering
        # `self` as the active coordinator. Once active, any concurrent
        # `get_active_coordinator()` reader (e.g. sm hot paths resolving
        # DraftStore on demand) will see `self`. If `self.draft_store`
        # weren't set by then, `_draft_store_or_none()` would raise
        # mid-`_is_pinned` and fail-close pin every root for a
        # microsecond. Pre-set the attrs to close the window.
        from turn_manager import TurnManager
        from user_prompt_manager import UserPromptManager
        from draft_store import DraftStore
        # Turn-lifecycle authority. Owns: cancel_events, active_run_ids,
        # current_assistant_msgs, current_turn_workers,
        # _turn_save_callbacks, in_flight_lifecycle_msg_id,
        # _interrupted_by_msg_id, _run_state. Owns the methods that
        # mutate them (run_turn, _drive_cli_run,
        # _apply_event_to_assistant_msg, run_state_*, cancel_turn,
        # accessors). Reaches back here only for non-turn collaborators
        # (session/message helpers, providers, internal_token).
        self.turn_manager = TurnManager(self)
        # User-prompt-lifecycle authority. Owns `in_flight_lifecycle_msg_id`,
        # the `_publish_user_lifecycle` funnel, `emit_user_msg_done/_failed`,
        # and `notify_user_msg_persisted`. `_interrupted_by_msg_id` stays
        # on TurnManager (turn-side handoff); UPM receives it as an
        # explicit `interrupted_by_msg_id=` parameter when needed.
        self.user_prompt_manager = UserPromptManager(self)
        # Debounced per-keystroke draft sidecar persistence. Owns its
        # own dirty/gen state + flush coalescer. sm hot paths resolve
        # via `get_active_coordinator().draft_store` on demand.
        self.draft_store = DraftStore(self)
        # NOW register as active — readers will find a fully-formed
        # triad.
        global _default_coordinator
        _default_coordinator = self
        try:
            _active_coordinator_var.set(self)
        except LookupError:
            pass
        self.ws_callbacks: dict[str, list[Callable[[dict], Awaitable[None]]]] = {}
        self.global_ws_callbacks: list[Callable[[dict], Awaitable[None]]] = []
        # Per-root BetterAgentJsonlTailer — sole producer of live WS
        # frames. Started on first WS subscriber for any session in the
        # root, stopped when last subscriber leaves. The tailer reads
        # events.jsonl and dispatches each new event to that root's
        # subscribers via dispatch_raw, keyed on the entry's `sid`.
        self._wire_tailers: dict[str, "BetterAgentJsonlTailer"] = {}
        self._wire_tailer_tasks: dict[str, asyncio.Task] = {}
        # root_id → set of app_session_ids that currently have at least
        # one WS subscriber. Used to ref-count tailer lifecycle.
        self._wire_tailer_subs: dict[str, set[str]] = {}
        # (app_session_id, _cb_token(ws_callback)) → _Subscriber, so unregister
        # can find and remove the right subscriber from its tailer.
        self._subscriber_index: dict[tuple, "_Subscriber"] = {}
        # Native-CLI-jsonl tailing (the OwnedClaudeJsonlTailers) is owned
        # entirely by `native_files_manager.native_files`. The orchestrator
        # only publishes demand (`native_files.demand`) on WS subscribe /
        # unsubscribe; the manager folds it and reconciles tailers.
        # Per-session "save wrapper" / cancel events / active run ids /
        # in-flight assistant msgs / worker panels / lifecycle msg ids /
        # interrupted-by cross-refs / run_state registry — all moved to
        # `self.turn_manager` (turn-lifecycle authority).
        # Per-session prompt queues + processor tasks owned by the
        # coordinator (NOT by any WebSocket). A WS disconnect simply
        # deregisters its ws_callback; the processor task continues
        # running so the detached runner's events keep flowing into
        # persistence. This is what makes "I refreshed and the turn was
        # still going" work.
        self._prompt_queues: dict[str, asyncio.Queue] = {}
        self._processor_tasks: dict[str, asyncio.Task] = {}
        # A10 TOCTOU closure: counter of prompts that have been
        # dequeued by `_run_session_processor` but not yet fully
        # processed (i.e. `handle_prompt` is still running). Stamped
        # to N>0 IMMEDIATELY after `q.get()` returns, BEFORE the first
        # subsequent await — closes the window between queue drain
        # and `_drive_cli_run`'s `active_run_ids` registration that
        # `has_active_runs` would otherwise miss.
        self._in_flight_prompts: dict[str, int] = {}
        # INVARIANT: bounded perf-gauge — sums depth across all
        # per-session prompt queues so the rollup line stays a single
        # entry regardless of how many sessions are active.
        perf.register_queue(
            "prompt.total",
            lambda: sum(q.qsize() for q in self._prompt_queues.values()),
        )
        # Per-session list of queued prompt IDs (in order). Used to
        # track queued prompts for promote_queued and WS events.
        self._queued_ids: dict[str, list[str]] = {}
        self._active_prompt_client_ids: dict[tuple[str, str], str] = {}
        self._prompt_client_id_by_item: dict[str, tuple[str, str]] = {}
        # Per-session set of prompt IDs cancelled while still queued.
        # The processor checks this before starting a dequeued prompt.
        self._cancelled_ids: dict[str, set[str]] = {}
        # Per-session flag: True after cancel_turn fires, cleared by the
        # queue processor before the next prompt's handle_prompt call.
        # Survives across run_turn invocations so maybe_run_verdict_loop
        # can bail even when no cancel_event exists momentarily.
        self._session_cancelled: dict[str, bool] = {}
        # Shared secret passed to manager_runner.py so the runner's
        # team MCP tool handlers (HTTP POST to the `/api/internal/*`
        # loopback endpoints — ask-fork, delegate, mssg, ask, create-*)
        # can prove they're legitimate. The endpoints verify
        # `X-Internal-Token`. Persisted to disk at
        # ~/.better-claude/internal_token so detached runners that
        # outlive a backend restart can still authenticate. Operators
        # who want to invalidate stale runners can delete the file.
        self.internal_token = _load_or_create_internal_token()
        # Phase-1 stage-5: token rotation grace window. `_prev_token` is
        # the previous token (post-rotation) accepted alongside the
        # current token for `_prev_token_grace_expires_at`. Stage-5b
        # adds the periodic rotation task that calls `rotate_internal_token`.
        # Until then `_prev_token` stays None — `verify_internal_token`
        # is equivalent to today's `== self.internal_token` check.
        self._prev_token: Optional[str] = None
        self._prev_token_grace_expires_at: float = 0.0  # monotonic time
        # Per-(caller_bc_sid, worker_bc_sid) serialization locks. The
        # fork is per-pair, and two concurrent `--resume <fork_sid>` on
        # the same fork would corrupt its jsonl (malformed parentUuid
        # DAG). Different pairs run in parallel — that's the point of
        # per-pair forks. In-process asyncio.Lock is sufficient for a
        # single backend instance.
        self.pair_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._known_worker_registry_cwds_by_session: dict[str, dict[str, str]] = {}
        # Per-app-session "currently inside a delegation" depth counter.
        # Top-level user turn = 0. The moment run_delegation enters, we
        # increment; on exit (success or error), decrement. A nested
        # delegate call (a manager-mode worker calling delegate from
        # inside its own forked turn) sees a counter > 0 and is rejected
        # if it asks for a fresh worker — only resumes are allowed.
        self.active_delegations: dict[str, int] = {}
        # Per-delegation in-memory Future for the approval handshake.
        # Resolved by the REST approve/deny handlers. Disk-backed via
        # pending_approvals.py so a backend restart doesn't strand a
        # detached runner — the runner's HTTP call has a 24h timeout
        # and we re-await on backend startup if approvals are still
        # pending. (TODO: cross-restart resume of waiting delegations
        # is deferred — for now the runner just waits and we re-emit
        # worker_creation_requested if the WS reconnects.)
        self.approval_waiters: dict[str, asyncio.Future] = {}
        # Turn-join: sender app_session_id -> {target lifecycle_msg_id -> Future}.
        # A fire-and-forget mssg registers a Future here; the sender's turn
        # completion awaits them so the turn stays open while work it
        # initiated is still running. Resolved when the target's
        # user_message_done/failed fires (observed via a target WS callback).
        self._mssg_turn_waiters: dict[str, dict[str, asyncio.Future]] = {}
        # Per-bc-session cancel events for in-flight worker init turns
        # spawned by POST /api/workers. DELETE /api/workers sets the
        # event so the init turn bails before the Better Agent session is fully
        # registered. Value is (owner_session_id, Event) so cancel_session
        # can scope cancellation to its own session's inits.
        self.init_cancel_events: dict[str, tuple[str, asyncio.Event]] = {}
        # Per-turn supervisor verdict counter — bumped by request_verdict,
        # reset on new-turn entry by orchs.supervisor.handle_turn. Keyed
        # by (supervisor_agent_session_id, worker_agent_session_id). After
        # SUPERVISOR_MAX_VERDICTS_PER_TURN, the verdict is force-DONE
        # regardless of what the supervisor says — prevents an
        # infinitely-looping supervisor from trapping the worker.

        # A4: the orchestrator no longer holds a rearranger reference.
        # It publishes `lifecycle.turn_complete` / `lifecycle.turn_stopped`
        # bus events at the natural points; the rearranger subscribes
        # via `event_bus_subscribers.bind_rearranger(...)` from main.py.
        # Decouples the orchestrator from the rearranger subsystem and
        # makes the lifecycle observable to any future subscriber
        # (metrics, trace exports, …) without another late-bind hook.

        # Reactive tailer acquisition for mid-session sid discoveries is
        # owned by `native_files_manager` (it subscribes to
        # `session.agent_sid_set` / `native_files.fork_target` itself).

    # ------------------------------------------------------------------
    # TurnManager facade — Coordinator is the public-facing object for
    # callers; TurnManager is the internal authority. These delegates
    # forward to `self.turn_manager` so external code says
    # `coordinator.X` and instance-level monkey-patches (used by tests
    # to inject fakes for run_turn)
    # work the same way they would for a class-defined method.
    # ------------------------------------------------------------------
    def _ensure_tm(self):
        """Lazy-create `turn_manager` for callers that bypass __init__
        (e.g. `Coordinator.__new__(Coordinator)` in test fixtures)."""
        tm = self.__dict__.get("turn_manager")
        if tm is None:
            from turn_manager import TurnManager
            tm = TurnManager(self)
            self.__dict__["turn_manager"] = tm
        return tm

    def _ensure_upm(self):
        """Lazy-create `user_prompt_manager` for callers that bypass
        __init__."""
        upm = self.__dict__.get("user_prompt_manager")
        if upm is None:
            from user_prompt_manager import UserPromptManager
            upm = UserPromptManager(self)
            self.__dict__["user_prompt_manager"] = upm
        return upm

    def _ensure_ds(self):
        """Lazy-create `draft_store` for callers that bypass __init__."""
        ds = self.__dict__.get("draft_store")
        if ds is None:
            from draft_store import DraftStore
            ds = DraftStore(self)
            self.__dict__["draft_store"] = ds
        return ds

    def known_worker_registry_cwd(
        self, app_session_id: str, worker_session_id: str,
    ) -> Optional[str]:
        by_session = getattr(self, "_known_worker_registry_cwds_by_session", {})
        return (by_session.get(app_session_id) or {}).get(worker_session_id)

    @property
    def cancel_events(self) -> dict:
        return self._ensure_tm().cancel_events

    @cancel_events.setter
    def cancel_events(self, value: dict) -> None:
        self._ensure_tm().cancel_events = value

    @property
    def active_run_ids(self) -> dict:
        return self._ensure_tm().active_run_ids

    @active_run_ids.setter
    def active_run_ids(self, value: dict) -> None:
        self._ensure_tm().active_run_ids = value

    @property
    def current_assistant_msgs(self) -> dict:
        return self._ensure_tm().current_assistant_msgs

    @current_assistant_msgs.setter
    def current_assistant_msgs(self, value: dict) -> None:
        self._ensure_tm().current_assistant_msgs = value

    @property
    def current_turn_workers(self) -> dict:
        return self._ensure_tm().current_turn_workers

    @current_turn_workers.setter
    def current_turn_workers(self, value: dict) -> None:
        self._ensure_tm().current_turn_workers = value

    @property
    def _turn_save_callbacks(self) -> dict:
        return self._ensure_tm()._turn_save_callbacks

    @_turn_save_callbacks.setter
    def _turn_save_callbacks(self, value: dict) -> None:
        self._ensure_tm()._turn_save_callbacks = value

    @property
    def in_flight_lifecycle_msg_id(self) -> dict:
        return self._ensure_upm().in_flight_lifecycle_msg_id

    @in_flight_lifecycle_msg_id.setter
    def in_flight_lifecycle_msg_id(self, value: dict) -> None:
        self._ensure_upm().in_flight_lifecycle_msg_id = value

    @property
    def _interrupted_by_msg_id(self) -> dict:
        return self._ensure_tm()._interrupted_by_msg_id

    @_interrupted_by_msg_id.setter
    def _interrupted_by_msg_id(self, value: dict) -> None:
        self._ensure_tm()._interrupted_by_msg_id = value

    @property
    def _run_state(self) -> dict:
        return self._ensure_tm()._run_state

    @_run_state.setter
    def _run_state(self, value: dict) -> None:
        # Test fixtures (e.g. test_monitoring_state) assign to this
        # attribute directly. Replace the dict on the owner.
        self._ensure_tm()._run_state = value

    # Run-state registry delegates.
    def run_state_add(self, *a, **kw):
        return self._ensure_tm().run_state_add(*a, **kw)

    def run_state_remove(self, *a, **kw):
        return self._ensure_tm().run_state_remove(*a, **kw)

    def run_state_set_pid(self, *a, **kw):
        return self._ensure_tm().run_state_set_pid(*a, **kw)

    def _run_state_set_target(self, *a, **kw):
        return self._ensure_tm()._run_state_set_target(*a, **kw)

    def _run_state_touch(self, *a, **kw):
        return self._ensure_tm()._run_state_touch(*a, **kw)

    def get_run_state(self, *a, **kw):
        return self._ensure_tm().get_run_state(*a, **kw)

    def get_all_run_states(self, *a, **kw):
        return self._ensure_tm().get_all_run_states(*a, **kw)

    async def emit_run_state(self, *a, **kw):
        return await self._ensure_tm().emit_run_state(*a, **kw)

    def is_running(self, *a, **kw):
        return self._ensure_tm().is_running(*a, **kw)

    def monitoring_state(self, *a, **kw):
        return self._ensure_tm().monitoring_state(*a, **kw)

    def _has_pending_approval(self, *a, **kw):
        return self._ensure_tm()._has_pending_approval(*a, **kw)

    def _has_background_work(self, *a, **kw):
        return self._ensure_tm()._has_background_work(*a, **kw)

    def _prune_dead_entries(self, *a, **kw):
        return self._ensure_tm()._prune_dead_entries(*a, **kw)

    def tick_running_state(self, *a, **kw):
        return self._ensure_tm().tick_running_state(*a, **kw)

    def _maybe_flip_streaming(self, *a, **kw):
        return self._ensure_tm()._maybe_flip_streaming(*a, **kw)

    # Accessor delegates.
    def has_active_turn(self, *a, **kw):
        return self._ensure_tm().has_active_turn(*a, **kw)

    def has_active_runs(self, *a, **kw):
        return self._ensure_tm().has_active_runs(*a, **kw)

    def get_in_flight_lifecycle_msg_id(self, *a, **kw):
        return self._ensure_upm().get_in_flight_lifecycle_msg_id(*a, **kw)

    def get_turn_save_callback(self, *a, **kw):
        return self._ensure_tm().get_turn_save_callback(*a, **kw)

    def get_in_flight_assistant_msg(self, *a, **kw):
        return self._ensure_tm().get_in_flight_assistant_msg(*a, **kw)

    # Core method delegates. Defined as instance methods so tests can
    # monkey-patch `coordinator.run_turn = fake` and have internal
    # `self.run_turn(...)` calls pick up the fake.
    async def run_turn(self, *a, **kw):
        return await self._ensure_tm().run_turn(*a, **kw)

    async def _drive_cli_run(self, *a, **kw):
        return await self._ensure_tm()._drive_cli_run(*a, **kw)

    async def cancel_turn(self, *a, **kw):
        return await self._ensure_tm().cancel_turn(*a, **kw)

    def _apply_event_to_assistant_msg(self, *a, **kw):
        return self._ensure_tm()._apply_event_to_assistant_msg(*a, **kw)

    def is_root_in_use(self, root_id: str, node_sids: set) -> bool:
        """True iff this orchestrator still holds a live reference to the
        root: an active turn for any of its sids, an open WS subscriber,
        or a live wire/owned tailer. Injected into session_manager via
        `bind_pin_predicate` so LRU eviction never drops a root whose
        render tree / events.jsonl is still being driven."""
        if root_id in self._wire_tailers:
            return True
        if self._wire_tailer_subs.get(root_id):
            return True
        from native_files_manager import native_files
        if native_files.is_tailing_root(root_id):
            return True
        return any(self.turn_manager.has_active_runs(sid) for sid in node_sids)


    def provider_for_session(self, app_session_id: str):
        """Resolve which `Provider` drives this session's claude
        invocations.

        **Per-turn freezing.** `_drive_cli_run` resolves the provider
        ONCE at turn start (line 2117) and reuses the local reference
        across the retry loop and the cancel-from-inside-loop call.
        That local-variable scope is the freezing mechanism — there's
        no need for an explicit Run.provider attribute because the
        function-local binding is unreachable from anywhere a
        provider_id PATCH could land. The 409 gate in PATCH selectors
        belt-and-suspenders this: it prevents the change from
        being persisted to disk while the turn runs, so post-turn
        rewind/cancel-by-session paths also see a coherent view.

        Multi-machine routing (multi-machine v1): a session whose
        `node_id` does not match `topology.local_node_id()` is routed
        to a `RemoteProviderProxy` bound to that node. Same call site
        for local and remote — the proxy is a pure-transport
        implementation of the same Provider protocol, so the rest of
        the orchestrator (start_run, cancel_run, queue drain) doesn't
        fork on local-vs-remote.

        Single-machine path (default): unchanged — pick the session's
        configured provider, fall back to the active provider when
        the record's provider_id is missing or defunct."""
        sess = session_manager.get_fields(app_session_id, ("node_id", "provider_id"))
        node_id = (sess or {}).get("node_id") or "primary"
        try:
            from topology import local_node_id
            here = local_node_id()
        except Exception:
            # topology.yaml absent / misconfigured ⇒ single-machine.
            here = "primary"
        if node_id != here:
            import extension_store
            not_ready = extension_store.runtime_not_ready_message(
                extension_store.BUILTIN_MACHINE_NODES_EXTENSION_ID
            )
            if not_ready is not None:
                raise RuntimeError(not_ready)
            import provider_remote
            return provider_remote.get_proxy(node_id)

        pid = (sess or {}).get("provider_id")
        if pid:
            try:
                prov = get_provider(pid)
                if getattr(prov, "suspended", False):
                    raise ProviderSuspendedError(
                        f"provider {pid} is suspended; cannot start runs"
                    )
                if not prov.defunct:
                    return prov
                logger.warning(
                    "session %s references defunct provider_id %s — "
                    "falling back to active",
                    app_session_id, pid,
                )
            except ProviderSuspendedError:
                raise
            except KeyError:
                logger.warning(
                    "session %s references unknown provider_id %s — "
                    "falling back to active",
                    app_session_id, pid,
                )
        return default_provider()

    def provider_for_run(self, app_session_id: str, provider_id: Optional[str] = None):
        pid = str(provider_id or "").strip()
        if not pid:
            return self.provider_for_session(app_session_id)
        try:
            prov = get_provider(pid)
            if getattr(prov, "suspended", False):
                raise ProviderSuspendedError(
                    f"provider {pid} is suspended; cannot start runs"
                )
            if not prov.defunct:
                return prov
            logger.warning(
                "per-turn provider_id %s is defunct for session %s — falling back to session provider",
                pid,
                app_session_id,
            )
        except ProviderSuspendedError:
            raise
        except KeyError:
            logger.warning(
                "per-turn provider_id %s is unknown for session %s — falling back to session provider",
                pid,
                app_session_id,
            )
        return self.provider_for_session(app_session_id)

    async def rewind_session(self, app_session_id: str, agent_sid: str, anchor_uuid: str) -> None:
        """Public rewind API for submodules that need to rewind a claude
        session without reaching into private methods."""
        return self.provider_for_session(app_session_id).rewind(agent_sid, anchor_uuid)

    # ------------------------------------------------------------------
    # Internal-token verification with rotation grace window (stage 5)
    # ------------------------------------------------------------------
    def verify_internal_token(self, token: Optional[str]) -> bool:
        """Validate `token` against the current internal_token, the
        on-disk token (covers races where coordinator hasn't re-read
        the file yet), or the previous token within its grace window.
        """
        import hmac as _hmac
        if not token:
            return False
        if _hmac.compare_digest(token, self.internal_token or ""):
            return True
        # On-disk fallback: a runner may have read a freshly-rotated
        # token from the file before this coordinator's in-memory
        # state caught up (e.g. across async tasks on the same loop).
        try:
            disk = _internal_token_path().read_text(encoding="utf-8").strip()
            if disk and _hmac.compare_digest(token, disk):
                return True
        except OSError:
            pass
        if self._prev_token and _hmac.compare_digest(token, self._prev_token):
            import time as _time
            if _time.monotonic() < self._prev_token_grace_expires_at:
                return True
        return False

    def mint_extension_token(self, extension_id: str) -> str:
        """Mint (or return the existing) per-extension internal-loopback token.
        Injected into the extension's subprocess env so the backend can derive
        its identity from the token instead of a spoofable header."""
        import extension_token_registry
        return extension_token_registry.mint(extension_id)

    def resolve_principal(self, token: Optional[str]) -> Optional[tuple[str, Optional[str]]]:
        """Classify an /api/internal/* caller by its token.

        Returns ("core", None) for the global runner/core token (incl. on-disk
        and grace variants), ("extension", <id>) for a per-extension token, or
        None if the token authenticates as neither. Identity comes from the
        secret — never from a self-asserted X-Extension-Id header."""
        if not token:
            return None
        if self.verify_internal_token(token):
            return ("core", None)
        import extension_token_registry
        ext_id = extension_token_registry.resolve(token)
        if ext_id:
            return ("extension", ext_id)
        return None

    def principal_extension_id(self, token: Optional[str]) -> Optional[str]:
        """Token-derived extension id, or None if the token is core/invalid."""
        principal = self.resolve_principal(token)
        if principal and principal[0] == "extension":
            return principal[1]
        return None

    def is_internal_caller(self, token: Optional[str]) -> bool:
        """True if the token authenticates as a valid /api/internal/* caller —
        the core/runner token OR a registered per-extension token. Identity-
        specific gates further narrow WHO via principal_extension_id."""
        return self.resolve_principal(token) is not None

    def rotate_internal_token(self, grace_seconds: float = 7200.0) -> None:
        """Mint a new internal_token; keep the old one valid for
        `grace_seconds`. Persists the new token to
        `ba_home()/internal_token` so runners' mtime-cached
        `_load_internal_token` discovers it.

        Default grace (7200s = 2h) exceeds the 1h rotation interval,
        so a token stays valid for at least one full rotation cycle
        after it's retired.
        """
        import secrets as _secrets
        import time as _time

        old = self.internal_token
        new = _secrets.token_urlsafe(32)
        try:
            path = _internal_token_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(new, encoding="utf-8")
            os.chmod(path, 0o600)
        except OSError:
            logger.exception(
                "rotate_internal_token: failed to persist new token; "
                "rotation aborted to avoid in-memory/disk divergence",
            )
            return
        self._prev_token = old
        self._prev_token_grace_expires_at = _time.monotonic() + grace_seconds
        self.internal_token = new
        logger.info(
            "rotate_internal_token: rotated; old token valid for %.0fs grace",
            grace_seconds,
        )

    @staticmethod
    def _cancel_run_fanout(run_id: str) -> bool:
        """HARD (delete-tier) cancel — walks every loaded Provider until
        one claims ownership, then SIGTERM→SIGKILL the runner pgroup and
        sweep detached bg shells. Used ONLY by `cancel_session`. Turn-
        stops go through `_cancel_turn_fanout`.

        Returns True if any provider acknowledged the cancel.
        """
        signalled = False
        for prov in known_providers():
            try:
                if prov.cancel_run(run_id):
                    signalled = True
            except Exception:
                logger.exception(
                    "cancel_run_fanout: provider %s raised on cancel %s",
                    prov.id, run_id,
                )
        return signalled

    @staticmethod
    def _cancel_turn_fanout(run_id: str) -> bool:
        """SOFT (turn-stop) cancel — walks every loaded Provider until
        one claims ownership, then writes the cancel sentinel so the
        runner's `_cancel_watcher` calls `client.interrupt()`. No
        killpg, no bg-sweep at the backend; the runner sweeps its own
        setsid'd bg shells before exit. Used by turn-stop paths
        (Stop-button, interrupt-by-new-message, promote_queued).

        Returns True if any provider acknowledged the cancel.
        """
        signalled = False
        for prov in known_providers():
            try:
                if prov.cancel_turn(run_id):
                    signalled = True
            except Exception:
                logger.exception(
                    "cancel_turn_fanout: provider %s raised on cancel %s",
                    prov.id, run_id,
                )
        return signalled

    @staticmethod
    def _cancel_recovered_run_fanout(run_id: str) -> bool:
        """Hard-stop a recovered stub run after cooperative stop failed.

        Normal turn-stop stays soft. Recovered stubs have no live
        in-process consumer left to guarantee the sentinel is observed,
        so a still-alive recovered process needs the delete-tier kill
        path after a grace period.
        """
        signalled = False
        for prov in known_providers():
            try:
                is_recovered = getattr(prov, "is_recovered_run", lambda _rid: False)
                if is_recovered(run_id) and prov.cancel_run(run_id):
                    signalled = True
            except Exception:
                logger.exception(
                    "cancel_recovered_run_fanout: provider %s raised on cancel %s",
                    prov.id, run_id,
                )
        return signalled

    @staticmethod
    def _message_has_steer_prompt(
        msg: Optional[dict],
        *,
        client_id: Optional[str],
        lifecycle_msg_id: Optional[str],
    ) -> bool:
        if msg is None:
            return False
        for event in msg.get("events") or []:
            if event.get("type") != "steer_prompt":
                continue
            data = event.get("data") or {}
            if client_id and data.get("client_id") == client_id:
                return True
            if lifecycle_msg_id and data.get("lifecycle_msg_id") == lifecycle_msg_id:
                return True
        return False

    async def steer_active_turn(
        self,
        *,
        app_session_id: str,
        prompt: str,
        display_prompt: Optional[str],
        images: Optional[list],
        client_id: Optional[str],
        lifecycle_msg_id: str,
    ) -> bool:
        # Resolve which provider owns the in-flight run. The common case
        # (and the path the steer tests exercise) is the session's current
        # provider — try it FIRST. Only when it doesn't own the run do we
        # scan the provider registry: the user may have switched
        # provider/model metadata mid-turn (that change applies lazily to
        # the next prompt), so the live run is still owned by the
        # previously-active provider instance.
        run_ids = list(self.turn_manager.active_run_ids.get(app_session_id, []))
        candidates: list[tuple[object, str]] = []
        seen_runs: set[str] = set()

        def _collect(prov: object) -> None:
            if not getattr(prov, "supports_steering", False):
                return
            runs = getattr(prov, "_runs", {})
            for rid in run_ids:
                if rid in runs and rid not in seen_runs:
                    seen_runs.add(rid)
                    candidates.append((prov, rid))

        try:
            _collect(self.provider_for_session(app_session_id))
        except Exception:
            pass
        if not candidates:
            for prov in known_providers():
                _collect(prov)

        save_callback = self.turn_manager._turn_save_callbacks.get(app_session_id)
        if save_callback is None:
            return False
        if len(candidates) != 1:
            return False
        provider, run_id = candidates[0]
        current_assistant_msgs = getattr(
            self.turn_manager, "current_assistant_msgs", {},
        )
        if self._message_has_steer_prompt(
            current_assistant_msgs.get(app_session_id),
            client_id=client_id,
            lifecycle_msg_id=lifecycle_msg_id,
        ):
            await self.dispatch_raw(app_session_id, {
                "type": "steer_prompt_persisted",
                "data": {
                    "app_session_id": app_session_id,
                    "client_id": client_id,
                    "lifecycle_msg_id": lifecycle_msg_id,
                },
            })
            return True
        deadline = _time.monotonic() + _STEER_READY_RETRY_SECONDS
        while True:
            if provider.steer_run(run_id, prompt, images):
                break
            if _time.monotonic() >= deadline:
                return False
            await asyncio.sleep(_STEER_READY_RETRY_INTERVAL_SECONDS)
        event_data = {
            "uuid": str(uuid.uuid4()),
            "prompt": display_prompt or prompt,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "client_id": client_id,
            "lifecycle_msg_id": lifecycle_msg_id,
        }
        if images:
            event_data["images"] = _save_message_images(
                app_session_id,
                event_data["uuid"],
                images,
            )
        await save_callback({
            "type": "steer_prompt",
            "data": event_data,
        })
        await self.dispatch_raw(app_session_id, {
            "type": "steer_prompt_persisted",
            "data": {
                "app_session_id": app_session_id,
                "client_id": client_id,
                "lifecycle_msg_id": lifecycle_msg_id,
            },
        })
        return True

    def _resolve_approval(self, delegation_id: str, payload: dict) -> bool:
        from orchs.manager._approval import resolve_approval as _impl
        return _impl(self, delegation_id, payload)

    # ------------------------------------------------------------------
    # Per-session prompt queue + processor task ownership
    # ------------------------------------------------------------------
    def _reject_if_adv_sync_fork_locked(self, app_session_id: str) -> None:
        _target = session_manager.get(app_session_id)
        if not _target or _target.get("kind") != "adv_sync_fork":
            return
        _parent_id = _target.get("parent_session_id")
        _parent = session_manager.get(_parent_id) if _parent_id else None
        for ov in (_parent or {}).get("adv_sync_overlays") or []:
            if ov.get("status") != "running":
                continue
            if app_session_id not in (
                ov.get("supportive_fork_id"),
                ov.get("adversarial_fork_id"),
            ):
                continue
            raise RuntimeError(
                "adv_sync_fork locked: parent has running overlay "
                f"{ov.get('id')}",
            )

    def _remember_active_prompt_client_id(
        self,
        app_session_id: str,
        item_id: str,
        client_id: object,
    ) -> Optional[str]:
        if not isinstance(client_id, str) or not client_id:
            return None
        key = (app_session_id, client_id)
        existing = self._active_prompt_client_ids.get(key)
        if existing:
            return existing
        self._active_prompt_client_ids[key] = item_id
        self._prompt_client_id_by_item[item_id] = key
        return None

    def _forget_active_prompt_item(self, item_id: Optional[str]) -> None:
        if not item_id:
            return
        key = self._prompt_client_id_by_item.pop(item_id, None)
        if key and self._active_prompt_client_ids.get(key) == item_id:
            self._active_prompt_client_ids.pop(key, None)

    def try_claim_prompt_client_id(
        self,
        app_session_id: str,
        item_id: str,
        client_id: object,
    ) -> Optional[str]:
        """Atomically claim `(app_session_id, client_id)` for `item_id`.

        Returns the item_id already holding the claim (⇒ this is a
        duplicate send — caller should echo the in-flight turn and skip)
        or None after claiming it for `item_id`. No await: atomic on the
        single-threaded loop, so two concurrent same-client_id sends
        (offline re-dispatch / reconnect) can't both claim. Closes the
        TOCTOU between the WS handler's read-only dedup checks and
        `submit_prompt`, which would otherwise let the second send persist
        and broadcast a phantom queued bubble before being deduped."""
        return self._remember_active_prompt_client_id(
            app_session_id, item_id, client_id,
        )

    def active_prompt_for_client_id(
        self,
        app_session_id: str,
        client_id: object,
    ) -> Optional[dict]:
        if not isinstance(client_id, str) or not client_id:
            return None
        item_id = self._active_prompt_client_ids.get((app_session_id, client_id))
        if not item_id:
            return None
        lifecycle_msg_id = self.user_prompt_manager.get_in_flight_lifecycle_msg_id(
            app_session_id,
        )
        return {
            "item_id": item_id,
            "lifecycle_msg_id": lifecycle_msg_id,
        }

    async def submit_prompt_async(self, app_session_id: str, params: dict) -> str:
        await asyncio.to_thread(self._reject_if_adv_sync_fork_locked, app_session_id)
        return self.submit_prompt(app_session_id, params, _adv_sync_checked=True)

    def submit_prompt(
        self,
        app_session_id: str,
        params: dict,
        *,
        _adv_sync_checked: bool = False,
    ) -> str:
        """Enqueue a prompt for `app_session_id` and ensure a processor
        task is running. Called by the WS handler (or any other producer).
        Returns immediately; the actual work happens in the per-session
        processor task owned by this coordinator and unaffected by WS
        lifecycle.

        Returns the queue item ID (for promote_queued / tracking).
        """
        import uuid
        if not _adv_sync_checked:
            self._reject_if_adv_sync_fork_locked(app_session_id)
        q = self._prompt_queues.get(app_session_id)
        if q is None:
            q = asyncio.Queue()
            self._prompt_queues[app_session_id] = q
        item_id = params.get("_queued_id") or str(uuid.uuid4())
        params["_queued_id"] = item_id
        # The WS handler may have already claimed the client_id for this
        # item_id via try_claim_prompt_client_id (atomic admission gate).
        # Re-claiming here would see its own claim and wrongly self-dedup,
        # so skip it in that case.
        if not params.pop("_client_id_claimed", False):
            existing_item_id = self._remember_active_prompt_client_id(
                app_session_id,
                item_id,
                params.get("client_id"),
            )
            if existing_item_id:
                if item_id != existing_item_id:
                    session_manager.remove_queued_prompt(app_session_id, item_id)
                return existing_item_id
        q.put_nowait(params)
        # Track queued IDs
        ids = self._queued_ids.setdefault(app_session_id, [])
        ids.append(item_id)
        task = self._processor_tasks.get(app_session_id)
        if task is None or task.done():
            task = asyncio.create_task(
                self._run_session_processor(app_session_id),
                name=f"prompt-processor-{app_session_id[:8]}",
            )
            self._processor_tasks[app_session_id] = task
        return item_id

    async def submit_team_message(
        self,
        *,
        sender_session_id: str,
        target_session_id: str,
        message: str,
        detach: bool = False,
        expect_mssg_response: bool = False,
        provider_id: str = "",
        model: str = "",
        reasoning_effort: str = "",
        model_task_key: str = "delegation_message",
    ) -> dict:
        import uuid
        import team_messaging

        sender, target = await asyncio.to_thread(
            team_messaging.validate_message_route,
            sender_session_id=sender_session_id,
            target_session_id=target_session_id,
        )
        run_config = self._resolve_delegation_run_config(
            model_task_key,
            sender=sender,
            target=target,
            provider_id=provider_id,
            model=model,
            reasoning_effort=reasoning_effort,
        )
        metadata = await asyncio.to_thread(
            team_messaging.build_message_metadata,
            sender_session_id=sender_session_id,
            target_session_id=target_session_id,
        )
        if expect_mssg_response:
            metadata["expects_response"] = True
            metadata["response_mode"] = team_messaging.MSSG_RESPONSE_MODE
        message_source = team_messaging.source_for_message_route(sender, target)
        queue_item_id = str(uuid.uuid4())
        lifecycle_msg_id = str(uuid.uuid4())
        panel = await self._start_team_message_panel(
            sender_session_id=sender_session_id,
            target_session_id=target_session_id,
            target=target,
            message=message,
            queue_item_id=queue_item_id,
            run_mode=team_messaging.SOURCE,
        )
        cancel_panel_watch = None
        if panel is not None:
            cancel_panel_watch = self._watch_team_message_panel(
                sender_session_id=sender_session_id,
                target_session_id=target_session_id,
                lifecycle_msg_id=lifecycle_msg_id,
                panel=panel,
            )
        # Turn-join: if the sender has an active turn, register this target
        # turn as outstanding so the sender's turn stays open until it finishes.
        # `detach` (the `delegate` tool) opts out — the dispatched work runs
        # independently and does NOT hold the sender's turn open (off-topic
        # handoff so the sender can complete its own work).
        if not detach and self.turn_manager.has_active_turn(sender_session_id):
            self.register_mssg_turn_waiter(
                sender_session_id=sender_session_id,
                lifecycle_msg_id=lifecycle_msg_id,
                target_session_id=target_session_id,
            )
        try:
            queue_item = await asyncio.to_thread(
                team_messaging.queue_payload,
                queue_item_id=queue_item_id,
                sender_session_id=sender_session_id,
                message=message,
                metadata=metadata,
                lifecycle_msg_id=lifecycle_msg_id,
                target_session_id=target_session_id,
                source=message_source,
            )
            await asyncio.to_thread(
                session_manager.add_queued_prompt,
                target_session_id,
                queue_item,
            )
            cli_prompt = await asyncio.to_thread(
                team_messaging.format_team_message_prompt,
                message,
                metadata,
                target_session_id=target_session_id,
            )
            await self.submit_prompt_async(target_session_id, {
                "_queued_id": queue_item_id,
                "app_session_id": target_session_id,
                "prompt": message,
                "cli_prompt": cli_prompt,
                "provider_id": run_config.get("provider_id") or "",
                "model": run_config.get("model") or "",
                "reasoning_effort": run_config.get("reasoning_effort") or "",
                "allow_model_override": True,
                "cwd": target.get("cwd") or sender.get("cwd") or "",
                "orchestration_mode": target.get("orchestration_mode") or "team",
                "source": message_source,
                "user_initiated": False,
                "lifecycle_msg_id": lifecycle_msg_id,
                "team_message": {
                    "message": message,
                    "metadata": metadata,
                },
            })
        except Exception:
            await asyncio.to_thread(
                session_manager.remove_queued_prompt,
                target_session_id,
                queue_item_id,
            )
            if cancel_panel_watch is not None:
                cancel_panel_watch()
            raise
        return {
            "success": True,
            "queued_id": queue_item_id,
            "target_session_id": target_session_id,
            "expects_response": expect_mssg_response,
        }

    def _resolve_delegation_run_config(
        self,
        task_key: str,
        *,
        sender: dict,
        target: Optional[dict] = None,
        provider_id: str = "",
        model: str = "",
        reasoning_effort: str = "",
    ) -> dict[str, str]:
        import config_store

        provider_id = str(provider_id or "").strip()
        model = str(model or "").strip()
        reasoning_effort = str(reasoning_effort or "").strip()
        assignment = config_store.get_internal_llm_task(task_key)
        if assignment:
            resolved = config_store.resolve_internal_llm(task_key)
            provider_id = provider_id or str(resolved.get("provider_id") or "").strip()
            model = model or str(resolved.get("model") or "").strip()
            reasoning_effort = (
                reasoning_effort
                or str(resolved.get("reasoning_effort") or "").strip()
            )
        if provider_id and not model:
            provider = config_store.get_provider(provider_id) or {}
            model = str(provider.get("default_model") or "").strip()
        target = target or {}
        return {
            "provider_id": provider_id or str(target.get("provider_id") or sender.get("provider_id") or "").strip(),
            "model": model or str(target.get("model") or sender.get("model") or "").strip(),
            "reasoning_effort": reasoning_effort or str(target.get("reasoning_effort") or sender.get("reasoning_effort") or "").strip(),
        }

    def register_mssg_turn_waiter(
        self,
        *,
        sender_session_id: str,
        lifecycle_msg_id: str,
        target_session_id: str,
    ) -> None:
        """Register an mssg-initiated target turn as outstanding for the
        sender's current turn (turn-join). The sender's turn completion will
        await this Future, which resolves when the target's
        user_message_done/failed fires for this lifecycle."""
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._mssg_turn_waiters.setdefault(sender_session_id, {})[lifecycle_msg_id] = fut

        async def resolver(event: dict) -> None:
            if event.get("type") not in ("user_message_done", "user_message_failed"):
                return
            if (event.get("data") or {}).get("lifecycle_msg_id") != lifecycle_msg_id:
                return
            if not fut.done():
                fut.set_result({"success": event["type"] == "user_message_done"})
            self.unregister_ws(target_session_id, resolver)
            self._mssg_turn_waiters.get(sender_session_id, {}).pop(lifecycle_msg_id, None)

        self.register_ws(target_session_id, resolver)

    async def await_outstanding_mssg(self, app_session_id: str) -> None:
        """Block until every mssg target turn the sender initiated this turn
        has completed (or the safety timeout elapses). Called at sender turn
        completion so the turn is not 'done' while spawned work still runs."""
        waiters = self._mssg_turn_waiters.get(app_session_id, {})
        if not waiters:
            return
        futures = list(waiters.values())
        try:
            await asyncio.wait_for(
                asyncio.gather(*futures, return_exceptions=True),
                timeout=_MSSG_TURN_JOIN_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "turn-join: %s timed out waiting for %d outstanding mssg target(s); "
                "completing turn anyway",
                app_session_id, len(futures),
            )
        finally:
            self._mssg_turn_waiters.pop(app_session_id, None)

    def _delegate_task_create_session(
        self,
        parent_session_id: str,
        task: str,
        model: str,
        cwd: str,
        *,
        provider_id: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        sub_session: bool = True,
    ) -> str:
        """Mint the session target for an auto-created delegate_task."""
        import config_store
        name = (task or "").strip()[:60] or "delegate_task"
        if not model and provider_id:
            provider = config_store.get_provider(provider_id) or {}
            model = str(provider.get("default_model") or "").strip()
        if not model:
            model = config_store.default_session_model()
        if sub_session:
            sess = session_manager.create_sub_session(
                parent_session_id=parent_session_id,
                name=name,
                cwd=cwd,
                model=model,
                provider_id=provider_id,
                reasoning_effort=reasoning_effort,
            )
            return sess["id"]
        sess = session_manager.create(
            name=name, cwd=cwd, orchestration_mode="native",
            model=model,
            provider_id=provider_id,
            reasoning_effort=reasoning_effort,
            source="cli",
        )
        return sess["id"]

    async def run_delegate_task(
        self,
        *,
        sender_session_id: str,
        task: str,
        target_session_id: Optional[str] = None,
        model: str = "",
        cwd: str = "",
        provider_id: str = "",
        reasoning_effort: str = "",
        sub_session: bool = True,
        run_mode: str = "direct",
    ) -> dict:
        """The `delegate_task` router. Per the global `delegate_task_policy`:
        resolve a target (caller-supplied → search first suggestion → create
        new), optionally gate on user approval, then dispatch the task
        detached (does NOT join the sender's turn)."""
        import config_store
        import session_search
        from stores import pending_approvals

        policy = config_store.get_delegate_task_policy()
        caller = sender_session_id
        caller_session = session_manager.get(caller) or {}
        create_config = self._resolve_delegation_run_config(
            "delegation_task",
            sender=caller_session,
            provider_id=provider_id,
            model=model,
            reasoning_effort=reasoning_effort,
        )
        delegate_provider_id = create_config.get("provider_id") or None
        delegate_reasoning_effort = create_config.get("reasoning_effort") or None
        if run_mode not in ("direct", "fork"):
            raise ValueError("run_mode must be direct or fork")
        if run_mode == "fork" and not target_session_id:
            raise ValueError("run_mode=fork requires target_session_id")
        target: Optional[str] = target_session_id or None
        if target == caller:
            raise ValueError(
                "delegate_task target_session_id must not be the sender_session_id"
            )
        created = False
        forked_from: Optional[str] = None

        # Resolve the target session.
        if not target:
            if policy in ("always_new", "always_new_approve"):
                target = self._delegate_task_create_session(
                    caller,
                    task,
                    create_config.get("model") or "",
                    cwd,
                    provider_id=delegate_provider_id,
                    reasoning_effort=delegate_reasoning_effort,
                    sub_session=sub_session,
                )
                created = True
            else:  # auto / manual → search_sessions, take the first usable suggestion
                try:
                    suggestion = await session_search.search(task)
                except Exception:
                    logger.exception("delegate_task: session_search failed")
                    suggestion = {"session_ids": []}
                ids = (suggestion or {}).get("session_ids") or []
                target = next(
                    (
                        sid for sid in ids
                        if isinstance(sid, str)
                        and sid
                        and sid != caller
                        and session_manager.get(sid)
                    ),
                    None,
                )
                if not target:
                    target = self._delegate_task_create_session(
                        caller,
                        task,
                        create_config.get("model") or "",
                        cwd,
                        provider_id=delegate_provider_id,
                        reasoning_effort=delegate_reasoning_effort,
                        sub_session=sub_session,
                    )
                    created = True

        if run_mode == "fork":
            try:
                fork = await asyncio.to_thread(
                    session_manager.fork,
                    target,
                    user_initiated=False,
                    kind="delegate_task_fork",
                )
            except KeyError as exc:
                raise ValueError("target_session_id does not exist") from exc
            target = fork["id"]
            forked_from = target_session_id
            created = True

        # Approval gate (manual / always_new_approve) — reuses pending_approvals
        # + approval_waiters + the existing /api/pending_approvals/{id}/approve|deny.
        if policy in ("manual", "always_new_approve"):
            dt_id = f"dt_{uuid.uuid4().hex[:10]}"
            loop = asyncio.get_running_loop()
            fut: asyncio.Future = loop.create_future()
            self.approval_waiters[dt_id] = fut
            try:
                pending_approvals.create(
                    delegation_id=dt_id,
                    app_session_id=caller,
                    cwd=cwd,
                    justification=(
                        "Create a new target and delegate the task"
                        if created else f"Delegate the task to session {target}"
                    ),
                    proposed_description=(task or "")[:200] or "delegate_task",
                    proposed_orchestration_mode="native",
                    instructions_preview=task,
                    model=create_config.get("model") or "",
                )
            except Exception:
                self.approval_waiters.pop(dt_id, None)
                raise
            await self.persist_and_dispatch_raw(caller, {
                "type": "worker_creation_requested",
                "data": {
                    "delegation_id": dt_id,
                    "app_session_id": caller,
                    "kind": "delegate_task",
                    "task": task,
                    "target_session_id": target,
                    "created_session": created,
                },
            })
            try:
                await asyncio.wait_for(fut, timeout=_DELEGATE_TASK_APPROVAL_TIMEOUT)
            except asyncio.TimeoutError:
                return {"success": False, "error": "delegate_task approval timed out",
                        "target_session_id": target}
            finally:
                self.approval_waiters.pop(dt_id, None)
            rec = pending_approvals.get(dt_id)
            if not rec or rec.get("status") != "approved":
                return {"success": False, "error": "delegate_task denied by user",
                        "target_session_id": target}

        # Dispatch detached (does not join the sender's turn).
        target_session = session_manager.get(target) or {}
        run_config = self._resolve_delegation_run_config(
            "delegation_task",
            sender=caller_session,
            target=target_session,
            provider_id=provider_id,
            model=model,
            reasoning_effort=reasoning_effort,
        )
        await self.submit_team_message(
            sender_session_id=caller, target_session_id=target,
            message=task, detach=True,
            provider_id=run_config.get("provider_id") or "",
            model=run_config.get("model") or "",
            reasoning_effort=run_config.get("reasoning_effort") or "",
            model_task_key="delegation_task",
        )
        created_target = session_manager.get(target) if created else None
        return {
            "success": True,
            "target_session_id": target,
            "forked_from_session_id": forked_from,
            "run_mode": run_mode,
            "created_session": created,
            "created_sub_session": bool(
                created_target and created_target.get("kind") == "sub_session"
            ),
            "policy": policy,
        }

    @staticmethod
    def _team_message_panel_event_in_scope(
        event: dict,
        lifecycle_msg_id: str,
        active: bool,
    ) -> tuple[bool, bool]:
        data = event.get("data") or {}
        event_lifecycle = data.get("lifecycle_msg_id")
        if event_lifecycle is not None:
            matched = event_lifecycle == lifecycle_msg_id
            return matched, matched
        return active, active

    def _watch_team_message_panel(
        self,
        *,
        sender_session_id: str,
        target_session_id: str,
        lifecycle_msg_id: str,
        panel: dict,
    ):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None
        done: asyncio.Future = loop.create_future()
        state = {"forwarding_active": False}

        async def wait_callback(event: dict) -> None:
            in_scope, active = self._team_message_panel_event_in_scope(
                event,
                lifecycle_msg_id,
                bool(state["forwarding_active"]),
            )
            state["forwarding_active"] = active
            if not in_scope:
                return
            data = event.get("data") or {}
            await self._forward_team_message_panel_event(
                sender_session_id=sender_session_id,
                panel=panel,
                event=event,
            )
            if event.get("type") == "user_message_done" and not done.done():
                done.set_result({"success": True})
            elif event.get("type") == "user_message_failed" and not done.done():
                done.set_result({
                    "success": False,
                    "error": data.get("error") or data.get("reason") or "target turn failed",
                })

        self.register_ws(target_session_id, wait_callback)

        def cancel_watch() -> None:
            if not done.done():
                done.set_result({
                    "success": False,
                    "error": "message queue failed",
                })

        async def watch() -> None:
            try:
                result = await asyncio.wait_for(done, timeout=24 * 60 * 60)
            except Exception as exc:
                await self._emit_team_message_panel_complete(
                    sender_session_id=sender_session_id,
                    panel=panel,
                    success=False,
                    error=str(exc) or exc.__class__.__name__,
                )
            else:
                await self._emit_team_message_panel_complete(
                    sender_session_id=sender_session_id,
                    panel=panel,
                    success=bool(result.get("success")),
                    error=result.get("error"),
                )
            finally:
                self.unregister_ws(target_session_id, wait_callback)

        loop.create_task(watch(), name=f"team-message-panel-{target_session_id[:8]}")
        return cancel_watch

    async def _start_team_message_panel(
        self,
        *,
        sender_session_id: str,
        target_session_id: str,
        target: dict,
        message: str,
        queue_item_id: str,
        run_mode: str,
    ) -> Optional[dict]:
        turn_save = self.turn_manager.get_turn_save_callback(sender_session_id)
        panels = self.turn_manager.current_turn_workers.get(sender_session_id)
        if turn_save is None or panels is None:
            return None
        delegation_id = f"{run_mode}_{queue_item_id}"
        description = target.get("name") or target_session_id
        panel_kind = "sub_session" if target.get("kind") == "sub_session" else "session"
        started_at = datetime.now(timezone.utc).isoformat()
        insert_at = self.turn_manager.in_flight_event_count_after_current_event(
            sender_session_id
        )
        panel = {
            "delegation_id": delegation_id,
            "worker_session_id": target_session_id,
            "worker_description": description,
            "panel_kind": panel_kind,
            "started_at": started_at,
            "insert_at": insert_at,
            "orchestration_mode": target.get("orchestration_mode"),
            "is_new": False,
            "instructions_preview": message[:2000],
            "events": [],
            "jsonl_path": None,
            "new_byte_offset": None,
            "fork_agent_sid": None,
            "run_mode": run_mode,
            "token_usage": None,
        }
        panels.append(panel)
        await turn_save({"type": "worker_start", "data": {
            "delegation_id": delegation_id,
            "worker_session_id": target_session_id,
            "worker_description": description,
            "panel_kind": panel_kind,
            "started_at": started_at,
            "insert_at": insert_at,
            "orchestration_mode": target.get("orchestration_mode"),
            "run_mode": run_mode,
            "is_new": False,
            "instructions_preview": message[:2000],
        }})
        return panel

    async def emit_session_created_panel(
        self,
        *,
        sender_session_id: str,
        target_session: dict,
    ) -> Optional[dict]:
        turn_save = self.turn_manager.get_turn_save_callback(sender_session_id)
        panels = self.turn_manager.current_turn_workers.get(sender_session_id)
        if turn_save is None or panels is None:
            return None
        target_session_id = str(target_session.get("id") or "")
        if not target_session_id:
            return None
        is_sub_session = target_session.get("kind") == "sub_session"
        panel_kind = "sub_session_created" if is_sub_session else "session_created"
        description = target_session.get("name") or target_session_id
        started_at = datetime.now(timezone.utc).isoformat()
        insert_at = self.turn_manager.in_flight_event_count_after_current_event(
            sender_session_id
        )
        delegation_id = f"created_{target_session_id}"
        panel = {
            "delegation_id": delegation_id,
            "worker_session_id": target_session_id,
            "worker_description": f"{description} created",
            "panel_kind": panel_kind,
            "started_at": started_at,
            "insert_at": insert_at,
            "orchestration_mode": target_session.get("orchestration_mode"),
            "is_new": True,
            "instructions_preview": "",
            "events": [],
            "jsonl_path": None,
            "new_byte_offset": None,
            "fork_agent_sid": None,
            "run_mode": "created",
            "token_usage": None,
        }
        panels.append(panel)
        await turn_save({"type": "worker_start", "data": {
            "delegation_id": delegation_id,
            "worker_session_id": target_session_id,
            "worker_description": panel["worker_description"],
            "panel_kind": panel_kind,
            "started_at": started_at,
            "insert_at": insert_at,
            "orchestration_mode": target_session.get("orchestration_mode"),
            "run_mode": "created",
            "is_new": True,
            "instructions_preview": "",
        }})
        return panel

    async def _emit_team_message_panel_complete(
        self,
        *,
        sender_session_id: str,
        panel: Optional[dict],
        success: bool,
        error: Optional[str] = None,
    ) -> None:
        if not panel:
            return
        panel["success"] = success
        panel["error"] = error
        turn_save = self.turn_manager.get_turn_save_callback(sender_session_id)
        if turn_save is None:
            return
        await turn_save({"type": "worker_complete", "data": {
            "delegation_id": panel["delegation_id"],
            "worker_session_id": panel["worker_session_id"],
            "jsonl_path": None,
            "new_byte_offset": None,
            "token_usage": None,
            "success": success,
            "error": error,
            "run_mode": panel.get("run_mode"),
        }})

    async def _forward_team_message_panel_event(
        self,
        *,
        sender_session_id: str,
        panel: Optional[dict],
        event: dict,
    ) -> None:
        if not panel:
            return
        if event.get("type") not in {
            "agent_message",
            "manager_event",
            "todos_snapshot",
            "worker_prep_start",
            "worker_prep_event",
            "worker_prep_complete",
            "worker_prep_cancelled",
            "turn_start",
            "turn_complete",
            "turn_stopped",
        }:
            return
        panel.setdefault("events", []).append(event)
        turn_save = self.turn_manager.get_turn_save_callback(sender_session_id)
        if turn_save is None:
            return
        await turn_save({"type": "worker_event", "data": {
            "delegation_id": panel["delegation_id"],
            "event": event,
        }})

    async def ask_team_message(
        self,
        *,
        sender_session_id: str,
        target_session_id: str,
        message: str,
        ask_id: str = "",
        timeout_s: float = 24 * 60 * 60,
        provider_id: str = "",
        model: str = "",
        reasoning_effort: str = "",
        model_task_key: str = "delegation_ask",
    ) -> dict:
        import uuid
        import ask_status_store
        import team_messaging
        import user_msg_lifecycle

        # Restart re-attach: a stable client-side `ask_id` lets the runner's
        # retry re-find this call after a backend restart. If a result is
        # already stored, return it. If a (still-waiting) correlation is
        # stored, reuse its ids and do NOT re-queue a duplicate prompt.
        existing = ask_status_store.read_status(ask_id) if ask_id else None
        if existing and existing.get("result") is not None:
            return existing["result"]
        reattach = existing is not None
        lifecycle_msg_id = (existing or {}).get("lifecycle_msg_id") or str(uuid.uuid4())
        queue_item_id = (existing or {}).get("queue_item_id") or str(uuid.uuid4())

        sender, target = await asyncio.to_thread(
            team_messaging.validate_message_route,
            sender_session_id=sender_session_id,
            target_session_id=target_session_id,
        )
        run_config = self._resolve_delegation_run_config(
            model_task_key,
            sender=sender,
            target=target,
            provider_id=provider_id,
            model=model,
            reasoning_effort=reasoning_effort,
        )
        metadata = await asyncio.to_thread(
            team_messaging.build_message_metadata,
            sender_session_id=sender_session_id,
            target_session_id=target_session_id,
        )
        metadata["expects_response"] = True
        panel = None
        if not reattach:
            panel = await self._start_team_message_panel(
                sender_session_id=sender_session_id,
                target_session_id=target_session_id,
                target=target,
                message=message,
                queue_item_id=queue_item_id,
                run_mode=team_messaging.ASK_SOURCE,
            )
        ask_prompt = (
            f"{message}\n\n"
            "<response_contract>\n"
            "The sender is waiting for this turn to finish. Put the answer in "
            "your assistant response for this turn.\n"
            "</response_contract>"
        )
        done: asyncio.Future = asyncio.get_running_loop().create_future()
        forwarding_active = False

        async def wait_callback(event: dict) -> None:
            nonlocal forwarding_active
            in_scope, forwarding_active = self._team_message_panel_event_in_scope(
                event,
                lifecycle_msg_id,
                forwarding_active,
            )
            if not in_scope:
                return
            data = event.get("data") or {}
            if panel is not None:
                await self._forward_team_message_panel_event(
                    sender_session_id=sender_session_id,
                    panel=panel,
                    event=event,
                )
            if event.get("type") == "user_message_done" and not done.done():
                done.set_result({"success": True})
            elif event.get("type") == "user_message_failed" and not done.done():
                done.set_result({
                    "success": False,
                    "error": data.get("error") or data.get("reason") or "target turn failed",
                })

        self.register_ws(target_session_id, wait_callback)
        result: dict
        try:
            if not reattach:
                if ask_id:
                    await ask_status_store.write_status_async(
                        ask_id,
                        lifecycle_msg_id=lifecycle_msg_id,
                        queue_item_id=queue_item_id,
                        sender_session_id=sender_session_id,
                        target_session_id=target_session_id,
                    )
                queue_item = await asyncio.to_thread(
                    team_messaging.queue_payload,
                    queue_item_id=queue_item_id,
                    sender_session_id=sender_session_id,
                    message=message,
                    metadata=metadata,
                    lifecycle_msg_id=lifecycle_msg_id,
                    target_session_id=target_session_id,
                    source=team_messaging.ASK_SOURCE,
                )
                await asyncio.to_thread(
                    session_manager.add_queued_prompt,
                    target_session_id,
                    queue_item,
                )
                cli_prompt = await asyncio.to_thread(
                    team_messaging.format_team_message_prompt,
                    ask_prompt,
                    metadata,
                    target_session_id=target_session_id,
                )
                await self.submit_prompt_async(target_session_id, {
                    "_queued_id": queue_item_id,
                    "app_session_id": target_session_id,
                    "prompt": message,
                    "cli_prompt": cli_prompt,
                    "provider_id": run_config.get("provider_id") or "",
                    "model": run_config.get("model") or "",
                    "reasoning_effort": run_config.get("reasoning_effort") or "",
                    "allow_model_override": True,
                    "cwd": target.get("cwd") or sender.get("cwd") or "",
                    "orchestration_mode": target.get("orchestration_mode") or "team",
                    "source": team_messaging.ASK_SOURCE,
                    "user_initiated": False,
                    "lifecycle_msg_id": lifecycle_msg_id,
                    "team_message": {
                        "message": message,
                        "metadata": metadata,
                    },
                })
            # Close the crash-before-persist window: the target turn may have
            # completed during the restart before `result` was stored. Recovery
            # normally writes a durable user_message_done/failed terminal; older
            # already-reconciled runs may predate that terminal, so a reattached
            # ask also consults the target assistant message + its run
            # complete.json before it blocks again. That repairs existing
            # stuck ask callers without re-queueing a duplicate prompt.
            terminal = user_msg_lifecycle.terminal_event_for_lifecycle(
                target_session_id, lifecycle_msg_id
            )
            if terminal is not None:
                if terminal.get("type") == "user_message_done":
                    result = {"success": True}
                else:
                    tdata = terminal.get("data") or {}
                    result = {
                        "success": False,
                        "error": tdata.get("error") or tdata.get("reason") or "target turn failed",
                    }
            elif reattach:
                recovered = self._team_message_completed_result_from_store(
                    target_session_id=target_session_id,
                    lifecycle_msg_id=lifecycle_msg_id,
                )
                if recovered is not None:
                    result = recovered
                else:
                    result = await asyncio.wait_for(done, timeout=timeout_s)
            else:
                result = await asyncio.wait_for(done, timeout=timeout_s)
        except Exception as exc:
            if panel is not None:
                await self._emit_team_message_panel_complete(
                    sender_session_id=sender_session_id,
                    panel=panel,
                    success=False,
                    error=str(exc) or exc.__class__.__name__,
                )
            raise
        finally:
            self.unregister_ws(target_session_id, wait_callback)
        if panel is not None:
            await self._emit_team_message_panel_complete(
                sender_session_id=sender_session_id,
                panel=panel,
                success=bool(result.get("success")),
                error=result.get("error"),
            )
        response = {}
        if result.get("success"):
            response = self._team_message_turn_response(
                target_session_id=target_session_id,
                lifecycle_msg_id=lifecycle_msg_id,
            )
        full = {
            **result,
            "target_session_id": target_session_id,
            "queued_id": queue_item_id,
            **response,
        }
        if ask_id:
            await ask_status_store.write_status_async(ask_id, result=full)
        return full

    def _team_message_turn_response(
        self,
        *,
        target_session_id: str,
        lifecycle_msg_id: str,
    ) -> dict:
        found = self._team_message_user_and_assistant(
            target_session_id=target_session_id,
            lifecycle_msg_id=lifecycle_msg_id,
        )
        if found is None:
            return {"response_message_id": None, "assistant_content": ""}
        _user_msg, assistant_msg = found
        if assistant_msg is None:
            return {"response_message_id": None, "assistant_content": ""}
        return {
            "response_message_id": assistant_msg.get("id"),
            "assistant_content": self._team_message_assistant_content(
                target_session_id,
                assistant_msg,
            ),
        }

    def _team_message_assistant_content(
        self,
        target_session_id: str,
        assistant_msg: dict,
    ) -> str:
        content = assistant_msg.get("content")
        if isinstance(content, str) and content:
            return content
        events = assistant_msg.get("events") or []
        if not events:
            return ""
        projected = _project_content_snapshot(events, content)
        if projected:
            msg_id = assistant_msg.get("id")
            if msg_id:
                session_manager.update_running_content(
                    target_session_id,
                    str(msg_id),
                    projected,
                )
                assistant_msg["content"] = projected
        return projected

    def _team_message_user_and_assistant(
        self,
        *,
        target_session_id: str,
        lifecycle_msg_id: str,
    ) -> Optional[tuple[dict, Optional[dict]]]:
        session = session_manager.get(target_session_id) or {}
        messages = session.get("messages") or []
        user_idx = None
        for idx, msg in enumerate(messages):
            if (
                msg.get("role") == "user"
                and msg.get("lifecycle_msg_id") == lifecycle_msg_id
            ):
                user_idx = idx
                break
        if user_idx is None:
            return None
        for msg in messages[user_idx + 1:]:
            if msg.get("role") == "assistant":
                return messages[user_idx], msg
        return messages[user_idx], None

    def _team_message_complete_for_assistant(
        self,
        *,
        target_session_id: str,
        assistant_msg_id: str,
    ) -> Optional[dict]:
        try:
            from runs_dir import read_best_complete, runs_root
            root = runs_root()
        except Exception:
            logger.debug("ask reattach: runs_root unavailable", exc_info=True)
            return None
        if not root.exists():
            return None
        candidates = []
        for child in root.iterdir():
            if not child.is_dir():
                continue
            bs_path = child / "backend_state.json"
            if not bs_path.exists():
                continue
            try:
                state = json.loads(bs_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            persist_to = state.get("persist_to") or state.get("app_session_id")
            if persist_to != target_session_id:
                continue
            if state.get("target_message_id") != assistant_msg_id:
                continue
            try:
                candidates.append((child.stat().st_mtime, child))
            except OSError:
                continue
        for _mtime, child in sorted(candidates, reverse=True):
            try:
                complete = read_best_complete(child)
            except Exception:
                logger.debug(
                    "ask reattach: failed reading complete for %s", child.name,
                    exc_info=True,
                )
                continue
            if isinstance(complete, dict):
                return complete
        return None

    def _team_message_completed_result_from_store(
        self,
        *,
        target_session_id: str,
        lifecycle_msg_id: str,
    ) -> Optional[dict]:
        found = self._team_message_user_and_assistant(
            target_session_id=target_session_id,
            lifecycle_msg_id=lifecycle_msg_id,
        )
        if found is None:
            return None
        user_msg, assistant_msg = found
        if user_msg.get("status") == "error":
            return {
                "success": False,
                "error": user_msg.get("errorText") or "target turn failed",
            }
        if assistant_msg is None:
            return None

        response = {
            "response_message_id": assistant_msg.get("id"),
            "assistant_content": self._team_message_assistant_content(
                target_session_id,
                assistant_msg,
            ),
        }
        complete = self._team_message_complete_for_assistant(
            target_session_id=target_session_id,
            assistant_msg_id=str(assistant_msg.get("id") or ""),
        )
        if complete is not None:
            success = bool(complete.get("success"))
            if success:
                return {"success": True, **response}
            return {
                "success": False,
                "error": complete.get("error") or "target turn failed",
            }

        if assistant_msg.get("completed_at"):
            return {"success": True, **response}
        if assistant_msg.get("error") or assistant_msg.get("errorText"):
            return {
                "success": False,
                "error": assistant_msg.get("errorText") or "target turn failed",
            }
        return None

    async def create_worker_for_session(
        self,
        *,
        app_session_id: str,
        worker_description: str,
        justification: str,
        proposed_orchestration_mode: str,
        model: str,
        cwd: str,
        client_request_id: Optional[str] = None,
        node_id: Optional[str] = None,
    ) -> dict:
        import uuid
        from orchs.manager._approval import (
            await_fresh_worker_approval,
            spawn_approved_worker,
        )

        if not worker_description.strip():
            return {"success": False, "error": "worker_description is required"}
        if not justification.strip():
            return {"success": False, "error": t("delegation.justification_required")}
        if proposed_orchestration_mode == "manager":
            proposed_orchestration_mode = "team"
        if proposed_orchestration_mode not in ("team", "native"):
            return {
                "success": False,
                "error": t("delegation.orchestration_mode_required"),
            }
        caller_session = session_manager.get(app_session_id)
        if not caller_session:
            return {"success": False, "error": t("error.ws_session_not_found")}
        worker_creation_policy = (
            caller_session.get("worker_creation_policy") or "ask"
        )
        if worker_creation_policy not in ("ask", "approve", "deny"):
            worker_creation_policy = "ask"
        if worker_creation_policy == "deny":
            return {
                "success": False,
                "error": "Fresh worker creation is auto-denied for this session.",
            }
        if not model:
            model = caller_session.get("model") or ""

        turn_save = self.turn_manager.get_turn_save_callback(app_session_id)

        async def ws_callback(event: dict) -> None:
            if turn_save is not None:
                await turn_save(event)
            else:
                await self.persist_and_dispatch_raw(app_session_id, event)

        cancel_event = (
            self.turn_manager.cancel_events.get(app_session_id) or asyncio.Event()
        )
        request_id = client_request_id or f"cw_{uuid.uuid4().hex[:10]}"
        effective_node_id = node_id or caller_session.get("node_id") or "primary"
        if worker_creation_policy == "approve":
            approved = await spawn_approved_worker(
                self,
                cwd=cwd,
                model=model,
                mode=proposed_orchestration_mode,
                description=worker_description,
                ws_callback=ws_callback,
                cancel_event=cancel_event,
                delegation_id=request_id,
                app_session_id=app_session_id,
                provider_id=self.provider_for_session(app_session_id).id,
                node_id=effective_node_id,
            )
        else:
            if turn_save is None:
                return {"success": False, "error": t("delegation.no_active_turn")}
            approved = await await_fresh_worker_approval(
                self,
                delegation_id=request_id,
                app_session_id=app_session_id,
                cwd=cwd,
                justification=justification,
                proposed_description=worker_description,
                proposed_orchestration_mode=proposed_orchestration_mode,
                instructions_preview="",
                model=model,
                ws_callback=ws_callback,
                cancel_event=cancel_event,
                node_id=effective_node_id,
            )
        if approved is None:
            return {"success": False, "error": t("delegation.user_denied_creation")}
        return {
            "success": True,
            "worker_session_id": approved["agent_session_id"],
            "worker_description": approved["description"],
            "orchestration_mode": proposed_orchestration_mode,
            "node_id": effective_node_id,
        }



    def get_queued_count(self, app_session_id: str) -> int:
        q = self._prompt_queues.get(app_session_id)
        if q is None:
            return 0
        return q.qsize()

    def has_queued_prompts(self, app_session_id: str) -> bool:
        """Return True if there are queued (not yet dequeued) prompts for this session."""
        return bool(self._queued_ids.get(app_session_id))

    def _queue_persisted_prompts_for_promotion(self, app_session_id: str) -> None:
        queued = (session_manager.get(app_session_id) or {}).get("queued_prompts") or []
        if not queued:
            return
        q = self._prompt_queues.get(app_session_id)
        if q is None:
            q = asyncio.Queue()
            self._prompt_queues[app_session_id] = q
        ids = self._queued_ids.setdefault(app_session_id, [])
        for qp in queued:
            qp_id = qp.get("id") or str(uuid.uuid4())
            if qp_id in ids:
                continue
            team_message = None
            import team_messaging
            if qp.get("source") in team_messaging.MESSAGE_SOURCES:
                sender_session_id = str(qp.get("sender_session_id") or "")
                metadata = team_messaging.build_message_metadata(
                    sender_session_id=sender_session_id,
                    target_session_id=app_session_id,
                )
                team_message = {
                    "message": qp.get("content", ""),
                    "metadata": metadata,
                }
            q.put_nowait({
                "_queued_id": qp_id,
                "prompt": qp.get("content", ""),
                "cli_prompt": qp.get("cli_prompt"),
                "images": qp.get("images"),
                "files": qp.get("files"),
                "client_id": qp.get("client_id"),
                "lifecycle_msg_id": qp.get("lifecycle_msg_id"),
                "source": qp.get("source"),
                "team_message": team_message,
                "capability_contexts": qp.get("capability_contexts") or [],
                "_alter_rewind_latest": bool(qp.get("alter_rewind_latest")),
            })
            ids.append(qp_id)

    async def promote_queued(
        self,
        app_session_id: str,
        action: Literal["interrupt", "steer"] = "interrupt",
    ) -> bool:
        q = self._prompt_queues.get(app_session_id)
        if not q or q.empty():
            self._queue_persisted_prompts_for_promotion(app_session_id)
            q = self._prompt_queues.get(app_session_id)
            if not q or q.empty():
                return False
        items = []
        while not q.empty():
            items.append(await q.get())
        if not items:
            return False

        first = items[0]
        if action == "steer":
            if await self.steer_active_turn(
                app_session_id=app_session_id,
                prompt=first.get("cli_prompt") or first.get("prompt") or "",
                display_prompt=first.get("prompt"),
                images=first.get("images"),
                client_id=first.get("client_id"),
                lifecycle_msg_id=first.get("lifecycle_msg_id") or str(uuid.uuid4()),
            ):
                item_id = first.get("_queued_id")
                ids = self._queued_ids.get(app_session_id, [])
                if item_id in ids:
                    ids.remove(item_id)
                if not ids:
                    self._queued_ids.pop(app_session_id, None)
                await asyncio.to_thread(
                    session_manager.remove_queued_prompt,
                    app_session_id,
                    item_id,
                )
                self._forget_active_prompt_item(item_id)
                for item in items[1:]:
                    await q.put(item)
                return True
            for item in items:
                await q.put(item)
            return False

        first["_interrupt"] = True
        for item in items:
            await q.put(item)
        # Cancel the current turn so the processor picks up the queued one.
        # Pass the queued prompt's lifecycle_msg_id so the displaced
        # turn's done event carries the interrupted_by cross-ref.
        interrupting_id = first.get("lifecycle_msg_id")
        cancelled = await self.cancel_turn(
            app_session_id, interrupted_by_msg_id=interrupting_id,
        )
        return cancelled or True  # Even if no active turn, the promote is valid

    def cancel_queued(self, app_session_id: str) -> bool:
        """Remove all queued items for a session (e.g. because the frontend
        is merging them into a single combined prompt). Items already
        dequeued by the processor but not yet started are caught by
        stamping their IDs into _cancelled_ids — the processor checks
        this set before executing and skips cancelled items."""
        q = self._prompt_queues.get(app_session_id)
        cancelled_any = False
        # Drain the queue and discard
        while q and not q.empty():
            try:
                item = q.get_nowait()
                if isinstance(item, dict):
                    self._forget_active_prompt_item(item.get("_queued_id"))
                cancelled_any = True
            except Exception:
                break
        # Also mark any dequeued-but-not-started items as cancelled
        ids = self._queued_ids.get(app_session_id, [])
        if ids:
            cancelled_set = self._cancelled_ids.setdefault(app_session_id, set())
            cancelled_set.update(ids)
            ids.clear()
        return cancelled_any

    async def update_queued(
        self, app_session_id: str, queued_id: str, content: str,
    ) -> bool:
        q = self._prompt_queues.get(app_session_id)
        if not q or q.empty():
            return False
        items = []
        updated = False
        while not q.empty():
            item = await q.get()
            if item and item.get("_queued_id") == queued_id:
                item["prompt"] = content
                if item.get("cli_prompt") is not None:
                    item["cli_prompt"] = content
                updated = True
            items.append(item)
        for item in items:
            await q.put(item)
        return updated

    async def update_latest_queued(
        self,
        app_session_id: str,
        content: str,
        cli_content: Optional[str],
        client_id: Optional[str],
        lifecycle_msg_id: str,
        capability_contexts: Optional[list[dict]] = None,
    ) -> Optional[str]:
        q = self._prompt_queues.get(app_session_id)
        if not q or q.empty():
            return None
        items = []
        latest_idx: Optional[int] = None
        while not q.empty():
            item = await q.get()
            if item:
                latest_idx = len(items)
            items.append(item)
        queued_id: Optional[str] = None
        if latest_idx is not None:
            item = items[latest_idx]
            queued_id = item.get("_queued_id")
            item["prompt"] = content
            if item.get("cli_prompt") is not None or cli_content is not None:
                item["cli_prompt"] = cli_content or content
            item["client_id"] = client_id
            item["lifecycle_msg_id"] = lifecycle_msg_id
            item["capability_contexts"] = list(capability_contexts or [])
        for item in items:
            await q.put(item)
        return queued_id

    async def _run_session_processor(self, app_session_id: str) -> None:
        """Consume the per-session prompt queue. Lives independently of
        any WebSocket — survives WS disconnects, viewer changes, and
        backend reloads (until next backend start, where pending in-flight
        runs are recovered via run_recovery)."""
        q = self._prompt_queues.get(app_session_id)
        if q is None:
            return
        while True:
            try:
                params = await q.get()
            except asyncio.CancelledError:
                break
            if params is None:
                # Sentinel: cancel_session fed this to unblock us.
                break
            # A10 TOCTOU closure: stamp the session as "claimed for
            # processing" the INSTANT we dequeue, BEFORE any other
            # await. `has_active_runs` reads this counter, so a PATCH
            # /selectors landing between this point and
            # `_drive_cli_run`'s `active_run_ids` registration sees
            # True and 409s. Decremented in the `finally` below
            # regardless of how the turn ends.
            self._in_flight_prompts[app_session_id] = (
                self._in_flight_prompts.get(app_session_id, 0) + 1
            )
            # Pop this item from the queued IDs tracker
            item_id = params.pop("_queued_id", None)
            if item_id:
                params["queue_item_id"] = item_id
                ids = self._queued_ids.get(app_session_id, [])
                if item_id in ids:
                    ids.remove(item_id)
                # If cancel_queued marked this item as cancelled (race:
                # cancel arrived after dequeue but before execution),
                # skip it — the frontend already sent a merged replacement.
                cancelled_set = self._cancelled_ids.get(app_session_id, set())
                if item_id in cancelled_set:
                    cancelled_set.discard(item_id)
                    # Release the client_id claim — this item is cancelled
                    # and will never complete, so the turn-end _forget never
                    # runs. Without this its (session, client_id) claim leaks
                    # and would block a future genuine re-send.
                    self._forget_active_prompt_item(item_id)
                    await asyncio.to_thread(
                        session_manager.remove_queued_prompt,
                        app_session_id,
                        item_id,
                    )
                    # A gap-window cancel aimed at this (already
                    # cancelled) item must not abort the next prompt.
                    self.turn_manager._pending_cancel.pop(app_session_id, None)
                    # Decrement the in-flight counter we just incremented
                    remaining = self._in_flight_prompts.get(app_session_id, 1) - 1
                    if remaining > 0:
                        self._in_flight_prompts[app_session_id] = remaining
                    else:
                        self._in_flight_prompts.pop(app_session_id, None)
                    continue
                # Notify all subscribers that this queue item was consumed.
                # Critical for frontends that are subscribed to this session
                # but NOT currently viewing it — they won't get turn_start
                # unless they clear the stale queuedBySession entry now.
                # MUST be after the cancel check: a cancelled item's frontend
                # state was already cleared optimistically, and a new prompt
                # may have been queued in the race window — emitting here
                # would wipe the new legitimate banner.
                try:
                    await self.dispatch_raw(app_session_id, {
                        "type": "queue_consumed",
                        "data": {
                            "app_session_id": app_session_id,
                            "queued_id": item_id,
                        },
                    })
                except Exception:
                    logger.debug("queue_consumed emit failed", exc_info=True)
                await asyncio.to_thread(
                    session_manager.remove_queued_prompt,
                    app_session_id,
                    item_id,
                )
            if params.get("source") == "team_message":
                import team_messaging

                batched = [params]
                rest = []
                stopped = False
                while not q.empty():
                    try:
                        next_params = q.get_nowait()
                    except Exception:
                        break
                    if next_params is None:
                        rest.append(next_params)
                        stopped = True
                        continue
                    if not stopped and next_params.get("source") == "team_message":
                        batched.append(next_params)
                        next_item_id = next_params.pop("_queued_id", None)
                        if next_item_id:
                            next_params["queue_item_id"] = next_item_id
                            ids = self._queued_ids.get(app_session_id, [])
                            if next_item_id in ids:
                                ids.remove(next_item_id)
                            try:
                                await self.dispatch_raw(app_session_id, {
                                    "type": "queue_consumed",
                                    "data": {
                                        "app_session_id": app_session_id,
                                        "queued_id": next_item_id,
                                    },
                                })
                            except Exception:
                                logger.debug(
                                    "queue_consumed emit failed",
                                    exc_info=True,
                                )
                            await asyncio.to_thread(
                                session_manager.remove_queued_prompt,
                                app_session_id,
                                next_item_id,
                            )
                            self._forget_active_prompt_item(next_item_id)
                        continue
                    stopped = True
                    rest.append(next_params)
                for item in rest:
                    q.put_nowait(item)
                if len(batched) > 1:
                    items = [
                        item.get("team_message") or {
                            "message": item.get("prompt") or "",
                            "metadata": {},
                        }
                        for item in batched
                    ]
                    params["prompt"] = "\n\n".join(
                        str(item.get("message") or "") for item in items
                    )
                    params["cli_prompt"] = team_messaging.format_team_message_batch(
                        items,
                        target_session_id=app_session_id,
                    )
                    params["team_message"] = {
                        "messages": items,
                    }
            # If marked as interrupt, prepend the interruption prefix to
            # BOTH the displayed prompt AND the model-facing cli_prompt
            # (when the caller split them — e.g. the Ask singleton). Only
            # prefixing `prompt` would leave the model unaware of the
            # interrupt whenever `cli_prompt` differs from `prompt`.
            is_interrupt = params.pop("_interrupt", False)
            if is_interrupt:
                _prefix = "<user-interrupt>[User interrupted the previous turn with a new message]</user-interrupt>\n\n"
                params["prompt"] = _prefix + params.get("prompt", "")
                if params.get("cli_prompt"):
                    params["cli_prompt"] = _prefix + params["cli_prompt"]
            # Clear the cancellation flag before starting the next prompt.
            # The previous prompt's verdict loop may have bailed due to
            # this flag — the new prompt deserves a clean slate.
            self._session_cancelled.pop(app_session_id, None)
            # Stash the lifecycle id so an inbound interrupt can read it
            # via get_in_flight_lifecycle_msg_id. Cleared in the finally
            # below regardless of how the turn ends.
            lifecycle_msg_id = params.pop("lifecycle_msg_id", None)
            if lifecycle_msg_id:
                self.user_prompt_manager.set_in_flight_lifecycle_msg_id(
                    app_session_id, lifecycle_msg_id,
                )
            # Replace the captured ws_callback (which closes over the
            # exact WebSocket that submitted the prompt — and may have
            # silently died if that WS reconnected mid-turn) with a
            # registry-based dispatcher that resolves the live set of WS
            # callbacks AT EMIT TIME. This is what makes direct emits
            # (user_message_persisted, turn_start, turn_complete, …)
            # reach the CURRENT live socket instead of being swallowed
            # by the dead-socket exception trap in main.py's ws_callback.
            async def dispatch_ws(event_dict, _sid=app_session_id):
                await self.dispatch_raw(_sid, event_dict)
            params["ws_callback"] = dispatch_ws
            is_review = params.pop("_review", False)
            try:
                import startup_recovery_gate
                await startup_recovery_gate.wait_for_recovery_ready()
                # Barrier: never start a turn while externally-registered
                # runs (recovered subprocesses) are still alive for this
                # session — two CLI subprocesses on one session interleave.
                await self.turn_manager.wait_for_clear_runs(app_session_id)
                if is_review:
                    from orchs.supervisor import request_review
                    await request_review(
                        self,
                        app_session_id=app_session_id,
                        ws_callback=dispatch_ws,
                    )
                elif await self._handle_special_session_prompt(
                    app_session_id,
                    params,
                    lifecycle_msg_id=lifecycle_msg_id,
                    dispatch_ws=dispatch_ws,
                ):
                    pass
                else:
                    if params.pop("_alter_rewind_latest", False):
                        session = session_manager.get(app_session_id)
                        messages = (session or {}).get("messages") or []
                        latest_user = next(
                            (
                                m for m in reversed(messages)
                                if m.get("role") == "user" and not m.get("source")
                            ),
                            None,
                        )
                        if latest_user is None:
                            raise RuntimeError("No latest user message to alter")
                        rewind_data = await self.rewind_files(
                            app_session_id,
                            latest_user["id"],
                            semantic_alter=True,
                        )
                        previous_prompt = rewind_data.get("semantic_alter_previous_prompt")
                        if previous_prompt is not None:
                            replacement_prompt = (
                                params.get("cli_prompt")
                                or params.get("prompt")
                                or ""
                            )
                            params["cli_prompt"] = build_semantic_alter_prompt(
                                str(previous_prompt),
                                str(replacement_prompt),
                            )
                    await self.handle_prompt(**params)
            except asyncio.CancelledError:
                # Backend shutdown propagating into us; bail.
                if lifecycle_msg_id:
                    self.user_prompt_manager.clear_in_flight_lifecycle_msg_id(
                        app_session_id,
                    )
                break
            except Exception as e:
                logger.exception("prompt processor: handle_prompt failed: %s", e)
                try:
                    await dispatch_ws({"type": "error", "data": {"error": str(e)}})
                except Exception:
                    pass
            finally:
                self._forget_active_prompt_item(item_id)
                if lifecycle_msg_id:
                    self.user_prompt_manager.clear_in_flight_lifecycle_msg_id(
                        app_session_id,
                    )
                    # Guaranteed per-prompt cleanup of the delivery marker
                    # so `_sent_lifecycle_ids` can never grow unbounded,
                    # even if no terminal emit fired for this prompt.
                    self.user_prompt_manager._clear_sent(lifecycle_msg_id)
                # A pending cancel set during this item's flight that
                # run_turn didn't consume is dead once the item ends —
                # clear it so it can't abort the next unrelated prompt.
                self.turn_manager._pending_cancel.pop(app_session_id, None)
                # Decrement the claimed-for-processing counter exactly
                # once per dequeued item, matched to the increment
                # immediately after `q.get()` above.
                remaining = self._in_flight_prompts.get(app_session_id, 0) - 1
                if remaining > 0:
                    self._in_flight_prompts[app_session_id] = remaining
                else:
                    self._in_flight_prompts.pop(app_session_id, None)
        # If the queue is empty, drop ourselves so a future submit can
        # spawn a fresh task. (Don't pop the queue itself — it may have
        # been swapped by a re-spawn race.)
        self.turn_manager._pending_cancel.pop(app_session_id, None)
        self._processor_tasks.pop(app_session_id, None)

    async def _handle_special_session_prompt(
        self,
        app_session_id: str,
        params: dict,
        *,
        lifecycle_msg_id: Optional[str],
        dispatch_ws: Callable[[dict], Awaitable[None]],
    ) -> bool:
        client_id = params.get("client_id")
        prompt = params.get("cli_prompt") or params.get("prompt") or ""
        return await virtual_session_prompt_handlers.handle(
            app_session_id,
            prompt=str(prompt),
            cwd=str(params.get("cwd") or ""),
            client_id=client_id if isinstance(client_id, str) else None,
            lifecycle_msg_id=lifecycle_msg_id,
            dispatch_ws=dispatch_ws,
        )


    def register_ws(
        self,
        app_session_id: str,
        ws_callback,
        *,
        from_seq: int = 0,
    ) -> None:
        """Register a WS callback for live events.

        `from_seq` is the highest event seq the client has already
        received (typically from the REST snapshot's `max_seq_by_sid`).
        On subscribe, BetterAgentJsonlTailer drains the gap from
        `from_seq+1` to current cursor before live events flow — so the
        client's stream is gap-free without uuid-dedup reconciliation.
        """
        cbs = self.ws_callbacks.setdefault(app_session_id, [])
        if ws_callback not in cbs:
            cbs.append(ws_callback)
        # Subscribe-with-watermark to the per-root tailer. Async because
        # gap-fill reads from disk; we schedule it without blocking the
        # caller (FastAPI WS handler). Skip when called outside a
        # running loop (sync test contexts) — the in-memory callback
        # registration above is enough for `broadcast_global` to reach
        # the subscriber; only the historic gap-fill is skipped.
        try:
            asyncio.get_running_loop()
            asyncio.create_task(
                self._subscribe_to_wire_tailer(app_session_id, ws_callback, from_seq),
                name=f"wire-subscribe-{app_session_id[:8]}",
            )
        except RuntimeError:
            pass

    def register_global_ws(self, ws_callback) -> None:
        if ws_callback not in self.global_ws_callbacks:
            self.global_ws_callbacks.append(ws_callback)

    def unregister_global_ws(self, ws_callback) -> None:
        try:
            self.global_ws_callbacks.remove(ws_callback)
        except ValueError:
            pass

    def unregister_ws(self, app_session_id: str, ws_callback=None) -> None:
        # Remove _Subscriber bindings for this callback first; we need to
        # know which subscriber objects to drop before mutating the
        # ws_callbacks list.
        root_ids = self._unsubscribe_from_wire_tailer(app_session_id, ws_callback)
        if ws_callback is None:
            self.ws_callbacks.pop(app_session_id, None)
        else:
            cbs = self.ws_callbacks.get(app_session_id)
            if cbs:
                try:
                    cbs.remove(ws_callback)
                except ValueError:
                    pass
                if not cbs:
                    self.ws_callbacks.pop(app_session_id, None)
        if app_session_id not in self.ws_callbacks:
            for root_id in root_ids:
                self._maybe_stop_wire_tailer(root_id, app_session_id)

    def unregister_all_ws(self, ws_callback) -> None:
        """Unregister `ws_callback` from EVERY session it is registered for.

        Called on WS disconnect. A single socket subscribes to many sessions
        (focused pane + every `additionalAppSessionIds` pane); cleaning up
        only the last-seen id leaks the rest in `ws_callbacks` /
        `_subscriber_index`, and a leaked entry blocks the same connection's
        re-subscribe after a reconnect. Resolve the full set authoritatively
        from both registries (keyed by this callback's per-connection token)
        so nothing is missed."""
        token = _cb_token(ws_callback)
        sids: set[str] = {
            sid for (sid, tok) in list(self._subscriber_index) if tok == token
        }
        sids.update(
            sid for sid, cbs in list(self.ws_callbacks.items())
            if ws_callback in cbs
        )
        for sid in sids:
            self.unregister_ws(sid, ws_callback)
        self.unregister_global_ws(ws_callback)

    # ------------------------------------------------------------------
    # BetterAgentJsonlTailer lifecycle
    # ------------------------------------------------------------------
    async def _subscribe_to_wire_tailer(
        self, app_session_id: str, ws_callback, from_seq: int,
    ) -> None:
        """Ensure the tailer for this session's root is running and add
        a `_Subscriber` for this callback. Called by `register_ws`.

        Idempotent: re-subscribing with the same `ws_callback` is a
        no-op — the existing subscriber's watermark is preserved (we
        DO NOT rewind to the new `from_seq`, since events past the
        existing watermark may have already been delivered)."""
        idx_key = (app_session_id, _cb_token(ws_callback))
        if idx_key in self._subscriber_index:
            return
        root_id = await asyncio.to_thread(
            session_manager._root_id_for,
            app_session_id,
        )
        if not root_id:
            return
        subs = self._wire_tailer_subs.setdefault(root_id, set())
        subs.add(app_session_id)

        tailer = self._wire_tailers.get(root_id)
        tailer_task = self._wire_tailer_tasks.get(root_id)

        # Detect dead tailer: the task completed (crashed or stopped)
        # but the object is still in _wire_tailers. Remove it so a
        # fresh tailer is created below. Preserve _wire_tailer_subs so
        # existing subscriber registrations survive the restart.
        if tailer is not None and tailer_task is not None and tailer_task.done():
            logger.warning(
                "wire tailer for root=%s is dead (task done), restarting",
                root_id[:8],
            )
            self._wire_tailers.pop(root_id, None)
            self._wire_tailer_tasks.pop(root_id, None)
            # Do NOT pop _wire_tailer_subs — other subscribers still
            # need their registrations. The subs set will be reused by
            # the new tailer via add_subscriber below.
            tailer = None

        if tailer is None:
            try:
                from jsonl_tailer import BetterAgentJsonlTailer
                from paths import ba_home
                events_path = ba_home() / "sessions" / root_id / "events.jsonl"
                tailer = BetterAgentJsonlTailer(
                    events_jsonl_path=events_path,
                    root_id=root_id,
                )
                task = asyncio.create_task(
                    tailer.run(),
                    name=f"wire-tailer-{root_id[:8]}",
                )
                self._wire_tailers[root_id] = tailer
                self._wire_tailer_tasks[root_id] = task
                logger.info(
                    "started BetterAgentJsonlTailer root=%s", root_id,
                )
            except Exception:
                logger.exception(
                    "failed to start wire tailer for %s", app_session_id,
                )
                return

        # Index the subscriber FIRST so a fast unsubscribe arriving
        # during `add_subscriber`'s gap-fill await can find and
        # deactivate it. (`_Subscriber._active=False` makes
        # `add_subscriber` short-circuit; no orphan in `_subscribers`.)
        try:
            from jsonl_tailer import _Subscriber
            sub = _Subscriber(
                app_session_id=app_session_id,
                ws_callback=ws_callback,
                from_seq=from_seq,
                root_id=root_id,
            )
            self._subscriber_index.setdefault(
                (app_session_id, _cb_token(ws_callback)), sub,
            )
            await tailer.add_subscriber(sub)
        except Exception:
            logger.exception(
                "failed to add wire-tailer subscriber for %s", app_session_id,
            )

        # Publish tail DEMAND for this session. native_files_manager folds
        # it and reconciles the OwnedClaudeJsonlTailers (primary + worker
        # forks reachable from this session). first-in opens, last-out
        # closes — entirely inside the manager.
        self._publish_native_demand(
            app_session_id,
            ws_callback,
            present=True,
            root_id=root_id,
        )

    def _publish_native_demand(
        self,
        app_session_id: str,
        ws_callback,
        *,
        present: bool,
        root_id: str | None = None,
    ) -> None:
        """Emit `native_files.demand` so native_files_manager learns a WS
        subscriber attached/detached for this session. `ws_callback=None`
        with present=False means "drop all demand for this session"."""
        if not root_id:
            return
        token = _cb_token(ws_callback) if ws_callback is not None else None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(
            bus.publish(
                BusEvent(
                    type="native_files.demand",
                    root_id=root_id,
                    sid=app_session_id,
                    payload={
                        "owning_session": app_session_id,
                        "token": token,
                        "present": present,
                    },
                    persist=False,
                )
            ),
            name=f"native-demand-{app_session_id[:8]}",
        )

    def _unsubscribe_from_wire_tailer(
        self, app_session_id: str, ws_callback=None,
    ) -> set[str]:
        """Remove all _Subscriber objects bound to (sid, ws_callback). If
        ws_callback is None, remove every subscriber for this sid."""
        # We don't keep a reverse map per ws_callback; iterate and match.
        if ws_callback is None:
            keys = [k for k in self._subscriber_index
                    if k[0] == app_session_id]
        else:
            keys = [(app_session_id, _cb_token(ws_callback))]
        root_ids: set[str] = set()
        for key in keys:
            sub = self._subscriber_index.pop(key, None)
            if sub is not None:
                root_ids.add(sub.root_id)
                tailer = self._wire_tailers.get(sub.root_id)
                if tailer is not None:
                    tailer.remove_subscriber(sub)
        for root_id in root_ids:
            self._publish_native_demand(
                app_session_id,
                ws_callback,
                present=False,
                root_id=root_id,
            )
        return root_ids

    def _maybe_stop_wire_tailer(self, root_id: str, app_session_id: str) -> None:
        """If no remaining WS subscribers exist for any session in the
        root, stop its tailer."""
        subs = self._wire_tailer_subs.get(root_id)
        if subs is None:
            return
        subs.discard(app_session_id)
        for sid in list(subs):
            if self.ws_callbacks.get(sid):
                return
            subs.discard(sid)
        if subs:
            return
        self._wire_tailer_subs.pop(root_id, None)
        tailer = self._wire_tailers.pop(root_id, None)
        task = self._wire_tailer_tasks.pop(root_id, None)
        if tailer is not None:
            tailer.stop()
        if task is not None:
            # Runs on the main loop normally, but also from a worker thread
            # via unregister_all_ws — WS disconnect drives it through
            # asyncio.to_thread (main.py), where there is no running loop.
            # asyncio.create_task needs one; on the no-loop path close the
            # reap coroutine instead of leaking it ("never awaited").
            # tailer.stop() above already signaled graceful stop.
            coro = self._await_tailer_stop(root_id, task)
            try:
                asyncio.get_running_loop().create_task(coro)
            except RuntimeError:
                coro.close()

    async def _await_tailer_stop(self, root_id: str, task: asyncio.Task) -> None:
        try:
            await asyncio.wait_for(task, timeout=3.0)
        except asyncio.TimeoutError:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        except (asyncio.CancelledError, Exception):
            pass
        logger.info("stopped BetterAgentJsonlTailer root=%s", root_id)

    def _messages_delta_payload(
        self, msg: dict, *, omit_render_events: bool,
    ) -> dict:
        if not omit_render_events:
            return msg
        from messages_delta_compaction import compact_message_delta_payload
        return compact_message_delta_payload(msg)

    async def _dispatch_messages_delta(
        self,
        app_session_id: str,
        persist_to: str,
        msg: dict,
        *,
        omit_render_events: bool = False,
    ) -> None:
        """Push a single-message `messages_delta` frame through the
        persist+dispatch path. Used by error-tail finalize and the
        run-state error path so the frontend can render the new
        assistant state without waiting for a REST refetch."""
        await self.persist_and_dispatch_raw(app_session_id, {
            "type": "messages_delta",
            "data": {
                "app_session_id": persist_to,
                "messages": [
                    self._messages_delta_payload(
                        msg,
                        omit_render_events=omit_render_events,
                    )
                ],
            },
        })

    async def persist_and_dispatch_raw(
        self,
        app_session_id: str,
        event_dict: dict,
    ) -> None:
        """Like `dispatch_raw` but ALSO persists the event into
        `events.jsonl` via the event bus. Used by fallthrough paths
        that emit outside an active turn (detached-runner reconnect
        after backend restart, approval flow events fired without a
        parent `save_ws_callback`). Without this, events arriving in
        those windows are visible only to currently-connected WS
        clients and lost to restart recovery.

        BetterAgentJsonlTailer picks the persisted line up and fans
        it out to subscribers automatically, so the explicit
        `dispatch_raw` call below is for clients connected at the
        moment of emit; the tailer covers late subscribers and
        cold-load."""
        try:
            root_id = session_manager._root_id_for(app_session_id)
            if root_id:
                etype = event_dict.get("type")
                data = event_dict.get("data") or {}
                if isinstance(etype, str) and isinstance(data, dict):
                    await bus.publish(BusEvent(
                        type=etype,
                        root_id=root_id,
                        sid=app_session_id,
                        payload=data,
                    ))
        except Exception:
            logger.exception(
                "persist_and_dispatch_raw: bus.publish failed for %s",
                event_dict.get("type"),
            )
        await self.dispatch_raw(app_session_id, event_dict)

    async def dispatch_raw(self, app_session_id: str, event_dict: dict) -> None:
        """Fan a single event out to every raw WS callback registered for
        `app_session_id` AT THE MOMENT OF EMIT. Resilient to WS reconnects:
        a closure-bound callback that died (ws closed mid-turn) is no
        longer in the registry by the time the new connection's subscribe
        replaces it, so live events route to the live socket. Per-callback
        failures are swallowed so one dead socket can't poison the rest.

        `app_session_id` is the WS-subscriber key (whose callbacks we fan
        to). `data.app_session_id` is the LOGICAL target session — the one
        the frontend uses to upsert the event onto the right pane in
        split views. The two values DIFFER in supervisor mode: events
        for a worker turn dispatch through the supervisor's WS callback
        (subscriber key) but carry `data.app_session_id = worker_fork_id`
        for routing.

        Previously this method overwrote `data.app_session_id` to match
        the dispatch key, which broke supervisor mode's split view: worker
        msgs ended up appended to the supervisor's `messages` array on the
        frontend until a REST refetch corrected it. Now: only set the
        field as a DEFAULT when missing; never overwrite a caller-set
        value. We shallow-copy when stamping so concurrent fanouts can't
        collide on the same dict.
        """
        cbs = list(self.ws_callbacks.get(app_session_id, []))
        if not cbs:
            return
        data = event_dict.get("data")
        if isinstance(data, dict) and "app_session_id" not in data:
            event_dict = {
                **event_dict,
                "data": {**data, "app_session_id": app_session_id},
            }
        for cb in cbs:
            try:
                await cb(event_dict)
            except Exception:
                logger.exception("raw ws dispatch failed")

    # ------------------------------------------------------------------
    # Run state — backend is source of truth for "what's running"
    # ------------------------------------------------------------------
    # Run kinds that drive `isStreaming` on the target assistant msg.
    # Workers and adv-sync forks register against the PARENT msg but
    # they don't represent "the assistant typing right now" — workers
    # have their own worker-source msgs, adv-sync forks own a separate
    # forked session. Listing here keeps the single source of truth
    # for streaming-ness narrow and audited.
    _STREAMING_KINDS = frozenset({"manager", "native"})

















    # ------------------------------------------------------------------
    # Broadcast — two named channels, hard-separated by intent.
    #
    # INVARIANT: every WS frame that crosses this layer goes through
    # exactly one of these two methods. Direct iteration over
    # `ws_callbacks` is forbidden outside this class.
    #
    #   `broadcast_session(sid, type, data, source)`
    #       Per-session state event. Funnels through
    #       `event_ingester.ingest` → events.jsonl → the
    #       `BetterAgentJsonlTailer` (which fans out to WS subscribers
    #       of `sid`). Durable: an offline reader of events.jsonl can
    #       replay these to rebuild session state. Use for any frame
    #       that carries state that isn't also fully captured in a
    #       persistent *_store.py.
    #
    #   `broadcast_global(type, data)`
    #       Cross-session UI invalidation ping. Direct fan-out via
    #       `ws_callbacks`; NOT persisted. Use only for cache-bust
    #       signals whose authoritative state already lives on disk in
    #       a store (the frontend re-reads via REST snapshot on receipt).
    #       An allowlist enforces the intent — any other event_type
    #       raises `ValueError`. Adding a new global type is a conscious
    #       allowlist edit.
    # ------------------------------------------------------------------
    GLOBAL_EVENT_ALLOWLIST: set[str] = {
        "provider_changed",
        # Streaming provider-CLI install (Settings → Provider CLI tools).
        # Authoritative state is provider_setup._INSTALL_RUNS; REST
        # snapshot at GET /api/provider-setup/installs. Per-line stdout/
        # stderr deltas (progress) + terminal state (finished).
        "provider_install_progress",
        "provider_install_finished",
        "projects_changed",
        "project_mappings_changed",
        "workers_changed",
        # Per-project task-definition list invalidation. Authoritative
        # state lives in `task_store`; payload carries {cwd, node_id} and
        # clients refetch the project's tasks on receipt (mirrors
        # `workers_changed`). Tasks are the on-demand, run-when-clicked
        # definitions surfaced in the sidebar Tasks tab.
        "tasks_changed",
        "extensions_changed",
        "session_organization_changed",
        # Global user-preferences mutation ping (folder-view toggle,
        # session sort, tabs visibility, fonts, language…). Authoritative
        # state lives in the user_prefs.json store; payload carries the
        # full `get_all()` snapshot so other tabs converge without a
        # refetch. PATCH /api/user-prefs raised an uncaught ValueError
        # here (500 on every pref write) until this was allowlisted.
        "user_prefs_changed",
        # Per-machine UI navigation-restore state (selected project +
        # last session per project×node). Authoritative state lives in
        # ui_selection.json; payload carries the full get_all() snapshot
        # so other tabs converge without a refetch. Tabs use it for
        # cold-load restore, not to force-navigate an active view.
        "ui_selection_changed",
        "session_metadata_updated",
        "todos_snapshot",
        "session_created",
        "session_forked",
        # Sidebar lifecycle pings — authoritative state lives in
        # session_store; frontend dedup-by-id makes them idempotent.
        # Were silently failing (ValueError → unretrieved task) so
        # multi-tab convergence on delete/rename didn't work.
        "session_deleted",
        "session_renamed",
        # Async-reconcile progress (>0.3s threshold). Authoritative
        # state is `session_manager._in_flight_reconcile`; clients
        # render a per-root "reconciling…" badge that flips on
        # `started` and clears on `finished`.
        "session_processing_started",
        "session_processing_finished",
        # Post-reconcile invalidation ping. Frontend silently refetches
        # the session if the user is viewing it, replacing stale cache
        # served by the initial GET. Authoritative state is the
        # reconciled render tree in session_manager._roots.
        "session_reconciled",
        # Tier-1 lazy-fetch: a non-latest (collapsed) historical msg
        # gained events during reconcile, so its `stub` went stale.
        # Carries `{app_session_id, msg_id, stub}`; clients with that
        # turn collapsed swap in the fresh stub, expanded turns re-fetch.
        # Not persisted — authoritative events live in the render tree.
        "stub_invalidated",
        # Per-message recovering pill toggled by run_recovery while it
        # reconciles an in-flight run after a backend restart.
        # Authoritative state is `session_manager._recovering_msg_ids`
        # (stamped onto REST snapshots via `_stamp_recovering_tree`);
        # the WS ping carries `{session_id, msg_id, value}` for live
        # convergence. Must NOT be persisted to events.jsonl — the
        # flag is transient and rebuilt at startup.
        "message_recovering_changed",
        # Per-message "Retrying in Ns…" pill stamped by the orchestrator
        # while it sleeps between a 429 rate-limit response and the next
        # retry. `retrying_until` lives on the assistant message (part of
        # the persisted session); the WS ping carries
        # `{session_id, msg_id, retry_at}` for live convergence.
        # Transient — must NOT be persisted to events.jsonl.
        "message_retrying_changed",
        # Per-message auto-retry toggle (orchestrator arms/disarms the
        # background auto-retry on a transiently-failed message).
        # Authoritative state lives on the assistant message; payload
        # `{session_id, msg_id, value}` for live cross-tab convergence.
        # Was missing → broadcast_global raised ValueError on every toggle.
        "message_auto_retry_changed",
        "message_content_updated",
        "message_continuation_changed",
        # Backend startup-task lifecycle. Authoritative state lives in
        # `startup_task_registry` (in-memory); REST snapshot via
        # `GET /api/startup_tasks` for first paint, WS push for live
        # deltas. Payload is `{task: {id,label,state,...}}` for an
        # upsert, or `{cleared: true}` on registry reset (uvicorn
        # --reload). Frontend banner is non-blocking — UI stays usable
        # while migrations/recovery run.
        "startup_task_changed",
        # Message ownership resolution: orphan events bracketed onto a
        # finalized assistant msg during reconcile. Carries
        # `{session_id, messages}` delta. Authoritative state is the
        # render tree; WS push lets other tabs see the delta without
        # a full refetch.
        "messages_delta",
        # Ask-result / ask-choice live convergence. Originating tab
        # resolves an ask prompt; other tabs need to see the result
        # or choice update. Authoritative state lives on the message.
        "message_ask_result_changed",
        "message_ask_choice_changed",
        "user_input_requested",
        "user_input_resolved",
        # Per-session unrecoverable-error state. Authoritative state lives on
        # the assistant message/session manager projection; SessionStatusBadge
        # renders the error dot from this live convergence payload.
        "session_error_changed",
        # Per-session pending request_user_input count. Authoritative state
        # is user_input_store; the sidebar/session registry uses this to
        # render the "input needed" dot without leaking the full request body
        # onto unrelated session rows.
        "session_user_input_changed",
        # Per-session running-flag transition. Authoritative state is
        # computed live by `coordinator.is_running(sid)` (walks
        # `_run_state[sid]` + checks pid liveness). Frontend
        # sessionRegistry mirrors; SessionStatusBadge/ProjectStatusBadge
        # subscribe via eventBus. Payload: `{session_id, value: bool}`.
        "session_running_changed",
        # Per-session monitoring-state transition (active / idle /
        # blocked_on_user / waiting_on_background / stopped). Authoritative
        # state is computed live by `coordinator.monitoring_state(sid)`;
        # payload `{session_id, monitoring_state}`. Mirrors
        # session_running_changed but fires on finer changes that don't flip
        # the running boolean (e.g. turn ends but bg work keeps running).
        "session_monitoring_changed",
        # Per-session provenance append. Authoritative log is
        # `provenance.jsonl` (read via GET /api/sessions/{id}/details); this
        # ping tells an open Details panel to refetch. Payload `{session_id}`.
        "session_provenance_changed",
        # Per-session unread-cursor transition. Authoritative state is
        # `session_manager._unread_counts` (transient — hydrated lazily
        # from the persisted `last_seen_event_uid` on each Session
        # record). Fires on every event-append in apply_event AND on
        # ack via POST /api/sessions/{id}/seen. Payload:
        # `{session_id, unread_count, last_seen_event_uid?}`.
        "session_unread_changed",
        # Per-session extension attention marker (set/cleared by an
        # extension via session_manager.set_marker / clear_marker).
        # Authoritative state lives in session_store's marker projection;
        # payload `{session_id, extension_id, marker|None, cwd, node_id}`
        # so the tabs bar / sidebar render the marker dot across tabs
        # without a refetch. Was missing → broadcast_global raised
        # ValueError on every marker_set/marker_cleared.
        "session_marker_changed",
        # Multi-machine: worker-node connect/disconnect transitions.
        # Authoritative state lives in `node_store` (in-memory registry);
        # REST snapshot via GET /api/nodes for first paint, this WS push
        # for live deltas so the frontend Machines page + per-session
        # picker render online/offline badges without polling. Payload
        # is `{node_id, state}` where state ∈ {connected, disconnected}.
        "node_state_changed",
        # Multi-machine: a brand-new worker-node is awaiting operator
        # approval. Authoritative state lives in the
        # `pending_node_registrations` store; REST snapshot via
        # GET /api/pending_nodes for first paint, this WS push so the
        # approval popup appears live in any open browser. Payload is the
        # public record (node_id, address, cwd_roots, fingerprint, ...);
        # the node's secret never crosses the wire.
        "node_registration_requested",
        # Companion resolution ping for the above — fires on approve/deny.
        # Payload is `{node_id, status}` where status ∈ {approved, denied}.
        # Lets every open browser dismiss the popup without polling.
        "node_registration_resolved",
        # Per-provider model catalog delta. Authoritative state lives in
        # `~/.better-claude/models_cache.<provider_id>.json` (written by
        # `models_mod.refresh_one`). Frontend `useModelsCatalogChanged`
        # refetches `/api/models` on receipt. Payload:
        # `{provider_id, newly_added, became_active, went_retired,
        # truly_removed}`. Fires on startup catalog refresh and on
        # POST /api/providers/{id}/models/refresh.
        "models_catalog_changed",
        # Per-project updates badge delta. Authoritative state lives in
        # `project_update_store` (in-memory + ~/.better-claude/project_
        # updates/). Frontend refetches through the Project Structure extension.
        # Payload: `{project_id, unseen_count}`. Fires on capture /
        # mark-seen.
        "project_updates_changed",
        # Provider-native config or memory file changed. The frontend
        # refetches Provider Config Sync through its extension backend for the active scope.
        # Payload: `{scope, category, path, cwd}`. Not persisted.
        "provider_config_sync_changed",
        # Credential-broker consent list changed (created/approved/denied/
        # revoked). Authoritative state lives in the consent_store; frontend
        # refetches `GET /api/credentials/pending`. Payload:
        # `{app_session_id}`.
        "credential_consent_changed",
        # Internal-LLM task assignments changed (which provider/model/effort
        # runs each backend-internal LLM task). Authoritative state lives in
        # config_store config.json; frontend refetches
        # GET /api/settings/internal-llm. Payload: `{}`.
        "internal_llm_changed",
    }

    @perf.timed_fn("ws.broadcast_session")
    async def broadcast_session(
        self,
        app_session_id: str,
        event_type: str,
        data: dict,
        *,
        source: str,
    ) -> None:
        """Persist a per-session event into events.jsonl. The tailer
        broadcasts it to that session's WS subscribers from disk —
        callers never touch `ws_callbacks` directly for per-session
        frames."""
        try:
            await asyncio.to_thread(
                self._broadcast_session_sync,
                app_session_id,
                event_type,
                data,
                source,
            )
        except Exception as exc:
            from event_journal import EventJournalWriteError
            if (
                isinstance(exc, EventJournalWriteError)
                and "writer is closed" in str(exc)
            ):
                logger.debug(
                    "broadcast_session skipped after journal close sid=%s type=%s",
                    app_session_id, event_type,
                )
                return
            logger.exception(
                "broadcast_session ingest failed sid=%s type=%s",
                app_session_id, event_type,
            )

    def _broadcast_session_sync(
        self,
        app_session_id: str,
        event_type: str,
        data: dict,
        source: str,
    ) -> None:
        from event_journal import publish_event_sync

        root_id = session_manager._root_id_for(app_session_id) or app_session_id
        publish_event_sync(
            session_id=root_id,
            context_id=app_session_id,
            event_type=event_type,
            data=data,
            source=source,
        )

    async def broadcast_global(self, event_type: str, data: dict) -> None:
        """Cross-session UI invalidation ping. Fire-and-forget; failures
        per-callback are swallowed. NOT persisted to events.jsonl —
        authoritative state must already live in a store. Allowlist is
        enforced: adding a new global type requires editing
        `GLOBAL_EVENT_ALLOWLIST` consciously."""
        if event_type not in self.GLOBAL_EVENT_ALLOWLIST:
            raise ValueError(
                f"broadcast_global called with non-allowlisted type "
                f"{event_type!r}; per-session events must use "
                f"broadcast_session, or add the type to "
                f"GLOBAL_EVENT_ALLOWLIST if it really is a cross-session "
                f"invalidation ping with authoritative state in a store"
            )
        snapshot = list(dict.fromkeys(self.global_ws_callbacks))
        event = SerializedGlobalEvent({"type": event_type, "data": data})
        if snapshot:
            event._bc_serialized_json_task = asyncio.create_task(  # type: ignore[attr-defined]
                dumps_ws_json(event)
            )
        outer_t = _time.perf_counter()
        for cb in snapshot:
            asyncio.create_task(self._broadcast_global_one(cb, event, event_type))
        perf.record(
            "ws.broadcast_global.enqueue",
            (_time.perf_counter() - outer_t) * 1000.0,
        )

    async def _broadcast_global_one(
        self,
        cb: Callable[[dict], Awaitable[None]],
        event: dict,
        event_type: str,
    ) -> None:
        cb_t = _time.perf_counter()
        try:
            await cb(event)
        except Exception:
            logger.exception(
                "broadcast_global failed type=%s",
                event_type,
            )
        finally:
            perf.record(
                "ws.broadcast_global.cb",
                (_time.perf_counter() - cb_t) * 1000.0,
            )

    async def broadcast_credential_consent_changed(
        self, app_session_id: Optional[str]
    ) -> None:
        """Credential consent list invalidation. Authoritative state is in
        the credential_broker consent_store; the frontend refetches
        `GET /api/credentials/pending` on receipt."""
        await self.broadcast_global(
            "credential_consent_changed", {"app_session_id": app_session_id}
        )

    async def broadcast_workers_changed(self, cwd: Optional[str]) -> None:
        """Cross-session workers list invalidation. `cwd` is None for
        cross-cwd actions (session delete, rewind without a known cwd)
        and a path for cwd-scoped mutations. Authoritative state is in
        `worker_store`; frontend refetches on receipt."""
        await self.broadcast_global("workers_changed", {"cwd": cwd})

    # ------------------------------------------------------------------
    # Rewind with files
    # ------------------------------------------------------------------
    async def _rewind_workers_for_turn(
        self,
        app_session_id: str,
        assistant_msg: dict,
        target_user_msg: dict,
    ) -> dict:
        from orchs.manager._rewind import rewind_workers_for_turn as _impl
        return await _impl(self, app_session_id, assistant_msg, target_user_msg)

    async def rewind_files(
        self,
        app_session_id: str,
        message_id: str,
        *,
        semantic_alter: bool = False,
        provider_rewind: bool = True,
    ) -> dict:
        """Invoke `claude --resume <sid> --rewind-files <uuid>` and truncate
        session messages at/after the rewound user message. Also rewinds
        every worker that participated in the discarded turn (see
        `_rewind_workers_for_turn`).

        `provider_rewind=False` truncates the render tree (and worker forks)
        and broadcasts the rewind WITHOUT calling the provider CLI rewind —
        used to discard a failed turn whose prompt never committed a provider
        rewind anchor (no `agent_message_uuid`), so a retry replaces it
        instead of duplicating the prompt.
        """
        session = session_manager.get(app_session_id)
        if not session:
            raise ValueError(t("orchestrator.session_not_found"))

        messages = session.get("messages") or []
        target_idx: Optional[int] = None
        for i, m in enumerate(messages):
            if m.get("id") == message_id:
                target_idx = i
                break
        if target_idx is None:
            raise ValueError(t("orchestrator.message_not_found"))

        target = messages[target_idx]
        provider = self.provider_for_session(app_session_id)
        use_semantic_alter = semantic_alter and provider.supports_semantic_alter
        do_provider_rewind = provider_rewind and not use_semantic_alter
        if do_provider_rewind and not provider.supports_rewind:
            raise ValueError(t("orchestrator.rewind_not_supported"))
        message_uuid = target.get("agent_message_uuid")
        rewind_session_id = app_session_id
        if do_provider_rewind and provider.rewind_requires_agent_identity:
            if not message_uuid:
                raise ValueError(t("orchestrator.message_no_claude_uuid"))

            sid_field = "agent_session_id"
            agent_sid = session.get(sid_field)
            if not agent_sid:
                raise ValueError(t("orchestrator.session_no_sid_field", sid_field=sid_field))
            rewind_session_id = agent_sid

        if do_provider_rewind:
            await provider.rewind(
                rewind_session_id,
                message_uuid or message_id,
            )

        # Rewind every worker the discarded turn touched. The assistant
        # message that lives directly after `target_idx` carries the
        # turn's `workers[]` panels; in manager mode each panel's first
        # user-message uuid is the rewind anchor for that worker's
        # claude jsonl. Native mode has no panels — this is a no-op.
        worker_summary = {"rewound": 0, "deleted": 0, "skipped": 0}
        if target_idx + 1 < len(messages):
            asst_after = messages[target_idx + 1]
            if asst_after.get("role") == "assistant":
                try:
                    worker_summary = await self._rewind_workers_for_turn(
                        app_session_id, asst_after, target,
                    )
                except Exception:
                    logger.exception("worker fan-out rewind failed")

        new_messages = messages[:target_idx]
        session_manager.truncate_messages(app_session_id, target_idx)

        await self.broadcast_session(
            app_session_id,
            "rewind_complete",
            {"session_id": app_session_id, "messages": new_messages},
            source="orchestrator.rewind",
        )

        result = {"messages": new_messages, "workers": worker_summary}
        if use_semantic_alter:
            result["semantic_alter_previous_prompt"] = target.get("content") or ""
        return result

    # ------------------------------------------------------------------
    # Cancellation

    def is_session_cancelled(self, app_session_id: str) -> bool:
        """True after ``cancel_turn`` fires for this session, until the
        queue processor clears the flag before the next prompt. Used by
        the supervisor verdict loop to bail on cancelled sessions."""
        return self._session_cancelled.get(app_session_id, False)

    async def cancel_session(self, app_session_id: str) -> int:
        """Cancel ALL runs for a session (current turn + any orphaned).
        Returns count of cancelled runs.

        `provider.cancel_run` blocks for up to several seconds (SIGTERM +
        wait, then SIGKILL fallback). We move the kill loop off the
        event loop into a worker thread so an unresponsive runner
        doesn't pin every other request — both flows run in parallel
        rather than serialized.

        Aggregates `runs_for_session` from EVERY loaded provider, not
        just the session's bound one — multi-provider history can leave
        runs on a different provider's _runs dict."""
        self._session_cancelled[app_session_id] = True
        # Signal the cancel event for the current turn
        event = self.turn_manager.cancel_events.get(app_session_id)
        if event:
            event.set()

        seen = set(self.turn_manager.active_run_ids.get(app_session_id, []))
        all_run_ids: set[str] = set()
        for prov in known_providers():
            try:
                all_run_ids.update(prov.runs_for_session(app_session_id))
            except Exception:
                logger.exception(
                    "cancel_session: provider %s raised in runs_for_session",
                    prov.id,
                )
        run_ids_to_cancel = list(seen.union(all_run_ids))

        def _delete_one(rid: str) -> bool:
            # Crash-window safety net: if a prior turn-stop left the runner
            # dead with surviving bg shells (Linux: in cgroup; macOS: best-
            # effort if leader pid still live), reap them BEFORE the
            # provider fanout. Idempotent — a no-op if already gone, or if
            # the provider's own killpg+sweep below already covered it.
            try:
                from containment import containment
                containment().force_kill_all(rid)
            except Exception:
                logger.exception(
                    "cancel_session: force_kill_all failed run=%s", rid,
                )
            return self._cancel_run_fanout(rid)

        if run_ids_to_cancel:
            await asyncio.gather(
                *(asyncio.to_thread(_delete_one, rid)
                  for rid in run_ids_to_cancel),
                return_exceptions=True,
            )
        cancelled = len(set(all_run_ids) - seen)

        # Cleanup orchestrator state
        self.turn_manager.active_run_ids.pop(app_session_id, None)
        self.turn_manager.current_turn_workers.pop(app_session_id, None)
        self.turn_manager.cancel_events.pop(app_session_id, None)
        self.active_delegations.pop(app_session_id, None)
        self.turn_manager._turn_save_callbacks.pop(app_session_id, None)
        self.turn_manager.current_assistant_msgs.pop(app_session_id, None)
        self.turn_manager._run_state.pop(app_session_id, None)
        # A10: clear the in-flight-prompt counter too. The matched
        # finally-block decrement in `_run_session_processor` already
        # handles the normal lifecycle, but a hard cancel mid-handle
        # could leave a stale entry. Sids are UUIDs so a stale value
        # doesn't block reuse, but cleaning up matches the symmetry
        # of the other dict pops above.
        self._in_flight_prompts.pop(app_session_id, None)
        # NOTE: _session_cancelled is NOT popped here. The sentinel
        # fed above kills the queue processor, but the verdict loop
        # may still be awaiting inside run_turn. If we pop the flag
        # before the verdict loop reads it, is_session_cancelled()
        # returns False and the loop parses garbage from the partial
        # turn. The flag is harmless — the processor is dead so no
        # new handle_prompt will clear it. If the session is somehow
        # reused, the stale flag causes an early verdict-loop bail,
        # which is the correct conservative behavior.
        # Clean up per-pair locks, approval waiters, and verdict counts
        # that belong to this session (as caller).
        for key in [k for k in self.pair_locks if k[0] == app_session_id]:
            self.pair_locks.pop(key, None)
        # Cancel pending approval futures for THIS session so
        # runners waiting on them exit promptly instead of hanging.
        # Only cancel waiters whose delegation belongs to this session
        # (check disk record) — don't kill other sessions' approvals.
        from stores import pending_approvals
        for did in list(self.approval_waiters):
            fut = self.approval_waiters.get(did)
            if fut is None or fut.done():
                continue
            rec = pending_approvals.get(did)
            if rec and rec.get("app_session_id") == app_session_id:
                fut.cancel()
                self.approval_waiters.pop(did, None)
        # Cancel in-flight worker init turns for THIS session only.
        # Values are (owner_session_id, Event) tuples.
        for bc_id in list(self.init_cancel_events):
            entry = self.init_cancel_events.get(bc_id)
            if entry is None:
                continue
            owner_sid, evt = entry
            if owner_sid == app_session_id:
                if not evt.is_set():
                    evt.set()
                self.init_cancel_events.pop(bc_id, None)
        # Drop our dict refs to the processor task + queue so a future
        # submit_prompt() spawns fresh state. We deliberately do NOT
        # `proc.cancel()` here — the kill loop above already triggered
        # `run_turn`'s `_Cancelled` finalization (cancel_event was
        # set), and cancelling the task mid-finalize preempts the
        # trace.save() + session_store flush + ws_callback emit.
        # Instead, feed a sentinel None to unblock `await q.get()` so
        # the processor exits its loop naturally after finalization.
        proc = self._processor_tasks.pop(app_session_id, None)
        q = self._prompt_queues.pop(app_session_id, None)
        if q is not None and proc is not None and not proc.done():
            q.put_nowait(None)
        # Purge cancellation tracking for this session
        self._cancelled_ids.pop(app_session_id, None)

        total = len(seen) + cancelled
        if total:
            logger.info("cancel_session: killed %d runs for session %s", total, app_session_id)
        return total

    # ------------------------------------------------------------------
    # Main turn entry point
    # ------------------------------------------------------------------
    async def handle_prompt(
        self,
        prompt: str,
        app_session_id: str,
        model: str,
        cwd: str,
        ws_callback: Callable[[dict], Awaitable[None]],
        provider_id: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        images: Optional[list] = None,
        files: Optional[list] = None,
        orchestration_mode: Optional[str] = None,
        client_id: Optional[str] = None,
        send_target: Optional[str] = None,
        cli_prompt: Optional[str] = None,
        source: Optional[str] = None,
        user_initiated: bool = True,
        disallowed_tools: Optional[list[str]] = None,
        known_worker_registry_cwds: Optional[dict[str, str]] = None,
        queue_item_id: Optional[str] = None,
        team_message: Optional[dict] = None,
        capability_contexts: Optional[list[dict]] = None,
        file_discussion_id: Optional[str] = None,
        allow_model_override: bool = False,
    ) -> None:
        # `source`/`user_initiated` flow through to run_turn; the
        # scheduler submits source="schedule", user_initiated=False so
        # scheduled turns skip the user-facing nudges (open_file_panel,
        # open-todo reminder) while everything else stays a normal turn.
        # `cli_prompt` (when set) is the text actually sent to the model,
        # while `prompt` is persisted/displayed as the user message. Used
        # by the Ask singleton, team-messaging, file-discussion, semantic-alter,
        # and the CLI override to keep their wrapper out of the visible history.
        # MUST stay None when no caller set it: run_primary relies on
        # `cli_prompt is None` to know it should apply wrap_cli_prompt (the
        # team/manager BOOTSTRAP). Defaulting it to `prompt` here defeated
        # that check and silently stripped the bootstrap from every normal
        # team turn, so the coordinator never received delegation instructions.
        # The supervisor-direct branch (the one run_turn caller that bypasses
        # run_primary) defaults None→prompt itself.
        session = session_manager.get(app_session_id)
        if not session:
            await ws_callback({"type": "error", "data": {"error": t("error.ws_session_not_found")}})
            return

        # The session record is the source of truth for orchestration_mode
        # (CLAUDE.md state-ownership rule). The per-turn payload is treated
        # as a hint only: if it disagrees with the session's stored mode we
        # log it and keep the session's value. Silently rewriting the
        # session from a per-turn payload caused session 83522e5d to flip
        # native→manager mid-conversation when a stale frontend localStorage
        # value rode in on a send_message.
        stored_mode = session.get("orchestration_mode") or "team"
        if stored_mode == "manager":
            stored_mode = "team"
        if stored_mode not in ("team", "native"):
            logger.warning(
                "Invalid stored orchestration_mode %r for session %s — falling back to 'team'",
                stored_mode, app_session_id,
            )
            stored_mode = "team"
        if orchestration_mode and orchestration_mode != stored_mode:
            logger.warning(
                "Discarding per-turn orchestration_mode=%r for session %s; "
                "session record says %r and is authoritative.",
                orchestration_mode, app_session_id, stored_mode,
            )
        mode = stored_mode

        # The session record is likewise the source of truth for cwd
        # (CLAUDE.md state-ownership rule). A stale frontend value
        # riding in on send_message previously pointed `claude
        # --resume` at the wrong ~/.claude*/projects/<encoded-cwd>
        # directory → "No conversation found" → exit 1 → the turn
        # died with no surfaced error and a permanently blank bubble.
        stored_cwd = session.get("cwd")
        if stored_cwd:
            if cwd and cwd != stored_cwd:
                logger.warning(
                    "Discarding per-turn cwd=%r for session %s; "
                    "session record says %r and is authoritative.",
                    cwd, app_session_id, stored_cwd,
                )
            cwd = stored_cwd

        stored_model = session.get("model")
        if stored_model and not allow_model_override:
            if model and model != stored_model:
                logger.warning(
                    "Discarding per-turn model=%r for session %s; "
                    "session record says %r and is authoritative.",
                    model, app_session_id, stored_model,
                )
            model = stored_model
        elif allow_model_override:
            if not model:
                model = stored_model or ""
        else:
            provider_id = None
            reasoning_effort = None

        # Note: WS registration is owned by the /ws/chat handler in main.py,
        # which registers before putting the prompt on the queue. No need to
        # re-register here.

        # --- Auto-name session on first prompt (mode-independent) ---
        # SessionWSBroadcaster's `renamed` listener now globally fans
        # the `session_renamed` WS frame to every connected tab — no
        # manual broadcast needed here.
        if not session.get("messages"):
            short_name = prompt.strip().replace("\n", " ")
            if not short_name and images:
                short_name = "Image"
            if short_name:
                if len(short_name) > 50:
                    short_name = short_name[:47] + "..."
                session_manager.rename(app_session_id, short_name)

        # Lifecycle: read the in-flight id (stashed by the processor when
        # it picked this prompt off the queue). May be None if this call
        # path didn't go through the processor (legacy / direct invocation).
        lifecycle_msg_id = self.user_prompt_manager.get_in_flight_lifecycle_msg_id(
            app_session_id,
        )
        if lifecycle_msg_id:
            from orchs import get_strategy
            try:
                get_strategy(mode).record_turn_start(lifecycle_msg_id)
            except Exception:
                logger.debug("lifecycle: record_turn_start failed", exc_info=True)

        # When send_target is "supervisor" and the session has the
        # supervisor toggle enabled, bypass the primary orchestration and
        # run a direct native turn on the supervisor sub-session
        # (lazy-spawned on first use, resumed thereafter via the
        # `supervisor_agent_session_id` slot on the primary's record).
        # The message is tagged source="supervisor" so the split view
        # routes it to the supervisor panel.
        if send_target == "supervisor" and session.get("supervisor_enabled"):
            import extension_store
            not_ready = extension_store.runtime_not_ready_message(
                extension_store.BUILTIN_SUPERVISOR_EXTENSION_ID
            )
            if not_ready is not None:
                raise RuntimeError(not_ready)
            try:
                await self.run_turn(
                    session=session,
                    prompt=prompt,
                    # Native turn with no override: cli_prompt is the raw
                    # prompt. This branch bypasses run_primary (whose wrap
                    # fallback would otherwise supply it), so default
                    # None→prompt here or the model receives an empty prompt.
                    cli_prompt=cli_prompt or prompt,
                    app_session_id=app_session_id,
                    model=model,
                    cwd=cwd,
                    ws_callback=ws_callback,
                    provider_id=provider_id,
                    reasoning_effort=reasoning_effort,
                    images=images,
                    files=files,
                    trace_step_name="supervisor_direct",
                    session_id_field="supervisor_agent_session_id",
                    mode="native",
                    client_id=client_id,
                    source="supervisor",
                    queue_item_id=queue_item_id,
                    capability_contexts=capability_contexts,
                    file_discussion_id=file_discussion_id,
                    # Genuine user-facing turn — the user explicitly
                    # typed and sent to the supervisor. Same semantics
                    # as the native/manager `handle_turn` user_initiated
                    # flag: gates the open_file_panel MCP tool AND the
                    # open-todo reminder at run_turn. Without this, every
                    # direct-supervisor user prompt would silently
                    # skip the nudge.
                    user_initiated=True,
                )
            except asyncio.CancelledError:
                if lifecycle_msg_id:
                    interrupted_by = self.turn_manager._interrupted_by_msg_id.pop(
                        app_session_id, None,
                    )
                    # Parity with the native/manager path: a supervisor
                    # prompt aborted before it ever reached the CLI was
                    # never delivered → terminal `failed`, not a
                    # success-shaped `done`.
                    await self.user_prompt_manager.emit_user_msg_cancel_terminal(
                        app_session_id, lifecycle_msg_id, mode,
                        interrupted_by_msg_id=interrupted_by,
                    )
                raise
            except Exception as e:
                if lifecycle_msg_id:
                    await self.user_prompt_manager.emit_user_msg_failed(
                        app_session_id, lifecycle_msg_id,
                        reason="handle_turn_exception", error=str(e),
                    )
                raise
            else:
                if lifecycle_msg_id:
                    # `interrupted_by_msg_id` is stamped onto TurnManager
                    # state by `cancel_turn` (turn-side handoff); pop it
                    # here and pass into UPM so the cross-ref reaches the
                    # done event.
                    interrupted_by = self.turn_manager._interrupted_by_msg_id.pop(
                        app_session_id, None,
                    )
                    await self.user_prompt_manager.emit_user_msg_done(
                        app_session_id, lifecycle_msg_id, mode,
                        interrupted_by_msg_id=interrupted_by,
                    )
            return

        from orchs import get_handler
        worker_registry_cwds_by_session = self.__dict__.setdefault(
            "_known_worker_registry_cwds_by_session", {},
        )
        previous_worker_registry_cwds = worker_registry_cwds_by_session.get(app_session_id)
        if known_worker_registry_cwds:
            worker_registry_cwds_by_session[app_session_id] = dict(
                known_worker_registry_cwds,
            )
        else:
            worker_registry_cwds_by_session.pop(app_session_id, None)
        try:
            await get_handler(mode)(
                self,
                session=session,
                prompt=prompt,
                app_session_id=app_session_id,
                model=model,
                cwd=cwd,
                ws_callback=ws_callback,
                provider_id=provider_id,
                reasoning_effort=reasoning_effort,
                images=images,
                files=files,
                client_id=client_id,
                cli_prompt=cli_prompt,
                source=source,
                user_initiated=user_initiated,
                disallowed_tools=disallowed_tools,
                queue_item_id=queue_item_id,
                team_message=team_message,
                capability_contexts=capability_contexts,
                file_discussion_id=file_discussion_id,
            )
        except asyncio.CancelledError:
            if lifecycle_msg_id:
                interrupted_by = self.turn_manager._interrupted_by_msg_id.pop(
                    app_session_id, None,
                )
                # `done(cancelled)` only if the prompt actually reached the
                # CLI; a turn aborted before the runner ever spawned (e.g.
                # backend shutdown between persist and spawn) was never
                # delivered and must terminate as `failed`, not a
                # success-shaped `done`.
                await self.user_prompt_manager.emit_user_msg_cancel_terminal(
                    app_session_id, lifecycle_msg_id, mode,
                    interrupted_by_msg_id=interrupted_by,
                )
            raise
        except Exception as e:
            if lifecycle_msg_id:
                await self.user_prompt_manager.emit_user_msg_failed(
                    app_session_id, lifecycle_msg_id,
                    reason="handle_turn_exception", error=str(e),
                )
            raise
        else:
            if lifecycle_msg_id:
                interrupted_by = self.turn_manager._interrupted_by_msg_id.pop(
                    app_session_id, None,
                )
                await self.user_prompt_manager.emit_user_msg_done(
                    app_session_id, lifecycle_msg_id, mode,
                    interrupted_by_msg_id=interrupted_by,
                )
        finally:
            if previous_worker_registry_cwds is None:
                worker_registry_cwds_by_session.pop(app_session_id, None)
            else:
                worker_registry_cwds_by_session[app_session_id] = previous_worker_registry_cwds





    # ------------------------------------------------------------------
    # Worker run (called from /api/internal/ask-fork)
    # ------------------------------------------------------------------
    async def run_delegation(
        self,
        app_session_id: str,
        instructions: str,
        worker_session_id: Optional[str],
        worker_description: str,
        model: str,
        cwd: str,
        provider_id: str = "",
        reasoning_effort: str = "",
        justification: Optional[str] = None,
        proposed_orchestration_mode: Optional[str] = None,
        client_delegation_id: Optional[str] = None,
        node_id: Optional[str] = None,
        run_mode: str = "fork",
        worker_registry_cwd: Optional[str] = None,
        ephemeral: bool = False,
        machine_completion: bool = False,
        provision_prompt: Optional[str] = None,
        include_events: bool = False,
    ) -> dict:
        from orchs.manager._delegation import run_delegation as _impl
        return await _impl(
            self,
            app_session_id=app_session_id,
            instructions=instructions,
            worker_session_id=worker_session_id,
            worker_description=worker_description,
            provider_id=provider_id,
            model=model,
            reasoning_effort=reasoning_effort,
            cwd=cwd,
            justification=justification,
            proposed_orchestration_mode=proposed_orchestration_mode,
            client_delegation_id=client_delegation_id,
            node_id=node_id,
            run_mode=run_mode,
            worker_registry_cwd=worker_registry_cwd,
            ephemeral=ephemeral,
            machine_completion=machine_completion,
            provision_prompt=provision_prompt,
            include_events=include_events,
        )

    async def _init_target_agent_session(
        self,
        *,
        bc_session: dict,
        model: str,
        cwd: str,
        description: str,
        cancel_event: asyncio.Event,
        ws_callback=None,
        provision_prompt: Optional[str] = None,
    ) -> Optional[str]:
        from orchs.manager._approval import init_target_agent_session as _impl
        return await _impl(
            self,
            bc_session=bc_session,
            model=model,
            cwd=cwd,
            description=description,
            cancel_event=cancel_event,
            ws_callback=ws_callback,
            provision_prompt=provision_prompt,
        )


    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def _init_turn_messages(
        self,
        *,
        session: dict,
        app_session_id: str,
        prompt: str,
        images: Optional[list],
        files: Optional[list] = None,
        client_id: Optional[str] = None,
        source: Optional[str] = None,
        lifecycle_msg_id: Optional[str] = None,
        cli_prompt: Optional[str] = None,
        queue_item_id: Optional[str] = None,
        team_message: Optional[dict] = None,
        file_discussion_id: Optional[str] = None,
    ) -> dict:
        """Create + persist the user message ONLY. Returns it.

        The assistant message is created lazily inside `save_ws_callback`
        the first time an event needs to be mirrored onto it (see
        `_ensure_assistant_msg`). Lazy creation means: a turn that fails
        before producing any output never leaves an empty husk on disk
        for the UI to render, and the user never sees a phantom cursor
        in empty space — the bubble only exists once it has content.

        Persisting the user message upfront still closes the
        "typed-but-lost" window: if the backend crashes mid-turn, the
        prompt is on disk and `run_recovery.py` can reattach.
        """
        user_msg = {
            "id": str(uuid.uuid4()),
            "role": "user",
            "content": prompt,
            "events": [],
            "timestamp": datetime.now().isoformat(),
            "isStreaming": False,
            "agent_message_uuid": None,
            # Echo of the frontend's optimistic in-flight id. Lets the
            # client match this canonical user_msg back to the
            # `pendingMessages` entry it created on send and remove it
            # from the in-flight list. None for non-WS callers.
            "client_id": client_id,
            # Correlation id for the 5-state user-message lifecycle
            # (queued→sent→received→done/failed). Lets the frontend map
            # lifecycle WS events back to this message for status display.
            "lifecycle_msg_id": lifecycle_msg_id,
        }
        if cli_prompt is not None and cli_prompt != prompt:
            user_msg["cli_prompt"] = cli_prompt
        if file_discussion_id:
            user_msg["file_discussion_id"] = file_discussion_id
        if source:
            user_msg["source"] = source
            # Sub-turn (supervisor verdict, worker delegation, etc.):
            # derive parent_id from the session's last real (non-source)
            # user message so the frontend can render jump-to-parent.
            for m in reversed(session.get("messages") or []):
                if m.get("role") == "user" and not m.get("source"):
                    user_msg["parent_id"] = m["id"]
                    break
        if team_message:
            user_msg["team_message"] = team_message
        if images:
            user_msg["images"] = _save_message_images(
                app_session_id,
                user_msg["id"],
                images,
            )
        if files:
            user_msg["files"] = _message_file_metadata(files)

        persisted_user_msg = session_manager.append_user_msg(app_session_id, user_msg)
        if queue_item_id:
            session_manager.remove_queued_prompt(app_session_id, queue_item_id)
        if client_id:
            session_manager.remove_queued_prompt_by_client_id(app_session_id, client_id)
        return persisted_user_msg or user_msg

    def _build_assistant_msg(
        self,
        *,
        session: dict,
        app_session_id: Optional[str] = None,
    ) -> dict:
        """Build (but DO NOT persist) a fresh assistant message scaffold.

        Delegates to the per-mode strategy. In supervisor mode `session`
        is the worker (mode=native), but `app_session_id` is the
        supervisor's id — so we look up the real mode from the app session.
        """
        # In supervisor mode `session` is the worker (mode=native), but
        # `app_session_id` is the supervisor's id.  Look up the real
        # orchestration mode from the app session so the strategy
        # builds the correct scaffold.
        app_session = (
            session_manager.get(app_session_id) if app_session_id else None
        )
        effective_mode = (app_session or session).get("orchestration_mode")
        from orchs import get_strategy
        return get_strategy(effective_mode or "manager").build_assistant_scaffold()


    def _finalize_turn_messages(
        self,
        *,
        session: dict,
        app_session_id: str,
        user_msg: dict,
        assistant_msg: Optional[dict],
        primary_result: dict,
        workers: list[dict],
        stopped_at: Optional[str],
        trace_id: Optional[str],
        error_text: Optional[str] = None,
        interrupted_by_msg_id: Optional[str] = None,
    ) -> None:
        """Seal the persisted user/assistant pair at turn end. All writes
        coalesce into a single disk persist via SessionManager's batch.

        On success / cancel: fills in the final events, content, workers,
        manager session id, stop marker and trace id on the assistant
        message. With lazy creation, `assistant_msg` may be None — in
        that case we skip assistant finalization unless the run produced
        non-empty output via `sdk_output` (in which case we create a
        fresh assistant_msg here and persist it).

        On error: mark user_msg error; remove assistant_msg if one was
        created; else leave the user message standing alone.
        """
        with session_manager.batch(app_session_id):
            if error_text:
                session_manager.mark_user_error(
                    app_session_id, user_msg["id"], error_text,
                )
                if assistant_msg is not None:
                    session_manager.remove_assistant_msg(
                        app_session_id, assistant_msg["id"],
                    )
                # Exception-path failure: surface the sidebar error dot
                # here too so the single chokepoint covers every failure
                # shape (raises vs. returns success=False).
                session_manager.set_unseen_error(app_session_id, error_text)
                return

            if assistant_msg is None:
                # Lazy never fired (no events). If the SDK still produced
                # text output (sdk_output path — used when CLI didn't write
                # a session jsonl), materialize an assistant_msg now so
                # the output is captured. Otherwise nothing to finalize:
                # the turn produced literally nothing to persist beyond
                # the user_msg + the turn_complete WS event.
                sdk_text = (primary_result.get("sdk_output") or "").strip()
                if not sdk_text and not stopped_at:
                    # Plain success with no output AND no events — record
                    # a minimal assistant_msg so the conversation history
                    # isn't visually broken.
                    assistant_msg = self._build_assistant_msg(session=session)
                elif sdk_text:
                    assistant_msg = self._build_assistant_msg(session=session)
                else:
                    # Cancelled before any output. Nothing to finalize.
                    return
                # Inherit source tag from the user message (e.g.
                # source="supervisor") so the assistant message lands in
                # the correct frontend pane.
                if user_msg.get("source"):
                    assistant_msg["source"] = user_msg["source"]
                if user_msg.get("file_discussion_id"):
                    assistant_msg["file_discussion_id"] = user_msg["file_discussion_id"]
                session_manager.append_assistant_msg(
                    app_session_id, assistant_msg,
                )

            primary_events = _strip_synthetic_events(
                primary_result.get("events", [])
            )
            primary_sid = primary_result.get("session_id")
            msg_id = assistant_msg["id"]

            session_manager.set_streaming(app_session_id, msg_id, False)

            extracted = _extract_output_text(primary_events)
            if not extracted:
                # Fallback: use sdk_output captured by runner from SDK
                # messages (needed when CLI doesn't write a session jsonl,
                # e.g. API credentials).
                extracted = primary_result.get("sdk_output") or ""
            # A stopped/interrupted turn already carries the indication
            # (`stopped_at` and/or `interrupted_by_msg_id`, rendered as the
            # "Stopped/Interrupted at …" badge in the UI), so we don't
            # synthesize placeholder content when it produced nothing.
            session_manager.update_running_content(
                app_session_id, msg_id, extracted,
            )

            run_error = primary_result.get("error")
            run_failed = (
                primary_result.get("success") is False and not stopped_at
            )
            assistant_failed = False
            dot_error_text = ""
            content_looks_erroring = bool(
                re.search(
                    r"API Error:"
                    r"|^Failed to authenticate"
                    r"|^Invalid stream",
                    extracted or "",
                    re.MULTILINE,
                )
            )
            # INVARIANT: every failed run MUST surface SOMETHING to the
            # user. Four branches in priority order:
            #   1. runner gave us a typed `run_error` → surface it
            #   2. success=False and content looks like an API error →
            #      surface the content (better UX than the typed code)
            #   3. success=True but content contains an API error →
            #      Gemini CLI sometimes reports quota/API errors as
            #      assistant text while claiming the run succeeded.
            #      Treat the content as the error message.
            #   4. success=False with neither — generic silent-failure
            #      fallback so the assistant message gets an error
            #      bubble instead of staying blank. Catches gemini-cli
            #      silent exits, future provider regressions, anything
            #      that slips through.
            if (run_error and not stopped_at) or (run_failed and content_looks_erroring):
                # Prefer the user-facing content (e.g. "API Error: 429
                # Request rejected — Fair Usage Policy …") over the
                # typed run_error code (e.g. "rate_limit"). The typed
                # code is great for backend classification but useless
                # to a user staring at a red bubble. Fall back to the
                # code only when there's no extracted content text.
                err_text = (
                    extracted
                    if (run_failed and content_looks_erroring and extracted)
                    else (run_error if isinstance(run_error, str) and run_error else "")
                )
                session_manager.set_assistant_error(
                    app_session_id, msg_id, err_text,
                )
                assistant_failed = True
                dot_error_text = err_text
            elif content_looks_erroring and not stopped_at:
                # Gemini CLI reported success but the content IS an API
                # error (e.g. quota exhaustion with no 4xx code).
                session_manager.set_assistant_error(
                    app_session_id, msg_id, extracted or "",
                )
                assistant_failed = True
                dot_error_text = extracted or ""
            elif run_failed and not run_error:
                session_manager.set_assistant_error(
                    app_session_id,
                    msg_id,
                    "Run failed without producing an error message.",
                )
                assistant_failed = True
                dot_error_text = "Run failed without producing an error message."

            if assistant_failed:
                # Mirror the surfaced failure onto the sidebar error dot.
                # This is the chokepoint for the COMMON non-exception
                # failure path (provider API errors, quota, silent exits)
                # which return success=False instead of raising and so
                # never reach turn_manager's except block. The dot retires
                # on the next turn-start; it is NOT tied to view/seen.
                session_manager.set_unseen_error(
                    app_session_id, dot_error_text or "Turn failed",
                )

            # Delegate mode-specific finalization (pin session ids,
            # promote recovered placeholders, etc.)
            from orchs import get_strategy
            orch_mode = (session_manager.get_ref(app_session_id) or {}).get("orchestration_mode") or "team"
            get_strategy(orch_mode).finalize_turn(
                app_session_id=app_session_id,
                assistant_msg=assistant_msg,
                primary_result=primary_result,
            )
            session_manager.snapshot_workers(app_session_id, msg_id, workers)
            if primary_result.get("success") and not stopped_at and not assistant_failed:
                completed_at = datetime.now().isoformat()
                assistant_msg["completed_at"] = completed_at
                session_manager.set_completed_at(
                    app_session_id, msg_id, completed_at,
                )
            if stopped_at:
                session_manager.set_stopped_at(
                    app_session_id, msg_id, stopped_at,
                )
            if interrupted_by_msg_id:
                session_manager.set_interrupted_by_msg_id(
                    app_session_id, msg_id, interrupted_by_msg_id,
                )
            if trace_id:
                session_manager.set_trace_id(
                    app_session_id, msg_id, trace_id,
                )

            # Aggregate token usage: primary session + each worker.
            # Call once with the combined total so token_usage_last
            # represents the full turn, not just the last worker.
            primary_tu = extract_provider_result_token_usage(primary_result) or {}
            combined = dict(primary_tu)
            for w in workers:
                wtu = w.get("token_usage") or {}
                for k in (
                    "input_tokens",
                    "output_tokens",
                    "cache_creation_input_tokens",
                    "cache_read_input_tokens",
                ):
                    combined[k] = int(combined.get(k, 0)) + int(wtu.get(k, 0))
            session_manager.add_session_token_usage(
                app_session_id, combined,
            )
