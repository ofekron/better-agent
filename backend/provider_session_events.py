"""SessionEventsProvider — shared `Provider` base for every CLI-backed
provider whose runner normalizes its native stream to Claude-shaped
`session_events.jsonl` (recovery_family="session_events").

Subclasses supply the concrete CLI: `KIND`, `build_env`, `start_run`
(which spawns their own detached runner) and `run_headless`. Everything
else — run bookkeeping, the events tail/bootstrap, completion watching,
backend_state.json, crash recovery, pruning and rate-limit parsing — is
implemented once here.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, ClassVar, Optional

from provider import (
    Provider,
    RecoveredPopen,
    StreamEvent,
    await_line_tailer_drained,
    build_better_agent_run_env,
    path_exists_off_loop,
    popen_is_running_off_loop,
    schedule_loop_task,
)
from containment import containment
from provider_watch_helpers import emit_early_failure, wait_for_complete_or_process_death
from provider_family_execution_runtime import (
    cleanup_failed_family_execution,
    install_family_execution_payload,
    release_staged_family_execution,
    resolve_family_execution_payload,
)
from provider_runtime_plan_source import hydrate_frozen_provider_runtime_plan
from proc_control import process_control
from ingestion_versions import current_ingestion_version, marker_matches_current
import config_store
from process_identity import process_identity_to_dict
from runs_dir import (
    atomic_write_json as _atomic_write_json,
    iter_run_dirs,
    pid_alive as _pid_alive,
    prune_old_completed_runs,
    runs_root as _runs_root,
)

logger = logging.getLogger(__name__)


_TAIL_POLL_INTERVAL = 0.05
_RUNNER_EVENT_TYPES = {"agent_message", "worker_start", "worker_event", "worker_complete"}


def _read_json_file(path: Path) -> Any:
    """Sync read+parse — run via `asyncio.to_thread` from async callers."""
    return json.loads(path.read_text(encoding="utf-8"))


def runner_event_to_stream_event(event: dict) -> StreamEvent:
    event_type = event.get("type")
    event_data = event.get("data")
    if event_type in _RUNNER_EVENT_TYPES and isinstance(event_data, dict):
        return StreamEvent(event_type, event_data)
    return StreamEvent("agent_message", event)


# ============================================================================
# RunState — per-run bookkeeping (mirrors ClaudeProvider.RunState)
# ============================================================================
@dataclass
class RunState:
    run_id: str
    run_dir: Path
    popen: subprocess.Popen
    mode: str
    app_session_id: str
    queue: asyncio.Queue
    session_id: Optional[str] = None
    processed_line: int = 0
    tailer: Optional["object"] = None  # SessionEventsJsonlTailer; typed loosely to avoid import cycle
    tailer_task: Optional[asyncio.Task] = None
    complete_task: Optional[asyncio.Task] = None
    started_at: str = ""
    cancelled: bool = False
    turn_cancelled: bool = False
    # Where this run's messages PERSIST. In supervisor mode, a worker
    # turn's events route to the worker Better Agent session even though the run
    # is bookkept under the supervisor's app_session_id. Mirrors
    # ClaudeProvider.RunState.persist_to.
    persist_to: str = ""
    target_message_id: Optional[str] = None
    turn_run_id: Optional[str] = None
    runner: str = ""
    runner_identity: Optional[dict] = None


# ============================================================================
# SessionEventsProvider
# ============================================================================
class SessionEventsProvider(Provider):
    """Base for providers driven by a detached runner that writes
    Claude-shaped `session_events.jsonl`. The provider tails that file
    and pushes events onto the orchestrator queue."""

    # These CLIs expose no non-interactive fork primitive. Every
    # fork-using feature (fork-and-send, prompt-engineer refine,
    # manager-mode delegate-fork) must read this
    # flag and disable itself for such sessions.
    supports_fork: ClassVar[bool] = False
    # These CLIs use provider-native MCP/settings files, not the
    # in-process SDK MCP registration path that Claude uses for manager
    # mode.
    supports_manager_mode: ClassVar[bool] = False
    # No native rewind primitive, but it is simulated by starting a
    # fresh turn without --resume when the user wants to abandon a
    # stuck/broken session.
    supports_rewind: ClassVar[bool] = True
    rewind_requires_agent_identity: ClassVar[bool] = False

    def __init__(self, record: dict) -> None:
        super().__init__(record)
        self._runs: dict[str, RunState] = {}

    def prepare_run(self, **start_arguments: Any):
        from provider_session_events_execution import (
            prepare_session_events_execution,
        )

        return prepare_session_events_execution(self, start_arguments)

    def _install_execution_payloads(self, execution, run_dir: Path) -> None:
        install_family_execution_payload(execution, run_dir)

    def _cleanup_failed_execution_payloads(
        self,
        execution,
        run_dir: Path,
    ) -> None:
        cleanup_failed_family_execution(execution, run_dir)

    def _release_execution_authority(self, execution) -> None:
        release_staged_family_execution(execution)

    def start_session_events_execution(
        self,
        *,
        execution,
        loop: asyncio.AbstractEventLoop,
        queue: asyncio.Queue,
        internal_token: str | None,
        extra_env: dict[str, str] | None,
    ) -> None:
        arguments = execution.start_arguments()
        run_id = arguments["run_id"]
        run_dir = _runs_root() / run_id
        launch, capabilities = resolve_family_execution_payload(
            execution.artifact,
            run_dir,
        )
        inputs = execution.artifact.runtime_policy.get("runner_input")
        if type(inputs) is not dict:
            raise RuntimeError("frozen session-events runner input is unavailable")
        hydrated_plan = hydrate_frozen_provider_runtime_plan(capabilities.plan)
        runtime_hydration = {
            "capability_plan": hydrated_plan,
            "prewarm_status": capabilities.prewarm_status,
            "skill_dirs": {
                name: str(path)
                for name, path in capabilities.skill_dirs.items()
            },
        }
        from execution_artifact_io import bind_execution_input

        _atomic_write_json(
            run_dir / "input.json",
            bind_execution_input(execution.artifact, inputs),
        )
        from execution_spawn_authority import consume_execution_spawn_authority

        consume_execution_spawn_authority(execution.artifact, run_dir)
        containment().create(run_id)
        stdout_fp = (run_dir / "stdout.log").open("ab")
        stderr_fp = (run_dir / "stderr.log").open("ab")
        try:
            with launch.open_runner() as pinned:
                # Env is built after runner materialization: the runtime
                # bootstrap issued inside build_better_agent_run_env is a
                # short single-use lease that must start at spawn time.
                env = self.finalize_run_env(
                    self.build_env(),
                    run_id=run_id,
                    app_session_id=inputs["app_session_id"],
                    resolved_harness_run_config=inputs.get(
                        "resolved_harness_run_config",
                    ),
                )
                if extra_env:
                    env.update(extra_env)
                env.update(build_better_agent_run_env(
                    backend_url=inputs["backend_url"],
                    internal_token=internal_token,
                    run_id=run_id,
                    app_session_id=inputs["app_session_id"],
                    cwd=inputs["cwd"],
                    model=inputs.get("model"),
                    provider_id=self.id,
                    bare_config=bool(inputs["bare_config"]),
                    user_facing=bool(inputs["user_facing"])
                    and not bool(inputs["bare_config"]),
                    disabled_builtin_extensions=inputs[
                        "disabled_builtin_extensions"
                    ],
                    runtime_hydration=runtime_hydration,
                ))
                pass_fds = (
                    {"pass_fds": pinned.pass_fds}
                    if os.name != "nt" and pinned.pass_fds
                    else {}
                )
                popen = subprocess.Popen(
                    list(pinned.argv),
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_fp,
                    stderr=stderr_fp,
                    cwd=inputs["cwd"],
                    env=env,
                    **pass_fds,
                    **process_control().detach_spawn_kwargs(),
                    **containment().spawn_kwargs(run_id),
                )
        except BaseException:
            containment().teardown(run_id)
            raise
        finally:
            stdout_fp.close()
            stderr_fp.close()
        containment().after_spawn(run_id, popen.pid)
        state = RunState(
            run_id=run_id,
            run_dir=run_dir,
            popen=popen,
            mode=inputs["canonical_mode"],
            app_session_id=inputs["app_session_id"],
            queue=queue,
            started_at=datetime.now(timezone.utc).isoformat(),
            persist_to=(
                inputs.get("worker_agent_session_id")
                or inputs["app_session_id"]
            ),
            target_message_id=inputs.get("target_message_id"),
            turn_run_id=inputs.get("turn_run_id"),
            runner=str(inputs.get("runner") or ""),
            runner_identity=process_identity_to_dict(popen.pid),
        )
        self._runs[run_id] = state
        self._write_backend_state(state)
        schedule_loop_task(
            loop,
            self._bootstrap_run(state),
            name=f"{self.KIND}-bootstrap-{run_id[:8]}",
        )

    # ------------------------------------------------------------------
    # _bootstrap_run — wait for state.json, then tail session_events.jsonl
    # ------------------------------------------------------------------
    async def _bootstrap_run(self, rs: RunState) -> None:
        state_path = rs.run_dir / "state.json"
        complete_path = rs.run_dir / "complete.json"
        events_path = rs.run_dir / "session_events.jsonl"

        # 1) Poll for state.json
        runner_state: Optional[dict] = None
        while True:
            if await path_exists_off_loop(state_path):
                try:
                    parsed = await asyncio.to_thread(_read_json_file, state_path)
                    if parsed.get("session_id"):
                        runner_state = parsed
                        break
                except (json.JSONDecodeError, OSError):
                    pass

            # Runner is dead — enter regardless of state.json existing.
            # state.json with null session_id + dead runner is a pre-run
            # failure (e.g. invalid --resume target); the old
            # `and not state_path.exists()` gate would spin forever.
            if not await popen_is_running_off_loop(rs.popen):
                if await path_exists_off_loop(complete_path):
                    break
                await self._emit_early_failure(
                    rs, f"runner exited early with code {rs.popen.returncode}"
                )
                return
            await asyncio.sleep(_TAIL_POLL_INTERVAL)

        if runner_state is None:
            await self._emit_complete_from_file(rs, complete_path)
            self._cleanup_run(rs.run_id)
            return

        session_id = runner_state["session_id"]
        rs.session_id = session_id
        # Persist the discovered sid into backend_state.json NOW so a
        # crash between session_discovered and the first tailer cursor
        # advance still surfaces the sid to run_recovery on restart.
        self._write_backend_state(rs)

        # 2) Emit session_discovered
        try:
            rs.queue.put_nowait(StreamEvent("session_discovered", {"session_id": session_id}))
        except Exception:
            logger.exception("failed to enqueue session_discovered")

        # 3) Start the polling tailer on session_events.jsonl. Same
        # JsonlEventTailer base as ClaudeJsonlTailer; the concrete
        # `_open_source` / `_next_line` differ (polling read vs tail -F)
        # but cancel + dispatch + cursor are shared.
        from jsonl_tailer import SessionEventsJsonlTailer

        def _dispatch_to_queue(event: dict, _rs: RunState = rs) -> None:
            try:
                _rs.queue.put_nowait(runner_event_to_stream_event(event))
            except Exception:
                logger.exception(
                    "SessionEventsJsonlTailer dispatch: put_nowait failed for run %s",
                    _rs.run_id,
                )

        def _on_cursor(n: int, _rs: RunState = rs) -> None:
            # Mirror ClaudeProvider._on_tailer_progress: called
            # synchronously from the tailer's read loop, so this MUST
            # stay non-blocking. In-memory state updates immediately
            # (cheap; this is what the deterministic drain polls); the
            # actual `backend_state.json` write hands off to
            # `cursor_ledger_worker`, off this call path entirely.
            _rs.processed_line = n
            from cursor_ledger_worker import worker as cursor_ledger_worker
            cursor_ledger_worker.note(_rs.run_id, lambda: self._write_backend_state(_rs))

        rs.tailer = SessionEventsJsonlTailer(
            path=events_path,
            start_offset=rs.processed_line,
            dispatch=_dispatch_to_queue,
            on_cursor_advance=_on_cursor,
        )
        rs.tailer_task = asyncio.get_event_loop().create_task(
            rs.tailer.run(),
            name=f"session-events-tailer-{rs.run_id[:8]}",
        )

        # 4) Schedule completion watcher
        rs.complete_task = asyncio.get_event_loop().create_task(
            self._watch_complete(rs),
            name=f"session-events-complete-{rs.run_id[:8]}",
        )

    # ------------------------------------------------------------------
    # _watch_complete
    # ------------------------------------------------------------------
    async def _watch_complete(self, rs: RunState) -> None:
        complete_path = rs.run_dir / "complete.json"
        try:
            # INVARIANT: process death MUST end this wait. If the runner is
            # SIGKILLed (OOM, manual kill, OS) it never writes complete.json
            # — the old "complete.json AND process dead" condition would
            # spin forever, leaving the turn stuck in flight forever.
            # Returning on process-dead alone lets `_emit_complete_from_file`'s
            # built-in fallback (`error="runner exited without writing
            # complete.json"`) synthesize the error complete event. A short
            # grace window lets a normal exit's complete.json land before we
            # synthesize.
            await wait_for_complete_or_process_death(
                complete_path=complete_path,
                popen=rs.popen,
                poll_interval=_TAIL_POLL_INTERVAL,
            )

            # Deterministic drain: the runner appends every event line
            # BEFORE writing complete.json, so wait until the tailer's
            # line cursor covers the file as it stands now. A fixed
            # sleep guess let `complete` overtake trailing lines when
            # the poll tailer lagged — the turn loop then broke and the
            # lines never reached the render tree (stale-content grabs).
            await await_line_tailer_drained(
                path=rs.run_dir / "session_events.jsonl",
                get_cursor=lambda: rs.processed_line,
                run_id=rs.run_id,
                on_drained=lambda: self._flush_cursor_ledger(rs),
            )
            if rs.tailer is not None:
                rs.tailer.stop()
            if rs.tailer_task is not None:
                try:
                    await asyncio.wait_for(rs.tailer_task, timeout=2.0)
                except asyncio.TimeoutError:
                    logger.warning(
                        "session-events tailer did not exit in time for %s", rs.run_id,
                    )
                except Exception:
                    logger.exception(
                        "session-events tailer task failed for %s", rs.run_id,
                    )
            await self._emit_complete_from_file(rs, complete_path)
        finally:
            self._cleanup_run(rs.run_id)

    # ------------------------------------------------------------------
    # _emit_complete_from_file
    # ------------------------------------------------------------------
    async def _emit_complete_from_file(self, rs: RunState, complete_path: Path) -> None:
        payload: dict[str, Any] = {
            "success": False,
            "error": "runner exited without writing complete.json",
            "session_id": rs.session_id,
            "token_usage": None,
        }
        if await path_exists_off_loop(complete_path):
            try:
                payload = await asyncio.to_thread(_read_json_file, complete_path)
            except Exception:
                logger.exception("failed to parse complete.json for %s", rs.run_id)
        try:
            rs.queue.put_nowait(StreamEvent("complete", payload))
        except Exception:
            logger.exception("failed to enqueue complete for %s", rs.run_id)

    # ------------------------------------------------------------------
    # _emit_early_failure
    # ------------------------------------------------------------------
    async def _emit_early_failure(self, rs: RunState, msg: str) -> None:
        await emit_early_failure(
            logger=logger,
            log_prefix="session-events",
            run_id=rs.run_id,
            msg=msg,
            queue=rs.queue,
            cleanup=lambda: self._cleanup_run(rs.run_id),
        )

    # _backend_state_path / _read_backend_state inherited from
    # AbstractStreamingProvider. is_running / cancel_all / active_runs /
    # runs_for_session / _cleanup_run / cancel_run all inherited.

    def _write_backend_state(self, rs: RunState) -> None:
        """Provider-specific backend_state.json contents.
        Mirrors `ClaudeProvider._write_backend_state` (run_id /
        app_session_id / mode / runner_pid / started_at / session_id /
        processed_line / cancelled / provider_id / persist_to /
        jsonl_path) so `run_recovery._integrate_one` reads the same
        keys regardless of provider kind."""
        data = {
            "run_id": rs.run_id,
            "app_session_id": rs.app_session_id,
            "persist_to": rs.persist_to or rs.app_session_id,
            "mode": rs.mode,
            "runner_pid": rs.popen.pid,
            "runner_identity": rs.runner_identity,
            "started_at": rs.started_at,
            "session_id": rs.session_id,
            "jsonl_path": str(rs.run_dir / "session_events.jsonl"),
            "processed_line": rs.processed_line,
            "cancelled": rs.cancelled,
            "turn_cancelled": getattr(rs, "turn_cancelled", False),
            "target_message_id": rs.target_message_id,
            "turn_run_id": rs.turn_run_id,
            "provider_id": self.id,
            "provider_kind": self.KIND,
            "ingestion_version": current_ingestion_version(self.KIND),
            "runner": rs.runner,
        }
        try:
            _atomic_write_json(self._backend_state_path(rs), data)
            if rs.session_id:
                import spawn_ledger
                spawn_ledger.record_discovered(rs.session_id)
        except Exception:
            logger.exception("failed to write backend_state.json for %s", rs.run_id)

    async def _flush_cursor_ledger(self, rs: RunState) -> None:
        """Block until `cursor_ledger_worker` has written this run's
        latest known cursor to `backend_state.json`, once a drain
        concludes — crash recovery must see the true final cursor, not
        whatever was last coalesced. Off-loop so the event loop itself
        never blocks on the write."""
        from cursor_ledger_worker import worker as cursor_ledger_worker
        await asyncio.to_thread(cursor_ledger_worker.flush_now, rs.run_id)

    def attach_recovered_run(
        self,
        *,
        desc: dict,
        queue: asyncio.Queue,
        loop: asyncio.AbstractEventLoop,
    ) -> bool:
        """Re-attach a still-running detached session-events-family runner after
        a backend restart.

        The recovered descriptor proves the runner is still alive. Rebuild the
        in-memory RunState and restart the normal tailer/completion watcher so
        post-restart events are streamed immediately and the turn finalizes in
        this backend lifetime.
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
            processed_line = int(desc.get("processed_line") or 0)
        except (TypeError, ValueError):
            processed_line = 0

        rs = RunState(
            run_id=run_id,
            run_dir=_runs_root() / run_id,
            popen=RecoveredPopen(runner_pid),
            mode=desc.get("mode") or "native",
            app_session_id=desc.get("app_session_id") or "",
            queue=queue,
            session_id=desc.get("session_id"),
            processed_line=processed_line,
            started_at=(
                desc.get("started_at")
                or datetime.now(timezone.utc).isoformat()
            ),
            runner_identity=desc.get("runner_identity"),
            cancelled=bool(desc.get("cancelled", False)),
            turn_cancelled=bool(desc.get("turn_cancelled", False)),
            persist_to=desc.get("persist_to") or desc.get("app_session_id") or "",
            target_message_id=desc.get("target_message_id"),
            turn_run_id=desc.get("turn_run_id"),
            runner=str(desc.get("runner") or ""),
        )
        self._runs[run_id] = rs
        self._write_backend_state(rs)
        schedule_loop_task(
            loop,
            self._bootstrap_run(rs),
            name=f"{self.KIND}-recover-bootstrap-{run_id[:8]}",
        )
        return True

    def _post_cancel_hook(self, rs: RunState) -> None:
        """Wake the tailer's stop_event so it exits its poll-sleep
        promptly rather than waiting up to _POLL_INTERVAL."""
        if rs.tailer is not None:
            try:
                rs.tailer.stop()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # recover_in_flight
    # ------------------------------------------------------------------
    def recover_in_flight(
        self,
        loop: Optional[asyncio.AbstractEventLoop] = None,
        run_id_filter: Optional[set[str]] = None,
    ) -> list[dict]:
        """Mirror `ClaudeProvider.recover_in_flight`'s descriptor shape
        so `run_recovery._integrate_one` reads identical keys regardless
        of provider kind.

        DEAD orphans get a synthesized complete.json + a full descriptor
        so the orchestrator can replay events into the assistant
        message. LIVE orphans return descriptors too so startup
        recovery can re-register active runs before accepting new
        prompts for the same session."""
        del loop

        recovered: list[dict] = []
        if not _runs_root().exists():
            return recovered

        if config_store.provider_suspended(self.id):
            return recovered

        for child in iter_run_dirs(run_id_filter):
            marker_path = child / "reconciled.marker"
            if marker_path.exists() and marker_matches_current(marker_path, self.KIND):
                continue
            complete_path = child / "complete.json"
            has_complete_json = complete_path.exists()

            backend_state_path = child / "backend_state.json"
            runner_state_path = child / "state.json"
            bs: dict = {}
            rs_disk: dict = {}
            if backend_state_path.exists():
                try:
                    bs = json.loads(backend_state_path.read_text(encoding="utf-8"))
                except Exception:
                    pass
            if runner_state_path.exists():
                try:
                    rs_disk = json.loads(runner_state_path.read_text(encoding="utf-8"))
                except Exception:
                    pass

            pid: Optional[int] = None
            try:
                pid = int(bs.get("runner_pid")) if bs.get("runner_pid") else None
            except (TypeError, ValueError):
                pass

            alive = _pid_alive(pid) if pid else False

            live_orphan = alive and not has_complete_json

            if live_orphan:
                logger.info(
                    "session-events recover_in_flight: live orphan %s (pid=%s) "
                    "still running; re-attaching for recovery",
                    child.name, pid,
                )

            if not live_orphan and not has_complete_json:
                # Dead orphan — materialize complete.json so future scans
                # skip and the replay path is unambiguous. Prefer the
                # BA-owned runner's `terminal.json` checkpoint when present:
                # it is the authoritative success/failure record written
                # (atomically, just before complete.json) by
                # runner_better_agent, so a crash in the tiny window between
                # the two writes is restored from the checkpoint instead of
                # being reported as a synthetic "runner died" failure.
                terminal_path = child / "terminal.json"
                source: Optional[dict] = None
                if terminal_path.exists():
                    try:
                        source = json.loads(terminal_path.read_text(encoding="utf-8"))
                    except Exception:
                        logger.exception(
                            "failed to read terminal.json for %s; synthesizing error",
                            child.name,
                        )
                payload = source if source is not None else {
                    "success": False,
                    "session_id": bs.get("session_id") or rs_disk.get("session_id"),
                    "error": "runner died before completion (recovered at startup)",
                    "token_usage": None,
                    "finished_at": datetime.now().isoformat(),
                }
                try:
                    complete_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
                    has_complete_json = True
                except Exception:
                    logger.exception(
                        "failed to write recovery complete.json for %s", child.name,
                    )

            try:
                processed_line = int(bs.get("processed_line") or 0)
            except (TypeError, ValueError):
                processed_line = 0

            recovered.append({
                "run_id": child.name,
                "pid": pid,
                "runner_identity": bs.get("runner_identity"),
                "alive": live_orphan,
                "has_complete_json": has_complete_json,
                "session_id": bs.get("session_id") or rs_disk.get("session_id"),
                "jsonl_path": (
                    bs.get("jsonl_path")
                    or rs_disk.get("jsonl_path")
                    or str(child / "session_events.jsonl")
                ),
                "app_session_id": bs.get("app_session_id") or rs_disk.get("app_session_id"),
                "persist_to": bs.get("persist_to") or bs.get("app_session_id"),
                "started_at": bs.get("started_at") or rs_disk.get("started_at") or "",
                "processed_line": processed_line,
                "cancelled": bool(bs.get("cancelled", False)),
                "turn_cancelled": bool(bs.get("turn_cancelled", False)),
                "mode": bs.get("mode") or rs_disk.get("mode") or "native",
                "provider_id": bs.get("provider_id") or self.id,
                "provider_kind": bs.get("provider_kind") or self.KIND,
                "ingestion_version": bs.get("ingestion_version"),
                "target_message_id": bs.get("target_message_id"),
                "turn_run_id": bs.get("turn_run_id"),
                "runner": bs.get("runner"),
                "recovered_as": "live_orphan" if live_orphan else "dead_orphan",
            })

        return recovered

    # ------------------------------------------------------------------
    # prune_old_runs
    # ------------------------------------------------------------------
    def prune_old_runs(self, max_age_days: int = 7) -> int:
        return prune_old_completed_runs(max_age_days)

    # ------------------------------------------------------------------
    # Models — inherits `Provider.available_models()` which routes
    # through `models.models_for_provider(self.id)` → the disk-backed
    # catalog written by the daily refresher. NO override here —
    # overriding would re-introduce
    # the cache-vs-gate divergence where the dropdown shows a freshly-
    # scraped model but `start_run` rejects it because the validator
    # reads the static list directly.
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Rate-limit parsing — daily quotas + RESOURCE_EXHAUSTED shapes.
    # ------------------------------------------------------------------
    _RATE_LIMIT_KEYWORDS = (
        "rate limit", "quota exceeded", "resource exhausted",
        "exhausted your capacity", "status: 429", "error 429",
        "too many requests",
    )

    def parse_rate_limit(
        self, error: Optional[str], events: list[dict],
    ) -> Optional[datetime]:
        """Parse the rate-limit reset time from error / event text."""
        texts: list[str] = []
        if error:
            texts.append(error[-2000:] if len(error) > 2000 else error)
        extracted = self._extract_text_for_rate_limit(events)
        if extracted:
            texts.append(extracted)
        corpus = "\n".join(texts).lower()
        if not corpus:
            return None

        # Daily quota → reset at midnight UTC tomorrow
        if "daily quota" in corpus:
            tomorrow = datetime.now(timezone.utc).date() + timedelta(days=1)
            return datetime.combine(tomorrow, datetime.min.time(), tzinfo=timezone.utc)

        if not any(kw in corpus for kw in self._RATE_LIMIT_KEYWORDS):
            return None

        return None

    # ------------------------------------------------------------------
    # rewind — we simulate rewind by clearing the session_id so the
    # NEXT turn starts a fresh CLI session.
    # ------------------------------------------------------------------
    async def rewind(self, app_sid: str, message_uuid: str) -> None:
        from session_manager import manager as session_manager
        session_manager.set_agent_sid(app_sid, "native", None)
        session_manager.set_agent_sid(app_sid, "manager", None)
