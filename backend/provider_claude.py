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
from provider import (
    Provider,
    RecoveredPopen,
    StreamEvent,
    build_better_agent_run_env,
    path_exists_off_loop,
    popen_is_running_off_loop,
    schedule_loop_task,
    runner_argv,
)
import config_store
from extension_run_policy import disabled_builtin_extensions_for_run
from provider_env import is_ollama_base_url
from reasoning_effort import CLAUDE_REASONING_EFFORTS, DEFAULT_REASONING_EFFORT
import git_policy

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


def _jsonl_size_bytes(path: Path) -> int:
    return path.stat().st_size

DEFAULT_DISALLOWED_TOOLS = [
    "AskUserQuestion",
    "EnterPlanMode",
    "ExitPlanMode",
]
# In-process CLI timer tools are replaced by the backend-owned durable
# scheduler (stores/schedule_store.py + the runner's `scheduler` MCP
# server). Stripped on EVERY spawn so a lingering (babysitter) runner
# can never start a TIMER-driven turn of its own. Note this is NOT a
# full "linger can't run inference" guarantee: background-work
# completion (task notifications, detached shell/Monitor exits) DOES
# re-invoke the model on the lingering CLI as a continuation turn
# (runner._LingerStreamState). Because of that, `start_run` serializes
# any new turn that resumes the SAME native agent session id against a
# live linger (cancel the linger, wait for its release event, then
# spawn) — spawning a second --resume CLI while the lingering instance
# holds the session would cross-process-enqueue the prompt into the
# live instance and return a ghost zero-token result. The runner
# refuses to spawn if these tools are missing from input.json. Single
# source: runs_dir.TIMER_TOOLS.
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
    # Set by Provider._cleanup_run when the run is deregistered (runner
    # exited, linger released). start_run's linger-serialization gate
    # awaits this before spawning a --resume on the same native session.
    released: asyncio.Event = field(default_factory=asyncio.Event)
    # True once the queue consumer is gone — set when a terminal event
    # (`complete`/`error`) is enqueued (consumer breaks on it) and by
    # `release_queue` when the consumer exits for any other reason
    # (cancel/timeout/exception). Tailer lines dispatched afterwards
    # bypass the dead queue and go through the orphan funnel
    # (`_ingest_late_flush`).
    turn_finalized: bool = False
    # ── Prompt handoff (promptable linger) ──
    # A handoff run is a normal top-level run dir whose turn is served by
    # ANOTHER run's lingering runner on its live SDK client — it shares
    # the host's popen but owns its run dir, tailer, and completion
    # watcher.
    is_handoff_turn: bool = False
    # On the handoff run: the hosting (lingering) RunState.
    handoff_host: Optional["RunState"] = None
    # On the handoff run: original start_run kwargs, kept so a rejected /
    # never-picked-up handoff falls back to the cancel+respawn path
    # without losing the prompt.
    handoff_spawn_kwargs: Optional[dict] = None
    # On the host: the in-flight handoff run (the gate serializes further
    # prompts behind its `released` event).
    handoff_target: Optional["RunState"] = None
    # On the host: the handed-off turn's jsonl byte boundary. Host-tailer
    # lines at/after it belong to the handoff run's own tailer and are
    # skipped; lines before it keep orphan-ingesting. None = boundary not
    # yet known (handoff state.json unread) — lines are held in
    # `handoff_hold` until armed.
    handoff_route_from: Optional[int] = None
    handoff_hold: Optional[list] = None
    # Spawn-time snapshots compared by the handoff eligibility check — a
    # live client cannot absorb provider-record (auth/base_url/config_dir)
    # or env changes, so any drift forces a fresh spawn.
    record_version_at_spawn: Optional[int] = None
    extra_env_at_spawn: Optional[dict] = None


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
        # Lingering runs whose runner rejected (or never picked up) a
        # handoff — barred from further handoff attempts so the fallback
        # respawn can't loop back into another handoff.
        self._handoff_barred: set[str] = set()
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
        disabled_builtin_extensions: Optional[list[str]] = None,
        provisioned_tool_profile: str = "",
    ) -> None:
        """Spawn `runner.py` detached and schedule a bootstrap task that,
        as soon as the runner writes `state.json`, starts a `FileTailer`
        on claude's own session jsonl and forwards translated events
        onto `queue`.

        Returns immediately — the run continues in the background even
        if the backend dies. When a babysitter runner is still lingering
        on the SAME native `session_id`, the spawn is deferred: the
        linger is cancelled and the runner subprocess is spawned only
        after the lingering run's release event fires (see
        `_start_after_linger_release`). The prompt is already durably
        queued by the orchestrator, so deferral cannot lose it.
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
            disabled_builtin_extensions=disabled_builtin_extensions,
            provisioned_tool_profile=provisioned_tool_profile,
        )

        # Linger-serialization gate. A babysitter runner lingering on the
        # SAME native session id still owns a live CLI instance that runs
        # continuation turns off task notifications; a second --resume CLI
        # spawned now would cross-process-enqueue the prompt into that
        # instance and return a ghost zero-token success while the
        # continuation orphans the user message. Keyed on the NATIVE
        # agent session id (rs.session_id), never app_session_id — worker
        # forks share the parent's app_session_id but run their own
        # native session. `fork=True` spawns are exempt: `--fork-session`
        # creates a NEW native session id rather than continuing the
        # lingering instance's turn queue, and serializing worker-fork
        # creation behind the parent's linger would stall (and cancel)
        # legitimate parent background work.
        if session_id and not fork:
            # A handoff turn already in flight on this native session:
            # serialize behind its TURN-end release (its completion
            # watcher cleans up at complete.json, not process exit), then
            # re-check the gate — the next prompt may hand off too.
            in_flight = [
                getattr(rs, "handoff_target", None)
                for rs in self._runs.values()
                if getattr(rs, "handoff_target", None) is not None
                and getattr(rs, "session_id", None) == session_id
            ]
            if in_flight:
                logger.info(
                    "start_run: deferring run %s behind in-flight handoff "
                    "turn %s on native session %s",
                    run_id[:8], in_flight[0].run_id[:8], session_id[:8],
                )
                schedule_loop_task(
                    loop,
                    self._start_after_handoff_release(
                        in_flight[0], spawn_kwargs,
                    ),
                    name=f"bridge-handoff-gate-{run_id[:8]}",
                )
                return

            blockers = [
                rs for rs in self._runs.values()
                if getattr(rs, "lingering", False)
                and getattr(rs, "session_id", None) == session_id
            ]
            if (
                len(blockers) == 1
                and self._handoff_precheck(blockers[0], spawn_kwargs) is None
            ):
                # Promptable linger: serve the turn on the lingering
                # runner's live client instead of cancelling its linger
                # (which kills background work) and respawning.
                blocker = blockers[0]
                new_payload, _bare, payload_mode, _ = (
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
                        disabled_builtin_extensions=disabled_builtin_extensions,
                        provisioned_tool_profile=provisioned_tool_profile,
                    )
                )
                reason = self._handoff_payload_diff(blocker, new_payload)
                if reason is None:
                    self._handoff_spawn(
                        blocker, spawn_kwargs, new_payload, payload_mode,
                    )
                    return
                logger.info(
                    "start_run: handoff ineligible for run %s (%s) — "
                    "cancel+respawn", run_id[:8], reason,
                )
            if blockers:
                for blocker in blockers:
                    # Existing wind-down lever: the linger loop sees the
                    # cancel sentinel, lets any in-flight continuation
                    # settle (bounded), sweeps detached work, and exits.
                    self.cancel_turn(blocker.run_id)
                logger.info(
                    "start_run: deferring run %s behind lingering run(s) %s "
                    "on native session %s",
                    run_id[:8],
                    [b.run_id[:8] for b in blockers],
                    session_id[:8],
                )
                schedule_loop_task(
                    loop,
                    self._start_after_linger_release(blockers, spawn_kwargs),
                    name=f"bridge-linger-gate-{run_id[:8]}",
                )
                return

        self._spawn_run(**spawn_kwargs)

    async def _start_after_linger_release(
        self, blockers: list, spawn_kwargs: dict,
    ) -> None:
        """Wait for every blocking lingering run's release event (set by
        `Provider._cleanup_run` when the runner process exits), then
        re-enter `start_run`. Re-entering (rather than spawning directly)
        re-checks the gate, so a linger that re-armed in the meantime is
        cancelled and waited on again — the user prompt always wins over
        continuing to linger. Purely event-driven: no polling, resolves
        the moment the release fires."""
        try:
            waits = [
                blocker.released.wait()
                for blocker in blockers
                if getattr(blocker, "released", None) is not None
            ]
            if waits:
                await asyncio.gather(*waits)
            self.start_run(**spawn_kwargs)
        except Exception as e:
            logger.exception(
                "deferred start after linger release failed for run %s",
                spawn_kwargs.get("run_id"),
            )
            try:
                spawn_kwargs["queue"].put_nowait(StreamEvent("complete", {
                    "success": False,
                    "error": f"deferred start after linger release failed: {e}",
                    "session_id": spawn_kwargs.get("session_id"),
                    "token_usage": None,
                }))
            except Exception:
                logger.exception(
                    "failed to enqueue deferred-start failure for run %s",
                    spawn_kwargs.get("run_id"),
                )

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
        disabled_builtin_extensions: Optional[list[str]],
        provisioned_tool_profile: str,
    ) -> tuple[dict, bool, str, str]:
        """Single source for the runner's input.json payload — used by the
        spawn body AND by the handoff eligibility diff, so the two can
        never drift. Returns `(payload, bare, mode, resolved_backend_url)`
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

    # ------------------------------------------------------------------
    # Prompt handoff — serve a new turn on a lingering runner's live
    # client instead of cancelling its linger (which kills bg work) and
    # spawning a colliding --resume.
    # ------------------------------------------------------------------

    # input.json fields allowed to differ between the lingering run and a
    # handed-off turn. MUST stay in sync with runner._HANDOFF_TURN_FIELDS
    # (the runner re-validates fail-closed with the same whitelist).
    _HANDOFF_TURN_FIELDS = frozenset({
        "prompt", "images", "files", "target_message_id", "turn_run_id",
    })
    # Heartbeat staleness bound: the runner refreshes runner_alive every
    # ~5s; anything older means the linger may be a zombie — respawn.
    _HANDOFF_HEARTBEAT_FRESH_S = 20.0

    def _handoff_precheck(
        self, blocker: RunState, spawn_kwargs: dict,
    ) -> Optional[str]:
        """Cheap liveness/drift checks before the (heavier) payload diff.
        None when the blocker may host a handoff; otherwise the reason it
        can't. Fail closed: ANY doubt → cancel+respawn."""
        # getattr throughout: a post-restart lingering run is re-registered
        # as a SimpleNamespace stub (run_recovery) without the handoff
        # fields — it fails eligibility (fail closed) and takes the
        # cancel+respawn path.
        if blocker.run_id in self._handoff_barred:
            return "run barred after a prior rejection"
        if getattr(blocker, "is_handoff_turn", False):
            return "blocker is itself a handoff registration"
        if getattr(blocker, "cancelled", False):
            return "blocker cancelled"
        popen = getattr(blocker, "popen", None)
        if popen is None or popen.poll() is not None:
            return "blocker process exited"
        try:
            from runs_dir import runner_alive_path
            age = time.time() - runner_alive_path(blocker.run_dir).stat().st_mtime
            if age > self._HANDOFF_HEARTBEAT_FRESH_S:
                return f"heartbeat stale ({age:.0f}s)"
        except OSError:
            return "no heartbeat"
        current_version = config_store.provider_record_version(self.id)
        spawn_version = getattr(blocker, "record_version_at_spawn", None)
        if (
            spawn_version is None
            or current_version is None
            or spawn_version != current_version
        ):
            return "provider record changed since spawn"
        if (spawn_kwargs.get("extra_env") or {}) != (
            getattr(blocker, "extra_env_at_spawn", None) or {}
        ):
            return "extra_env differs"
        return None

    def _handoff_payload_diff(
        self, blocker: RunState, new_payload: dict,
    ) -> Optional[str]:
        """Whitelist diff of the new turn's input payload against the
        blocker's on-disk input.json: only per-turn fields may differ —
        including keys added in the future (fail closed: the live CLI's
        tool set, permissions, and env were fixed at connect())."""
        try:
            blocker_payload = json.loads(
                (blocker.run_dir / "input.json").read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            return "blocker input.json unreadable"
        for key in (set(blocker_payload) | set(new_payload)) - self._HANDOFF_TURN_FIELDS:
            if blocker_payload.get(key) != new_payload.get(key):
                return f"input field {key!r} differs"
        return None

    def _handoff_spawn(
        self,
        blocker: RunState,
        spawn_kwargs: dict,
        input_payload: dict,
        mode: str,
    ) -> None:
        """Register the new turn as a top-level run dir served by the
        blocker's lingering runner: write input.json, drop a pointer into
        the blocker's handoff mailbox, and start the NORMAL bootstrap
        (state.json → own tailer → handoff-aware completion watcher)."""
        run_id = spawn_kwargs["run_id"]
        run_dir = _runs_root() / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "input.json").write_text(
            json.dumps(input_payload), encoding="utf-8",
        )

        rs = RunState(
            run_id=run_id,
            run_dir=run_dir,
            popen=blocker.popen,
            mode=mode,
            app_session_id=spawn_kwargs["app_session_id"],
            queue=spawn_kwargs["queue"],
            session_id=blocker.session_id,
            started_at=datetime.now().isoformat(),
            persist_to=(
                spawn_kwargs.get("worker_agent_session_id")
                or spawn_kwargs["app_session_id"]
            ),
            target_message_id=spawn_kwargs.get("target_message_id"),
            turn_run_id=spawn_kwargs.get("turn_run_id"),
            is_handoff_turn=True,
            handoff_host=blocker,
            handoff_spawn_kwargs=spawn_kwargs,
        )
        self._runs[run_id] = rs
        self._write_backend_state(rs)

        blocker.handoff_target = rs
        blocker.handoff_route_from = None
        blocker.handoff_hold = []

        # Pointer last — once it exists the runner may pick the turn up,
        # and everything it needs must already be durable. Name is
        # monotonic-time-prefixed so name-sorted pickup = submission
        # order even when pointers coexist (e.g. after a mid-handoff
        # backend restart).
        mailbox = blocker.run_dir / "handoff"
        try:
            mailbox.mkdir(parents=True, exist_ok=True)
            _atomic_write_json(
                mailbox / f"{time.time_ns():020d}-{run_id}.json",
                {"run_dir": str(run_dir)},
            )
        except OSError:
            logger.exception(
                "handoff pointer write failed for %s — respawning", run_id,
            )
            self._fallback_respawn(rs)
            return

        logger.info(
            "handoff: run %s → lingering runner pid=%d (host run %s)",
            run_id[:8], blocker.popen.pid, blocker.run_id[:8],
        )
        schedule_loop_task(
            spawn_kwargs["loop"],
            self._bootstrap_run(rs),
            name=f"bridge-bootstrap-{run_id[:8]}",
        )

    def _fallback_respawn(self, rs: RunState) -> None:
        """A handoff that can't be served (rejected by the runner, pointer
        never picked up, host died pre-turn) falls back to the original
        cancel+respawn path — the prompt is never lost. Bars the host from
        further handoff attempts for this session so the retry can't
        loop."""
        host = rs.handoff_host
        if host is not None:
            self._handoff_barred.add(host.run_id)
            if host.handoff_target is rs:
                host.handoff_target = None
                self._flush_handoff_hold(host)
        spawn_kwargs = rs.handoff_spawn_kwargs or {}
        self._cleanup_run(rs.run_id)
        if not spawn_kwargs:
            logger.error(
                "handoff fallback for %s has no spawn kwargs — dropping",
                rs.run_id,
            )
            return
        logger.info("handoff fallback: respawning run %s", rs.run_id[:8])
        try:
            self.start_run(**spawn_kwargs)
        except Exception as e:
            logger.exception("handoff fallback respawn failed for %s", rs.run_id)
            try:
                rs.queue.put_nowait(StreamEvent("complete", {
                    "success": False,
                    "error": f"handoff fallback respawn failed: {e}",
                    "session_id": rs.session_id,
                    "token_usage": None,
                }))
            except Exception:
                logger.exception("failed to enqueue handoff fallback failure")

    def _flush_handoff_hold(self, host: RunState) -> None:
        """Flush the host's held lines once the routing boundary is known
        (or the handoff ended): lines before the boundary are the prior
        turn's late tail → orphan funnel; lines at/after it belong to the
        handoff run's own tailer → skipped."""
        held = host.handoff_hold or []
        host.handoff_hold = None
        boundary = host.handoff_route_from
        for enriched, line_start in held:
            if boundary is not None and line_start >= boundary:
                continue
            try:
                self._ingest_late_flush(host, enriched)
            except Exception:
                logger.exception(
                    "handoff hold flush: orphan ingest failed for %s",
                    host.run_id,
                )

    async def _start_after_handoff_release(
        self, target: RunState, spawn_kwargs: dict,
    ) -> None:
        """Serialize a prompt that arrived while a handoff turn was in
        flight: wait for that turn's release (fires at TURN end — its
        completion watcher cleans up at complete.json, not process exit),
        then re-enter start_run to re-check the gate."""
        try:
            await target.released.wait()
            self.start_run(**spawn_kwargs)
        except Exception as e:
            logger.exception(
                "deferred start after handoff release failed for %s",
                spawn_kwargs.get("run_id"),
            )
            try:
                spawn_kwargs["queue"].put_nowait(StreamEvent("complete", {
                    "success": False,
                    "error": f"deferred start after handoff release failed: {e}",
                    "session_id": spawn_kwargs.get("session_id"),
                    "token_usage": None,
                }))
            except Exception:
                logger.exception(
                    "failed to enqueue deferred-start failure for run %s",
                    spawn_kwargs.get("run_id"),
                )

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
        disabled_builtin_extensions: Optional[list[str]],
        provisioned_tool_profile: str,
    ) -> None:
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
            started_at=datetime.now().isoformat(),
            persist_to=worker_agent_session_id or app_session_id,
            target_message_id=target_message_id,
            turn_run_id=turn_run_id,
            record_version_at_spawn=config_store.provider_record_version(self.id),
            extra_env_at_spawn=dict(extra_env) if extra_env else {},
        )
        self._runs[run_id] = run_state

        # Seed backend_state.json so recovery scanners can see this run.
        self._write_backend_state(run_state)

        schedule_loop_task(
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
            if await path_exists_off_loop(runner_state_path):
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
            if not await popen_is_running_off_loop(rs.popen):
                if rs.is_handoff_turn:
                    # Host runner died before serving the turn — the
                    # prompt is still durable in input.json; respawn.
                    self._fallback_respawn(rs)
                    return
                if await path_exists_off_loop(complete_path):
                    break  # falls through to "runner_state is None" drain
                await self._emit_early_failure(
                    rs,
                    f"runner exited early with code {rs.popen.returncode}",
                )
                return

            # A handoff rejected by the runner writes complete.json
            # (handoff_rejected) with NO state.json while the host stays
            # alive — fall back to the respawn path; the turn never ran.
            if rs.is_handoff_turn and await path_exists_off_loop(complete_path):
                payload = None
                try:
                    payload = json.loads(
                        complete_path.read_text(encoding="utf-8"),
                    )
                except (OSError, ValueError):
                    pass
                if payload is not None:
                    if str(payload.get("error") or "").startswith(
                        "handoff_rejected"
                    ):
                        self._fallback_respawn(rs)
                    else:
                        # Turn failed before state.json (e.g. jsonl
                        # unreadable) — surface the real failure.
                        await self._emit_complete_from_file(rs, complete_path)
                        self._finish_handoff(rs)
                        self._cleanup_run(rs.run_id)
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

        # Handoff: the runner's boundary-corrected pre_query_byte_offset
        # is the routing boundary — arm the host's dispatch cap and flush
        # the lines it held while the boundary was unknown.
        if rs.is_handoff_turn and rs.handoff_host is not None:
            host = rs.handoff_host
            host.handoff_route_from = start_offset
            self._flush_handoff_hold(host)

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
            self._dispatch_tailer_line(_rs, enriched)

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

    def _dispatch_tailer_line(self, rs: RunState, enriched: dict) -> None:
        """Route one tailed jsonl line: live turn → queue; finalized turn
        → orphan funnel; finalized turn hosting a handoff → lines at/after
        the handoff boundary belong to the handoff run's OWN tailer and
        are skipped (double-writing them through the orphan funnel would
        race the msg_id stamp in events.jsonl); boundary unknown → held
        until _bootstrap_run arms it."""
        if rs.turn_finalized:
            if rs.handoff_target is not None:
                line_start = (
                    rs.tailer.processed_offset if rs.tailer is not None else 0
                )
                if rs.handoff_route_from is None:
                    if rs.handoff_hold is None:
                        rs.handoff_hold = []
                    rs.handoff_hold.append((enriched, line_start))
                    return
                if line_start >= rs.handoff_route_from:
                    return
            self._ingest_late_flush(rs, enriched)
            return
        try:
            rs.queue.put_nowait(StreamEvent("agent_message", enriched))
        except Exception:
            logger.exception(
                "ClaudeJsonlTailer dispatch: put_nowait failed for run %s",
                rs.run_id,
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
            await self._await_tailer_drained(rs)

            if await popen_is_running_off_loop(rs.popen) and await path_exists_off_loop(complete_path):
                if rs.is_handoff_turn:
                    # Handoff turn done on a still-alive HOST process.
                    # This run is not the linger owner — release at TURN
                    # end (drain own tailer, emit complete, deregister)
                    # so the gate's defer path resumes immediately; the
                    # host's _watch_linger_exit owns process-exit
                    # cleanup.
                    await self._await_tailer_drained(rs)
                    if rs.tailer is not None:
                        rs.tailer.stop()
                    if rs.tailer_task is not None:
                        try:
                            await asyncio.wait_for(rs.tailer_task, timeout=2.0)
                        except asyncio.TimeoutError:
                            logger.warning(
                                "tailer did not exit in time for %s", rs.run_id,
                            )
                        except Exception:
                            logger.exception(
                                "tailer task failed for %s", rs.run_id,
                            )
                    await self._emit_complete_from_file(rs, complete_path)
                    self._finish_handoff(rs)
                    return  # cleanup=True deregisters + fires released
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
            if rs.is_handoff_turn:
                self._finish_handoff(rs)
        finally:
            if cleanup:
                self._cleanup_run(rs.run_id)

    def _cleanup_run(self, run_id: str) -> None:
        self._handoff_barred.discard(run_id)
        super()._cleanup_run(run_id)

    def _finish_handoff(self, rs: RunState) -> None:
        """Detach a finished handoff turn from its host: re-open the
        host's orphan funnel (flushing anything still held) so late CLI
        tail lines keep flowing through it for the rest of the linger."""
        host = rs.handoff_host
        if host is None:
            return
        if host.handoff_target is rs:
            host.handoff_target = None
            self._flush_handoff_hold(host)
            host.handoff_route_from = None

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
            while await popen_is_running_off_loop(rs.popen):
                if (
                    not rs.lingering
                    and await path_exists_off_loop(lingering_sentinel)
                ):
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
            "is_handoff_turn": getattr(rs, "is_handoff_turn", False),
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

    def attach_recovered_run(
        self,
        *,
        desc: dict,
        queue: asyncio.Queue,
        loop: asyncio.AbstractEventLoop,
    ) -> bool:
        """Re-attach a still-running detached Claude runner after restart.

        `recover_in_flight` only classifies the on-disk run. This method
        rebuilds the provider-side RunState and restarts the same
        state.json bootstrap, Claude jsonl tailer, completion watcher, and
        lingering watcher used by a live spawn. That keeps post-restart
        provider-stream events flowing immediately instead of waiting for a
        later cold replay after complete.json appears.
        """
        run_id = str(desc.get("run_id") or "")
        pid = desc.get("pid")
        if not run_id or not pid or run_id in self._runs:
            return False
        try:
            runner_pid = int(pid)
        except (TypeError, ValueError):
            return False
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
            started_at=desc.get("started_at") or datetime.now().isoformat(),
            cancelled=bool(desc.get("cancelled", False)),
            persist_to=desc.get("persist_to") or desc.get("app_session_id") or "",
            target_message_id=desc.get("target_message_id"),
            turn_run_id=desc.get("turn_run_id"),
            # Recovered handoff turns keep turn-end release semantics
            # (their completion watcher must not wait for host-process
            # exit). handoff_host stays None — the host's hold/route
            # capping is a live-tailer concern that doesn't survive a
            # restart (the host is re-registered tailer-less).
            is_handoff_turn=bool(desc.get("is_handoff_turn", False)),
        )
        self._runs[run_id] = rs
        self._write_backend_state(rs)
        schedule_loop_task(
            loop,
            self._bootstrap_run(rs),
            name=f"claude-recover-bootstrap-{run_id[:8]}",
        )
        return True

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
                "is_handoff_turn": bool(bs.get("is_handoff_turn", False)),
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

        return None

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
