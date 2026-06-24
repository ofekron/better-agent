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
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, ClassVar, Optional

from event_bus import BusEvent, bus
from env_compat import get_env
from provider import Provider, StreamEvent, build_better_agent_run_env, create_loop_task, runner_argv
from provider_env import is_ollama_base_url
from reasoning_effort import CLAUDE_REASONING_EFFORTS, DEFAULT_REASONING_EFFORT

logger = logging.getLogger(__name__)


# ============================================================================
# Constants
# ============================================================================
from paths import ba_home


# Re-exports for back-compat with run_recovery + any out-of-tree
# code that imported these from provider_claude. New code should
# import from `runs_dir` directly.
from runs_dir import runs_root as _runs_root
import perf
from runs_dir import atomic_write_json as _atomic_write_json
from runs_dir import pid_alive as _pid_alive
from proc_control import process_control as _process_control
from ingestion_versions import CLAUDE_INGESTION_VERSION, marker_matches_current


_RUNNER_PATH = Path(__file__).parent / "runner.py"
_TAIL_POLL_INTERVAL = 0.05       # seconds between empty-read polls


def _jsonl_size_bytes(path: Path) -> int:
    return path.stat().st_size

DEFAULT_DISALLOWED_TOOLS = [
    "AskUserQuestion",
    "EnterPlanMode",
    "ExitPlanMode",
]
# In-process CLI timer tools are replaced by the backend-owned durable
# scheduler (stores/schedule_store.py + the runner's `scheduler` MCP
# server). Stripped on EVERY spawn: with no timers and no stdin input, a
# lingering (babysitter) runner can never start a turn of its own, so it
# can't race a fresh --resume instance on the shared session jsonl
# (lifecycle tests T16/T17). The runner refuses to spawn if these are
# missing from input.json. Single source: runs_dir.TIMER_TOOLS.
from runs_dir import TIMER_TOOLS


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
    tailer: Optional["ClaudeJsonlTailer"] = None
    tailer_task: Optional[asyncio.Task] = None
    bootstrap_task: Optional[asyncio.Task] = None
    complete_task: Optional[asyncio.Task] = None
    worker_tailers: dict[str, "ClaudeJsonlTailer"] = field(default_factory=dict)
    started_at: str = ""
    cancelled: bool = False
    persist_to: str = ""  # session messages are persisted to (differs from app_session_id in supervisor mode)
    target_message_id: Optional[str] = None
    turn_run_id: Optional[str] = None
    # Babysitter: True between "turn finalized off complete.json" and
    # "runner process actually exited" — the runner is lingering to keep
    # detached background work (bg shells, Monitor) alive. The run stays
    # in `_runs` so the cancel/kill levers keep resolving it; the tailer
    # stays up so late CLI flushes still flow.
    lingering: bool = False
    # True once the queue consumer is gone — set when a terminal event
    # (`complete`/`error`) is enqueued (consumer breaks on it) and by
    # `release_queue` when the consumer exits for any other reason
    # (cancel/timeout/exception). Tailer lines dispatched afterwards
    # bypass the dead queue and go through the orphan funnel
    # (`_ingest_late_flush`).
    turn_finalized: bool = False


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

    def __init__(self, record: dict) -> None:
        super().__init__(record)
        self._runs: dict[str, RunState] = {}
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
        cfg_dir = record.get("config_dir") or ""
        if cfg_dir:
            # expanduser + expandvars: handle both `~/.claude-zai` and
            # `$HOME/.claude-zai`. Without expanduser, claude CLI sees
            # a literal `~` and fails to find the dir.
            env["CLAUDE_CONFIG_DIR"] = os.path.expanduser(
                os.path.expandvars(cfg_dir)
            )
        else:
            env.pop("CLAUDE_CONFIG_DIR", None)
        # Enable file checkpointing for SDK/stream-json mode sessions so
        # --rewind-files works (required for retry/rewind functionality).
        env["CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING"] = "1"
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
        disabled_builtin_extensions: Optional[list[str]] = None,
    ) -> None:
        """Spawn `runner.py` detached and schedule a bootstrap task that,
        as soon as the runner writes `state.json`, starts a `FileTailer`
        on claude's own session jsonl and forwards translated events
        onto `queue`.

        Returns immediately — the run continues in the background even
        if the backend dies.
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

        run_dir = _runs_root() / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

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
        import config_store
        _sess_rec = _sm.get(app_session_id) or {}
        _worker_sess_rec = _sm.get(worker_agent_session_id) if worker_agent_session_id else {}
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
            "session_id": session_id,
            "mode": mode,
            "app_session_id": app_session_id,
            "working_mode": (_sess_rec or {}).get("working_mode"),
            "worker_working_mode": (_worker_sess_rec or {}).get("working_mode"),
            "disallowed_tools": list(dict.fromkeys(
                (disallowed_tools or DEFAULT_DISALLOWED_TOOLS)
                + list(TIMER_TOOLS)
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
            "disabled_builtin_tools": config_store.get_disabled_builtin_tools(),
            "disabled_builtin_extensions": (
                disabled_builtin_extensions
                if disabled_builtin_extensions is not None
                else config_store.get_disabled_builtin_extensions()
            ),
        }
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
            started_at=datetime.now().isoformat(),
            persist_to=worker_agent_session_id or app_session_id,
            target_message_id=target_message_id,
            turn_run_id=turn_run_id,
        )
        self._runs[run_id] = run_state

        # Seed backend_state.json so recovery scanners can see this run.
        self._write_backend_state(run_state)

        run_state.bootstrap_task = create_loop_task(
            loop,
            self._bootstrap_run(run_state),
            name=f"bridge-bootstrap-{run_id[:8]}",
        )

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
            if runner_state_path.exists():
                try:
                    raw = runner_state_path.read_text(encoding="utf-8")
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
            if rs.popen.poll() is not None:
                if complete_path.exists():
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
            current_stat = rs.jsonl_path.stat()
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

        backend_state = self._read_backend_state(rs)
        if backend_state:
            try:
                recovered = int(backend_state.get("processed_byte") or 0)
            except (TypeError, ValueError):
                recovered = 0
            try:
                current_stat = rs.jsonl_path.stat()
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
        self._write_backend_state(rs)

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
        process exit) and hands a still-alive babysitter runner to
        `_watch_linger_exit`."""
        from jsonl_tailer import ClaudeJsonlTailer

        def _dispatch_to_queue(enriched: dict, _rs: RunState = rs) -> None:
            if _rs.turn_finalized:
                self._ingest_late_flush(_rs, enriched)
                return
            try:
                _rs.queue.put_nowait(StreamEvent("agent_message", enriched))
            except Exception:
                logger.exception(
                    "ClaudeJsonlTailer dispatch: put_nowait failed for run %s",
                    _rs.run_id,
                )

        tailer = ClaudeJsonlTailer(
            path=rs.jsonl_path,
            start_offset=start_offset,
            dispatch=_dispatch_to_queue,
            on_cursor_advance=lambda n, rs=rs: self._on_tailer_progress(rs, n),
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

    # ------------------------------------------------------------------
    # Abandoned-queue routing — orphan funnel for lines the turn-loop
    # consumer will never read
    # ------------------------------------------------------------------
    def _ingest_late_flush(self, rs: RunState, enriched: dict) -> None:
        """Route a CLI line flushed AFTER the turn finalized through
        `strategy.ingest_orphan` (events.jsonl, `msg_id=None`, arms
        reconcile-dirty) — the SRP-paired path for provider-stream
        events with no streaming msg. Never `apply_event`: grafting
        onto a finalized msg is forbidden. Raises on failure so the
        tailer's cursor does not advance past an un-ingested line
        (jsonl_tailer durability contract)."""
        self._ingest_orphan_line(
            rs.persist_to or rs.app_session_id, rs.run_id, enriched,
        )

    def _ingest_orphan_line(
        self, app_sid: str, run_id: str, enriched: dict,
    ) -> None:
        from orchs import ApplyEventCtx, get_strategy
        from session_manager import manager as session_manager
        root_id = session_manager._root_id_for(app_sid) or app_sid
        sess = session_manager.get_lite(app_sid) or {}
        mode = sess.get("orchestration_mode") or "team"
        get_strategy(mode).ingest_orphan(
            app_session_id=app_sid,
            event={"type": "agent_message", "data": enriched},
            ctx=ApplyEventCtx(root_id=root_id, run_id=run_id),
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
        while True:
            try:
                ev = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            if ev.type != "agent_message":
                continue
            try:
                self._ingest_orphan_line(persist_to, run_id, ev.data)
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
    # complete.json is the turn-end authority: the babysitter runner
    # writes it BEFORE its linger, so the turn finalizes while the
    # process stays alive keeping detached background work running.
    # Process exit (with a grace wait for a crash-window complete.json)
    # remains the fallback for runners that die without writing it.
    # ------------------------------------------------------------------
    async def _watch_complete(self, rs: RunState) -> None:
        complete_path = rs.run_dir / "complete.json"
        cleanup = True
        try:
            while True:
                if complete_path.exists():
                    break
                # No heartbeat-based stuck detection — a live process is
                # assumed to be doing useful work (long tool calls, model
                # thinking, network waits). The user can stop via the UI.
                if rs.popen.poll() is not None:
                    loop = asyncio.get_event_loop()
                    grace_end = loop.time() + (_TAIL_POLL_INTERVAL * 6)
                    while not complete_path.exists() and loop.time() < grace_end:
                        await asyncio.sleep(_TAIL_POLL_INTERVAL)
                    break
                await asyncio.sleep(_TAIL_POLL_INTERVAL)

            # Drain the tailer to the current end of the jsonl before
            # firing `complete` — deterministic, not a fixed sleep, so a
            # late-flushed final line is captured under its msg_id first.
            await self._await_tailer_drained(rs)

            if rs.popen.poll() is None and complete_path.exists():
                # Turn done, process still alive. Tailer lifetime =
                # process lifetime (late post-Result CLI flushes keep
                # flowing) and the run stays registered so the
                # cancel/kill levers keep resolving it. Whether this is
                # a real babysitter linger (vs. a runner taking a moment
                # to shut down) is decided by the runner itself — it
                # touches `run_dir/lingering` only when detached
                # background work is actually alive; _watch_linger_exit
                # publishes run.lingering off that sentinel. Cleanup
                # happens there when the process exits.
                await self._emit_complete_from_file(rs, complete_path)
                rs.complete_task = asyncio.get_event_loop().create_task(
                    self._watch_linger_exit(rs),
                    name=f"bridge-linger-{rs.run_id[:8]}",
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

            await self._emit_complete_from_file(rs, complete_path)
        finally:
            if cleanup:
                self._cleanup_run(rs.run_id)

    # ------------------------------------------------------------------
    # _watch_linger_exit — babysitter epilogue. The turn is finalized but
    # the runner process is still alive — either briefly (normal
    # shutdown) or babysitting background work (it touched the
    # `lingering` sentinel). Publish run.lingering off the sentinel,
    # wait for exit, drain+stop the tailer, deregister the run.
    # ------------------------------------------------------------------
    async def _watch_linger_exit(self, rs: RunState) -> None:
        lingering_sentinel = rs.run_dir / "lingering"
        try:
            while rs.popen.poll() is None:
                if not rs.lingering and lingering_sentinel.exists():
                    rs.lingering = True
                    await self._publish_lingering(rs, True)
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
            if rs.lingering:
                rs.lingering = False
                await self._publish_lingering(rs, False)
            self._cleanup_run(rs.run_id)

    async def _publish_lingering(self, rs: RunState, lingering: bool) -> None:
        """Publish the babysitter-liveness FACT on the bus; the WS
        forwarding subscriber (main.py) projects it to connected tabs."""
        try:
            await bus.publish(BusEvent(
                type="run.lingering",
                root_id=rs.app_session_id,
                sid=rs.app_session_id,
                payload={
                    "app_session_id": rs.app_session_id,
                    "run_id": rs.run_id,
                    "lingering": lingering,
                },
                run_id=rs.run_id,
                persist=False,
            ))
        except Exception:
            logger.exception("run.lingering publish failed")

    # ------------------------------------------------------------------
    # _emit_complete_from_file — read complete.json and enqueue complete
    # ------------------------------------------------------------------
    async def _emit_complete_from_file(
        self, rs: RunState, complete_path: Path,
        *, synthetic_error: Optional[str] = None,
    ) -> None:
        default_msg = synthetic_error or "runner exited without writing complete.json"
        payload: dict[str, Any] = {
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
        from runs_dir import read_best_complete
        best = read_best_complete(rs.run_dir)
        if best is not None:
            if synthetic_error:
                best["success"] = False
                best["error"] = synthetic_error
            payload = best
        rs.turn_finalized = True
        try:
            rs.queue.put_nowait(StreamEvent("complete", payload))
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
    def _on_tailer_progress(self, rs: RunState, processed_byte: int) -> None:
        rs.processed_byte = processed_byte
        self._write_backend_state(rs)

    async def _await_tailer_drained(
        self, rs: RunState, *, timeout: float = 5.0,
    ) -> bool:
        """Block until the tailer has consumed EVERY line currently in the
        claude session jsonl — the deterministic replacement for the old
        fixed `sleep(0.2)` drain guess.

        The tailer (`rs.tailer`) runs in THIS process and advances
        `rs.processed_byte` (an absolute byte cursor over the jsonl)
        as it dispatches each line. The turn's content is in the jsonl by
        the time `complete.json` exists (the CLI writes the assistant
        message before the SDK result the runner waited on). So we snapshot
        the file size ONCE and wait until the tailer's cursor
        reaches it — guaranteeing the turn's final line was ingested (with
        the owning msg_id) before we fire `complete`. No timer race.

        Returns True on drain, False on timeout (degraded fallback — fire
        anyway so a wedged tailer can't hang the turn forever)."""
        if rs.jsonl_path is None:
            return True
        try:
            target = _jsonl_size_bytes(rs.jsonl_path)
        except OSError:
            return True
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while rs.processed_byte < target:
            if loop.time() >= deadline:
                logger.warning(
                    "tailer drain timeout run=%s processed=%d target=%d "
                    "(firing complete anyway)",
                    rs.run_id, rs.processed_byte, target,
                )
                return False
            await asyncio.sleep(_TAIL_POLL_INTERVAL)
        return True

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
            "processed_byte": rs.processed_byte,
            "jsonl_inode": jsonl_inode,
            "cancelled": rs.cancelled,
            "target_message_id": rs.target_message_id,
            "turn_run_id": rs.turn_run_id,
            "ingestion_version": CLAUDE_INGESTION_VERSION,
            # Stamp the owning provider so cross-provider recovery can
            # dispatch this run dir to the right Provider instance.
            "provider_id": self.id,
        }
        try:
            _atomic_write_json(self._backend_state_path(rs), data)
        except Exception:
            logger.exception("failed to write backend_state.json for %s", rs.run_id)

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

        for child in _runs_root().iterdir():
            if not child.is_dir():
                continue
            if run_id_filter is not None and child.name not in run_id_filter:
                continue
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
            }

            if has_complete_json:
                descriptor["recovered_as"] = "already_complete"
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
        if not _runs_root().exists():
            return 0
        cutoff = datetime.now() - timedelta(days=max_age_days)
        removed = 0
        for child in _runs_root().iterdir():
            if not child.is_dir():
                continue
            complete_path = child / "complete.json"
            if not complete_path.exists():
                continue
            try:
                mtime = datetime.fromtimestamp(complete_path.stat().st_mtime)
            except OSError:
                continue
            if mtime < cutoff:
                try:
                    shutil.rmtree(child)
                    removed += 1
                except OSError as e:
                    logger.warning("prune: failed to rm %s: %s", child, e)
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
    ) -> Optional[dict]:
        """Run `claude -p --output-format json` headless and return the
        parsed JSON envelope. Returns None on spawn / parse / timeout
        failure (logged with details). The CLI's own `is_error: true`
        is preserved in the returned dict — caller decides how to react.
        """
        cmd: list[str] = [
            "claude",
            "-p",
            "--output-format", "json",
            "--permission-mode", "bypassPermissions",
            "--input-format", "text",
        ]
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

        if proc.returncode != 0:
            logger.error(
                "ClaudeProvider.run_headless: CLI exited %s; stderr=%r",
                proc.returncode, stderr_bytes[:500],
            )
            return None

        stdout = stdout_bytes.decode(errors="replace").strip()
        if not stdout:
            logger.error("ClaudeProvider.run_headless: CLI produced no stdout")
            return None

        try:
            return json.loads(stdout)
        except json.JSONDecodeError:
            logger.error(
                "ClaudeProvider.run_headless: stdout not JSON: %r", stdout[:500]
            )
            return None

    # ------------------------------------------------------------------
    # Rate-limit parsing — Claude emits specific reset-time formats.
    # ------------------------------------------------------------------
    _CLAUDE_RATE_LIMIT_KEYWORDS = (
        "limit reached", "rate limit", "status: 429", "error 429",
        "too many requests", "hit your limit", "hit the limit",
        "reached your usage limit", "no more messages",
    )

    _RESET_FULL_RE = re.compile(
        r'resets\s+(\w+)\s+(\d{1,2})\s+at\s+(\d{1,2})(am|pm)',
        re.IGNORECASE,
    )
    _RESET_SHORT_RE = re.compile(
        r'resets\s+(\d{1,2})(am|pm)',
        re.IGNORECASE,
    )

    def parse_rate_limit(
        self, error: Optional[str], events: list[dict],
    ) -> Optional[datetime]:
        """Parse Claude rate-limit reset time from error / event text."""
        # Gather the text corpus to search
        texts: list[str] = []
        if error:
            texts.append(error[-2000:] if len(error) > 2000 else error)
        extracted = self._extract_text_for_rate_limit(events)
        if extracted:
            texts.append(extracted)
        corpus = "\n".join(texts).lower()
        if not corpus:
            return None

        if not any(kw in corpus for kw in self._CLAUDE_RATE_LIMIT_KEYWORDS):
            return None

        now = datetime.now(timezone.utc)

        # "resets Dec 11 at 11pm"
        m = self._RESET_FULL_RE.search(corpus)
        if m:
            try:
                month_str, day_s, hour_s, ampm = m.groups()
                hour = int(hour_s)
                day = int(day_s)
                if ampm.lower() == "pm" and hour != 12:
                    hour += 12
                elif ampm.lower() == "am" and hour == 12:
                    hour = 0
                months = {
                    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
                    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
                }
                month = months.get(month_str[:3].lower(), now.month)
                year = now.year
                # If the computed date is in the past, roll to next year.
                candidate = datetime(year, month, day, hour, 0, 0, tzinfo=timezone.utc)
                if candidate <= now:
                    year += 1
                return datetime(year, month, day, hour, 0, 0, tzinfo=timezone.utc)
            except Exception:
                logger.warning("failed to parse Claude full reset time", exc_info=True)

        # "resets 9pm"
        m = self._RESET_SHORT_RE.search(corpus)
        if m:
            try:
                hour = int(m.group(1))
                if m.group(2).lower() == "pm" and hour != 12:
                    hour += 12
                elif m.group(2).lower() == "am" and hour == 12:
                    hour = 0
                reset = now.replace(hour=hour, minute=0, second=0, microsecond=0)
                if reset <= now:
                    reset += timedelta(days=1)
                return reset
            except Exception:
                logger.warning("failed to parse Claude short reset time", exc_info=True)

        return self._fallback_rate_limit(hours=1)

    # ------------------------------------------------------------------
    # rewind — `claude --resume <sid> --rewind-files <uuid>`
    # ------------------------------------------------------------------
    async def rewind(self, claude_sid: str, message_uuid: str) -> None:
        """Invoke `claude --resume <sid> --rewind-files <uuid>` to undo
        the file edits the turn at `message_uuid` produced. Raises
        `RuntimeError` on non-zero exit so the caller can surface the
        exact stderr to the UI.
        """
        proc = await asyncio.create_subprocess_exec(
            "claude", "--resume", claude_sid, "--rewind-files", message_uuid,
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
