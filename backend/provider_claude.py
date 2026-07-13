"""ClaudeProvider — `Provider` implementation for Anthropic's `claude`
CLI (subscription mode + Anthropic-compatible API mode, both).

Also hosts the Claude-specific runtime helpers reused by `run_recovery.py`
and `jsonl_tailer.py`:

  - `_enrich_claude_line` — adds `parent_tool_use_id` for sidechain msgs
  - `enrich_jsonl_line` — parse + enrich one line (shared pipeline)
  - `register_agent_tool_uses` — Agent/Task registration (shared)
  - `_SubagentRegistry` / `_PendingAgent` — fan-out tracking for nested
     `Agent` / `Task` tool_uses
  - `RunState` + runs-dir helper aliases for in-flight recovery state.

`ClaudeProvider.start_run` spawns `runner.py` detached, with env from
`build_env()` so the spawned claude SDK uses the right
`CLAUDE_CONFIG_DIR` / `ANTHROPIC_BASE_URL` / `ANTHROPIC_API_KEY` for
this provider record (no longer relying on `os.environ` inheritance).
The bootstrap polls for `state.json`, then starts a
`ClaudeJsonlTailer` (byte-offset append follower, lives in `jsonl_tailer.py`) that
pushes claude's native jsonl lines onto the orchestrator's per-run queue.

Crash recovery: `run_dir/backend_state.json` (distinct from the
runner's `state.json`) holds the tailer's `processed_byte` cursor,
updated atomically after every dispatched event. On backend restart,
`recover_in_flight()` emits descriptors for `run_recovery.py`, which
replays unfinished output when needed and registers live runs for
lifecycle/cancel/finalization tracking.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, ClassVar, Optional

from rate_limits import build_corpus, parse_rate_limit as parse_provider_rate_limit

from event_bus import BusEvent, bus
from env_compat import get_env
from provider import (
    Provider,
    RecoveredPopen,
    live_recovery_pid,
    StreamEvent,
    build_better_agent_run_env,
    path_exists_off_loop,
    popen_is_running_off_loop,
    read_runner_activity_state,
    run_provider_poll_off_loop,
    run_provider_io_off_loop,
    run_provider_io_phase_off_loop,
    terminate_failed_run_process,
    persist_seed_or_terminate,
    RecoveryAttachReceipt,
    await_scheduled_tasks,
    schedule_loop_task,
    runner_argv,
)
import config_store
from extension_run_policy import disabled_builtin_extensions_for_run
from provider_env import is_ollama_base_url
from reasoning_effort import CLAUDE_REASONING_EFFORTS, DEFAULT_REASONING_EFFORT
import git_policy
from provider_lifecycle import LifecycleOutcome, RunLifecycleCoordinator

logger = logging.getLogger(__name__)


# ============================================================================
# Constants
# ============================================================================
from paths import ba_home, user_home


# Re-exports for back-compat with run_recovery + any out-of-tree
# code that imported these from provider_claude. New code should
# import from `runs_dir` directly.
from runs_dir import iter_run_dirs, prune_old_completed_runs, runs_root as _runs_root
from runs_dir import reap_run_dir as _reap_run_dir
import perf
from runs_dir import atomic_write_json as _atomic_write_json
from runs_dir import pid_alive as _pid_alive
from proc_control import process_control as _process_control
from ingestion_versions import CLAUDE_INGESTION_VERSION, marker_matches_current


_RUNNER_PATH = Path(__file__).parent / "runner.py"
_TAIL_POLL_INTERVAL = 0.05       # seconds between empty-read polls
# Wind-down grace: max seconds a runner may stay alive AFTER its turn
# finalized (complete.json read) before the backend force-kills the
# tree. Background execution is forbidden on every run, so a post-turn
# process is pure infrastructure — a hung disconnect must not pin the
# native session (the wind-down gate defers new prompts behind it).


def _jsonl_size_bytes(path: Path) -> int:
    return path.stat().st_size


def _line_has_final_text(raw: bytes, expected: str) -> bool:
    """True when a claude session jsonl line is a PRIMARY assistant
    message carrying a text block exactly equal to `expected`.
    Sidechain (subagent) lines are excluded — their text lives in the
    subagent's own jsonl and never matches the turn's primary final
    text."""
    try:
        entry = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False
    if not isinstance(entry, dict) or entry.get("type") != "assistant":
        return False
    if entry.get("isSidechain"):
        return False
    content = (entry.get("message") or {}).get("content")
    if not isinstance(content, list):
        return False
    return any(
        isinstance(block, dict)
        and block.get("type") == "text"
        and block.get("text") == expected
        for block in content
    )


def _scan_for_final_text(
    path: Path, start: int, expected: str,
) -> tuple[Optional[int], int]:
    """Scan complete jsonl lines in `[start, EOF)` for the turn's final
    assistant text. Returns `(last_match_line_end, next_scan_offset)` —
    the absolute byte offset just past the LAST matching line (None when
    no line matches yet) and the offset the next incremental scan should
    resume from (end of the last COMPLETE line, so a partially-flushed
    trailing line is re-read once finished)."""
    last_match_end: Optional[int] = None
    offset = start
    try:
        with path.open("rb") as fh:
            fh.seek(start)
            for raw in fh:
                if not raw.endswith(b"\n"):
                    break  # partial trailing line — rescan next round
                line_end = offset + len(raw)
                if _line_has_final_text(raw, expected):
                    last_match_end = line_end
                offset = line_end
    except OSError:
        return None, start
    return last_match_end, offset


DEFAULT_DISALLOWED_TOOLS = [
    "AskUserQuestion",
    "EnterPlanMode",
    "ExitPlanMode",
]
# In-process CLI timer tools are replaced by the backend-owned durable
# scheduler (stores/schedule_store.py + the runner's `scheduler` MCP
# server). Stripped on EVERY spawn so a runner can never start a
# TIMER-driven turn of its own. The runner refuses to spawn if these
# tools are missing from input.json. Single source: runs_dir.TIMER_TOOLS.
from runs_dir import (
    AUTO_BACKGROUND_ENV,
    BACKGROUND_TASKS_DISABLE_ENV,
    BACKGROUND_WORK_TOOLS,
    BG_EXIT_HANDOFF_DISABLE_ENV,
    TIMER_TOOLS,
)


# Re-exports for back-compat — enrichment pipeline lives in
# `claude_jsonl_enrich`. New code should import from there directly.
from claude_jsonl_enrich import (
    _enrich_claude_line,
    _PendingAgent,
    _SubagentRegistry,
    enrich_jsonl_line,
    register_agent_tool_uses,
)


# ============================================================================
# RunState — per-run bookkeeping held in-memory on the bridge
# ============================================================================
@dataclass
class RunState:
    run_id: str
    run_dir: Path
    popen: subprocess.Popen
    mode: str                               # "native" | "manager"
    app_session_id: str
    queue: asyncio.Queue
    session_id: Optional[str] = None
    jsonl_path: Optional[Path] = None
    processed_byte: int = 0
    # Durable resume-cursor: only advances once an event through this byte
    # offset has actually been applied to the render tree (see
    # `ack_applied_cursor`). `processed_byte` above is the eager tailer READ
    # cursor (used for drain-wait detection) and must never be persisted
    # directly — doing so lets a restart skip events that were read but
    # never applied.
    applied_byte: int = 0
    tailer: Optional["ClaudeJsonlTailer"] = None
    tailer_task: Optional[asyncio.Task] = None
    complete_task: Optional[asyncio.Task] = None
    worker_tailers: dict[str, "ClaudeJsonlTailer"] = field(default_factory=dict)
    started_at: str = ""
    cancelled: bool = False
    persist_to: str = ""  # session messages are persisted to (differs from app_session_id in supervisor mode)
    target_message_id: Optional[str] = None
    turn_run_id: Optional[str] = None
    lifecycle_msg_id: Optional[str] = None
    root_id: Optional[str] = None
    cwd: str = ""
    # Set by Provider._cleanup_run when the run is deregistered (runner
    # process exited). start_run's wind-down gate awaits this before
    # spawning a --resume on the same native session.
    released: asyncio.Event = field(default_factory=asyncio.Event)
    # True once the queue consumer is gone — set when a terminal event
    # (`complete`/`error`) is enqueued (consumer breaks on it) and by
    # `release_queue` when the consumer exits for any other reason
    # (cancel/timeout/exception). Tailer lines dispatched afterwards
    # bypass the dead queue and go through the orphan funnel
    # (`_ingest_late_flush`).
    turn_finalized: bool = False
    lifecycle_token: Any = None
    lifecycle_record: Any = None


@dataclass(frozen=True, slots=True)
class ClaudeLifecycleRecord:
    run_id: str
    cleanup_nonce: str
    pid: int
    run_dir: str


# ============================================================================
# ClaudeProvider — `Provider` impl that drives Anthropic's claude CLI
# ============================================================================
class ClaudeProvider(Provider):
    """Spawns detached `runner.py` subprocesses (which use the
    `claude_agent_sdk` in-process), one-shot `claude -p` invocations,
    and `claude --rewind-files` invocations — all with env threaded
    from this provider's record so `CLAUDE_CONFIG_DIR` /
    `ANTHROPIC_BASE_URL` / `ANTHROPIC_API_KEY` follow the provider, not
    the FastAPI process's `os.environ`.

    Demultiplexes claude's own session jsonl files onto per-run
    asyncio queues via `FileTailer`."""

    KIND: ClassVar[str] = "claude"
    supports_reasoning_effort: ClassVar[bool] = True
    reasoning_effort_options: ClassVar[tuple[str, ...]] = CLAUDE_REASONING_EFFORTS
    default_reasoning_effort: ClassVar[str] = DEFAULT_REASONING_EFFORT
    # `--tools ""` deterministically disables every built-in tool.
    supports_headless_no_tools: ClassVar[bool] = True

    def __init__(self, record: dict) -> None:
        super().__init__(record)
        self._runs: dict[str, RunState] = {}
        self._lifecycle: RunLifecycleCoordinator[ClaudeLifecycleRecord] | None = None
        self._lifecycle_runs: dict[str, RunState] = {}
        self._lifecycle_spawn_tasks: set[asyncio.Task] = set()
        self._recovery_attach_pending: set[str] = set()
        self._recovery_pending_states: dict[str, RunState] = {}
        # Aggregate gauge: sum of all run-queue depths for this provider
        # instance. Bounded — one entry regardless of run count. Name
        # is stashed on the instance so `provider.get_provider` can
        # `perf.unregister_queue` it when the provider record is deleted
        # (otherwise the gauge emits `depth=0` lines forever for a
        # defunct provider) AND re-register it via `_register_perf_gauge`
        # when the provider is resurrected.
        self._perf_gauge_name = (
            f"provider.claude.{record.get('id', 'unknown')}.run_q"
        )
        self._register_perf_gauge()

    def _register_perf_gauge(self) -> None:
        perf.register_queue(
            self._perf_gauge_name,
            lambda: sum(rs.queue.qsize() for rs in self._runs.values()),
        )

    def cancel_run(self, run_id: str) -> bool:
        rs = self._runs.get(run_id)
        signalled = super().cancel_run(run_id) if rs is not None else False
        lifecycle = self._lifecycle
        if lifecycle is None:
            return signalled

        async def cancel_owned() -> None:
            cancelled = await lifecycle.cancel(run_id)
            record = cancelled.value
            owned = self._lifecycle_runs.get(record.cleanup_nonce) if record else None
            if owned is not None:
                if owned is not rs:
                    await run_provider_io_phase_off_loop(
                        "claude_cancel_terminate", terminate_failed_run_process, owned
                    )
                await run_provider_io_phase_off_loop(
                    "claude_cancel_cleanup", self._cleanup_lifecycle_artifacts, owned
                )

        try:
            schedule_loop_task(
                lifecycle.owner_loop, cancel_owned(), name=f"claude-cancel-{run_id[:8]}"
            )
            return True
        except Exception:
            logger.exception("failed to schedule Claude lifecycle cancellation")
            return signalled

    async def shutdown_lifecycle(self, *, terminate_runs: bool = True) -> None:
        lifecycle = self._lifecycle
        if lifecycle is None:
            return
        await lifecycle.quiesce()
        pending = tuple(self._lifecycle_spawn_tasks)
        if pending:
            await await_scheduled_tasks(pending)
        inventory = await lifecycle.shutdown()
        if not terminate_runs:
            return
        cleaned: set[int] = set()
        for rs in tuple(self._recovery_pending_states.values()):
            cleaned.add(id(rs))
            await run_provider_io_phase_off_loop(
                "claude_shutdown_pending_terminate", terminate_failed_run_process, rs
            )
            await run_provider_io_phase_off_loop(
                "claude_shutdown_pending_cleanup", self._cleanup_lifecycle_artifacts, rs
            )
        for published in inventory.published:
            rs = self._lifecycle_runs.get(published.value.cleanup_nonce)
            if rs is None or id(rs) in cleaned:
                continue
            await run_provider_io_phase_off_loop(
                "claude_shutdown_terminate", terminate_failed_run_process, rs
            )
            await run_provider_io_phase_off_loop(
                "claude_shutdown_cleanup", self._cleanup_lifecycle_artifacts, rs
            )

    def _cleanup_lifecycle_artifacts(self, rs: RunState) -> None:
        run_id = rs.run_id
        record = getattr(rs, "lifecycle_record", None)
        if record is not None:
            self._lifecycle_runs.pop(record.cleanup_nonce, None)
        self._recovery_pending_states.pop(run_id, None)
        super()._cleanup_run(run_id)
        try:
            import active_run_catalog
            active_run_catalog.retire(_runs_root(), run_id)
        except Exception:
            logger.exception("failed to retire Claude run catalog entry %s", run_id)
        try:
            _reap_run_dir(rs.run_dir)
        except Exception:
            logger.exception("failed to reap Claude run directory %s", run_id)

    def _cleanup_run(self, run_id: str) -> None:
        rs = self._runs.get(run_id)
        super()._cleanup_run(run_id)
        if rs is None:
            return
        record = getattr(rs, "lifecycle_record", None)
        token = getattr(rs, "lifecycle_token", None)
        if record is None or token is None or self._lifecycle is None:
            return
        self._lifecycle_runs.pop(record.cleanup_nonce, None)
        schedule_loop_task(
            self._lifecycle.owner_loop,
            self._lifecycle.retire(token, record),
            name=f"claude-retire-{run_id[:8]}",
        )

    def build_env(self) -> dict[str, str]:
        """Compose the subprocess env for any claude CLI / runner spawn
        owned by this provider. Inherits the FastAPI process's env as
        a base (PATH, HOME, BETTER_CLAUDE_*, etc.) and overlays the
        provider record's auth/config_dir.

        Snapshots `self.record` into a local var so a concurrent
        provider-config edit (which atomically swaps the record dict)
        can't expose this caller to a half-replaced state.
        """
        env = os.environ.copy()
        home = user_home()
        env["HOME"] = str(home)
        env.pop("CLAUDE_CODE_SIMPLE", None)
        record = self.record  # atomic snapshot of the dict reference
        if record.get("mode") == "api_key":
            api_key = record.get("api_key") or ""
            if api_key:
                env["ANTHROPIC_API_KEY"] = api_key
                if is_ollama_base_url(record.get("base_url") or ""):
                    env["ANTHROPIC_AUTH_TOKEN"] = api_key
                else:
                    env.pop("ANTHROPIC_AUTH_TOKEN", None)
            else:
                env.pop("ANTHROPIC_API_KEY", None)
                env.pop("ANTHROPIC_AUTH_TOKEN", None)
            base_url = record.get("base_url") or ""
            if base_url:
                env["ANTHROPIC_BASE_URL"] = base_url
            else:
                env.pop("ANTHROPIC_BASE_URL", None)
        else:
            # subscription mode — clear API auth so claude uses logged-in creds
            env.pop("ANTHROPIC_API_KEY", None)
            env.pop("ANTHROPIC_AUTH_TOKEN", None)
            env.pop("ANTHROPIC_BASE_URL", None)
        # Isolate this account's credential store via CLAUDE_CONFIG_DIR (the
        # shared SSOT resolves/anchors config_dir and treats ~/.claude as the
        # default — see config_store.provider_credential_env). Without the
        # anchor the claude CLI would resolve a relative value against the
        # session cwd, scattering a per-project store that ingestion (which
        # resolves against the backend cwd) never finds.
        env.pop("CLAUDE_CONFIG_DIR", None)
        cred = config_store.provider_credential_env(record)
        if cred:
            env[cred[0]] = cred[1]
        # Enable file checkpointing for SDK/stream-json mode sessions so
        # --rewind-files works (required for retry/rewind functionality).
        env["CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING"] = "1"
        # Background execution is forbidden on every claude run (see
        # runs_dir.BACKGROUND_WORK_TOOLS): the CLI's native master switch
        # strips run_in_background from tool schemas, ignores a smuggled
        # param, disables timeout-auto-backgrounding, and forces
        # subagents synchronous. Also disable cross-exit bg adoption and
        # opt-in auto-backgrounding.
        env[BACKGROUND_TASKS_DISABLE_ENV] = "1"
        env[BG_EXIT_HANDOFF_DISABLE_ENV] = "1"
        env.pop(AUTO_BACKGROUND_ENV, None)
        return env

    # ------------------------------------------------------------------
    # start_run — spawn runner, schedule bootstrap task, return immediately
    # ------------------------------------------------------------------
    def start_run(
        self,
        *,
        run_id: str,
        prompt: str,
        images: Optional[list] = None,
        files: Optional[list] = None,
        cwd: str,
        loop: asyncio.AbstractEventLoop,
        queue: asyncio.Queue,
        model: Optional[str],
        reasoning_effort: Optional[str],
        session_id: Optional[str],
        mode: str,
        app_session_id: str,
        source: Optional[str] = None,
        disallowed_tools: Optional[list[str]] = None,
        setting_sources: Optional[list[str]] = None,
        backend_url: Optional[str] = None,
        internal_token: Optional[str] = None,
        fork: bool = False,
        supervised: bool = False,
        supervisor_agent_session_id: Optional[str] = None,
        worker_agent_session_id: Optional[str] = None,
        mssg_sender_session_id: Optional[str] = None,
        is_worker: bool = False,
        browser_harness_enabled: bool = False,
        open_file_panel_enabled: bool = False,
        working_mode: Optional[str] = None,
        extra_env: Optional[dict[str, str]] = None,
        continuation_chain: Optional[list[str]] = None,
        provider_run_config: Optional[dict] = None,
        capability_contexts: Optional[list[dict]] = None,
        target_message_id: Optional[str] = None,
        turn_run_id: Optional[str] = None,
        lifecycle_msg_id: Optional[str] = None,
        disabled_builtin_extensions: Optional[list[str]] = None,
        provisioned_tool_profile: str = "",
    ) -> None:
        """Spawn `runner.py` detached and schedule a bootstrap task that,
        as soon as the runner writes `state.json`, starts a `FileTailer`
        on claude's own session jsonl and forwards translated events
        onto `queue`.

        Returns immediately — the run continues in the background even
        if the backend dies. When a previous run on the SAME native
        `session_id` is still winding down (turn done, process exiting),
        the spawn is deferred until its release event fires (see
        `_start_after_release`). The prompt is already durably queued by
        the orchestrator, so deferral cannot lose it.
        """
        if mode == "team":
            mode = "manager"
        if mode not in ("native", "manager"):
            raise ValueError(f"mode must be 'native' or 'team', got {mode!r}")
        if self.defunct:
            raise RuntimeError(
                f"provider {self.id} is defunct (record deleted); "
                "cannot start new runs"
            )
        self.assert_not_suspended(action="start new runs")

        spawn_kwargs = dict(
            run_id=run_id,
            prompt=prompt,
            images=images,
            files=files,
            cwd=cwd,
            loop=loop,
            queue=queue,
            model=model,
            reasoning_effort=reasoning_effort,
            session_id=session_id,
            mode=mode,
            app_session_id=app_session_id,
            source=source,
            disallowed_tools=disallowed_tools,
            setting_sources=setting_sources,
            backend_url=backend_url,
            internal_token=internal_token,
            fork=fork,
            supervised=supervised,
            supervisor_agent_session_id=supervisor_agent_session_id,
            worker_agent_session_id=worker_agent_session_id,
            mssg_sender_session_id=mssg_sender_session_id,
            is_worker=is_worker,
            browser_harness_enabled=browser_harness_enabled,
            open_file_panel_enabled=open_file_panel_enabled,
            working_mode=working_mode,
            extra_env=extra_env,
            continuation_chain=continuation_chain,
            provider_run_config=provider_run_config,
            capability_contexts=capability_contexts,
            target_message_id=target_message_id,
            turn_run_id=turn_run_id,
            lifecycle_msg_id=lifecycle_msg_id,
            disabled_builtin_extensions=disabled_builtin_extensions,
            provisioned_tool_profile=provisioned_tool_profile,
        )

        if self._lifecycle is None:
            self._lifecycle = RunLifecycleCoordinator(loop)
        task = schedule_loop_task(
            loop,
            self._admit_and_spawn(spawn_kwargs),
            name=f"claude-admit-spawn-{run_id[:8]}",
        )
        if task is not None:
            self._lifecycle_spawn_tasks.add(task)
            task.add_done_callback(self._lifecycle_spawn_tasks.discard)
            self._track_run_start_receipt(run_id, task)

    async def _admit_and_spawn(self, spawn_kwargs: dict) -> None:
        lifecycle = self._lifecycle
        if lifecycle is None:
            raise RuntimeError("Claude lifecycle coordinator is unavailable")
        run_id = spawn_kwargs["run_id"]
        admission = await lifecycle.admit(run_id)
        if not admission.accepted or admission.token is None:
            if admission.outcome is LifecycleOutcome.DUPLICATE:
                raise RuntimeError(f"duplicate Claude run id: {run_id}")
            raise RuntimeError(f"Claude run admission rejected: {admission.outcome.value}")
        token = admission.token
        run_state = None
        published_record = None
        try:
            # Wind-down serialization gate. A completed run stays registered
            # until its runner process actually exits.
            session_id = spawn_kwargs.get("session_id")
            fork = bool(spawn_kwargs.get("fork"))
            if session_id and not fork:
                blockers = [
                    rs for rs in self._runs.values()
                    if getattr(rs, "session_id", None) == session_id
                ]
                if blockers:
                    logger.info(
                        "start_run: deferring run %s behind winding-down "
                        "run(s) %s on native session %s",
                        run_id[:8],
                        [b.run_id[:8] for b in blockers],
                        session_id[:8],
                    )
                    await self._start_after_release(blockers)
            run_state = await run_provider_io_phase_off_loop(
                "claude_spawn_seed", functools.partial(self._spawn_run, **spawn_kwargs)
            )
            record = ClaudeLifecycleRecord(
                run_id=run_id,
                cleanup_nonce=uuid.uuid4().hex,
                pid=int(run_state.popen.pid),
                run_dir=str(run_state.run_dir),
            )
            published = await lifecycle.publish(token, record)
            if not published.accepted:
                await run_provider_io_phase_off_loop(
                    "claude_publish_reject_terminate", terminate_failed_run_process, run_state
                )
                try:
                    _reap_run_dir(run_state.run_dir)
                except Exception:
                    logger.exception("failed to reap rejected Claude run %s", run_id)
                raise RuntimeError(f"Claude run publish rejected: {published.outcome.value}")
            run_state.lifecycle_token = token
            run_state.lifecycle_record = record
            published_record = record
            self._lifecycle_runs[record.cleanup_nonce] = run_state
            self._publish_started_run(run_id, run_state)
            await self._bootstrap_run(run_state)
        except BaseException:
            if published_record is not None and run_state is not None:
                await self._cleanup_failed_published_run(
                    lifecycle, token, published_record, run_state
                )
            else:
                if run_state is not None and run_state.popen.poll() is None:
                    await run_provider_io_phase_off_loop(
                        "claude_spawn_rollback_terminate", terminate_failed_run_process, run_state
                    )
                    try:
                        _reap_run_dir(run_state.run_dir)
                    except Exception:
                        logger.exception("failed to reap rolled-back Claude run %s", run_id)
                await lifecycle.rollback(token)
            raise

    async def _cleanup_failed_published_run(
        self, lifecycle, token, record: ClaudeLifecycleRecord, rs: RunState,
    ) -> None:
        if bool(getattr(rs, "recovered_attach", False)):
            self._lifecycle_runs.pop(record.cleanup_nonce, None)
            self._recovery_pending_states.pop(rs.run_id, None)
            super()._cleanup_run(rs.run_id)
            await lifecycle.retire(token, record)
            return
        try:
            await run_provider_io_phase_off_loop(
                "claude_bootstrap_failure_terminate", terminate_failed_run_process, rs
            )
        except BaseException:
            logger.exception("failed to terminate bootstrap-failed Claude run %s", rs.run_id)
        try:
            await run_provider_io_phase_off_loop(
                "claude_bootstrap_failure_cleanup", self._cleanup_lifecycle_artifacts, rs
            )
        except BaseException:
            logger.exception("failed to clean bootstrap-failed Claude run %s", rs.run_id)
        try:
            await lifecycle.retire(token, record)
        except BaseException:
            logger.exception("failed to retire bootstrap-failed Claude run %s", rs.run_id)

    async def _start_after_release(self, blockers: list) -> None:
        """Wait for every blocking run's release event (set by
        `Provider._cleanup_run` when the runner process exits), then
        re-enter `start_run`. Re-entering (rather than spawning directly)
        re-checks the gate against runs that registered in the meantime.
        Purely event-driven: no polling, resolves the moment the release
        fires."""
        waits = [
            blocker.released.wait()
            for blocker in blockers
            if getattr(blocker, "released", None) is not None
        ]
        if waits:
            await asyncio.gather(*waits)

    def _build_input_payload(
        self,
        *,
        prompt: str,
        images: Optional[list],
        files: Optional[list],
        cwd: str,
        model: Optional[str],
        reasoning_effort: Optional[str],
        session_id: Optional[str],
        mode: str,
        app_session_id: str,
        source: Optional[str],
        disallowed_tools: Optional[list[str]],
        setting_sources: Optional[list[str]],
        backend_url: Optional[str],
        internal_token: Optional[str],
        fork: bool,
        supervised: bool,
        supervisor_agent_session_id: Optional[str],
        worker_agent_session_id: Optional[str],
        mssg_sender_session_id: Optional[str],
        is_worker: bool,
        browser_harness_enabled: bool,
        open_file_panel_enabled: bool,
        continuation_chain: Optional[list[str]],
        provider_run_config: Optional[dict],
        capability_contexts: Optional[list[dict]],
        target_message_id: Optional[str],
        turn_run_id: Optional[str],
        lifecycle_msg_id: Optional[str],
        disabled_builtin_extensions: Optional[list[str]],
        provisioned_tool_profile: str,
    ) -> tuple[dict, bool, str, str]:
        """Single source for the runner's input.json payload.
        Returns `(payload, bare, mode, resolved_backend_url)`
        (mode may be promoted to "manager" for bare orchestration
        sessions)."""
        resolved_backend_url = (
            backend_url
            or get_env("BETTER_CLAUDE_BACKEND_URL")
            or "http://localhost:8000"
        )

        # Bare-config sessions (TestApe-isolated) are the single source of
        # truth for an empty system prompt. Routed entirely off the session
        # record so EVERY spawn path (top-level turn, worker init/prep,
        # delegation) is covered from this one chokepoint. For a delegation
        # turn `app_session_id` is the (bare) manager, so its workers inherit
        # bare; a worker's own init turn uses the worker's (bare) session.
        from session_manager import manager as _sm
        _run_config_fields = (
            "bare_config",
            "orchestration_mode",
            "permission",
            "provider_id",
            "disabled_builtin_extensions",
        )
        _sess_rec = _sm.get_fields(app_session_id, _run_config_fields)
        _worker_sess_rec = (
            _sm.get_fields(worker_agent_session_id, _run_config_fields)
            if worker_agent_session_id else {}
        )
        from permission import resolve_for_run as _resolve_perm
        _permission = _resolve_perm(
            sess_rec=_sess_rec,
            worker_sess_rec=_worker_sess_rec,
            is_worker=is_worker,
            fallback_kind=self.KIND,
        )
        _bare = bool(_sess_rec.get("bare_config"))
        if _bare:
            # No user/project/local CLAUDE.md, settings, or memory.
            setting_sources = []
            # Honor orchestration_mode at the runner level for the genuine
            # top-level manager turn only (NOT delegated worker turns, which
            # carry the manager's app_session_id but set is_worker=True).
            # This attaches the working `manager-delegate` `delegate` tool.
            if (
                not is_worker
                and _sess_rec.get("orchestration_mode") in ("team", "manager")
                and resolved_backend_url
                and internal_token
            ):
                mode = "manager"

        input_payload = {
            "prompt": prompt,
            "images": images or [],
            "files": files or [],
            "cwd": cwd,
            "model": model,
            "reasoning_effort": reasoning_effort,
            "permission": _permission,
            "session_id": session_id,
            "mode": mode,
            "source": source or "",
            "app_session_id": app_session_id,
            "working_mode": (_sess_rec or {}).get("working_mode"),
            "worker_working_mode": (_worker_sess_rec or {}).get("working_mode"),
            "active_capability_ids": [
                str(cid)
                for cid in ((_sess_rec or {}).get("active_capability_ids") or [])
                if str(cid or "").strip()
            ],
            "disallowed_tools": list(dict.fromkeys(
                (disallowed_tools or DEFAULT_DISALLOWED_TOOLS)
                + list(TIMER_TOOLS)
                + list(BACKGROUND_WORK_TOOLS)
                + git_policy.claude_disallowed_extra(_sess_rec)
            )),
            "setting_sources": setting_sources,
            "backend_url": resolved_backend_url,
            "internal_token": internal_token,
            "fork": bool(fork),
            "supervised": bool(supervised),
            "supervisor_agent_session_id": supervisor_agent_session_id,
            "worker_agent_session_id": worker_agent_session_id,
            "mssg_sender_session_id": mssg_sender_session_id,
            "browser_harness_enabled": browser_harness_enabled,
            "open_file_panel_enabled": open_file_panel_enabled,
            "bare_config": _bare,
            "continuation_chain": continuation_chain or [],
            "provider_run_config": provider_run_config or {},
            "capability_contexts": capability_contexts or [],
            "target_message_id": target_message_id,
            "turn_run_id": turn_run_id,
            "lifecycle_msg_id": lifecycle_msg_id,
            "provisioned_tool_profile": str(provisioned_tool_profile or "").strip(),
            "disabled_builtin_tools": config_store.get_disabled_builtin_tools(),
            "disabled_builtin_extensions": (
                disabled_builtin_extensions_for_run(
                    disabled_builtin_extensions,
                    session_record=_sess_rec,
                    worker_record=_worker_sess_rec,
                )
            ),
        }
        return input_payload, _bare, mode, resolved_backend_url

    def _spawn_run(
        self,
        *,
        run_id: str,
        prompt: str,
        images: Optional[list],
        files: Optional[list],
        cwd: str,
        loop: asyncio.AbstractEventLoop,
        queue: asyncio.Queue,
        model: Optional[str],
        reasoning_effort: Optional[str],
        session_id: Optional[str],
        mode: str,
        app_session_id: str,
        source: Optional[str],
        disallowed_tools: Optional[list[str]],
        setting_sources: Optional[list[str]],
        backend_url: Optional[str],
        internal_token: Optional[str],
        fork: bool,
        supervised: bool,
        supervisor_agent_session_id: Optional[str],
        worker_agent_session_id: Optional[str],
        mssg_sender_session_id: Optional[str],
        is_worker: bool,
        browser_harness_enabled: bool,
        open_file_panel_enabled: bool,
        working_mode: Optional[str],
        extra_env: Optional[dict[str, str]],
        continuation_chain: Optional[list[str]],
        provider_run_config: Optional[dict],
        capability_contexts: Optional[list[dict]],
        target_message_id: Optional[str],
        turn_run_id: Optional[str],
        lifecycle_msg_id: Optional[str],
        disabled_builtin_extensions: Optional[list[str]],
        provisioned_tool_profile: str,
    ) -> RunState:
        """Post-gate spawn body: write input.json, create containment,
        Popen the runner, register RunState, schedule bootstrap."""
        run_dir = _runs_root() / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        input_payload, _bare, mode, resolved_backend_url = (
            self._build_input_payload(
                prompt=prompt,
                images=images,
                files=files,
                cwd=cwd,
                model=model,
                reasoning_effort=reasoning_effort,
                session_id=session_id,
                mode=mode,
                app_session_id=app_session_id,
                source=source,
                disallowed_tools=disallowed_tools,
                setting_sources=setting_sources,
                backend_url=backend_url,
                internal_token=internal_token,
                fork=fork,
                supervised=supervised,
                supervisor_agent_session_id=supervisor_agent_session_id,
                worker_agent_session_id=worker_agent_session_id,
                mssg_sender_session_id=mssg_sender_session_id,
                is_worker=is_worker,
                browser_harness_enabled=browser_harness_enabled,
                open_file_panel_enabled=open_file_panel_enabled,
                continuation_chain=continuation_chain,
                provider_run_config=provider_run_config,
                capability_contexts=capability_contexts,
                target_message_id=target_message_id,
                turn_run_id=turn_run_id,
                lifecycle_msg_id=lifecycle_msg_id,
                disabled_builtin_extensions=disabled_builtin_extensions,
                provisioned_tool_profile=provisioned_tool_profile,
            )
        )
        (run_dir / "input.json").write_text(json.dumps(input_payload), encoding="utf-8")

        from containment import containment
        # Create the escape-proof container BEFORE spawn so the runner (and
        # every descendant, nested to infinity) is enrolled at birth.
        # Fail-closed: raises ContainmentUnavailable on a guaranteed platform
        # without the mechanism (e.g. Linux without a delegated cgroup).
        containment().create(run_id)
        stdout_fp = (run_dir / "stdout.log").open("ab")
        stderr_fp = (run_dir / "stderr.log").open("ab")
        try:
            env = self.build_env()
            if extra_env:
                env.update(extra_env)
            env.update(build_better_agent_run_env(
                backend_url=resolved_backend_url,
                internal_token=internal_token,
                app_session_id=app_session_id,
                cwd=cwd,
                model=model,
                provider_id=self.id,
                bare_config=_bare,
                user_facing=bool(open_file_panel_enabled) and not _bare,
                disabled_builtin_extensions=input_payload["disabled_builtin_extensions"],
            ))
            popen = subprocess.Popen(
                runner_argv(run_dir, dev_script=_RUNNER_PATH, kind="claude"),
                stdin=subprocess.DEVNULL,
                stdout=stdout_fp,
                stderr=stderr_fp,
                cwd=cwd,
                env=env,
                # detach: runner roots its own process tree so we can kill
                # it as a unit later (POSIX process group / Win32 taskkill).
                **_process_control().detach_spawn_kwargs(),
                # containment: Linux preexec_fn joins the cgroup before exec.
                **containment().spawn_kwargs(run_id),
            )
        except Exception:
            stdout_fp.close()
            stderr_fp.close()
            containment().teardown(run_id)
            raise
        finally:
            # Close our copies of the fds; the runner holds dup'd fds.
            stdout_fp.close()
            stderr_fp.close()
        containment().after_spawn(run_id, popen.pid)

        logger.info(
            "spawned runner pid=%d mode=%s run_id=%s run_dir=%s",
            popen.pid, mode, run_id, run_dir,
        )

        run_state = RunState(
            run_id=run_id,
            run_dir=run_dir,
            popen=popen,
            mode=mode,
            app_session_id=app_session_id,
            queue=queue,
            # Stamped at spawn (resume sid or None) so the wind-down
            # gate sees this run before state.json lands; bootstrap
            # overwrites with the discovered sid.
            session_id=session_id,
            started_at=datetime.now().isoformat(),
            persist_to=worker_agent_session_id or app_session_id,
            target_message_id=target_message_id,
            turn_run_id=turn_run_id,
            lifecycle_msg_id=lifecycle_msg_id,
            cwd=cwd,
        )
        # Seed backend_state.json so recovery scanners can see this run.
        persist_seed_or_terminate(self._write_backend_state, run_state)

        return run_state

    # ------------------------------------------------------------------
    # _bootstrap_run — wait for runner's state.json, then start tailer
    # ------------------------------------------------------------------
    async def _bootstrap_run(self, rs: RunState) -> None:
        run_dir = rs.run_dir
        runner_state_path = run_dir / "state.json"
        complete_path = run_dir / "complete.json"

        # 1) Poll runner state.json (written by runner.py on system.init).
        # No wall-clock timeout: the runner is responsible for either
        # writing state.json (success) or dying (failure handled below).
        # Slow providers (e.g. Z.AI) may legitimately take >30s for the
        # SDK's first system.init; a wall-clock cap was producing false
        # failures while the runner kept going on its own.
        runner_state: Optional[dict] = None
        while True:
            if await path_exists_off_loop(runner_state_path):
                try:
                    raw = await run_provider_io_phase_off_loop("bootstrap_read",
                        Path.read_text, runner_state_path, "utf-8",
                    )
                    parsed = json.loads(raw)
                    if parsed.get("session_id") and parsed.get("jsonl_path"):
                        runner_state = parsed
                        break
                except (json.JSONDecodeError, OSError):
                    # half-written; retry
                    pass

            # Runner is dead — enter regardless of state.json existing.
            # state.json with null/missing session_id + dead runner means
            # pre-run failure (SDK init without valid sid). Breaking here
            # falls through to the "runner_state is None" drain below.
            if not await popen_is_running_off_loop(rs.popen):
                if await path_exists_off_loop(complete_path):
                    break  # falls through to "runner_state is None" drain
                await self._emit_early_failure(
                    rs,
                    f"runner exited early with code {rs.popen.returncode}",
                )
                return

            await asyncio.sleep(_TAIL_POLL_INTERVAL)

        # 2) Handle the "runner died with complete.json but never wrote
        #    state.json" fast-path — happens on fatal pre-run failures.
        if runner_state is None:
            await self._emit_complete_from_file(rs, complete_path)
            self._cleanup_run(rs.run_id)
            return

        session_id = runner_state.get("session_id")
        jsonl_path_str = runner_state.get("jsonl_path")
        if not session_id or not jsonl_path_str:
            await self._emit_early_failure(
                rs, "runner state.json missing session_id/jsonl_path"
            )
            return

        rs.session_id = session_id
        rs.jsonl_path = Path(jsonl_path_str)
        if not rs.root_id:
            from session_manager import manager as session_manager
            rs.root_id = await asyncio.to_thread(
                session_manager._root_id_for,
                rs.persist_to or rs.app_session_id,
            )

        # 3) Emit synthesized session_discovered.
        try:
            rs.queue.put_nowait(StreamEvent(
                "session_discovered",
                {"session_id": session_id},
            ))
        except Exception:
            logger.exception("failed to enqueue session_discovered")

        # 4) Compute start_offset. The baseline is
        #    `pre_query_byte_offset` from runner's state.json — this is
        #    the byte offset already in the session jsonl BEFORE the
        #    runner sent its query, so slicing here excludes all prior
        #    turns and fixes the duplication bug. If
        #    backend_state.json's `processed_byte` is higher (we're
        #    resuming mid-turn after a backend crash), honor that
        #    instead.
        try:
            pre_query_byte_offset = int(runner_state.get("pre_query_byte_offset") or 0)
        except (TypeError, ValueError):
            pre_query_byte_offset = 0
        start_offset = pre_query_byte_offset
        try:
            current_stat = await run_provider_io_phase_off_loop("bootstrap_stat", rs.jsonl_path.stat)
            pre_query_inode = runner_state.get("pre_query_jsonl_inode")
            if pre_query_inode is not None and int(pre_query_inode) != current_stat.st_ino:
                logger.error(
                    "Claude jsonl inode changed before bootstrap for run %s "
                    "(%s != %s); starting from byte 0",
                    rs.run_id, pre_query_inode, current_stat.st_ino,
                )
                start_offset = 0
            elif current_stat.st_size < start_offset:
                logger.error(
                    "Claude jsonl size regressed before bootstrap for run %s "
                    "(size=%d baseline=%d); starting from byte 0",
                    rs.run_id, current_stat.st_size, start_offset,
                )
                start_offset = 0
        except (OSError, TypeError, ValueError):
            start_offset = 0

        backend_state = await run_provider_io_phase_off_loop("bootstrap_read", self._read_backend_state, rs)
        if backend_state:
            try:
                recovered = int(backend_state.get("processed_byte") or 0)
            except (TypeError, ValueError):
                recovered = 0
            try:
                current_stat = await run_provider_io_phase_off_loop("bootstrap_stat", rs.jsonl_path.stat)
                saved_inode = backend_state.get("jsonl_inode")
                if saved_inode is not None and int(saved_inode) != current_stat.st_ino:
                    logger.error(
                        "Claude jsonl inode changed for run %s (%s != %s); "
                        "ignoring recovered byte cursor",
                        rs.run_id, saved_inode, current_stat.st_ino,
                    )
                    recovered = start_offset
                elif current_stat.st_size < recovered:
                    logger.error(
                        "Claude jsonl size regressed for run %s "
                        "(size=%d cursor=%d); ignoring recovered byte cursor",
                        rs.run_id, current_stat.st_size, recovered,
                    )
                    recovered = start_offset
            except (OSError, TypeError, ValueError):
                recovered = start_offset
            if recovered > start_offset:
                start_offset = recovered
        rs.processed_byte = start_offset

        # 5) Persist the discovered session_id into backend_state.json now
        #    so crash recovery knows which jsonl to tail on restart.
        try:
            await run_provider_io_phase_off_loop("backend_state_commit", self._write_backend_state, rs)
        except Exception as exc:
            await run_provider_io_phase_off_loop("bootstrap_terminate", terminate_failed_run_process, rs)
            await self._emit_early_failure(rs, f"bootstrap persistence failed: {exc}")
            return
        if (
            self._runs.get(rs.run_id) is not rs
            or bool(getattr(rs, "cancelled", False))
            or bool(getattr(rs, "turn_finalized", False))
        ):
            return

        # 6+7) Start the jsonl tailer + completion watcher(s).
        self._start_tailer_and_watchers(rs, start_offset)

    def _start_tailer_and_watchers(
        self, rs: RunState, start_offset: int,
    ) -> None:
        """Start the ClaudeJsonlTailer (tail -F based) + completion
        watcher(s) for `rs`. The tailer dispatch pushes each enriched
        claude line onto the orchestrator queue as an `agent_message`
        StreamEvent; the tailer neither ingests nor pushes WS.

        `_watch_complete` finalizes the turn off complete.json (or
        process exit) and hands a still-alive (winding-down) runner to
        `_watch_process_exit`."""
        from jsonl_tailer import ClaudeJsonlTailer

        # Single-slot holder tagging the just-dispatched StreamEvent (when
        # the line was routed onto the live queue rather than the orphan
        # funnel) with its tailer cursor — see `_on_tailer_progress`. Safe
        # as a single slot: dispatch and the matching on_cursor_advance fire
        # back-to-back, synchronously, from the same tailer read loop with
        # no interleaving dispatch in between.
        pending_cursor_event: list = [None]

        async def _dispatch_to_queue(enriched: dict, _rs: RunState = rs) -> None:
            await self._dispatch_tailer_line(_rs, enriched, pending_cursor_event)

        tailer = ClaudeJsonlTailer(
            path=rs.jsonl_path,
            start_offset=start_offset,
            dispatch=_dispatch_to_queue,
            on_cursor_advance=lambda n, rs=rs: self._on_tailer_progress(
                rs, n, pending_cursor_event,
            ),
        )
        rs.tailer = tailer
        rs.tailer_task = asyncio.get_event_loop().create_task(
            tailer.run(),
            name=f"bridge-tailer-{rs.run_id[:8]}",
        )

        rs.complete_task = asyncio.get_event_loop().create_task(
            self._watch_complete(rs),
            name=f"bridge-complete-{rs.run_id[:8]}",
        )

    async def _dispatch_tailer_line(
        self, rs: RunState, enriched: dict, pending_cursor_event: list,
    ) -> None:
        """Route one tailed jsonl line: live turn → queue; finalized turn
        → orphan funnel."""
        if rs.turn_finalized:
            await self._ingest_late_flush(rs, enriched)
            return
        try:
            stream_event = StreamEvent("agent_message", enriched)
            pending_cursor_event[0] = stream_event
            rs.queue.put_nowait(stream_event)
        except Exception:
            logger.exception(
                "ClaudeJsonlTailer dispatch: put_nowait failed for run %s",
                rs.run_id,
            )

    # ------------------------------------------------------------------
    # Abandoned-queue routing — orphan funnel for lines the turn-loop
    # consumer will never read
    # ------------------------------------------------------------------
    async def _ingest_late_flush(self, rs: RunState, enriched: dict) -> None:
        """Route a CLI line flushed AFTER the turn finalized through
        `strategy.ingest_orphan` (events.jsonl, `msg_id=None`, arms
        reconcile-dirty) — the SRP-paired path for provider-stream
        events with no streaming msg. Never `apply_event`: grafting
        onto a finalized msg is forbidden. Raises on failure so the
        tailer's cursor does not advance past an un-ingested line
        (jsonl_tailer durability contract).

        `rs.mode` (captured at spawn) supplies the orchestration mode.
        Do NOT re-read it via `session_manager.get_lite` here: that
        acquires the per-root lock (and may rehydrate a cold root),
        and during startup run-recovery that lock is held for many
        seconds. Reaching it from this synchronous tailer dispatch
        path froze the main asyncio event loop for up to 17s
        (lag-watchdog dumps pinned to `session_manager.get_lite`).
        `get_strategy` returns one cached strategy for all modes — the
        mode arg only validates — so the spawn-time value is
        equivalent and keeps this hot path lock-free."""
        await asyncio.to_thread(
            self._ingest_orphan_line,
            rs.persist_to or rs.app_session_id,
            rs.run_id,
            enriched,
            mode=rs.mode,
            root_id=rs.root_id,
            cwd=rs.cwd,
        )

    def _ingest_orphan_line(
        self,
        app_sid: str,
        run_id: str,
        enriched: dict,
        *,
        mode: str,
        root_id: Optional[str] = None,
        cwd: str = "",
    ) -> None:
        from orchs import ApplyEventCtx, get_strategy
        if not root_id:
            from session_manager import manager as session_manager
            root_id = session_manager._root_id_for(app_sid) or app_sid
        # `get_strategy` raises on an unknown mode. The live path stores a
        # validated `rs.mode`, but the crash-recovery RunState is rebuilt
        # from persisted `backend_state.json` without re-validation; a
        # corrupt/stale value there must NOT stall the tailer (this method
        # raises to block cursor advance per the jsonl durability
        # contract). Fall back to "team" — matches the old `get_lite(...)
        # .get("orchestration_mode") or "team"` resilience, and
        # `get_strategy` returns one cached strategy for every valid mode.
        if mode not in ("team", "manager", "native"):
            mode = "team"
        get_strategy(mode).ingest_orphan(
            app_session_id=app_sid,
            event={"type": "agent_message", "data": enriched},
            ctx=ApplyEventCtx(
                root_id=root_id,
                run_id=run_id,
                cwd_override=cwd or None,
            ),
            source_is_provider_stream=True,
        )

    def release_queue(
        self, run_id: str, queue: asyncio.Queue, *, persist_to: str,
    ) -> None:
        """Consumer-exit handoff: the turn loop stops reading `queue`
        after this call — for ANY exit reason (complete/error/cancel/
        timeout/exception). Flip the late-flush gate so subsequent
        tailer dispatches orphan-ingest directly, then drain everything
        still queued through the same funnel so no exit path strands
        already-dispatched lines (their cursor advance is persisted —
        recovery would never replay them)."""
        rs = self._runs.get(run_id)
        if rs is not None:
            rs.turn_finalized = True
            persist_to = rs.persist_to or rs.app_session_id
        # Spawn-time mode from the run record; fallback "team" only when
        # the run is already gone (rs None). See `_ingest_late_flush` for
        # why this must NOT re-read orchestration_mode via get_lite.
        mode = rs.mode if rs is not None else "team"
        while True:
            try:
                ev = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            if ev.type != "agent_message":
                continue
            try:
                self._ingest_orphan_line(
                    persist_to,
                    run_id,
                    ev.data,
                    mode=mode,
                    root_id=rs.root_id if rs is not None else None,
                    cwd=rs.cwd if rs is not None else "",
                )
            except Exception:
                # Keep draining: the remaining lines are independent
                # salvage; one failed write must not strand the rest.
                logger.exception(
                    "release_queue: orphan ingest failed for run %s",
                    run_id,
                )

    # ------------------------------------------------------------------
    # _watch_complete — wait for turn end, drain tailer, emit complete.
    #
    # complete.json is the turn-end authority: the runner writes it just
    # before disconnecting, so the turn finalizes while the process is
    # still winding down. Process exit (with a grace wait for a
    # crash-window complete.json) remains the fallback for runners that
    # die without writing it.
    # ------------------------------------------------------------------
    async def _watch_complete(self, rs: RunState) -> None:
        complete_path = rs.run_dir / "complete.json"
        cleanup = True
        try:
            while True:
                if await path_exists_off_loop(complete_path):
                    break
                # No heartbeat-based stuck detection — a live process is
                # assumed to be doing useful work (long tool calls, model
                # thinking, network waits). The user can stop via the UI.
                if not await popen_is_running_off_loop(rs.popen):
                    loop = asyncio.get_event_loop()
                    grace_end = loop.time() + (_TAIL_POLL_INTERVAL * 6)
                    while (
                        not await path_exists_off_loop(complete_path)
                        and loop.time() < grace_end
                    ):
                        await asyncio.sleep(_TAIL_POLL_INTERVAL)
                    break
                await asyncio.sleep(_TAIL_POLL_INTERVAL)

            # Drain the tailer to the current end of the jsonl before
            # firing `complete` — deterministic, not a fixed sleep, so a
            # late-flushed final line is captured under its msg_id first.
            # Read complete.json BEFORE draining: its
            # `final_assistant_text` lets the drain wait for the CLI's
            # final assistant line, which can be flushed AFTER
            # complete.json exists (post-Result flush) — a one-shot
            # size snapshot would miss it, the consumer would break on
            # `complete`, and the line would fall to the orphan funnel
            # (journal-only), leaving the render tree stale.
            from runs_dir import read_best_complete
            best = await run_provider_poll_off_loop(
                read_best_complete, rs.run_dir,
            )
            expected_final_text = None
            if isinstance(best, dict) and best.get("success"):
                _fat = best.get("final_assistant_text")
                if isinstance(_fat, str) and _fat:
                    expected_final_text = _fat
            await self._await_tailer_drained(
                rs, expected_final_text=expected_final_text,
            )

            if await popen_is_running_off_loop(rs.popen) and await path_exists_off_loop(complete_path):
                # Turn done, process still alive (per-turn runner winding
                # down). Tailer lifetime = process lifetime (late
                # post-Result CLI flushes keep flowing) and the run stays
                # registered so the cancel/kill levers keep resolving it
                # and the wind-down gate defers a colliding --resume.
                # Cleanup happens in _watch_process_exit when the process
                # exits.
                await self._emit_complete_from_file(rs, complete_path, payload=best)
                rs.complete_task = asyncio.get_event_loop().create_task(
                    self._watch_process_exit(rs),
                    name=f"bridge-exit-{rs.run_id[:8]}",
                )
                cleanup = False
                return

            if rs.tailer is not None:
                rs.tailer.stop()
            if rs.tailer_task is not None:
                try:
                    await asyncio.wait_for(rs.tailer_task, timeout=2.0)
                except asyncio.TimeoutError:
                    logger.warning("tailer did not exit in time for %s", rs.run_id)
                except Exception:
                    logger.exception("tailer task failed for %s", rs.run_id)

            await self._emit_complete_from_file(rs, complete_path, payload=best)
        finally:
            if cleanup:
                self._cleanup_run(rs.run_id)

    # ------------------------------------------------------------------
    # _watch_process_exit — wind-down epilogue. The turn is finalized but
    # the runner process is still briefly alive (normal per-turn
    # shutdown). Wait for exit, drain+stop the tailer, deregister the
    # run (which fires `released` for the wind-down gate).
    # ------------------------------------------------------------------
    async def _watch_process_exit(self, rs: RunState) -> None:
        try:
            while await popen_is_running_off_loop(rs.popen):
                await asyncio.sleep(_TAIL_POLL_INTERVAL)
            await self._await_tailer_drained(rs)
            if rs.tailer is not None:
                rs.tailer.stop()
            if rs.tailer_task is not None:
                try:
                    await asyncio.wait_for(rs.tailer_task, timeout=2.0)
                except asyncio.TimeoutError:
                    logger.warning("tailer did not exit in time for %s", rs.run_id)
                except Exception:
                    logger.exception("tailer task failed for %s", rs.run_id)
        finally:
            # Task cancellation (backend shutdown) must not skip
            # `_cleanup_run` — a skipped cleanup leaves `released` unset
            # and wedges start_run's wind-down gate.
            self._cleanup_run(rs.run_id)

    # ------------------------------------------------------------------
    # _emit_complete_from_file — read complete.json and enqueue complete
    # ------------------------------------------------------------------
    async def _emit_complete_from_file(
        self, rs: RunState, complete_path: Path,
        *, synthetic_error: Optional[str] = None,
        payload: Optional[dict] = None,
    ) -> None:
        default_msg = synthetic_error or "runner exited without writing complete.json"
        result: dict[str, Any] = {
            "success": False,
            "error": default_msg,
            "session_id": rs.session_id,
            "token_usage": None,
        }
        # Prefer run-level complete.json; fall back to the latest per-turn
        # complete.json so a turn that succeeded but whose runner exited
        # before writing the run-level file (crash in the finalize gap)
        # still surfaces its real output instead of the synthetic "no
        # complete.json" error. When synthetic_error is set, override
        # success to False and stamp the error — a stale complete.json from
        # a prior turn must not mask the real reason the run ended.
        # `payload` short-circuits the read when the caller already read
        # the best complete (the drain used it as its final-text target —
        # emitting the SAME payload keeps drain and complete consistent).
        best = payload
        if best is None:
            from runs_dir import read_best_complete
            best = await run_provider_poll_off_loop(read_best_complete, rs.run_dir)
        if best is not None:
            if synthetic_error:
                best["success"] = False
                best["error"] = synthetic_error
            result = best
        rs.turn_finalized = True
        try:
            activity_state = await read_runner_activity_state(rs.run_dir)
            if activity_state is not None:
                rs.queue.put_nowait(StreamEvent("activity_state", activity_state))
            rs.queue.put_nowait(StreamEvent("complete", result))
        except Exception:
            logger.exception("failed to enqueue complete for %s", rs.run_id)

    # ------------------------------------------------------------------
    # _emit_early_failure — synthesize error + complete on startup failure
    # ------------------------------------------------------------------
    async def _emit_early_failure(self, rs: RunState, msg: str) -> None:
        logger.warning("start_run bootstrap failure for %s: %s", rs.run_id, msg)
        rs.turn_finalized = True
        try:
            rs.queue.put_nowait(StreamEvent("error", {"error": msg}))
            rs.queue.put_nowait(StreamEvent("complete", {
                "success": False,
                "error": msg,
                "session_id": None,
                "token_usage": None,
            }))
        except Exception:
            logger.exception("failed to enqueue early failure for %s", rs.run_id)
        self._cleanup_run(rs.run_id)

    # ------------------------------------------------------------------
    # _on_tailer_progress — called from FileTailer after each line dispatched
    # ------------------------------------------------------------------
    def _on_tailer_progress(
        self, rs: RunState, processed_byte: int, pending_cursor_event: list,
    ) -> None:
        # Called synchronously from the tailer's read loop — MUST stay
        # non-blocking. `rs.processed_byte` updates immediately (cheap,
        # in-memory; this is what the deterministic drain polls) — it is
        # NOT persisted here.
        rs.processed_byte = processed_byte
        pending = pending_cursor_event[0]
        if pending is not None:
            # Line was routed onto the live queue for deferred consumption:
            # tag it so `ack_applied_cursor` can persist this cursor only
            # once the consumer actually applies it (never at read time) —
            # a restart before it's applied must not skip it.
            pending.cursor = processed_byte
            pending_cursor_event[0] = None
            return
        # Line was routed through the synchronous orphan funnel
        # (`_ingest_late_flush`, which raises on failure to block cursor
        # advance) — dispatch succeeding already means it was applied, so
        # it's safe to persist eagerly here as before.
        rs.applied_byte = processed_byte
        from cursor_ledger_worker import worker as cursor_ledger_worker
        cursor_ledger_worker.note(rs.run_id, lambda: self._write_backend_state(rs))

    async def _await_tailer_drained(
        self, rs: RunState, *, timeout: float = 5.0,
        expected_final_text: Optional[str] = None,
    ) -> bool:
        """Block until the tailer has consumed EVERY line currently in the
        claude session jsonl — the deterministic replacement for the old
        fixed `sleep(0.2)` drain guess.

        The tailer (`rs.tailer`) runs in THIS process and advances
        `rs.processed_byte` (an absolute byte cursor over the jsonl)
        as it dispatches each line.

        Without `expected_final_text`: snapshot the file size once and
        wait until the tailer's cursor reaches it.

        With `expected_final_text` (complete.json's
        `final_assistant_text` on a successful turn): the CLI can flush
        the turn's final assistant line AFTER complete.json exists
        (post-Result flush), so a one-shot size snapshot can miss it —
        the line would then reach events.jsonl only via the orphan
        funnel and never the render tree, leaving `msg.content` stale
        for waiters like `ask_team_message`. Wait until the jsonl
        contains a primary assistant line carrying that exact text
        block, then wait for the cursor to cover the END of the LAST
        such line (last match — the model may have emitted identical
        text earlier in the turn). The byte target is fixed once the
        line is on disk, so a chatty jsonl cannot keep the drain alive.

        Returns True on drain, False on timeout (degraded fallback — fire
        anyway so a wedged tailer/CLI can't hang the turn forever)."""
        if rs.jsonl_path is None:
            return True
        try:
            initial_eof = _jsonl_size_bytes(rs.jsonl_path)
        except OSError:
            return True
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        scan_from = rs.tailer.start_offset if rs.tailer is not None else 0
        final_line_end: Optional[int] = None
        if expected_final_text:
            final_line_end, scan_from = await asyncio.to_thread(
                _scan_for_final_text,
                rs.jsonl_path, scan_from, expected_final_text,
            )
        while True:
            if expected_final_text and final_line_end is None:
                # Final line not on disk yet — rescan new bytes and keep
                # the byte target at the current EOF meanwhile.
                found_end, scan_from = await asyncio.to_thread(
                    _scan_for_final_text,
                    rs.jsonl_path, scan_from, expected_final_text,
                )
                if found_end is not None:
                    final_line_end = found_end
            wait_target = initial_eof
            if expected_final_text and final_line_end is not None:
                wait_target = final_line_end
            if rs.processed_byte >= wait_target and (
                not expected_final_text or final_line_end is not None
            ):
                await self._flush_cursor_ledger(rs)
                return True
            if loop.time() >= deadline:
                gap = max(0, wait_target - rs.processed_byte)
                log = logger.error if gap > 0 else logger.warning
                tailer = rs.tailer
                tailer_task = rs.tailer_task
                final_text_hash = (
                    hashlib.sha256(expected_final_text.encode()).hexdigest()[:16]
                    if expected_final_text else None
                )
                log(
                    "tailer drain timeout run=%s processed=%d target=%d gap=%d "
                    "initial_eof=%d final_line_end=%s final_text_sha256=%s "
                    "tailer_cursor=%s tailer_task_done=%s tailer_stopped=%s "
                    "(firing complete anyway)",
                    rs.run_id, rs.processed_byte, wait_target, gap, initial_eof,
                    final_line_end, final_text_hash,
                    getattr(tailer, "processed_offset", None),
                    tailer_task.done() if tailer_task is not None else None,
                    tailer._stop_event.is_set() if tailer is not None else None,
                )
                if gap > 0:
                    perf.record_count("tailer.drain_timeout_gap_bytes", gap)
                await self._flush_cursor_ledger(rs)
                return False
            await asyncio.sleep(_TAIL_POLL_INTERVAL)

    async def _flush_cursor_ledger(self, rs: RunState) -> None:
        """Block until `cursor_ledger_worker` has written this run's
        latest known cursor to `backend_state.json`, once a drain
        concludes — crash recovery must see the true final cursor, not
        whatever was last coalesced. Off-loop so the event loop itself
        never blocks on the write."""
        from cursor_ledger_worker import worker as cursor_ledger_worker
        await asyncio.to_thread(cursor_ledger_worker.flush_now, rs.run_id)

    # _backend_state_path / _read_backend_state inherited from
    # AbstractStreamingProvider. is_running / cancel_all / active_runs /
    # runs_for_session / cancel_run all inherited.

    def _write_backend_state(self, rs: RunState) -> None:
        """Provider-specific backend_state.json contents."""
        jsonl_inode = None
        if rs.jsonl_path and rs.jsonl_path.exists():
            try:
                jsonl_inode = rs.jsonl_path.stat().st_ino
            except OSError:
                jsonl_inode = None
        data = {
            "run_id": rs.run_id,
            "app_session_id": rs.app_session_id,
            "persist_to": rs.persist_to or rs.app_session_id,
            "mode": rs.mode,
            "runner_pid": rs.popen.pid,
            "started_at": rs.started_at,
            "session_id": rs.session_id,
            "jsonl_path": str(rs.jsonl_path) if rs.jsonl_path else None,
            # Durable resume cursor: `applied_byte`, NOT the eager read
            # cursor `processed_byte` — see RunState.applied_byte.
            "processed_byte": rs.applied_byte,
            "jsonl_inode": jsonl_inode,
            "cancelled": rs.cancelled,
            "target_message_id": rs.target_message_id,
            "turn_run_id": rs.turn_run_id,
            "lifecycle_msg_id": rs.lifecycle_msg_id,
            "root_id": rs.root_id,
            "cwd": rs.cwd,
            "ingestion_version": CLAUDE_INGESTION_VERSION,
            # Stamp the owning provider so cross-provider recovery can
            # dispatch this run dir to the right Provider instance.
            "provider_id": self.id,
        }
        try:
            _atomic_write_json(self._backend_state_path(rs), data)
            if rs.session_id:
                import spawn_ledger
                spawn_ledger.record_discovered(rs.session_id)
        except Exception:
            logger.exception("failed to write backend_state.json for %s", rs.run_id)
            raise

    def ack_applied_cursor(self, run_id: str, cursor: Optional[int]) -> None:
        if cursor is None:
            return
        rs = self._runs.get(run_id)
        if rs is None or cursor <= rs.applied_byte:
            return
        rs.applied_byte = cursor
        from cursor_ledger_worker import worker as cursor_ledger_worker
        cursor_ledger_worker.note(run_id, lambda: self._write_backend_state(rs))

    def attach_recovered_run(
        self,
        *,
        desc: dict,
        queue: asyncio.Queue,
        loop: asyncio.AbstractEventLoop,
    ) -> RecoveryAttachReceipt:
        """Re-attach a still-running detached Claude runner after restart.

        `recover_in_flight` only classifies the on-disk run. This method
        rebuilds the provider-side RunState and restarts the same
        state.json bootstrap, Claude jsonl tailer, and completion watcher
        used by a live spawn. That keeps post-restart
        provider-stream events flowing immediately instead of waiting for a
        later cold replay after complete.json appears.
        """
        run_id = str(desc.get("run_id") or "")
        pid = desc.get("pid")
        if (
            not run_id or not pid or run_id in self._runs
            or run_id in self._recovery_attach_pending
        ):
            return RecoveryAttachReceipt(None, lambda: False)
        try:
            runner_pid = int(pid)
        except (TypeError, ValueError):
            return RecoveryAttachReceipt(None, lambda: False)
        try:
            processed_byte = int(desc.get("processed_byte") or 0)
        except (TypeError, ValueError):
            processed_byte = 0

        rs = RunState(
            run_id=run_id,
            run_dir=_runs_root() / run_id,
            popen=RecoveredPopen(runner_pid),
            mode=desc.get("mode") or "native",
            app_session_id=desc.get("app_session_id") or "",
            queue=queue,
            session_id=desc.get("session_id"),
            jsonl_path=Path(desc["jsonl_path"]) if desc.get("jsonl_path") else None,
            processed_byte=processed_byte,
            applied_byte=processed_byte,
            started_at=desc.get("started_at") or datetime.now().isoformat(),
            cancelled=bool(desc.get("cancelled", False)),
            persist_to=desc.get("persist_to") or desc.get("app_session_id") or "",
            target_message_id=desc.get("target_message_id"),
            turn_run_id=desc.get("turn_run_id"),
            lifecycle_msg_id=desc.get("lifecycle_msg_id"),
            root_id=desc.get("root_id"),
            cwd=str(desc.get("cwd") or ""),
        )
        rs.recovered_attach = True
        if self._lifecycle is None:
            self._lifecycle = RunLifecycleCoordinator(loop)
        self._recovery_attach_pending.add(run_id)
        self._recovery_pending_states[run_id] = rs
        task = schedule_loop_task(
            loop,
            self._admit_recovered_run(rs),
            name=f"claude-recover-bootstrap-{run_id[:8]}",
        )
        if task is not None:
            self._lifecycle_spawn_tasks.add(task)

            def done(completed: asyncio.Task) -> None:
                self._lifecycle_spawn_tasks.discard(completed)
                self._recovery_attach_pending.discard(run_id)

            task.add_done_callback(done)
        if task is None:
            self._recovery_attach_pending.discard(run_id)
            self._recovery_pending_states.pop(run_id, None)
        return RecoveryAttachReceipt(
            task,
            lambda: self._runs.get(run_id) is rs
            and rs.tailer_task is not None and not rs.tailer_task.done()
            and rs.complete_task is not None and not rs.complete_task.done(),
        )

    async def _admit_recovered_run(self, rs: RunState) -> None:
        lifecycle = self._lifecycle
        if lifecycle is None:
            raise RuntimeError("Claude lifecycle coordinator is unavailable")
        admission = await lifecycle.admit(rs.run_id)
        if not admission.accepted or admission.token is None:
            if admission.outcome is LifecycleOutcome.SHUTDOWN:
                self._recovery_pending_states.pop(rs.run_id, None)
                await run_provider_io_phase_off_loop(
                    "claude_recovery_shutdown_terminate",
                    terminate_failed_run_process,
                    rs,
                )
                await run_provider_io_phase_off_loop(
                    "claude_recovery_shutdown_cleanup",
                    self._cleanup_lifecycle_artifacts,
                    rs,
                )
                return
            self._recovery_pending_states.pop(rs.run_id, None)
            raise RuntimeError(
                f"Claude recovered run admission rejected: {admission.outcome.value}"
            )
        token = admission.token
        record = ClaudeLifecycleRecord(
            run_id=rs.run_id,
            cleanup_nonce=uuid.uuid4().hex,
            pid=int(rs.popen.pid),
            run_dir=str(rs.run_dir),
        )
        published_record = None
        try:
            await run_provider_io_phase_off_loop(
                "claude_recovery_seed", self._write_backend_state, rs
            )
            published = await lifecycle.publish(token, record)
            if not published.accepted:
                raise RuntimeError(
                    f"Claude recovered run publish rejected: {published.outcome.value}"
                )
            rs.lifecycle_token = token
            rs.lifecycle_record = record
            published_record = record
            self._lifecycle_runs[record.cleanup_nonce] = rs
            self._recovery_pending_states.pop(rs.run_id, None)
            self._publish_started_run(rs.run_id, rs)
            await self._bootstrap_run(rs)
        except BaseException:
            if published_record is not None:
                await self._cleanup_failed_published_run(
                    lifecycle, token, published_record, rs
                )
            else:
                await lifecycle.rollback(token)
            raise

    # ------------------------------------------------------------------
    # recover_in_flight — startup scan for orphaned runs
    # ------------------------------------------------------------------
    def recover_in_flight(
        self,
        loop: Optional[asyncio.AbstractEventLoop] = None,
        run_id_filter: Optional[set[str]] = None,
    ) -> list[dict]:
        """Scan RUNS_ROOT for in-flight runs (no complete.json) owned
        by THIS provider.

        This provider scan classifies runs and returns descriptors:
          - Iterate child dirs.
          - Skip ones that already have complete.json.
          - When `run_id_filter` is given, skip dirs not in the set
            (cross-provider dispatch passes only this provider's runs).
          - For orphans whose runner pid is dead, synthesize a
            complete.json with an error so the run is no longer "in
            flight".
          - Report live runs so `run_recovery.py` can restore lifecycle
            and finalization tracking.

        Returns a list of descriptors for telemetry/logging.
        """
        del loop  # unused in v1
        recovered: list[dict] = []
        if not _runs_root().exists():
            return recovered

        if config_store.provider_suspended(self.id):
            return recovered

        for child in iter_run_dirs(run_id_filter):
            if marker_matches_current(child / "reconciled.marker", self.KIND):
                continue
            complete_path = child / "complete.json"
            has_complete_json = complete_path.exists()

            runner_state_path = child / "state.json"
            pid_path = child / "pid"
            backend_state_path = child / "backend_state.json"

            def _read_safe(path: Path, parser):
                """Best-effort read+parse — None if file missing or parser
                raises. Collapses the 3 inline `if exists: try parse
                except: None` blocks the recover scan repeats per run."""
                if not path.exists():
                    return None
                try:
                    return parser(path.read_text(encoding="utf-8"))
                except Exception:
                    return None

            runner_state: Optional[dict] = _read_safe(runner_state_path, json.loads)
            backend_state: Optional[dict] = _read_safe(backend_state_path, json.loads)
            pid: Optional[int] = _read_safe(pid_path, lambda s: int(s.strip()))
            if pid is None and backend_state:
                try:
                    pid = int(backend_state.get("runner_pid")) if backend_state.get("runner_pid") else None
                except (TypeError, ValueError):
                    pid = None

            alive = _pid_alive(pid) if pid else False

            bs = backend_state or {}
            session_id = (runner_state or {}).get("session_id") or bs.get("session_id")
            jsonl_path = (runner_state or {}).get("jsonl_path") or bs.get("jsonl_path")
            try:
                processed_byte = int(bs.get("processed_byte") or 0)
            except (TypeError, ValueError):
                processed_byte = 0

            cli_pid_raw = (runner_state or {}).get("cli_pid") or bs.get("cli_pid")
            try:
                cli_pid = int(cli_pid_raw) if cli_pid_raw else None
            except (TypeError, ValueError):
                cli_pid = None
            # Wrapper pid dead but the provider CLI it spawned is still alive
            # and writing its session jsonl: the turn is still running (the
            # wrapper died uncontrolled — crash/OOM — NOT via cancel_run, which
            # sweeps the CLI tree). Re-attach instead of declaring it dead.
            # Corroborated against the jsonl to reject a recycled pid.
            from runs_dir import cli_liveness_corroborated
            orphaned_cli = (
                not alive
                and not has_complete_json
                and cli_liveness_corroborated(
                    cli_pid, jsonl_path, bs.get("jsonl_inode"), processed_byte,
                )
            )

            descriptor = {
                "run_id": child.name,
                "pid": pid,
                "alive": alive,
                "session_id": session_id,
                "jsonl_path": jsonl_path,
                "app_session_id": bs.get("app_session_id"),
                "persist_to": bs.get("persist_to") or bs.get("app_session_id"),
                # Run-start wall clock, stamped at RunState construction
                # (so it exists even for a run that crashed before
                # computing pre_query_byte_offset). backend_state.json is
                # the authoritative source; state.json is the fallback
                # for legacy dirs written before this field.
                "started_at": bs.get("started_at")
                or (runner_state or {}).get("started_at")
                or "",
                "processed_byte": processed_byte,
                "jsonl_inode": bs.get("jsonl_inode"),
                "cancelled": bool(bs.get("cancelled", False)),
                "mode": bs.get("mode"),
                "has_complete_json": has_complete_json,
                # Owning provider for the run. Falls back to `self.id`
                # when backend_state.json predates this field — that's
                # legacy data, the dispatcher already routed it here so
                # claiming ownership is safe.
                "provider_id": bs.get("provider_id") or self.id,
                "provider_kind": self.KIND,
                "ingestion_version": bs.get("ingestion_version"),
                "target_message_id": bs.get("target_message_id"),
                "turn_run_id": bs.get("turn_run_id"),
                "lifecycle_msg_id": bs.get("lifecycle_msg_id"),
                "cli_pid": cli_pid,
                "orphaned_cli": bool(orphaned_cli),
            }

            if has_complete_json:
                descriptor["recovered_as"] = "already_complete"
            elif orphaned_cli:
                logger.info(
                    "recover_in_flight: wrapper dead but CLI still live for "
                    "%s (cli_pid=%s) — re-attaching to the running CLI",
                    child.name, cli_pid,
                )
                descriptor["recovered_as"] = "live_no_rehook"
            elif not alive:
                # Orphan: pid is dead and no run-level complete.json
                # exists. If a per-turn complete.json survived (turn
                # succeeded but the runner died before the run-level
                # write), promote it to run-level so the real output is
                # recovered instead of synthesizing an error. Only when
                # nothing survived do we write the synthetic error.
                from runs_dir import read_best_complete
                recovered_payload = read_best_complete(child)
                synth = {
                    "success": False,
                    "session_id": descriptor["session_id"],
                    "error": "runner died before completion (recovered at startup)",
                    "token_usage": None,
                    "finished_at": datetime.now().isoformat(),
                }
                try:
                    (child / "complete.json").write_text(json.dumps(
                        recovered_payload if recovered_payload is not None else synth,
                        indent=2,
                    ), encoding="utf-8")
                    logger.info(
                        "recover_in_flight: marked dead orphan %s",
                        child.name,
                    )
                    descriptor["has_complete_json"] = True
                except Exception:
                    logger.exception(
                        "recover_in_flight: failed to write synthetic complete.json for %s",
                        child.name,
                    )
                descriptor["recovered_as"] = "dead_orphan"
            else:
                logger.info(
                    "recover_in_flight: found live in-flight run %s (pid=%s) — "
                    "queued for run_recovery integration",
                    child.name, pid,
                )
                descriptor["recovered_as"] = "live_no_rehook"

            recovered.append(descriptor)

        return recovered

    # ------------------------------------------------------------------
    # prune_old_runs — delete complete-and-old run dirs
    # ------------------------------------------------------------------
    def prune_old_runs(self, max_age_days: int = 7) -> int:
        removed = prune_old_completed_runs(max_age_days)
        if removed:
            logger.info("pruned %d old run dirs", removed)
        return removed

    # ------------------------------------------------------------------
    # run_headless — one-shot `claude -p --output-format json`
    # ------------------------------------------------------------------
    async def run_headless(
        self,
        *,
        prompt: str,
        session_id: Optional[str] = None,
        resume_sid: Optional[str] = None,
        fork: bool = False,
        cwd: Optional[str] = None,
        timeout: Optional[float] = None,
        no_tools: bool = False,
    ) -> Optional[dict]:
        """Run `claude -p --output-format json` headless and return the
        parsed JSON envelope. Returns None on spawn / parse / timeout
        failure (logged with details). The CLI's own `is_error: true`
        is preserved in the returned dict — caller decides how to react.

        `no_tools=True` passes `--tools ""` so the CLI exposes ZERO
        built-in tools — the run can only produce text, never touch the
        filesystem or shell. Used for pure-generation callers (composer
        fill) that must not side-effect the user's workspace.
        """
        self.assert_not_suspended(action="run headless work")
        from cli_paths import resolve_cli_binary

        claude_bin = resolve_cli_binary("claude")
        if not claude_bin:
            logger.error("ClaudeProvider.run_headless: `claude` CLI not found on PATH")
            return None
        cmd: list[str] = [
            claude_bin,
            "-p",
            "--output-format", "json",
            "--permission-mode", "bypassPermissions",
            "--input-format", "text",
        ]
        if no_tools:
            cmd += ["--tools="]
        if session_id is not None:
            cmd += ["--session-id", session_id]
        if resume_sid is not None:
            cmd += ["--resume", resume_sid]
        if fork:
            cmd += ["--fork-session"]
        cmd += [prompt]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self.build_env(),
                cwd=cwd,
            )
        except FileNotFoundError:
            logger.error("ClaudeProvider.run_headless: `claude` CLI not found on PATH")
            return None
        except Exception:
            logger.exception("ClaudeProvider.run_headless: failed to spawn claude CLI")
            return None

        try:
            if timeout is not None:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout
                )
            else:
                stdout_bytes, stderr_bytes = await proc.communicate()
        except asyncio.TimeoutError:
            logger.error(
                "ClaudeProvider.run_headless: timeout after %ss", timeout
            )
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            return None

        stdout = stdout_bytes.decode(errors="replace").strip()
        if not stdout:
            if proc.returncode != 0:
                logger.error(
                    "ClaudeProvider.run_headless: CLI exited %s; stderr=%r",
                    proc.returncode, stderr_bytes[:500],
                )
                return None
            logger.error("ClaudeProvider.run_headless: CLI produced no stdout")
            return None

        try:
            parsed = json.loads(stdout)
        except json.JSONDecodeError:
            if proc.returncode != 0:
                logger.error(
                    "ClaudeProvider.run_headless: CLI exited %s; stderr=%r",
                    proc.returncode, stderr_bytes[:500],
                )
                return None
            logger.error(
                "ClaudeProvider.run_headless: stdout not JSON: %r", stdout[:500]
            )
            return None
        if proc.returncode != 0:
            logger.error(
                "ClaudeProvider.run_headless: CLI exited %s with JSON result; stderr=%r",
                proc.returncode, stderr_bytes[:500],
            )
        return parsed

    # ------------------------------------------------------------------
    # Rate-limit parsing — Claude emits specific reset-time formats.
    # ------------------------------------------------------------------
    def parse_rate_limit(
        self, error: Optional[str], events: list[dict],
    ) -> Optional[datetime]:
        corpus = build_corpus(error, events, self._extract_text_for_rate_limit)
        return parse_provider_rate_limit("claude", corpus)

    # ------------------------------------------------------------------
    # rewind — `claude --resume <sid> --rewind-files <uuid>`
    # ------------------------------------------------------------------
    async def rewind(self, claude_sid: str, message_uuid: str) -> None:
        """Invoke `claude --resume <sid> --rewind-files <uuid>` to undo
        the file edits the turn at `message_uuid` produced. Raises
        `RuntimeError` on non-zero exit so the caller can surface the
        exact stderr to the UI.
        """
        from cli_paths import resolve_cli_binary

        claude_bin = resolve_cli_binary("claude")
        if not claude_bin:
            raise RuntimeError("claude --rewind-files failed: `claude` CLI not found on PATH")
        proc = await asyncio.create_subprocess_exec(
            claude_bin, "--resume", claude_sid, "--rewind-files", message_uuid,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self.build_env(),
        )
        _, stderr_b = await proc.communicate()
        if proc.returncode != 0:
            stderr = stderr_b.decode("utf-8", errors="replace")
            raise RuntimeError(
                f"claude --rewind-files failed (exit {proc.returncode}): {stderr}"
            )


# `_pid_alive` lives in `runs_dir` — see re-export at top of this file.
