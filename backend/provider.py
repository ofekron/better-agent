"""Provider abstraction for Claude-like coding-CLI backends.

Each `Provider` subclass owns the full surface the rest of the backend
uses to talk to its underlying CLI:

  - `start_run` / `cancel_run` / `is_running` / `runs_for_session`     — long-lived turn streaming (manager + worker spawns)
  - `run_headless`                                                     — one-shot `-p` invocations
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
import inspect
import json
import logging
import ntpath
import os
import posixpath
import re
import shutil
import subprocess
import sys
import threading
import time
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, ClassVar, Iterable, Literal, Optional

import config_store
import perf
from execution_template import (
    ExecutionArtifact,
    ExecutionAuthorityError,
    PreparedExecution,
    prepare_execution,
)
from env_compat import dual_env_many
from paths import (
    ba_home,
    make_private_directory,
    require_private_directory,
)
from proc_control import process_control as _process_control

logger = logging.getLogger(__name__)

def _new_provider_poll_executor() -> concurrent.futures.ThreadPoolExecutor:
    return concurrent.futures.ThreadPoolExecutor(
        max_workers=2,
        thread_name_prefix="provider-poll",
    )


_PROVIDER_POLL_EXECUTOR = _new_provider_poll_executor()
_PROVIDER_TASKS: set[asyncio.Task] = set()
_PROVIDER_TASKS_LOCK = threading.Lock()
_PROVIDER_TASKS_ACCEPTING = True

_DEFAULT_RECOVERY_SCAN_PARALLELISM = 4
_MAX_RECOVERY_SCAN_PARALLELISM = 16
_RECOVERY_SCAN_PARALLELISM_ENV = "BETTER_AGENT_RECOVERY_SCAN_PARALLELISM"


def _ensure_execution_run_dir(run_dir: Path) -> bool:
    try:
        run_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        require_private_directory(run_dir)
        return False
    try:
        make_private_directory(run_dir)
        require_private_directory(run_dir)
    except BaseException:
        try:
            run_dir.rmdir()
        except OSError:
            pass
        raise
    return True


def _run_was_likely_running_before_restart(runs_root: Path, run_id: str) -> bool:
    child = runs_root / run_id
    try:
        if (
            not run_id
            or Path(run_id).name != run_id
            or child.resolve(strict=False).parent != runs_root.resolve(strict=False)
        ):
            return False
    except OSError:
        return False
    if (child / "complete.json").exists():
        return False
    try:
        bs = json.loads((child / "backend_state.json").read_text(encoding="utf-8"))
    except Exception:
        return False
    try:
        runner_pid = int(bs.get("runner_pid")) if bs.get("runner_pid") else None
    except (TypeError, ValueError):
        runner_pid = None
    return bool(runner_pid and _process_control().pid_alive(runner_pid))


def _split_recovery_scan_run_ids(
    runs_root: Path,
    run_ids: set[str],
) -> tuple[set[str], set[str]]:
    likely_running: set[str] = set()
    other: set[str] = set()
    for run_id in run_ids:
        if _run_was_likely_running_before_restart(runs_root, run_id):
            likely_running.add(run_id)
        else:
            other.add(run_id)
    return likely_running, other


def _live_runner_run_ids(runs_root: Path) -> set[str]:
    import psutil

    root = runs_root.resolve(strict=False)
    run_ids: set[str] = set()
    for process in psutil.process_iter(["cmdline"]):
        try:
            command = process.info.get("cmdline") or []
            option_index = command.index("--run-dir")
            run_dir = Path(command[option_index + 1]).resolve(strict=False)
        except (IndexError, OSError, ValueError, psutil.Error):
            continue
        if run_dir.parent == root:
            run_ids.add(run_dir.name)
    return run_ids


BoundRunAuthorityState = Literal["owned", "absent", "unknown"]


@dataclass(frozen=True)
class BoundRunAuthority:
    state: BoundRunAuthorityState
    reason: str


def _argv_path_belongs_to_run(
    value: str,
    *,
    runs_root: str,
    run_id: str,
    windows: bool,
) -> bool:
    path_module = ntpath if windows else posixpath
    if not value or not path_module.isabs(value):
        return False
    normalized_root = path_module.normcase(path_module.normpath(runs_root))
    normalized_value = path_module.normcase(path_module.normpath(value))
    exact_run_root = path_module.normcase(
        path_module.normpath(path_module.join(normalized_root, run_id))
    )
    try:
        return path_module.commonpath(
            (exact_run_root, normalized_value),
        ) == exact_run_root
    except ValueError:
        return False


def _command_belongs_to_run(
    command: list[str],
    *,
    runs_root: str,
    run_id: str,
    windows: bool,
) -> bool:
    for index, argument in enumerate(command):
        if (
            argument == "--run-dir"
            and index + 1 < len(command)
            and _argv_path_belongs_to_run(
                command[index + 1],
                runs_root=runs_root,
                run_id=run_id,
                windows=windows,
            )
        ):
            return True
        if _argv_path_belongs_to_run(
            argument,
            runs_root=runs_root,
            run_id=run_id,
            windows=windows,
        ):
            return True
    return False


def _process_argv_authority(
    runs_root: Path,
    run_id: str,
    *,
    started_after: float | None = None,
    process_name_prefixes: tuple[str, ...] = (),
) -> BoundRunAuthority:
    import psutil

    normalized_prefixes = tuple(
        prefix.casefold()
        for prefix in process_name_prefixes
        if prefix
    )
    incomplete = False
    try:
        for process in psutil.process_iter():
            try:
                if (
                    started_after is not None
                    and process.create_time() < started_after
                ):
                    continue
                command = process.cmdline()
            except psutil.ZombieProcess:
                continue
            except psutil.AccessDenied:
                try:
                    process_name = process.name().casefold()
                except (psutil.NoSuchProcess, psutil.ZombieProcess):
                    continue
                except (psutil.AccessDenied, psutil.Error):
                    incomplete = True
                    continue
                if (
                    not normalized_prefixes
                    or process_name.startswith(normalized_prefixes)
                ):
                    incomplete = True
                continue
            except psutil.NoSuchProcess:
                continue
            except psutil.Error:
                incomplete = True
                continue
            if not isinstance(command, list) or any(
                not isinstance(argument, str)
                for argument in command
            ):
                incomplete = True
                continue
            if _command_belongs_to_run(
                command,
                runs_root=str(runs_root),
                run_id=run_id,
                windows=os.name == "nt",
            ):
                return BoundRunAuthority("owned", "process argv owns run")
    except (psutil.AccessDenied, psutil.ZombieProcess, psutil.Error, OSError):
        return BoundRunAuthority("unknown", "process enumeration incomplete")
    if incomplete:
        return BoundRunAuthority("unknown", "process inspection incomplete")
    return BoundRunAuthority("absent", "no process argv owns run")


def classify_bound_run_authority(
    provider_id: str,
    run_id: str,
    *,
    started_after: float | None = None,
) -> BoundRunAuthority:
    from containment import containment
    from runs_dir import runs_root

    if (
        not provider_id
        or provider_id.startswith("remote:")
        or not run_id
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", run_id)
        or run_id in {".", ".."}
        or Path(run_id).name != run_id
    ):
        return BoundRunAuthority("unknown", "invalid or remote authority")
    record = config_store.get_provider(provider_id)
    if record is None or record.get("suspended") is True:
        return BoundRunAuthority("unknown", "historical provider unavailable")
    try:
        owner = get_provider(provider_id)
    except (KeyError, ProviderSuspendedError):
        return BoundRunAuthority("unknown", "historical provider unavailable")
    if owner.defunct or owner.suspended:
        return BoundRunAuthority("unknown", "historical provider unavailable")
    if not owner.supports_bound_run_argv_authority:
        return BoundRunAuthority("unknown", "provider argv authority undeclared")
    root = runs_root().resolve(strict=False)
    run_dir = (root / run_id).resolve(strict=False)
    if run_dir.parent != root:
        return BoundRunAuthority("unknown", "run path escaped root")
    if run_dir.exists():
        return BoundRunAuthority("owned", "run directory exists")
    if owner.has_admitted_run(run_id):
        return BoundRunAuthority("owned", "provider admits run")
    try:
        if containment().enumerate(run_id):
            return BoundRunAuthority("owned", "containment owns run")
    except Exception:
        return BoundRunAuthority("unknown", "containment inspection failed")
    process_authority = _process_argv_authority(
        root,
        run_id,
        started_after=started_after,
        process_name_prefixes=owner.bound_run_process_name_prefixes,
    )
    if process_authority.state != "absent":
        return process_authority
    if run_dir.exists():
        return BoundRunAuthority("owned", "run directory reappeared")
    if owner.has_admitted_run(run_id):
        return BoundRunAuthority("owned", "provider admitted run during census")
    return BoundRunAuthority("absent", "no local run authority")


async def path_exists_off_loop(path: Path) -> bool:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_PROVIDER_POLL_EXECUTOR, path.exists)


async def popen_poll_off_loop(popen: Any) -> Optional[int]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_PROVIDER_POLL_EXECUTOR, popen.poll)


async def popen_is_running_off_loop(popen: Any) -> bool:
    return (await popen_poll_off_loop(popen)) is None


async def run_provider_poll_off_loop(fn, /, *args):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_PROVIDER_POLL_EXECUTOR, fn, *args)


def _count_event_lines(path: Path) -> int:
    """Non-blank lines in a runner-owned event stream — matches the
    line-count cursor `JsonlEventTailer` advances per dispatched line
    (`SessionEventsJsonlTailer._read_new_lines` filters blank lines the same
    way)."""
    try:
        with path.open("r", encoding="utf-8") as fh:
            return sum(1 for line in fh if line and not line.isspace())
    except OSError:
        return 0


def _file_byte_size(path: Path) -> int:
    """Byte size of a tailed file — matches the byte-offset cursor
    `CodexRolloutTailer` advances per dispatched line (the Codex rollout
    is an externally-owned file the CLI appends to, tailed by byte
    offset rather than line count)."""
    try:
        return path.stat().st_size
    except OSError:
        return 0


async def await_line_tailer_drained(
    *,
    path: Path,
    get_cursor: Callable[[], int],
    run_id: str,
    timeout: float = 5.0,
    poll: float = 0.05,
    count_fn: Callable[[Path], int] = _count_event_lines,
    on_drained: Optional[Callable[[], Any]] = None,
) -> bool:
    """Deterministic drain for a tailed event stream (the
    `session_events.jsonl` a runner writes itself, or an externally-owned
    file like the Codex rollout): wait until the tailer's cursor reaches
    the size the file holds at complete-detection time — the replacement
    for a fixed sleep guess.

    Ordering contract: the writer appends every event line BEFORE
    signalling completion, so a snapshot taken once completion is
    detected covers the whole turn. Without the drain a lagging poll
    tailer lets `complete` overtake trailing event lines — the turn loop
    breaks, the lines never reach the render tree, and waiters (e.g.
    `ask_team_message`) grab stale content.

    `count_fn` selects the cursor unit: `_count_event_lines` (default)
    for line-count cursors, `_file_byte_size` for byte-offset cursors.

    `on_drained`, if given, runs once after the wait concludes (success
    or timeout) — callers use it to force a final flush of whatever
    cursor-advance persistence they coalesce (see `cursor_ledger_worker`)
    so `backend_state.json` matches the true final cursor for crash
    recovery. May be a plain callable or return an awaitable (e.g. an
    `async def` method reference) — either way this function waits for
    it to finish before returning, so the flush is guaranteed durable by
    the time the caller treats the drain as concluded.

    Returns True on drain, False on timeout (degraded fallback — fire
    anyway so a wedged tailer can't hang the turn forever). A timeout
    with a nonzero gap means real content never reached the render tree;
    that case logs at ERROR (not WARNING) and records a `perf` metric so
    it can't go unnoticed."""
    target = await run_provider_poll_off_loop(count_fn, path)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while get_cursor() < target:
        if loop.time() >= deadline:
            gap = max(0, target - get_cursor())
            log = logger.error if gap > 0 else logger.warning
            log(
                "line tailer drain timeout run=%s processed=%d target=%d "
                "gap=%d (firing complete anyway)",
                run_id, get_cursor(), target, gap,
            )
            if gap > 0:
                perf.record_count("tailer.drain_timeout_gap_units", gap)
            await _call_maybe_async(on_drained)
            return False
        await asyncio.sleep(poll)
    await _call_maybe_async(on_drained)
    return True


async def _call_maybe_async(fn: Optional[Callable[[], Any]]) -> None:
    if fn is None:
        return
    result = fn()
    if inspect.isawaitable(result):
        await result


def reopen_provider_tasks() -> None:
    global _PROVIDER_POLL_EXECUTOR, _PROVIDER_TASKS_ACCEPTING
    with _PROVIDER_TASKS_LOCK:
        if _PROVIDER_TASKS_ACCEPTING:
            return
        _PROVIDER_POLL_EXECUTOR = _new_provider_poll_executor()
        _PROVIDER_TASKS_ACCEPTING = True


async def shutdown_provider_tasks() -> None:
    global _PROVIDER_TASKS_ACCEPTING
    started = time.perf_counter()
    with _PROVIDER_TASKS_LOCK:
        _PROVIDER_TASKS_ACCEPTING = False
        tasks = set(_PROVIDER_TASKS)
    for provider_instance in known_providers():
        for run_state in getattr(provider_instance, "_runs", {}).values():
            for value in vars(run_state).values():
                if isinstance(value, asyncio.Task):
                    tasks.add(value)
                elif isinstance(value, dict):
                    tasks.update(
                        item for item in value.values()
                        if isinstance(item, asyncio.Task)
                    )
    tasks = tuple(tasks)
    for task in tasks:
        task.cancel()
    results = await asyncio.gather(*tasks, return_exceptions=True) if tasks else []
    await asyncio.to_thread(
        _PROVIDER_POLL_EXECUTOR.shutdown,
        wait=True,
        cancel_futures=True,
    )
    perf.record("shutdown.provider_tasks", (time.perf_counter() - started) * 1000)
    perf.record_count("shutdown.provider_tasks.cancelled", len(tasks))
    perf.record_count(
        "shutdown.provider_tasks.failed",
        sum(isinstance(result, Exception) for result in results),
    )


def _provider_task_done(task: asyncio.Task) -> None:
    with _PROVIDER_TASKS_LOCK:
        _PROVIDER_TASKS.discard(task)
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        logger.error("provider lifecycle task failed: %s", error, exc_info=error)


def schedule_loop_task(
    loop: asyncio.AbstractEventLoop,
    coro,
    *,
    name: str,
) -> Optional[asyncio.Task]:
    """Schedule `coro` to run on `loop`, callable from any thread.

    Returns the task when called on its event-loop thread. Cross-thread
    callers return immediately while the loop admits and owns the task.

    This replaces a synchronous cross-thread wait that fatally raised
    TimeoutError whenever the loop couldn't service a `call_soon` within
    5s, killing the whole turn under transient loop lag during spawn.
    Scheduling non-blockingly decouples turn success from loop
    responsiveness; the bootstrap coroutine's own try/except surfaces
    its failures.
    """
    def _admit() -> Optional[asyncio.Task]:
        with _PROVIDER_TASKS_LOCK:
            if not _PROVIDER_TASKS_ACCEPTING:
                coro.close()
                perf.record_count("shutdown.provider_tasks.rejected", 1)
                return None
            try:
                task = loop.create_task(coro, name=name)
            except RuntimeError:
                coro.close()
                perf.record_count("shutdown.provider_tasks.rejected", 1)
                return None
            _PROVIDER_TASKS.add(task)
        task.add_done_callback(_provider_task_done)
        return task

    try:
        if asyncio.get_running_loop() is loop:
            return _admit()
    except RuntimeError:
        pass
    # Admission stays on the owning loop while the lock closes the race
    # with shutdown's acceptance gate.
    try:
        loop.call_soon_threadsafe(_admit)
    except RuntimeError:
        coro.close()
        perf.record_count("shutdown.provider_tasks.rejected", 1)
    return None


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


def live_recovery_pid(desc: dict) -> Optional[int]:
    """Pid of the process actually executing a recovered run: the provider
    CLI child when the runner wrapper died but its CLI is still alive
    (`orphaned_cli`), else the runner wrapper pid. Every liveness/completion
    check on a recovered run reads through here so it tracks the live process
    instead of a dead wrapper."""
    pid = desc.get("cli_pid") if desc.get("orphaned_cli") else desc.get("pid")
    try:
        return int(pid) if pid else None
    except (TypeError, ValueError):
        return None


def runner_argv(run_dir: Path, *, dev_script: Path, kind: str) -> list[str]:
    """argv to spawn a runner subprocess.

    In a PyInstaller-frozen app `sys.executable` is the app binary, not a
    Python interpreter, so `python <script>` is impossible — the frozen
    entrypoint (`app_entry.py`) re-execs the app binary and dispatches on
    `--run-dir`. In a dev checkout `sys.executable` is the interpreter and
    the runner script runs directly. `kind` (the provider kind) tells the
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
    run_id: str = "",
    app_session_id: str,
    cwd: str,
    model: str | None,
    provider_id: str,
    bare_config: bool,
    user_facing: bool,
    disabled_builtin_extensions: list[str] | None,
    runtime_hydration: dict[str, object] | None = None,
) -> dict[str, str]:
    from operation_cli import install_launcher
    from sdk_pythonpath import sdk_pythonpath

    state_home = str(ba_home())
    operation_bin = install_launcher()
    existing_path = os.environ.get("PATH", "")
    env = {
        "BETTER_AGENT_HOME": state_home,
        "BETTER_CLAUDE_HOME": state_home,
        "BETTER_AGENT_OPERATION_CLI": "better-agent-cli",
        "PATH": str(operation_bin) + (os.pathsep + existing_path if existing_path else ""),
    }
    if run_id:
        from runtime_bootstrap import issue

        env.update(dual_env_many({
            "BETTER_CLAUDE_RUNTIME_BOOTSTRAP": issue(
                str(internal_token or ""),
                runtime_hydration=runtime_hydration,
            ),
        }))
    pythonpath = sdk_pythonpath(
        Path(__file__).resolve().parents[1], os.environ.get("PYTHONPATH", "")
    )
    if not getattr(sys, "frozen", False):
        # Dev runners execute a materialized copy of their entry under the
        # run dir (open_pinned_runner_launch), so the backend checkout and
        # its site-packages are no longer implied by the script location.
        # The Claude runner runs isolated (-I) and takes these roots via
        # argv; every other runner takes them here, like sdk/ already does.
        import sysconfig

        entries = [entry for entry in pythonpath.split(os.pathsep) if entry]
        for root in (
            str(Path(sysconfig.get_path("purelib")).resolve()),
            str(Path(__file__).resolve().parent),
        ):
            if root not in entries:
                entries.insert(0, root)
        pythonpath = os.pathsep.join(entries)
    if pythonpath:
        env["PYTHONPATH"] = pythonpath
    if os.name == "nt":
        pathext = os.environ.get("PATHEXT", "")
        entries = [entry.upper() for entry in pathext.split(os.pathsep) if entry]
        if ".CMD" not in entries:
            env["PATHEXT"] = pathext + (os.pathsep if pathext else "") + ".CMD"
    env.update(dual_env_many({
        "BETTER_CLAUDE_BACKEND_URL": str(backend_url or ""),
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


class ProviderCredentialError(RuntimeError):
    """Raised before spawn when provider credentials are not authoritative.

    Carries structured fields so turn-failure surfacing can render a
    credential-specific error (with an in-session fix action) instead of
    a generic failure."""

    def __init__(
        self,
        message: str,
        *,
        provider_id: str,
        credential_status: str,
    ) -> None:
        super().__init__(message)
        self.provider_id = provider_id
        self.credential_status = credential_status

    def error_meta(self) -> dict:
        return {
            "kind": "provider_credential",
            "provider_id": self.provider_id,
            "credential_status": self.credential_status,
        }


@dataclass
class ParkedRun:
    """A run accepted by `start_run` but not spawned yet because another
    run still owns its native session (the wind-down gate).

    A parked run has no process, so it must still report as *live* to
    every liveness reader: the turn drive-loop declares a run dead when
    the provider says it is not running, and would otherwise synthesize
    `runner exited without delivering a complete event` for a run that
    is merely waiting its turn.

    `released` fires when the run leaves the parked state — either it
    spawned, or it was cancelled — so runs queued behind it proceed."""

    run_id: str
    session_id: str
    app_session_id: str
    # Orchestrator-level turn id. A cancel fanned out by turn_run_id has
    # no other way to find a run that never spawned, so the gate records
    # it at park time.
    turn_run_id: Optional[str]
    execution: PreparedExecution
    released: asyncio.Event
    # Loop the `released` event belongs to. `unpark_run` runs on a turn
    # dispatch worker thread, and `asyncio.Event.set` does not wake
    # waiters safely from off-loop — the wakeup is bounced through it.
    loop: asyncio.AbstractEventLoop
    # Arrival order at the gate. A run only ever waits on runs AHEAD of
    # it; without that ordering two parked runs each pick the other as a
    # blocker when they re-enter the gate and deadlock.
    seq: int
    cancelled: bool = False

    def release(self) -> None:
        if self.released.is_set():
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self.loop:
            self.released.set()
            return
        self.loop.call_soon_threadsafe(self.released.set)


class Provider(ABC):
    KIND: ClassVar[str]
    uses_managed_api_key: ClassVar[bool] = False
    supports_bound_run_argv_authority: ClassVar[bool] = False
    bound_run_process_name_prefixes: ClassVar[tuple[str, ...]] = ()

    # ------------------------------------------------------------------
    # Capabilities — overridden per-provider. INVARIANT: every CLI-level
    # primitive that some providers expose but others don't is published
    # here as a `supports_*` boolean so callers can gate features (fork &
    # send, prompt-engineer refine, …)
    # without `isinstance(provider, ClaudeProvider)` checks. Capabilities
    # are also exposed on the public providers list so the frontend can
    # disable buttons/menus without a per-feature roundtrip.
    #
    # Defense-in-depth: capability gates are checked at THREE layers —
    # 1) frontend, which reads the flags off /api/providers and disables
    #    the unsupported menu/button so the user can't even ask;
    # 2) backend caller, which skips the operation cleanly when the
    #    provider says it can't do it (e.g. session_manager.fork,
    #    prompt-engineer);
    # 3) provider's start_run, which raises NotImplementedError as the
    #    last line of defence.
    # If you add a new capability flag, gate it at all three.
    # ------------------------------------------------------------------
    supports_fork: ClassVar[bool] = True

    def has_admitted_run(self, run_id: str) -> bool:
        return run_id in self._runs or run_id in self._parked_runs

    # Whether this provider can run as the persistent "manager" session
    # in manager mode (i.e. supports MCP tool registration + resumable
    # sessions so the BOOTSTRAP_PROMPT can be re-applied across turns).
    # CLIs lacking both gate manager mode client-side off this flag,
    # and a server-side `raise NotImplementedError` enforces it.
    supports_manager_mode: ClassVar[bool] = True
    # Whether this provider's CLI exposes a non-interactive rewind /
    # session-truncation primitive that lets us cut the jsonl at a given
    # message UUID. Drives UI gating for the Rewind button + rewind-and-
    # retry flow. Most external CLIs don't have one.
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
        self._execution_record = threading.local()
        self._headless_authority_lock = asyncio.Lock()
        self.defunct: bool = False
        self.suspended: bool = config_store.provider_suspended(self.id)
        self._parked_runs: dict[str, ParkedRun] = {}
        self._park_seq: int = 0
        self._cache_active = False
        self._apply_capability_overrides()

    def _activate_cache_resources(self) -> None:
        pass

    def _deactivate_cache_resources(self) -> None:
        pass

    def _activate_cache(self) -> None:
        if self._cache_active:
            return
        try:
            self._activate_cache_resources()
        except BaseException:
            try:
                self._deactivate_cache_resources()
            except BaseException:
                logger.exception("provider cache activation cleanup failed")
            raise
        self._cache_active = True

    def _deactivate_cache(self) -> None:
        if not self._cache_active:
            return
        try:
            self._deactivate_cache_resources()
        except BaseException:
            try:
                self._activate_cache_resources()
            except BaseException:
                logger.exception("provider cache deactivation rollback failed")
            raise
        self._cache_active = False

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
        if getattr(self._execution_record, "value", None) is not None:
            self.suspended = False
            return
        if config_store.provider_suspended(self.id):
            self.suspended = True
            raise ProviderSuspendedError(
                f"provider {self.id} is suspended; cannot {action}"
            )
        self.suspended = False

    def runtime_record(self) -> dict:
        active = getattr(self._execution_record, "value", None)
        return active if active is not None else self.record

    def require_runtime_credential(self) -> None:
        record = self.runtime_record()
        if record.get("mode") != "api_key" or not self.uses_managed_api_key:
            return
        from provider_env import is_ollama_base_url
        if is_ollama_base_url(str(record.get("base_url") or "")) and record.get("api_key"):
            return
        if record.get("_credential_authoritative") is not True:
            hydrated_status = record.get("_credential_status")
            if hydrated_status in {"available", "missing", "blocked"}:
                raise ProviderCredentialError(
                    f"provider {self.id} credential is {hydrated_status}; "
                    "cannot start provider process",
                    provider_id=self.id,
                    credential_status=hydrated_status,
                )
            try:
                status = config_store.provider_credential_status(self.id)
            except (EOFError, OSError, RuntimeError):
                status = "blocked"
            raise ProviderCredentialError(
                f"provider {self.id} credential is not supervisor-authoritative",
                provider_id=self.id,
                credential_status=status,
            )
        hydrated_status = record.get("_credential_status")
        if hydrated_status == "available" and record.get("api_key"):
            return
        try:
            status = config_store.provider_credential_status(self.id)
        except (EOFError, OSError, RuntimeError):
            raise ProviderCredentialError(
                f"provider {self.id} credential authority is unavailable",
                provider_id=self.id,
                credential_status="blocked",
            ) from None
        if status == "available" and record.get("api_key"):
            return
        raise ProviderCredentialError(
            f"provider {self.id} credential is {status}; cannot start provider process",
            provider_id=self.id,
            credential_status=status,
        )

    # ------------------------------------------------------------------
    # Env — base for every CLI subprocess this provider spawns.
    # ------------------------------------------------------------------
    @abstractmethod
    def build_env(self) -> dict[str, str]: ...

    def finalize_env(self, env: dict[str, str]) -> dict[str, str]:
        return env

    def finalize_run_env(
        self,
        env: dict[str, str],
        *,
        run_id: str,
        app_session_id: str,
        resolved_harness_run_config: Optional[dict],
    ) -> dict[str, str]:
        from provider_transport import apply_provider_transport

        return apply_provider_transport(
            env,
            provider_id=self.id,
            provider_kind=str(self.runtime_record().get("kind") or self.KIND),
            provider_mode=str(self.runtime_record().get("mode") or ""),
            run_id=run_id,
            session_id=app_session_id,
            resolved_harness_run_config=resolved_harness_run_config,
        )

    # ------------------------------------------------------------------
    # Long-lived turn — spawn worker process, stream events onto queue.
    # ------------------------------------------------------------------
    def discard_prepared_execution(self, execution: PreparedExecution) -> bool:
        if type(execution) is not PreparedExecution:
            raise TypeError("discard requires a prepared execution")
        if not execution.admission_pending:
            return False
        execution._mark_cancelled()
        self._release_execution_authority(execution)
        return True

    def start_run(
        self,
        *,
        execution: PreparedExecution,
        loop: asyncio.AbstractEventLoop,
        queue: asyncio.Queue,
    ) -> bool:
        if type(execution) is not PreparedExecution:
            raise TypeError("start_run requires a prepared execution")
        try:
            return self._start_authorized_execution(
                execution=execution,
                loop=loop,
                queue=queue,
            )
        except BaseException as exc:
            execution._mark_admission_failed(exc)
            execution._mark_spawn_failed(exc)
            raise

    def _start_authorized_execution(
        self,
        *,
        execution: PreparedExecution,
        loop: asyncio.AbstractEventLoop,
        queue: asyncio.Queue,
    ) -> bool:
        arguments = execution.start_arguments()
        with self._execution_authority_context(
            execution,
            arguments,
        ) as authority:
            execution.artifact.require_authority(authority)
            if self.defunct:
                raise RuntimeError(
                    f"provider {self.id} is defunct; cannot start new runs"
                )
            self.assert_not_suspended(action="start new runs")
            self.require_runtime_credential()
            with self._execution_admission(
                execution,
                loop=loop,
                queue=queue,
            ) as admitted:
                if not admitted:
                    return False
                self._admit_execution(execution)
                if not execution._try_commit_spawn():
                    return False
                if execution.cancel_after_admission_requested:
                    return False
                started = self._persist_and_start_execution(
                    execution,
                    arguments=arguments,
                    loop=loop,
                    queue=queue,
                )
                if not started:
                    return False
                execution._mark_spawn_completed()
                if execution.cancel_after_admission_requested:
                    self.cancel_turn(arguments["run_id"])
                return True

    def _persist_and_start_execution(
        self,
        execution: PreparedExecution,
        *,
        arguments: dict[str, Any],
        loop: asyncio.AbstractEventLoop,
        queue: asyncio.Queue,
    ) -> bool:
        from runs_dir import atomic_write_json, runs_root

        run_id = arguments["run_id"]
        run_dir = runs_root() / run_id
        artifact_path = run_dir / "execution.json"
        if not _ensure_execution_run_dir(run_dir):
            existing = tuple(run_dir.iterdir())
            if existing != (artifact_path,):
                raise RuntimeError(f"run directory already exists: {run_id}")
            try:
                persisted = json.loads(artifact_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError(
                    f"run execution authority is unreadable: {run_id}",
                ) from exc
            if persisted != execution.artifact.to_dict():
                raise RuntimeError(f"run execution authority conflicts: {run_id}")
        else:
            atomic_write_json(artifact_path, execution.artifact.to_dict())
        try:
            self._install_execution_payloads(execution, run_dir)
            started = self._start_run(
                loop=loop,
                queue=queue,
                _execution=execution,
                **arguments,
            )
            if type(started) is not bool:
                raise TypeError("_start_run must return bool")
        except BaseException:
            self._cleanup_failed_start(execution, run_dir)
            raise
        if not started:
            self._cleanup_failed_start(execution, run_dir)
        return started

    def _cleanup_failed_start(
        self,
        execution: PreparedExecution,
        run_dir: Path,
    ) -> None:
        try:
            self._cleanup_failed_execution_payloads(execution, run_dir)
        finally:
            shutil.rmtree(run_dir)

    def _install_execution_payloads(
        self,
        execution: PreparedExecution,
        run_dir: Path,
    ) -> None:
        del execution, run_dir

    def _cleanup_failed_execution_payloads(
        self,
        execution: PreparedExecution,
        run_dir: Path,
    ) -> None:
        del execution, run_dir

    def prepare_run(self, **start_arguments: Any) -> PreparedExecution:
        authority = self.execution_authority_record(start_arguments)
        return prepare_execution(authority, **start_arguments)

    def execution_authority_record(
        self,
        start_arguments: dict[str, Any],
    ) -> dict:
        del start_arguments
        return self.record

    @contextmanager
    def _execution_authority_context(
        self,
        execution: PreparedExecution,
        start_arguments: dict[str, Any],
    ):
        del start_arguments
        installed = False
        owned = False
        try:
            artifact = execution.artifact
            self._assert_execution_provider(artifact)
            owned = True
            from execution_spawn_authority import (
                attest_execution_spawn_authority,
            )

            attest_execution_spawn_authority(artifact)
            from model_execution_admission import admit_execution_model

            admit_execution_model(artifact)
            hydration = config_store.hydrate_provider_execution(
                artifact.provider_id,
                expected_generation=artifact.provider_generation,
                expected_execution_revision=(
                    artifact.provider_execution_revision
                ),
            )
            if hydration is None:
                if config_store.provider_suspended(artifact.provider_id):
                    self.suspended = True
                    raise ProviderSuspendedError(
                        f"provider {artifact.provider_id} is suspended; "
                        "cannot start new runs"
                    )
                raise RuntimeError(
                    f"provider {artifact.provider_id} is unavailable",
                )
            authority = hydration.provider
            self._execution_record.value = hydration.runtime_record()
            installed = True
            yield authority
        finally:
            if installed:
                del self._execution_record.value
            if owned:
                self._release_execution_authority(execution)

    def _assert_execution_provider(
        self,
        artifact: ExecutionArtifact,
    ) -> None:
        # artifact.provider_kind is the configured record's kind. For
        # better_agent_runner delegation the executing instance class KIND
        # is the runtime family (e.g. openai for a codex record), so the
        # identity assertion compares against the record.
        if (
            artifact.provider_id != self.id
            or artifact.provider_kind
            != str(self.runtime_record().get("kind") or self.KIND)
        ):
            raise ExecutionAuthorityError(
                "prepared execution belongs to a different provider",
            )

    def _release_execution_authority(
        self,
        execution: PreparedExecution,
    ) -> None:
        del execution

    @contextmanager
    def _execution_admission(
        self,
        execution: PreparedExecution,
        *,
        loop: asyncio.AbstractEventLoop,
        queue: asyncio.Queue,
    ):
        del execution, loop, queue
        yield True

    def _admit_execution(self, execution: PreparedExecution) -> None:
        del execution

    @abstractmethod
    def _start_run(
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
        user_facing: bool = False,
        working_mode: Optional[str] = None,
        extra_env: Optional[dict[str, str]] = None,
        continuation_chain: Optional[list[str]] = None,
        provider_run_config: Optional[dict] = None,
        capability_contexts: Optional[list[dict]] = None,
        target_message_id: Optional[str] = None,
        resolved_harness_run_config: Optional[dict] = None,
        turn_run_id: Optional[str] = None,
        disabled_builtin_extensions: Optional[list[str]] = None,
        provisioned_tool_profile: str = "",
        _execution: PreparedExecution,
    ) -> bool: ...

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
        if run_id in self._parked_runs:
            return True
        rs = self._runs.get(run_id)
        return rs is not None and rs.popen.poll() is None

    async def is_running_off_loop(self, run_id: str) -> bool:
        if run_id in self._parked_runs:
            return True
        rs = self._runs.get(run_id)
        if rs is None:
            return False
        return await popen_is_running_off_loop(rs.popen)

    # ------------------------------------------------------------------
    # Parked-run registry — the wind-down gate's holding area. Kept on
    # the base provider (not the gate's own subclass) so the liveness,
    # cancel, and blocker readers all see one registry.
    # ------------------------------------------------------------------
    def park_run(
        self,
        run_id: str,
        *,
        session_id: str,
        app_session_id: str,
        loop: asyncio.AbstractEventLoop,
        execution: PreparedExecution,
        turn_run_id: Optional[str] = None,
    ) -> ParkedRun:
        """Park a run, or return its existing entry. A run that re-enters
        the gate keeps its original arrival order — re-issuing it would
        send it to the back of the line every round and starve it."""
        existing = self._parked_runs.get(run_id)
        if existing is not None:
            return existing
        self._park_seq += 1
        parked = ParkedRun(
            run_id=run_id,
            session_id=session_id,
            app_session_id=app_session_id,
            turn_run_id=turn_run_id,
            execution=execution,
            released=asyncio.Event(),
            loop=loop,
            seq=self._park_seq,
        )
        self._parked_runs[run_id] = parked
        return parked

    def unpark_run(self, run_id: str) -> Optional[ParkedRun]:
        """Drop a run from the parked registry and wake anything queued
        behind it. Idempotent."""
        parked = self._parked_runs.pop(run_id, None)
        if parked is not None:
            parked.release()
        return parked

    def parked_runs_ahead_of(
        self, session_id: str, run_id: str,
    ) -> list[ParkedRun]:
        """Parked runs on `session_id` that arrived at the gate before
        `run_id` (all of them when `run_id` is not parked yet)."""
        own = self._parked_runs.get(run_id)
        cutoff = own.seq if own is not None else self._park_seq + 1
        return [
            p for p in self._parked_runs.values()
            if p.session_id == session_id and p.seq < cutoff
        ]

    def cancel_all(self) -> int:
        """Cancel all active runs. Returns count of runs signalled."""
        count = 0
        for rid in list(self._parked_runs.keys()) + list(self._runs.keys()):
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
        rs = self._runs.pop(run_id, None)
        # Fire the run's release event so anything serialized behind it
        # (the Claude wind-down gate in start_run) resumes immediately.
        released = getattr(rs, "released", None)
        if released is not None:
            try:
                released.set()
            except Exception:
                logger.exception("release event set failed run=%s", run_id)
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
        # A parked run has no process to signal: mark it cancelled and
        # release it so the gate drops it instead of spawning a CLI for
        # a turn that is already over.
        parked = self._parked_runs.get(run_id)
        if parked is not None:
            parked.cancelled = True
            parked.execution._mark_cancelled()
            self.unpark_run(run_id)
            logger.info(
                "%s.cancel_run: dropped parked run %s",
                type(self).__name__, run_id,
            )
            return True
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
    # complete.json, exits. CLI + same-pgroup descendants survive the
    # interrupt and are closed cleanly by the SDK's `disconnect()`.
    # ------------------------------------------------------------------
    def cancel_turn(self, run_id: str) -> bool:
        # A parked run has neither a process nor a run dir, so the cancel
        # sentinel below cannot reach it and the turn would stay wedged at
        # the gate forever. Dropping it from the gate IS the cancel, and
        # it releases anything queued behind it. Resolve by turn_run_id
        # too: a cancel fanned out by the orchestrator never carries this
        # provider's own run id.
        # Drop inline rather than via `cancel_run`: that path is dual-mode
        # and, if the gate spawned between the lookup and the call, would
        # escalate this cooperative cancel into a SIGTERM/SIGKILL of the
        # process tree — losing the graceful interrupt this method exists
        # to provide. Every match is dropped: retry/continuation attempts
        # mint a new provider run id per attempt under one turn_run_id, so
        # a survivor would keep blocking the gate.
        parked_matches = [
            p for p in self._parked_runs.values()
            if p.run_id == run_id or p.turn_run_id == run_id
        ]
        if parked_matches:
            for parked in parked_matches:
                parked.cancelled = True
                parked.execution._mark_cancelled()
                self.unpark_run(parked.run_id)
                logger.info(
                    "%s.cancel_turn: dropped parked run %s",
                    type(self).__name__, parked.run_id,
                )
            return True
        rs = self._runs.get(run_id)
        if rs is None:
            # `run_id` may be the orchestrator-level turn_run_id rather than
            # this provider's own run id: `active_run_ids`/`_run_state`
            # register live turns under turn_run_id (turn_manager.py), which
            # never matches this provider's `_runs` dict key (its own
            # generated run id) or the on-disk run-dir name. Every RunState
            # stamps `turn_run_id` at spawn time, so resolve through it
            # before falling back to disk — otherwise a cancel fanned out by
            # turn_run_id always misses every provider.
            rs = next(
                (r for r in self._runs.values() if r.turn_run_id == run_id),
                None,
            )
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
        if rs is not None:
            rs.turn_cancelled = True
            try:
                self._write_backend_state(rs)
            except Exception:
                logger.exception(
                    "%s.cancel_turn: failed to persist soft cancel for %s",
                    type(self).__name__,
                    run_id,
                )
        else:
            backend_state_path = target.parent / "backend_state.json"
            if backend_state_path.exists():
                try:
                    state = json.loads(
                        backend_state_path.read_text(encoding="utf-8")
                    )
                    if not isinstance(state, dict):
                        raise ValueError(
                            "backend_state.json must contain an object"
                        )
                    state["turn_cancelled"] = True
                    from runs_dir import atomic_write_json
                    atomic_write_json(backend_state_path, state)
                except (OSError, ValueError, json.JSONDecodeError):
                    logger.exception(
                        "%s.cancel_turn: failed to persist detached soft "
                        "cancel for %s",
                        type(self).__name__,
                        run_id,
                    )
        return True

    def steer_run(
        self,
        run_id: str,
        prompt: str,
        images: Optional[list] = None,
        files: Optional[list] = None,
    ) -> bool:
        return False

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

    async def run_admitted_headless(self, admitted: Any) -> dict:
        async with self._headless_authority_lock:
            return await self._run_admitted_headless_legacy(admitted)

    async def _run_admitted_headless_legacy(self, admitted: Any) -> dict:
        from headless_request_contract import AdmittedHeadlessRequest
        import runtime_profile

        if not isinstance(admitted, AdmittedHeadlessRequest):
            raise TypeError("admitted headless request is invalid")
        request = admitted.request
        authority = admitted.authority
        record = self.record
        expected = (
            authority.provider_id,
            authority.provider_kind,
            authority.provider_generation,
            authority.provider_execution_revision,
        )
        actual = (
            record.get("id"),
            record.get("kind"),
            record.get("generation"),
            record.get("execution_revision"),
        )
        if actual != expected:
            raise ValueError("headless provider authority changed")
        if authority.runner != runtime_profile.resolve_runner(
            record,
            record.get("runner"),
        ):
            raise ValueError("headless runner changed")
        if request.fork and not self.supports_fork:
            raise ValueError("headless provider does not support fork")
        if request.no_tools and not self.supports_headless_no_tools:
            raise ValueError("headless provider does not support no-tools")
        effort = authority.reasoning_effort
        if self.supports_reasoning_effort:
            if effort not in self.reasoning_effort_options:
                raise ValueError("headless reasoning effort is unsupported")
        elif effort:
            raise ValueError("headless provider does not support reasoning effort")
        if self.KIND not in {"claude", "openai"}:
            # The pair profile (tombstones included — a deleted profile never
            # blocks turns on old sessions) declares the configured model; a
            # pair with no profile history has nothing configured to protect.
            pair_profile = config_store.find_runtime_profile(
                str(record.get("id") or ""), str(record.get("runner") or "")
            )
            if pair_profile is not None and authority.model != str(
                pair_profile.get("default_model") or ""
            ):
                raise ValueError(
                    "headless provider cannot override its configured model"
                )
        hydration = config_store.hydrate_provider_execution(
            authority.provider_id,
            expected_generation=authority.provider_generation,
            expected_execution_revision=authority.provider_execution_revision,
        )
        if hydration is None:
            raise RuntimeError("headless provider authority is unavailable")
        runtime_record = dict(hydration.runtime_record())
        runtime_record["default_model"] = authority.model
        runtime_record["default_reasoning_effort"] = effort
        installed = getattr(self._execution_record, "value", None)
        self._execution_record.value = runtime_record
        try:
            result = await self.run_headless(
                prompt=request.prompt,
                resume_sid=authority.resume_sid,
                fork=request.fork,
                cwd=authority.cwd,
                timeout=request.timeout,
                no_tools=request.no_tools,
            )
        finally:
            if installed is None:
                del self._execution_record.value
            else:
                self._execution_record.value = installed
        if type(result) is not dict:
            raise RuntimeError("headless provider returned no result")
        return result

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
_PROVIDER_CACHE: dict[tuple[str, str], Provider] = {}
_CACHE_LOCK = threading.Lock()


class _ProviderKeyLock:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.users = 0


_PROVIDER_KEY_LOCKS: dict[tuple[str, str], _ProviderKeyLock] = {}


def reset_provider_cache_for_test_home() -> None:
    import paths

    if not paths.is_test_mode():
        raise RuntimeError("provider-cache test-home reset requires test mode")
    with _CACHE_LOCK:
        if _PROVIDER_KEY_LOCKS:
            raise RuntimeError("provider resolution is active during test-home reset")
        cached = dict(_PROVIDER_CACHE)
    for instance in cached.values():
        if instance.active_runs():
            raise RuntimeError("provider run is active during test-home reset")
        instance._deactivate_cache()
    with _CACHE_LOCK:
        if _PROVIDER_CACHE != cached:
            raise RuntimeError("provider cache changed during test-home reset")
        _PROVIDER_CACHE.clear()


@contextmanager
def _provider_key_guard(cache_key: tuple[str, str]):
    with _CACHE_LOCK:
        entry = _PROVIDER_KEY_LOCKS.get(cache_key)
        if entry is None:
            entry = _ProviderKeyLock()
            _PROVIDER_KEY_LOCKS[cache_key] = entry
        entry.users += 1
    started = time.perf_counter()
    entry.lock.acquire()
    try:
        perf.record(
            "provider.resolve.key_wait",
            (time.perf_counter() - started) * 1000.0,
        )
        yield
    finally:
        entry.lock.release()
        with _CACHE_LOCK:
            entry.users -= 1
            if entry.users == 0 and _PROVIDER_KEY_LOCKS.get(cache_key) is entry:
                _PROVIDER_KEY_LOCKS.pop(cache_key, None)


def prepare_and_start_run(provider: Provider, **start_arguments: Any) -> PreparedExecution:
    try:
        loop = start_arguments.pop("loop")
        queue = start_arguments.pop("queue")
    except KeyError as exc:
        raise TypeError("loop and queue are required") from exc
    with perf.timed("provider.prepare_run"):
        execution = provider.prepare_run(**start_arguments)
    with perf.timed("provider.start_run"):
        provider.start_run(execution=execution, loop=loop, queue=queue)
    return execution


def start_prepared_run(
    provider: Provider,
    execution: PreparedExecution,
    *,
    loop: asyncio.AbstractEventLoop,
    queue: asyncio.Queue,
) -> bool:
    return provider.start_run(execution=execution, loop=loop, queue=queue)


def _resolve_class(kind: str) -> type[Provider]:
    # Lazy import from the canonical manifest so provider_* subclasses can
    # import from this module without a cycle at import time. Virtual kinds
    # (claude-remote) are coordinator-side proxies, never resolved here.
    import importlib
    import provider_manifest
    spec = provider_manifest.spec_for(kind)
    if spec is None or spec.virtual:
        raise ValueError(f"unknown provider kind: {kind!r}")
    # Configuring a provider does not wait for its runtime, so a provider can
    # legitimately be selected before the activation that installs it. Say so
    # here, where the kind is known, instead of letting the runner module fail
    # on an import deep inside a detached subprocess.
    import dependency_plan
    dependency_plan.assert_provider_runtime_ready(kind)
    module = importlib.import_module(spec.module)
    return getattr(module, spec.cls)


def _provider_runtime_kind(record: dict) -> str:
    import runtime_profile
    return runtime_profile.runtime_kind(record, record.get("runner"))


def _provider_authority_token(record: dict) -> tuple[object, ...]:
    return (
        record.get("generation"),
        record.get("revision"),
        record.get("execution_revision"),
        record.get("kind"),
        record.get("mode"),
        record.get("suspended") is True,
        record.get("runner"),
        _provider_runtime_kind(record),
    )


def _provider_record_for_runner(record: dict, runner: Optional[str]) -> dict:
    import runtime_profile

    return runtime_profile.provider_record_for_runner(record, runner)


def _provider_record_matches(
    current: dict,
    expected: dict,
    runner: Optional[str],
) -> bool:
    if current.get("suspended") is True:
        return False
    try:
        normalized = _provider_record_for_runner(current, runner)
    except (RuntimeError, ValueError):
        return False
    return _provider_authority_token(normalized) == _provider_authority_token(expected)


def _cached_provider_candidates(
    provider_id: str,
) -> list[tuple[tuple[str, str], Provider]]:
    with _CACHE_LOCK:
        return [
            (cache_key, cached)
            for cache_key, cached in _PROVIDER_CACHE.items()
            if cache_key[0] == provider_id
        ]


def _resolve_unavailable_provider(
    provider_id: str,
    runner: Optional[str],
) -> Provider | None:
    requested_runner = str(runner or "").strip()
    with config_store.provider_state_read_transaction():
        with perf.timed("provider.resolve.config_read"):
            current = config_store.get_provider_with_key(provider_id)
        if current is not None:
            return None
        suspended = config_store.provider_suspended(provider_id)
        candidates = _cached_provider_candidates(provider_id)
        selected: Provider | None = None
        cleanup_failures: list[BaseException] = []
        for cache_key, cached in candidates:
            if selected is None and (
                not requested_runner or cache_key[1] == requested_runner
            ):
                selected = cached
            if suspended:
                cached.suspended = True
                cached.defunct = False
            else:
                try:
                    cached._deactivate_cache()
                except BaseException as exc:
                    cleanup_failures.append(exc)
                cached.defunct = True
                cached.suspended = False
        if cleanup_failures:
            first_failure = cleanup_failures[0]
            for failure in cleanup_failures[1:]:
                first_failure.add_note(
                    f"additional provider cache cleanup failure: {failure!r}"
                )
            raise first_failure
        if selected is not None:
            return selected
        if suspended:
            raise ProviderSuspendedError(
                f"provider {provider_id} is suspended; cannot start runs"
            )
        raise KeyError(provider_id)


def get_provider(provider_id: str, runner: Optional[str] = None) -> Provider:
    """Return the cached `Provider` for `provider_id`, refreshing its
    record from disk on every call so config edits are visible.

    A deleted provider with in-flight bookkeeping still resolves: we
    return the cached instance (marked defunct) so callers can cancel
    its runs cleanly. Only a provider that was *never* loaded raises
    `KeyError`.

    Concurrency is scoped to one provider/runner key. Parallel first lookups
    for the same key publish one instance, while unrelated providers and
    runners resolve independently. Config authority is revalidated atomically
    with each in-memory refresh or publication.
    """
    while True:
        with perf.timed("provider.resolve.config_read"):
            record = config_store.get_provider_with_key(provider_id)
        if record is None:
            unavailable = _resolve_unavailable_provider(provider_id, runner)
            if unavailable is not None:
                return unavailable
            continue
        with perf.timed("provider.resolve.runner"):
            record = _provider_record_for_runner(record, runner)
        cache_key = (provider_id, record["runner"])
        planned_authority = _provider_authority_token(record)
        with perf.timed("provider.resolve.runtime_class"):
            cls = _resolve_class(_provider_runtime_kind(record))
        with _provider_key_guard(cache_key):
            with perf.timed("provider.resolve.config_read"):
                current = config_store.get_provider_with_key(provider_id)
            if current is None:
                continue
            current = _provider_record_for_runner(current, runner)
            if (
                (provider_id, current["runner"]) != cache_key
                or _provider_authority_token(current) != planned_authority
            ):
                continue
            with _CACHE_LOCK:
                cached = _PROVIDER_CACHE.get(cache_key)
            if cached is not None and isinstance(cached, cls):
                was_defunct = cached.defunct

                def refresh() -> None:
                    if was_defunct:
                        cached._activate_cache()
                    try:
                        cached.record = current
                    except BaseException:
                        if was_defunct:
                            cached._deactivate_cache()
                        raise
                    cached.defunct = False
                    cached.suspended = False

                with perf.timed("provider.resolve.refresh"):
                    matched = config_store.apply_if_provider_matches(
                        provider_id,
                        lambda value: _provider_record_matches(value, current, runner),
                        refresh,
                    )
                if matched:
                    return cached
                continue
            if cached is not None:
                active_runs = cached.active_runs()
                if active_runs:
                    raise RuntimeError(
                        f"provider {provider_id} runtime changed while runs are active"
                    )
            with perf.timed("provider.resolve.construct"):
                instance = cls(current)

            def publish() -> None:
                instance._activate_cache()
                with _CACHE_LOCK:
                    if _PROVIDER_CACHE.get(cache_key) is not cached:
                        instance._deactivate_cache()
                        raise RuntimeError("provider cache authority changed during publish")
                    _PROVIDER_CACHE[cache_key] = instance
                if cached is not None:
                    try:
                        cached._deactivate_cache()
                    except BaseException:
                        with _CACHE_LOCK:
                            if _PROVIDER_CACHE.get(cache_key) is instance:
                                _PROVIDER_CACHE[cache_key] = cached
                        instance._deactivate_cache()
                        raise

            try:
                with perf.timed("provider.resolve.publish"):
                    matched = config_store.apply_if_provider_matches(
                        provider_id,
                        lambda value: _provider_record_matches(value, current, runner),
                        publish,
                    )
            except BaseException:
                instance._deactivate_cache()
                raise
            if matched:
                return instance
            instance._deactivate_cache()




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
    provider usage so active turns cannot keep spending that provider
    after the setting flips."""
    ids = set(run_ids or [])
    ids.update(_run_ids_for_provider(provider_id))
    with _CACHE_LOCK:
        cached_instances = [
            instance for (pid, _runner), instance in _PROVIDER_CACHE.items()
            if pid == provider_id
        ]
    for cached in cached_instances:
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
        for cached in cached_instances:
            try:
                signalled = bool(cached.cancel_run(run_id)) or signalled
            except Exception:
                logger.exception("cancel_provider_runs: cancel_run failed run=%s", run_id)
        count += 1 if signalled or not cached_instances else 0
    for cached in cached_instances:
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


# ============================================================================
# Cross-provider in-flight recovery
# ============================================================================
def _recovery_scan_parallelism(provider_count: int) -> int:
    if provider_count <= 1:
        return 1
    raw = os.environ.get(_RECOVERY_SCAN_PARALLELISM_ENV)
    if raw is None or not raw.strip():
        requested = _DEFAULT_RECOVERY_SCAN_PARALLELISM
    else:
        try:
            requested = int(raw)
        except ValueError:
            logger.warning(
                "invalid %s=%r; using default parallelism=%d",
                _RECOVERY_SCAN_PARALLELISM_ENV,
                raw,
                _DEFAULT_RECOVERY_SCAN_PARALLELISM,
            )
            requested = _DEFAULT_RECOVERY_SCAN_PARALLELISM
    return max(1, min(provider_count, _MAX_RECOVERY_SCAN_PARALLELISM, requested))


def recover_all_in_flight(
    loop: Optional[asyncio.AbstractEventLoop] = None,
    *,
    candidate_targets: Optional[set[tuple[str, str]]] = None,
    live_only: bool = False,
    exclude_live: bool = False,
) -> list[dict]:
    return _recover_all_in_flight_owned(
        loop,
        candidate_targets=candidate_targets,
        live_only=live_only,
        exclude_live=exclude_live,
    )


_RECOVERY_SCAN_OWNERSHIP: tuple[list[dict], bool] = ([], True)


def take_recovery_scan_ownership() -> tuple[list[dict], bool]:
    global _RECOVERY_SCAN_OWNERSHIP
    snapshot = _RECOVERY_SCAN_OWNERSHIP
    _RECOVERY_SCAN_OWNERSHIP = ([], True)
    return snapshot


def _recover_all_in_flight_owned(
    loop: Optional[asyncio.AbstractEventLoop] = None,
    *,
    candidate_targets: Optional[set[tuple[str, str]]] = None,
    live_only: bool = False,
    exclude_live: bool = False,
) -> list[dict]:
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
    global _RECOVERY_SCAN_OWNERSHIP
    _RECOVERY_SCAN_OWNERSHIP = ([], True)
    runs_root = _runs_root()
    if not runs_root.exists():
        return []
    live_run_ids = _live_runner_run_ids(runs_root)
    total_started = time.perf_counter()
    phase_started = time.perf_counter()
    if not live_only:
        ensure_reconciled_marker_index_backfilled(runs_root)
    perf.record(
        "startup.recovery.marker_backfill",
        (time.perf_counter() - phase_started) * 1000.0,
    )
    phase_started = time.perf_counter()
    reconciled_index = (
        {} if live_only else load_reconciled_marker_index(runs_root)
    )
    perf.record(
        "startup.recovery.marker_index_load",
        (time.perf_counter() - phase_started) * 1000.0,
    )

    ownership_documents: list[dict] = []
    ownership_safe = True
    candidate_sids = (
        {sid for sid, _ in candidate_targets}
        if candidate_targets is not None
        else None
    )

    # Group run_ids by owning provider_id.
    by_provider: dict[tuple[Optional[str], str], list[str]] = {}
    enumerated = 0
    indexed_skips = 0
    marker_fallback_reads = 0
    backend_state_reads = 0
    phase_started = time.perf_counter()

    def cleanup_reconciled_codex_payload(
        run_dir: Path,
        provider_kind: str,
    ) -> None:
        if provider_kind not in {"codex", "fugu"}:
            return
        try:
            from codex_execution_runtime import (
                cleanup_installed_codex_runtime_agent_payload,
            )

            cleanup_installed_codex_runtime_agent_payload(run_dir)
        except OSError:
            logger.exception(
                "failed cleaning reconciled Codex payload run=%s",
                run_dir.name,
            )

    if live_only:
        run_dirs = (runs_root / run_id for run_id in live_run_ids)
    else:
        run_dirs = (
            child
            for child in runs_root.iterdir()
            if not (exclude_live and child.name in live_run_ids)
        )
    for child in run_dirs:
        if child.is_symlink():
            ownership_safe = False
            continue
        if not child.is_dir():
            continue
        enumerated += 1
        indexed_marker = reconciled_index.get(child.name)
        if (
            indexed_marker is not None
            and reconciled_marker_index_row_matches(child, indexed_marker)
            and marker_data_matches_current(
                indexed_marker,
                str(indexed_marker.get("provider_kind") or ""),
            )
        ):
            cleanup_reconciled_codex_payload(
                child,
                str(indexed_marker.get("provider_kind") or ""),
            )
            indexed_skips += 1
            continue
        marker_path = child / "reconciled.marker"
        if marker_path.is_symlink():
            ownership_safe = False
        elif marker_path.exists():
            marker_fallback_reads += 1
            try:
                marker = json.loads(marker_path.read_text(encoding="utf-8"))
                if marker_data_matches_current(
                    marker,
                    str(marker.get("provider_kind") or ""),
                ):
                    cleanup_reconciled_codex_payload(
                        child,
                        str(marker.get("provider_kind") or ""),
                    )
                    append_reconciled_marker_index(
                        marker_path,
                        str(marker.get("provider_kind") or ""),
                        int(marker.get("ingestion_version")),
                        root=runs_root,
                    )
                    indexed_skips += 1
                    continue
            except Exception:
                pass
        bs_path = child / "backend_state.json"
        input_path = child / "input.json"
        execution_path = child / "execution.json"
        pid: Optional[str] = None
        runner = ""
        documents: list[dict] = []
        execution_document: Optional[dict] = None
        execution_kind = ""
        if execution_path.exists():
            from execution_artifact_io import (
                load_execution_artifact,
                requires_execution_artifact,
            )

            try:
                artifact = load_execution_artifact(
                    child,
                    validate_input=input_path.exists(),
                )
                arguments = artifact.template.arguments()
                execution_document = {
                    "persist_to": (
                        arguments.get("worker_agent_session_id")
                        or arguments["app_session_id"]
                    ),
                    "target_message_id": arguments.get(
                        "target_message_id",
                    ),
                    "provider_id": artifact.provider_id,
                }
                execution_kind = artifact.provider_kind
            except Exception:
                try:
                    raw_execution = json.loads(
                        execution_path.read_text(encoding="utf-8"),
                    )
                    failed_kind = str(
                        raw_execution.get("provider_kind") or "",
                    )
                except Exception:
                    failed_kind = ""
                if requires_execution_artifact(failed_kind):
                    ownership_safe = False
                    continue
        backend_document_error = False
        if bs_path.exists():
            backend_state_reads += 1
            try:
                if bs_path.is_symlink():
                    raise ValueError("invalid backend_state ownership document")
                bs = json.loads(bs_path.read_text(encoding="utf-8"))
                if not isinstance(bs, dict):
                    raise ValueError("invalid backend_state ownership document")
                documents.append(bs)
                pid = bs.get("provider_id")
                runner = str(bs.get("runner") or "").strip()
            except Exception:
                backend_document_error = True
        backend_sid = str(
            (
                documents[0].get("persist_to")
                or documents[0].get("worker_agent_session_id")
                or documents[0].get("app_session_id")
                or ""
            )
            if documents else ""
        )
        inspect_input = (
            candidate_sids is None
            or not backend_sid
            or backend_sid in candidate_sids
        )
        input_document: Optional[dict] = None
        input_document_error = False
        if inspect_input and input_path.exists():
            try:
                if input_path.is_symlink():
                    raise ValueError("invalid input ownership document")
                inp = json.loads(input_path.read_text(encoding="utf-8"))
                if not isinstance(inp, dict):
                    raise ValueError("invalid input ownership document")
                documents.append(inp)
                input_document = inp
            except Exception:
                input_document_error = True
        input_sid = str(
            (
                input_document.get("persist_to")
                or input_document.get("worker_agent_session_id")
                or input_document.get("app_session_id")
                or ""
            )
            if input_document else ""
        )
        if (
            execution_document is not None
            and requires_execution_artifact(execution_kind)
        ):
            documents = [execution_document]
            pid = str(execution_document["provider_id"])
            runner = ""
            backend_sid = str(execution_document["persist_to"])
            input_sid = backend_sid
            backend_document_error = False
            input_document_error = False
        relevant_or_unknown = bool(
            candidate_sids is None
            or not (backend_sid or input_sid)
            or backend_sid in candidate_sids
            or input_sid in candidate_sids
        )
        if relevant_or_unknown and (backend_document_error or input_document_error):
            ownership_safe = False
        for document in documents:
            persist_sid = str(
                document.get("persist_to")
                or document.get("worker_agent_session_id")
                or document.get("app_session_id")
                or ""
            )
            if not persist_sid:
                if relevant_or_unknown:
                    ownership_safe = False
                continue
            if candidate_sids is not None and persist_sid not in candidate_sids:
                continue
            ownership_documents.append({
                "persist_to": persist_sid,
                "target_message_id": document.get("target_message_id"),
                "run_id": child.name,
            })
        by_provider.setdefault((pid, runner), []).append(child.name)
    _RECOVERY_SCAN_OWNERSHIP = (ownership_documents, ownership_safe)
    perf.record(
        "startup.recovery.discovery",
        (time.perf_counter() - phase_started) * 1000.0,
    )
    perf.record_count("startup.recovery.discovery.dirs", enumerated)
    perf.record_count("startup.recovery.discovery.indexed_skips", indexed_skips)
    perf.record_count(
        "startup.recovery.discovery.marker_fallback_reads", marker_fallback_reads,
    )
    perf.record_count(
        "startup.recovery.discovery.backend_state_reads", backend_state_reads,
    )

    results: list[dict] = []
    # Fall back: runs without a provider_id go to the active provider
    # (legacy data; fix is forward-only).
    fallback_id: Optional[str] = None
    if any(pid is None for pid, _runner in by_provider):
        try:
            fallback_id = default_provider().id
        except Exception:
            fallback_id = None
    import logging
    log = logging.getLogger(__name__)

    scan_inputs: list[tuple[str, Provider, set[str]]] = []
    phase_started = time.perf_counter()
    for (pid, runner), run_ids in by_provider.items():
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
            owner = get_provider(owner_id, runner or None)
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
        likely_running, other = _split_recovery_scan_run_ids(runs_root, set(run_ids))
        if likely_running:
            scan_inputs.append((owner_id, owner, likely_running))
        if other:
            scan_inputs.append((owner_id, owner, other))
    perf.record(
        "startup.recovery.owner_resolution",
        (time.perf_counter() - phase_started) * 1000.0,
    )
    perf.record_count("startup.recovery.owner_buckets", len(scan_inputs))

    parallelism = _recovery_scan_parallelism(len(scan_inputs))
    started = time.monotonic()
    if scan_inputs:
        log.info(
            "recover_all_in_flight: classifying %d provider bucket(s) "
            "with parallelism=%d",
            len(scan_inputs),
            parallelism,
        )

    def _scan_one(owner_id: str, owner: Provider, run_ids: set[str]) -> list[dict]:
        del owner_id
        scan_started = time.perf_counter()
        try:
            recovered = owner.recover_in_flight(loop=loop, run_id_filter=run_ids)
        except BaseException:
            perf.record_count("startup.recovery.provider_scan.error", 1)
            raise
        perf.record_count("startup.recovery.provider_scan.success", 1)
        perf.record_count("startup.recovery.provider_scan.runs", len(run_ids))
        perf.record(
            "startup.recovery.provider_scan",
            (time.perf_counter() - scan_started) * 1000.0,
        )
        runner = str(owner.record.get("runner") or "").strip()
        for descriptor in recovered:
            descriptor.setdefault("runner", runner)
        return recovered

    if parallelism <= 1:
        for owner_id, owner, run_ids in scan_inputs:
            results.extend(_scan_one(owner_id, owner, run_ids))
    else:
        failures: list[tuple[str, BaseException]] = []
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=parallelism,
            thread_name_prefix="provider-recovery-scan",
        ) as executor:
            future_to_owner = {
                executor.submit(_scan_one, owner_id, owner, run_ids): owner_id
                for owner_id, owner, run_ids in scan_inputs
            }
            for future in concurrent.futures.as_completed(future_to_owner):
                owner_id = future_to_owner[future]
                try:
                    results.extend(future.result())
                except Exception as exc:
                    failures.append((owner_id, exc))
                    log.exception(
                        "recover_all_in_flight: provider %s scan failed", owner_id,
                    )
        if failures:
            failed_ids = ",".join(owner_id for owner_id, _exc in failures)
            raise RuntimeError(
                f"recover_all_in_flight: provider scan failed for {failed_ids}"
            ) from failures[0][1]
    if scan_inputs:
        log.info(
            "recover_all_in_flight: classified %d recovered run(s) from %d "
            "provider bucket(s) in %.3fs",
            len(results),
            len(scan_inputs),
            time.monotonic() - started,
        )
    perf.record(
        "startup.recovery.total",
        (time.perf_counter() - total_started) * 1000.0,
    )
    perf.record_count("startup.recovery.recovered", len(results))
    return results
