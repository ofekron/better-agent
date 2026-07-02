"""Provider abstraction for Claude-like coding-CLI backends.

Each `Provider` subclass owns the full surface the rest of the backend
uses to talk to its underlying CLI:

  - `start_run` / `cancel_run` / `is_running` / `runs_for_session`     — long-lived turn streaming (manager + worker spawns)
  - `run_headless`                                                     — one-shot `-p` invocations (rearranger)
  - `rewind`                                                           — file-system rewind
  - `recover_in_flight` / `prune_old_runs` / `cancel_all`              — lifecycle housekeeping
  - `build_env`                                                        — env vars threaded into every CLI subprocess

Adding a new provider type:
  1. Subclass `Provider`, set `KIND = "<your-kind>"`, implement every
     abstract method.
  2. Register the class in `_resolve_class` (or via a side-effect import
     this module performs).
  3. Provider records on disk gain `kind: "<your-kind>"`; existing
     records default to `"claude"`.

`default_provider()` returns the cached instance for the currently-active
provider record. `get_provider(provider_id)` does the same for any id.
Instances are cached so per-instance run-tracking state survives across
calls; the underlying record is refreshed from disk on every lookup so
config edits show up without dropping in-flight state.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, ClassVar, Iterable, Optional

import config_store
from env_compat import dual_env_many
from paths import ba_home
from proc_control import process_control as _process_control

logger = logging.getLogger(__name__)

_PROVIDER_POLL_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix="provider-poll",
)


async def path_exists_off_loop(path: Path) -> bool:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_PROVIDER_POLL_EXECUTOR, path.exists)


async def popen_poll_off_loop(popen: Any) -> Optional[int]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_PROVIDER_POLL_EXECUTOR, popen.poll)


async def popen_is_running_off_loop(popen: Any) -> bool:
    return (await popen_poll_off_loop(popen)) is None


def shutdown_provider_poll_executor() -> None:
    _PROVIDER_POLL_EXECUTOR.shutdown(wait=False, cancel_futures=True)


def schedule_loop_task(
    loop: asyncio.AbstractEventLoop,
    coro,
    *,
    name: str,
) -> None:
    """Schedule `coro` to run on `loop`, callable from any thread.

    Returns immediately — the coroutine runs when the loop next services
    its ready queue. The task handle is intentionally not surfaced.

    This replaces a synchronous cross-thread wait that fatally raised
    TimeoutError whenever the loop couldn't service a `call_soon` within
    5s, killing the whole turn under transient loop lag during spawn.
    Scheduling non-blockingly decouples turn success from loop
    responsiveness; the bootstrap coroutine's own try/except surfaces
    its failures.
    """
    try:
        if asyncio.get_running_loop() is loop:
            loop.create_task(coro, name=name)
            return
    except RuntimeError:
        pass
    # run_coroutine_threadsafe enqueues the task creation on the loop
    # and returns at once; the loop retains the created task, so it is
    # not garbage-collected before completion.
    asyncio.run_coroutine_threadsafe(coro, loop)


class RecoveredPopen:
    recovered_stub = True

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.returncode: Optional[int] = None

    def poll(self) -> Optional[int]:
        if _process_control().pid_alive(self.pid):
            return None
        if self.returncode is None:
            self.returncode = -1
        return self.returncode

    def wait(self, timeout: Optional[float] = None) -> int:
        del timeout
        return self.poll() or 0


def runner_argv(run_dir: Path, *, dev_script: Path, kind: str) -> list[str]:
    """argv to spawn a runner subprocess.

    In a PyInstaller-frozen app `sys.executable` is the app binary, not a
    Python interpreter, so `python <script>` is impossible — the frozen
    entrypoint (`app_entry.py`) re-execs the app binary and dispatches on
    `--run-dir`. In a dev checkout `sys.executable` is the interpreter and
    the runner script runs directly. `kind` ("claude"/"gemini") tells the
    frozen entrypoint which runner to dispatch to.
    """
    if getattr(sys, "frozen", False):
        import provider_manifest
        argv = [sys.executable, "--run-dir", str(run_dir)]
        # Only the default Claude runner needs no flag; every other kind tells
        # the frozen entrypoint which runner module to dispatch to.
        if provider_manifest.runner_module_for(kind) != "runner":
            argv += ["--runner-kind", kind]
        return argv
    return [sys.executable, str(dev_script), "--run-dir", str(run_dir)]


def build_better_agent_run_env(
    *,
    backend_url: str | None,
    internal_token: str | None,
    app_session_id: str,
    cwd: str,
    model: str | None,
    provider_id: str,
    bare_config: bool,
    user_facing: bool,
    disabled_builtin_extensions: list[str] | None,
) -> dict[str, str]:
    state_home = str(ba_home())
    env = {
        "BETTER_AGENT_HOME": state_home,
        "BETTER_CLAUDE_HOME": state_home,
    }
    env.update(dual_env_many({
        "BETTER_CLAUDE_BACKEND_URL": str(backend_url or ""),
        "BETTER_CLAUDE_INTERNAL_TOKEN": str(internal_token or ""),
        "BETTER_CLAUDE_APP_SESSION_ID": str(app_session_id or ""),
        "BETTER_CLAUDE_CWD": str(cwd or ""),
        "BETTER_CLAUDE_MODEL": str(model or ""),
        "BETTER_CLAUDE_PROVIDER_ID": str(provider_id or ""),
        "BETTER_CLAUDE_BARE_CONFIG": "1" if bare_config else "0",
        "BETTER_CLAUDE_USER_FACING": "1" if user_facing else "0",
        "BETTER_CLAUDE_DISABLED_BUILTIN_EXTENSIONS": ",".join(
            sorted(set(disabled_builtin_extensions or []))
        ),
    }))
    return env


# ============================================================================
# StreamEvent — provider-agnostic event envelope pushed onto orchestrator queues
# ============================================================================
@dataclass
class StreamEvent:
    type: str
    data: dict


# ============================================================================
# Provider ABC
# ============================================================================
class ProviderSuspendedError(RuntimeError):
    """Raised when a provider is suspended and may not run work."""


class Provider(ABC):
    KIND: ClassVar[str]

    # ------------------------------------------------------------------
    # Capabilities — overridden per-provider. INVARIANT: every CLI-level
    # primitive that some providers expose but others don't is published
    # here as a `supports_*` boolean so callers can gate features (fork &
    # send, adversarial sync, prompt-engineer refine, rearranger, …)
    # without `isinstance(provider, ClaudeProvider)` checks. Capabilities
    # are also exposed on the public providers list so the frontend can
    # disable buttons/menus without a per-feature roundtrip.
    #
    # Defense-in-depth: capability gates are checked at THREE layers —
    # 1) frontend, which reads the flags off /api/providers and disables
    #    the unsupported menu/button so the user can't even ask;
    # 2) backend caller, which skips the operation cleanly when the
    #    provider says it can't do it (e.g. session_manager.fork,
    #    rearranger, prompt-engineer);
    # 3) provider's start_run, which raises NotImplementedError as the
    #    last line of defence.
    # If you add a new capability flag, gate it at all three.
    # ------------------------------------------------------------------
    supports_fork: ClassVar[bool] = True
    # Whether this provider can run as the persistent "manager" session
    # in manager mode (i.e. supports MCP tool registration + resumable
    # sessions so the BOOTSTRAP_PROMPT can be re-applied across turns).
    # Gemini's CLI has neither; manager mode is gated client-side off
    # this flag and a server-side `raise NotImplementedError` enforces.
    supports_manager_mode: ClassVar[bool] = True
    # Whether this provider's CLI exposes a non-interactive rewind /
    # session-truncation primitive that lets us cut the jsonl at a given
    # message UUID. Drives UI gating for the Rewind button + rewind-and-
    # retry flow. Gemini doesn't have one.
    supports_rewind: ClassVar[bool] = True
    # Internal server-side rewind contract: real CLI rewind providers
    # need the provider-native session id + user-message UUID; simulated
    # providers reset Better Agent's stored provider session ids instead.
    rewind_requires_agent_identity: ClassVar[bool] = True
    # Internal Alter-only escape hatch for CLIs that cannot rewind or reset
    # conversation state but can accept a model-facing correction tag.
    supports_semantic_alter: ClassVar[bool] = False
    # Whether an active turn accepts additional user input without being
    # cancelled and replaced by a new turn.
    supports_steering: ClassVar[bool] = False
    supports_native_subagents: ClassVar[bool] = False
    supports_reasoning_effort: ClassVar[bool] = False
    reasoning_effort_options: ClassVar[tuple[str, ...]] = ()
    default_reasoning_effort: ClassVar[str] = ""
    # Whether `run_headless(no_tools=True)` can GUARANTEE the one-shot
    # invocation runs with every built-in tool disabled (no Bash / file
    # writes / edits). Fail-closed default: a provider that cannot prove
    # it disables tools advertises False, and tool-less callers (composer
    # fill) refuse to route to it rather than risk a side-effecting run.
    supports_headless_no_tools: ClassVar[bool] = False

    def __init__(self, record: dict):
        self.id: str = record["id"]
        # Atomic-replace pattern: every read snapshots `self._record`
        # into a local var before touching it; writes assign a NEW dict
        # so partial-state reads can't observe a half-replaced record.
        # Subclass methods MUST snapshot at top, never deref `self._record`
        # twice in one method.
        self._record: dict = dict(record)
        self.defunct: bool = False
        self.suspended: bool = config_store.provider_suspended(self.id)
        self._apply_capability_overrides()

    # Per-provider capability overrides (record `capabilities` map) win
    # over the kind/subclass ClassVar defaults. Applied as instance attrs
    # so the existing `self.supports_*` reads pick them up, and re-applied
    # whenever the record is refreshed.
    def _apply_capability_overrides(self) -> None:
        overrides = (self._record.get("capabilities") or {})
        for key in (
            "supports_fork",
            "supports_manager_mode",
            "supports_rewind",
            "supports_steering",
            "supports_native_subagents",
            "supports_reasoning_effort",
        ):
            value = overrides.get(key)
            if isinstance(value, bool):
                object.__setattr__(self, key, value)
            else:
                # Clear a stale instance override so the class default shows.
                self.__dict__.pop(key, None)

    @property
    def record(self) -> dict:
        """Snapshot view of the provider's current record. Returns the
        same dict reference until `record.setter` is called; mutations
        to the returned dict are NOT safe — callers should treat the
        snapshot as read-only."""
        return self._record

    @record.setter
    def record(self, value: dict) -> None:
        self._record = dict(value)
        self.suspended = config_store.provider_suspended(self.id)
        self._apply_capability_overrides()

    def assert_not_suspended(self, *, action: str = "start runs") -> None:
        if config_store.provider_suspended(self.id):
            self.suspended = True
            raise ProviderSuspendedError(
                f"provider {self.id} is suspended; cannot {action}"
            )
        self.suspended = False

    # ------------------------------------------------------------------
    # Env — base for every CLI subprocess this provider spawns.
    # ------------------------------------------------------------------
    @abstractmethod
    def build_env(self) -> dict[str, str]: ...

    # ------------------------------------------------------------------
    # Long-lived turn — spawn worker process, stream events onto queue.
    # ------------------------------------------------------------------
    @abstractmethod
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
    ) -> None: ...

    # ------------------------------------------------------------------
    # Run-registry bookkeeping — concrete defaults shared by every
    # subprocess-streaming provider. Subclasses populate `self._runs`
    # in their own __init__ (the registry dict is dynamic — each
    # provider's RunState dataclass has provider-specific fields, but
    # every RunState shares the structural attributes these methods
    # touch: run_id, run_dir, popen, mode, app_session_id, session_id,
    # cancelled).
    # ------------------------------------------------------------------
    _runs: dict[str, Any]

    def is_running(self, run_id: str) -> bool:
        rs = self._runs.get(run_id)
        return rs is not None and rs.popen.poll() is None

    async def is_running_off_loop(self, run_id: str) -> bool:
        rs = self._runs.get(run_id)
        if rs is None:
            return False
        return await popen_is_running_off_loop(rs.popen)

    def cancel_all(self) -> int:
        """Cancel all active runs (in-flight turns AND lingering
        babysitters — their background work dies too). Returns count of
        runs signalled."""
        count = 0
        for rid in list(self._runs.keys()):
            if self.cancel_run(rid):
                count += 1
        if count:
            logger.info("%s.cancel_all: signalled %d runs", type(self).__name__, count)
        return count

    def active_runs(self) -> list[dict]:
        result = []
        for run_id, rs in list(self._runs.items()):
            result.append({
                "run_id": run_id,
                "pid": rs.popen.pid,
                "alive": rs.popen.poll() is None,
                "mode": rs.mode,
                "app_session_id": rs.app_session_id,
                "session_id": rs.session_id,
                "cancelled": rs.cancelled,
                "run_dir": str(rs.run_dir),
            })
        return result

    def runs_for_session(self, app_session_id: str) -> list[str]:
        return [
            rid for rid, rs in self._runs.items()
            if rs.app_session_id == app_session_id
        ]

    def is_recovered_run(self, run_id: str) -> bool:
        rs = self._runs.get(run_id)
        return bool(
            rs is not None
            and getattr(getattr(rs, "popen", None), "recovered_stub", False)
        )

    def _cleanup_run(self, run_id: str) -> None:
        self._runs.pop(run_id, None)
        # Release the containment handle. Never kills members (never-kill
        # rule) — drops the handle / removes an already-empty cgroup.
        try:
            from containment import containment
            containment().teardown(run_id)
        except Exception:
            logger.debug("containment teardown failed run=%s", run_id, exc_info=True)

    # ------------------------------------------------------------------
    # JSONL flush gate — before SIGTERM, confirm the native CLI has
    # written the current turn's user prompt to its session JSONL.
    # Reads the runner's state.json for jsonl_path and the line-count
    # baseline (pre_query_line_count). Polls until the JSONL grows past
    # that baseline or the timeout expires.
    #
    # For resumed sessions (pre_query_line_count > 0) the first new line
    # past the baseline IS the user message, so we wait for >= 1 new line.
    # For fresh sessions (pre_query_line_count == 0) the first line is
    # the system init event — we require >= 2 new lines to confirm the
    # user prompt was also written.
    # ------------------------------------------------------------------
    _JSONL_FLUSH_TIMEOUT = 3.0   # seconds
    _JSONL_FLUSH_POLL = 0.1      # seconds

    def _await_jsonl_flush(self, rs: Any) -> None:
        state_path = rs.run_dir / "state.json"
        jsonl_path: Optional[Path] = None
        pre_query_line_count = 0
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            raw = state.get("jsonl_path")
            if raw:
                jsonl_path = Path(raw)
            try:
                pre_query_line_count = int(
                    state.get("pre_query_line_count") or 0
                )
            except (TypeError, ValueError):
                pre_query_line_count = 0
        except (OSError, json.JSONDecodeError):
            # state.json missing / unreadable — CLI hasn't started yet,
            # nothing to flush. Proceed with cancel.
            return

        if jsonl_path is None or not jsonl_path.exists():
            return

        # Fresh sessions: first new line is system init, second is user
        # message. Resumed sessions: first new line is user message.
        min_new_lines = 2 if pre_query_line_count == 0 else 1
        target = pre_query_line_count + min_new_lines

        deadline = time.monotonic() + self._JSONL_FLUSH_TIMEOUT
        waited = False
        while time.monotonic() < deadline:
            try:
                with jsonl_path.open("rb") as f:
                    line_count = sum(1 for _ in f)
                if line_count >= target:
                    if waited:
                        logger.info(
                            "%s._await_jsonl_flush: JSONL flushed after "
                            "%.1fs (lines %d >= target %d)",
                            type(self).__name__,
                            self._JSONL_FLUSH_TIMEOUT
                            - (deadline - time.monotonic()),
                            line_count, target,
                        )
                    return
            except OSError:
                pass
            waited = True
            time.sleep(self._JSONL_FLUSH_POLL)

        logger.warning(
            "%s._await_jsonl_flush: timed out after %.1fs waiting for "
            "JSONL flush (run=%s, path=%s, baseline=%d)",
            type(self).__name__, self._JSONL_FLUSH_TIMEOUT,
            rs.run_dir.name, jsonl_path, pre_query_line_count,
        )

    # ------------------------------------------------------------------
    # Cancel — SIGTERM the runner's process group, SIGKILL after 3s if
    # it refuses. Drops `<run_dir>/cancel` as a sentinel for cooperative-
    # exit runners. Before SIGTERM, waits for the native CLI to flush
    # the current turn's user prompt into its session JSONL so the
    # conversation history survives the interrupt. Subclasses extend
    # behaviour by overriding `_post_cancel_hook`.
    # ------------------------------------------------------------------
    def cancel_run(self, run_id: str) -> bool:
        rs = self._runs.get(run_id)
        if rs is None:
            return False
        signalled = False
        try:
            (rs.run_dir / "cancel").touch()
            signalled = True
        except OSError as e:
            logger.warning(
                "%s.cancel_run: touch sentinel failed: %s",
                type(self).__name__, e,
            )

        # Give the native CLI a moment to flush the user prompt into its
        # session JSONL before we SIGTERM the process group. Without this,
        # an interrupt can kill the CLI before it persists the prompt,
        # breaking conversation continuity for the next turn.
        self._await_jsonl_flush(rs)

        # Sweep detached background-shell process groups (run_in_background
        # bash the CLI spawned with setsid) BEFORE killing the runner: the
        # killpg below only reaches the runner's OWN group, and once the CLI
        # dies these orphan. cancel_run is the hard-kill path (session
        # delete, shutdown Y=kill), so an explicit kill must actually end
        # the session's background work.
        try:
            swept = _process_control().kill_detached_descendant_groups(rs.popen.pid)
            if swept:
                logger.info(
                    "%s.cancel_run: swept %d detached bg-shell group(s) for %s",
                    type(self).__name__, swept, run_id,
                )
        except Exception:
            logger.exception(
                "%s.cancel_run: detached-group sweep failed", type(self).__name__,
            )

        if rs.popen.poll() is None:
            try:
                # Politely stop the runner's whole process tree, then force
                # it after a grace period. POSIX: SIGTERM→SIGKILL on the
                # process group; Windows: CTRL_BREAK→taskkill /T /F.
                forced = _process_control().terminate_tree(rs.popen, timeout=3.0)
                signalled = True
                logger.info(
                    "%s.cancel_run terminated process tree pid=%d run=%s (forced=%s)",
                    type(self).__name__, rs.popen.pid, run_id, forced,
                )
                try:
                    rs.popen.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    logger.error(
                        "%s.cancel_run: process refused to die pid=%d",
                        type(self).__name__, rs.popen.pid,
                    )
            except (ProcessLookupError, PermissionError, OSError) as e:
                logger.warning(
                    "%s.cancel_run terminate_tree failed pid=%d: %s",
                    type(self).__name__, rs.popen.pid, e,
                )

        rs.cancelled = True
        try:
            self._post_cancel_hook(rs)
        except Exception:
            logger.exception(
                "%s._post_cancel_hook raised", type(self).__name__,
            )
        try:
            self._write_backend_state(rs)
        except Exception:
            logger.exception(
                "%s.cancel_run: _write_backend_state raised", type(self).__name__,
            )
        return signalled

    def _post_cancel_hook(self, rs: Any) -> None:
        """Extension point — called after the process group is signalled
        but before backend_state is rewritten. Default no-op. Subclasses
        with their own tailer can call `tailer.stop()` here so the tailer
        wakes from its poll-sleep promptly."""
        return None

    # ------------------------------------------------------------------
    # Soft turn-stop — runner-driven `client.interrupt()` via sentinel.
    # NO killpg, NO bg-sweep on the backend side. The hard kill
    # (`cancel_run`) is delete-only.
    #
    # Writes `runs/<run_id>/cancel`, which the runner's `_cancel_watcher`
    # polls. Mid-turn: runner interrupts, drains to ResultMessage
    # (bounded ~15s), sweeps its own setsid'd bg shells, writes
    # complete.json, exits. During a babysitter linger: the linger loop
    # sees the sentinel, sweeps the detached groups, and exits. CLI +
    # same-pgroup descendants survive the interrupt and are closed
    # cleanly by the SDK's `disconnect()`.
    # ------------------------------------------------------------------
    def cancel_turn(self, run_id: str) -> bool:
        rs = self._runs.get(run_id)
        if rs is None:
            try:
                from runs_dir import runs_root
                run_dir = runs_root() / run_id
                if run_dir.name != run_id or not run_dir.is_dir():
                    logger.info(
                        "%s.cancel_turn: unknown run_id=%s",
                        type(self).__name__,
                        run_id,
                    )
                    return False
                target = run_dir / "cancel"
            except Exception:
                logger.info(
                    "%s.cancel_turn: unknown run_id=%s",
                    type(self).__name__,
                    run_id,
                )
                return False
        else:
            target = rs.run_dir / "cancel"
        try:
            target.touch()
        except OSError as e:
            logger.warning(
                "%s.cancel_turn: sentinel write failed run=%s: %s",
                type(self).__name__, run_id, e,
            )
            return False
        return True

    def steer_run(self, run_id: str, prompt: str, images: Optional[list] = None) -> bool:
        return False

    def lingering_runs(self, app_session_id: str) -> list[str]:
        """run_ids of registered runs whose runner is babysitter-lingering
        (turn finalized, process alive keeping background work running)
        for `app_session_id`. Empty for providers without a linger."""
        return [
            run_id for run_id, rs in self._runs.items()
            if getattr(rs, "app_session_id", None) == app_session_id
            and getattr(rs, "lingering", False)
        ]

    def lingering_run_details(self, app_session_id: str) -> list[dict]:
        """Rich per-run snapshot for the babysitter-lingering runs of
        `app_session_id`: what each run is (mode), when it started, and
        the prompt that kicked it off (read from the run's `input.json`).
        The frontend background-strip surfaces this in its "info" expand
        so the user can see WHAT is still running and WHY, not just that
        something is. Falls back to an empty prompt when the run dir or
        input.json is gone (defensive across providers)."""
        out: list[dict] = []
        for run_id, rs in self._runs.items():
            if getattr(rs, "app_session_id", None) != app_session_id:
                continue
            if not getattr(rs, "lingering", False):
                continue
            prompt = ""
            run_dir = getattr(rs, "run_dir", None)
            if run_dir is not None:
                inp = run_dir / "input.json"
                try:
                    if inp.exists():
                        raw = json.loads(inp.read_text(encoding="utf-8"))
                        if isinstance(raw, dict):
                            prompt = str(raw.get("prompt") or "").strip()
                except Exception:
                    prompt = ""
            out.append({
                "run_id": run_id,
                "mode": getattr(rs, "mode", None),
                "started_at": getattr(rs, "started_at", "") or "",
                "target_message_id": getattr(rs, "target_message_id", None),
                "prompt": prompt,
            })
        return out

    # ------------------------------------------------------------------
    # backend_state.json — shared path; subclass writes provider-specific
    # contents.
    # ------------------------------------------------------------------
    def _backend_state_path(self, rs: Any) -> Path:
        return rs.run_dir / "backend_state.json"

    def _read_backend_state(self, rs: Any) -> Optional[dict]:
        path = self._backend_state_path(rs)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.exception(
                "%s: failed to read backend_state.json for %s",
                type(self).__name__, rs.run_id,
            )
            return None

    @abstractmethod
    def _write_backend_state(self, rs: Any) -> None:
        """Provider-specific backend_state.json contents."""

    @abstractmethod
    def recover_in_flight(
        self,
        loop: Optional[asyncio.AbstractEventLoop] = None,
        run_id_filter: Optional[set[str]] = None,
    ) -> list[dict]:
        """Reconcile in-flight runs on disk. When `run_id_filter` is
        given, ONLY consider those run_ids — used by the cross-provider
        dispatcher in `recover_all_in_flight` so each provider only
        touches the runs it owns. When `None`, fall back to the legacy
        "scan everything" behavior (kept for the single-provider case)."""
        ...

    @abstractmethod
    def prune_old_runs(self, max_age_days: int = 7) -> int: ...

    # ------------------------------------------------------------------
    # One-shot headless invocation — JSON envelope back, no streaming.
    # ------------------------------------------------------------------
    @abstractmethod
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
    ) -> Optional[dict]: ...

    # ------------------------------------------------------------------
    # File-system rewind — undo the file edits a turn produced.
    # Raises on non-zero CLI exit so the caller can surface the error.
    # ------------------------------------------------------------------
    @abstractmethod
    async def rewind(self, rewind_session_id: str, message_uuid: str) -> None: ...

    # ------------------------------------------------------------------
    # Models — provider-specific. Different providers (z.ai vs Claude
    # subscription vs custom) have completely different model lists,
    # so this is an INSTANCE method routed through `models.py` with
    # the provider id. Subclasses can override if they have their own
    # discovery mechanism.
    # ------------------------------------------------------------------
    def available_models(self) -> list[str]:
        import models as models_mod
        if hasattr(models_mod, "models_for_provider"):
            return models_mod.models_for_provider(self.id)
        # Backwards-compat for the older "active only" model fetcher.
        return models_mod.available_models()

    # ------------------------------------------------------------------
    # Rate-limit parsing — extract provider-specific reset time from
    # error text / streamed events so the orchestrator can sleep until
    # the reset instead of busy-retrying on a fixed cadence.
    # ------------------------------------------------------------------
    def format_tool_result(self, tool_use_id: str, content: Any) -> dict:
        """Format an internal tool result event for the provider's API.

        Default uses the Anthropic tool_result structure; providers with
        a different wire format should override.
        """
        return {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": content,
                }],
            },
        }

    @staticmethod
    def _extract_text_for_rate_limit(events: list[dict]) -> str:
        """Concatenate user-facing text from streamed events for rate-
        limit keyword scanning.  Checks the last 2000 chars to avoid
        false positives on long outputs.

        Handles both `manager_event` (manager mode) and
        `agent_message` (native / worker mode) envelopes — both carry
        assistant text in their content blocks."""
        parts: list[str] = []
        for e in events:
            etype = e.get("type")
            data = e.get("data") or {}
            # Unwrap to the inner event dict.  manager_event nests it
            # under data.event; agent_message is already flat.
            if etype == "manager_event":
                inner = data.get("event") or data
            elif etype == "agent_message":
                inner = data
            else:
                continue
            content = (inner.get("message") or {}).get("content") or []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block.get("text", ""))
        text = "\n".join(parts)
        if len(text) <= 2000:
            return text
        return text[-2000:]

    @staticmethod
    def _fallback_rate_limit(hours: int = 1) -> datetime:
        """Fallback reset time: now + hours (UTC)."""
        return datetime.now(timezone.utc) + timedelta(hours=hours)


# ============================================================================
# Registry / factory
# ============================================================================
_PROVIDER_CACHE: dict[str, Provider] = {}
_CACHE_LOCK = threading.Lock()


def _resolve_class(kind: str) -> type[Provider]:
    # Lazy import from the canonical manifest so provider_* subclasses can
    # import from this module without a cycle at import time. Virtual kinds
    # (claude-remote) are coordinator-side proxies, never resolved here.
    import importlib
    import provider_manifest
    spec = provider_manifest.spec_for(kind)
    if spec is None or spec.virtual:
        raise ValueError(f"unknown provider kind: {kind!r}")
    module = importlib.import_module(spec.module)
    return getattr(module, spec.cls)


def _provider_runtime_kind(record: dict) -> str:
    runner = str(record.get("runner") or "").strip()
    if runner == "better_agent_runner":
        return "openai"
    return record.get("kind") or "claude"


def get_provider(provider_id: str) -> Provider:
    """Return the cached `Provider` for `provider_id`, refreshing its
    record from disk on every call so config edits are visible.

    A deleted provider with in-flight bookkeeping still resolves: we
    return the cached instance (marked defunct) so callers can cancel
    its runs cleanly. Only a provider that was *never* loaded raises
    `KeyError`.

    Concurrency: the cache mutation is locked so two parallel first-
    lookups can't each create their own instance and lose run state on
    the loser. Subsequent record refreshes go through the instance's
    `record` setter which atomically replaces the record dict.
    """
    record = config_store.get_provider_with_key(provider_id)
    suspended_record = record is None and config_store.provider_suspended(provider_id)
    with _CACHE_LOCK:
        cached = _PROVIDER_CACHE.get(provider_id)
        if record is None:
            if cached is not None:
                if suspended_record:
                    cached.suspended = True
                    cached.defunct = False
                    return cached
                cached.defunct = True
                cached.suspended = config_store.provider_suspended(provider_id)
                # Unregister the perf depth gauge so a deleted
                # provider stops emitting `q.provider.*.run_q
                # depth=0` lines on every rollup. Idempotent
                # (`unregister_queue` is a `dict.pop(..., None)`).
                gauge_name = getattr(cached, "_perf_gauge_name", None)
                if gauge_name:
                    import perf as _perf
                    _perf.unregister_queue(gauge_name)
                return cached
            if suspended_record:
                raise ProviderSuspendedError(
                    f"provider {provider_id} is suspended; cannot start runs"
                )
            raise KeyError(provider_id)
        kind = _provider_runtime_kind(record)
        cls = _resolve_class(kind)
        if cached is not None and isinstance(cached, cls):
            was_defunct = cached.defunct
            cached.record = record
            cached.defunct = False
            # Re-register the perf gauge if the provider was resurrected
            # (its gauge was unregistered when it went defunct, and
            # `_register_perf_gauge` is idempotent — `register_queue`
            # is a dict assignment).
            if was_defunct and hasattr(cached, "_register_perf_gauge"):
                cached._register_perf_gauge()
            return cached
        if cached is not None:
            active_runs = []
            try:
                active_runs = cached.active_runs()
            except Exception:
                active_runs = []
            if active_runs:
                raise RuntimeError(
                    f"provider {provider_id} runner changed while runs are active"
                )
        instance = cls(record)
        _PROVIDER_CACHE[provider_id] = instance
        return instance




def _run_ids_for_provider(provider_id: str) -> list[str]:
    from runs_dir import runs_root
    root = runs_root()
    if not root.exists():
        return []
    run_ids: list[str] = []
    for child in root.iterdir():
        if not child.is_dir():
            continue
        bs_path = child / "backend_state.json"
        if not bs_path.exists():
            continue
        try:
            data = json.loads(bs_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if data.get("provider_id") == provider_id:
            run_ids.append(child.name)
    return run_ids


def cancel_provider_runs(provider_id: str, *, run_ids: Iterable[str] | None = None) -> int:
    """Hard-stop every known run owned by a provider. Used when suspending
    provider usage so active turns and babysitter background work cannot keep
    spending that provider after the setting flips."""
    ids = set(run_ids or [])
    ids.update(_run_ids_for_provider(provider_id))
    with _CACHE_LOCK:
        cached = _PROVIDER_CACHE.get(provider_id)
    if cached is not None:
        try:
            ids.update(run.get("run_id") for run in cached.active_runs() if run.get("run_id"))
        except Exception:
            logger.debug("cancel_provider_runs: active_runs failed", exc_info=True)
    count = 0
    for run_id in sorted(ids):
        # Containment first: if the provider instance is absent (e.g. backend
        # restarted and the provider is now suspended), run dirs still give us
        # the run_id and containment can kill the whole tree on supported OSes.
        try:
            from containment import containment
            containment().force_kill_all(run_id)
        except Exception:
            logger.debug("cancel_provider_runs: containment kill failed", exc_info=True)
        signalled = False
        if cached is not None:
            try:
                signalled = bool(cached.cancel_run(run_id))
            except Exception:
                logger.exception("cancel_provider_runs: cancel_run failed run=%s", run_id)
        count += 1 if signalled or cached is None else 0
    if cached is not None:
        cached.suspended = config_store.provider_suspended(provider_id)
    return count

def default_provider() -> Provider:
    """The provider for the currently-active config_store record.

    Raises `RuntimeError` if no providers are configured at all.
    """
    record = config_store.get_default_provider()
    if record is None:
        raise RuntimeError("no active provider configured")
    return get_provider(record["id"])


def known_providers() -> list[Provider]:
    """All providers we've instantiated so far. Useful for shutdown
    hooks that need to fan out across every provider that may hold
    in-flight runs."""
    with _CACHE_LOCK:
        return list(_PROVIDER_CACHE.values())


def load_all_providers() -> list[Provider]:
    """Instantiate every provider record on disk. Called at startup so
    `known_providers()` reflects ALL configured providers, not just
    those touched by request traffic. Required for cross-provider fan-
    outs like in-flight recovery, /api/processes aggregation, and
    shutdown's cancel_all."""
    listed = [
        p for p in (config_store.list_providers().get("providers", []) or [])
        if not p.get("suspended")
    ]
    if not listed:
        return []
    # Parallelize instantiation so multiple slow/timing-out keyring
    # calls (during first get_provider of an api_key provider) don't
    # stack sequentially. 10 workers is enough for a typical list.
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=10, thread_name_prefix="load-providers") as executor:
        return list(executor.map(lambda p: get_provider(p["id"]), listed))


# ============================================================================
# Cross-provider in-flight recovery
# ============================================================================
def recover_all_in_flight(loop: Optional[asyncio.AbstractEventLoop] = None) -> list[dict]:
    """Scan the global runs root and dispatch each in-flight run to
    its owning provider's `recover_in_flight`. Each run dir's
    `backend_state.json` carries `provider_id`; runs created before
    that field existed default to the currently-active provider.

    Returns a flat list of recovery descriptors aggregated across
    every provider.
    """
    import json
    from ingestion_versions import marker_data_matches_current
    from runs_dir import (
        append_reconciled_marker_index,
        ensure_reconciled_marker_index_backfilled,
        load_reconciled_marker_index,
        reconciled_marker_index_row_matches,
        runs_root as _runs_root,
    )
    runs_root = _runs_root()
    if not runs_root.exists():
        return []
    ensure_reconciled_marker_index_backfilled(runs_root)
    reconciled_index = load_reconciled_marker_index(runs_root)

    # Group run_ids by owning provider_id.
    by_provider: dict[Optional[str], list[str]] = {}
    for child in runs_root.iterdir():
        if not child.is_dir() or child.is_symlink():
            continue
        indexed_marker = reconciled_index.get(child.name)
        if (
            indexed_marker is not None
            and reconciled_marker_index_row_matches(child, indexed_marker)
            and marker_data_matches_current(
                indexed_marker,
                str(indexed_marker.get("provider_kind") or ""),
            )
        ):
            continue
        marker_path = child / "reconciled.marker"
        if marker_path.exists():
            try:
                marker = json.loads(marker_path.read_text(encoding="utf-8"))
                if marker_data_matches_current(
                    marker,
                    str(marker.get("provider_kind") or ""),
                ):
                    append_reconciled_marker_index(
                        marker_path,
                        str(marker.get("provider_kind") or ""),
                        int(marker.get("ingestion_version")),
                        root=runs_root,
                    )
                    continue
            except Exception:
                pass
        bs_path = child / "backend_state.json"
        pid: Optional[str] = None
        if bs_path.exists():
            try:
                bs = json.loads(bs_path.read_text(encoding="utf-8"))
                pid = bs.get("provider_id")
            except Exception:
                pass
        by_provider.setdefault(pid, []).append(child.name)

    results: list[dict] = []
    # Fall back: runs without a provider_id go to the active provider
    # (legacy data; fix is forward-only).
    fallback_id: Optional[str] = None
    if None in by_provider:
        try:
            fallback_id = default_provider().id
        except Exception:
            fallback_id = None
    import logging
    log = logging.getLogger(__name__)
    for pid, run_ids in by_provider.items():
        owner_id = pid or fallback_id
        if owner_id is not None and owner_id.startswith("remote:"):
            # Remote run dirs can't be classified without the node
            # online — `run_recovery.integrate_remote_runs_for_node`
            # reconciles them when their node (re)connects.
            log.info(
                "recover_all_in_flight: %d remote run(s) owned by %s — "
                "deferred to node-connect recovery",
                len(run_ids), owner_id,
            )
            continue
        if owner_id is None:
            log.warning(
                "recover_all_in_flight: %d run(s) have no owning "
                "provider and no fallback (zero configured) — "
                "they remain on disk pending next startup",
                len(run_ids),
            )
            continue
        owner = None
        try:
            owner = get_provider(owner_id)
        except ProviderSuspendedError:
            log.info(
                "recover_all_in_flight: %d run(s) owned by suspended "
                "provider %s — leaving on disk while suspended",
                len(run_ids), owner_id,
            )
            continue
        except KeyError:
            owner = None
        # `get_provider` keeps a cached instance even after the on-disk
        # record is deleted; treat defunct as "owner is gone" so we
        # don't re-route to a stale-credentials Provider. Active
        # fallback is intentionally NOT used here — the run dir was
        # written under the deleted provider's CLAUDE_CONFIG_DIR; an
        # active-provider recovery would synthesize complete.json with
        # the wrong session-id-resolution rules.
        if owner is not None and getattr(owner, "suspended", False):
            log.info(
                "recover_all_in_flight: %d run(s) owned by suspended "
                "provider %s — leaving on disk while suspended",
                len(run_ids), owner_id,
            )
            continue
        if owner is None or owner.defunct:
            log.warning(
                "recover_all_in_flight: %d run(s) owned by missing/"
                "defunct provider %s — leaving on disk for manual cleanup",
                len(run_ids), owner_id,
            )
            continue
        owned = owner.recover_in_flight(loop=loop, run_id_filter=set(run_ids))
        results.extend(owned)
    return results
