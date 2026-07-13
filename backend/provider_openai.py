"""OpenAIProvider — `Provider` implementation for the BA-owned Better Agent runner.

Unlike claude/gemini/codex (which drive an external CLI subprocess), the
`openai` provider runs the agent loop inside BA itself: `runner_better_agent.py`
makes HTTP Chat Completions calls and executes tools in-process. It
normalizes its events to the Claude-jsonl shape and writes them to
`session_events.jsonl`; this provider tails that file (reusing
`GeminiJsonlTailer` verbatim — it is provider-agnostic, only the file
path differs) and pushes events onto the orchestrator queue.

Mirror of `provider_gemini.py` section-by-section: same RunState, same
bootstrap/complete lifecycle, same recovery classification, same
`_write_backend_state` shape so `run_recovery._integrate_one` reads
identical keys regardless of provider kind.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar, Optional

from rate_limits import build_corpus, parse_rate_limit as parse_provider_rate_limit

import httpx
import config_store
from extension_run_policy import disabled_builtin_extensions_for_run

from provider import (
    Provider,
    RecoveredPopen,
    StreamEvent,
    await_line_tailer_drained,
    build_better_agent_run_env,
    path_exists_off_loop,
    popen_is_running_off_loop,
    run_provider_io_off_loop,
    run_provider_io_phase_off_loop,
    terminate_failed_run_process,
    publish_run_state_and_bootstrap,
    persist_seed_or_terminate,
    RecoveryAttachReceipt,
    await_scheduled_tasks,
    schedule_loop_task,
    runner_argv,
)
import provider_runtime
from provider_run_config import normalize_provider_run_config
from provider_lifecycle import LifecycleOutcome, RunLifecycleCoordinator
from ingestion_versions import OPENAI_INGESTION_VERSION, marker_matches_current
from reasoning_effort import (
    ALL_REASONING_EFFORTS,
    DEFAULT_REASONING_EFFORT,
    normalize_reasoning_effort,
)
from proc_control import process_control as _process_control
from runs_dir import (
    atomic_write_json as _atomic_write_json,
    iter_run_dirs,
    pid_alive as _pid_alive,
    prune_old_completed_runs,
    reap_run_dir as _reap_run_dir,
    runs_root as _runs_root,
)

logger = logging.getLogger(__name__)


_RUNNER_PATH = Path(__file__).parent / "runner_better_agent.py"
_HEADLESS_TIMEOUT_S = 60.0
_TAIL_POLL_INTERVAL = 0.05
_RUNNER_EVENT_TYPES = {"agent_message", "worker_start", "worker_event", "worker_complete"}


def runner_event_to_stream_event(event: dict) -> StreamEvent:
    event_type = event.get("type")
    event_data = event.get("data")
    if event_type in _RUNNER_EVENT_TYPES and isinstance(event_data, dict):
        return StreamEvent(event_type, event_data)
    return StreamEvent("agent_message", event)


async def _openai_headless_completion(
    *,
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict],
    timeout_s: float,
) -> tuple[str, dict]:
    """Small non-streaming Chat Completions call used by run_headless."""
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {"model": model, "messages": messages, "stream": False}
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    timeout = httpx.Timeout(connect=15.0, read=timeout_s, write=30.0, pool=15.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, json=payload, headers=headers)
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:500]}")
        body = resp.json()
    choices = body.get("choices") or []
    message = (choices[0].get("message") if choices and isinstance(choices[0], dict) else {}) or {}
    content = message.get("content")
    if isinstance(content, list):
        text = "".join(
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict) and part.get("type") in ("text", "output_text")
        )
    else:
        text = str(content or "")
    return text, body.get("usage") or {}


# ============================================================================
# RunState — per-run bookkeeping (mirrors GeminiProvider.RunState exactly)
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
    # Durable resume-cursor: only advances once an event through this line
    # has actually been applied to the render tree (see `ack_applied_cursor`).
    # `processed_line` above is the eager tailer READ cursor (used for
    # drain-wait detection) and must never be persisted directly — doing so
    # lets a restart skip events that were read but never applied.
    applied_line: int = 0
    tailer: Optional["object"] = None  # GeminiJsonlTailer; typed loosely to avoid import cycle
    tailer_task: Optional[asyncio.Task] = None
    complete_task: Optional[asyncio.Task] = None
    started_at: str = ""
    cancelled: bool = False
    # Where this run's messages PERSIST. In supervisor mode, a worker
    # turn's events route to the worker Better Agent session even though the run
    # is bookkept under the supervisor's app_session_id. Mirrors
    # ClaudeProvider.RunState.persist_to.
    persist_to: str = ""
    target_message_id: Optional[str] = None
    turn_run_id: Optional[str] = None
    lifecycle_msg_id: Optional[str] = None
    lifecycle_token: Any = None
    lifecycle_record: Any = None


@dataclass(frozen=True, slots=True)
class OpenAILifecycleRecord:
    run_id: str
    cleanup_nonce: str
    pid: int
    run_dir: str


# ============================================================================
# OpenAIProvider
# ============================================================================
class OpenAIProvider(Provider):
    """Drives the BA-owned `runner_better_agent.py` subprocess. The runner
    performs Chat Completions calls + in-process tool execution itself
    and writes normalized events to `session_events.jsonl`; this provider
    tails that file and pushes events onto the orchestrator queue."""

    KIND: ClassVar[str] = "openai"

    # The Better Agent runner owns the agent loop/history, so features that are
    # awkward CLI-specific hacks elsewhere are implemented directly here:
    # fork = copy BA-owned message history to a fresh agent session,
    # manager mode = expose the same loopback orchestration tools, and
    # steering = append an in-flight user steering message on the next round.
    supports_fork: ClassVar[bool] = True
    supports_manager_mode: ClassVar[bool] = True
    supports_rewind: ClassVar[bool] = True
    rewind_requires_agent_identity: ClassVar[bool] = False
    supports_steering: ClassVar[bool] = True
    supports_native_subagents: ClassVar[bool] = True
    supports_reasoning_effort: ClassVar[bool] = True
    reasoning_effort_options: ClassVar[tuple[str, ...]] = ALL_REASONING_EFFORTS
    default_reasoning_effort: ClassVar[str] = DEFAULT_REASONING_EFFORT
    supports_headless_no_tools: ClassVar[bool] = True

    def __init__(self, record: dict) -> None:
        super().__init__(record)
        self._runs: dict[str, RunState] = {}
        self._lifecycle: RunLifecycleCoordinator[OpenAILifecycleRecord] | None = None
        self._lifecycle_runs: dict[str, RunState] = {}
        self._lifecycle_spawn_tasks: set[asyncio.Task] = set()
        self._recovery_pending_states: dict[str, RunState] = {}

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
                        "openai_cancel_terminate", terminate_failed_run_process, owned
                    )
                await run_provider_io_phase_off_loop(
                    "openai_cancel_cleanup", self._cleanup_lifecycle_artifacts, owned
                )

        try:
            schedule_loop_task(
                lifecycle.owner_loop, cancel_owned(), name=f"openai-cancel-{run_id[:8]}"
            )
            return True
        except Exception:
            logger.exception("failed to schedule OpenAI lifecycle cancellation")
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
                "openai_shutdown_pending_terminate", terminate_failed_run_process, rs
            )
            await run_provider_io_phase_off_loop(
                "openai_shutdown_pending_cleanup", self._cleanup_lifecycle_artifacts, rs
            )
        for published in inventory.published:
            rs = self._lifecycle_runs.get(published.value.cleanup_nonce)
            if rs is None or id(rs) in cleaned:
                continue
            await run_provider_io_phase_off_loop(
                "openai_shutdown_terminate", terminate_failed_run_process, rs
            )
            await run_provider_io_phase_off_loop(
                "openai_shutdown_cleanup", self._cleanup_lifecycle_artifacts, rs
            )

    def _cleanup_lifecycle_artifacts(self, rs: RunState) -> None:
        record = rs.lifecycle_record
        if record is not None:
            self._lifecycle_runs.pop(record.cleanup_nonce, None)
        self._recovery_pending_states.pop(rs.run_id, None)
        super()._cleanup_run(rs.run_id)
        try:
            import active_run_catalog
            active_run_catalog.retire(_runs_root(), rs.run_id)
        except Exception:
            logger.exception("failed to retire OpenAI run catalog entry %s", rs.run_id)
        try:
            _reap_run_dir(rs.run_dir)
        except Exception:
            logger.exception("failed to reap OpenAI run directory %s", rs.run_id)

    def _cleanup_run(self, run_id: str) -> None:
        rs = self._runs.get(run_id)
        super()._cleanup_run(run_id)
        if rs is None:
            return
        record, token = rs.lifecycle_record, rs.lifecycle_token
        if record is None or token is None or self._lifecycle is None:
            return
        self._lifecycle_runs.pop(record.cleanup_nonce, None)
        schedule_loop_task(
            self._lifecycle.owner_loop,
            self._lifecycle.retire(token, record),
            name=f"openai-retire-{run_id[:8]}",
        )

    # ------------------------------------------------------------------
    # Env — copy os.environ, strip foreign-provider vars, add OpenAI auth
    # ------------------------------------------------------------------
    def build_env(self) -> dict[str, str]:
        env = os.environ.copy()
        # Clear foreign-provider env so it can't interfere with the runner.
        env.pop("CLAUDE_CONFIG_DIR", None)
        env.pop("ANTHROPIC_API_KEY", None)
        env.pop("ANTHROPIC_BASE_URL", None)
        env.pop("CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING", None)
        env.pop("GEMINI_CLI_HOME", None)
        env.pop("GEMINI_API_KEY", None)
        env.pop("GOOGLE_API_KEY", None)
        env.pop("CODEX_HOME", None)
        env.pop("OPENAI_API_KEY", None)
        env.pop("OPENAI_BASE_URL", None)
        # Snapshot the record atomically (provider.py record setter
        # replaces the whole dict); pass api_key + base_url through to
        # the runner so it can authenticate against Chat Completions.
        rec = self.record
        api_key = rec.get("api_key")
        base_url = rec.get("base_url")
        if api_key:
            env["OPENAI_API_KEY"] = str(api_key)
        if base_url:
            env["OPENAI_BASE_URL"] = str(base_url)
        return env

    # ------------------------------------------------------------------
    # start_run
    # ------------------------------------------------------------------
    def _spawn_run(
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
        if mode == "manager":
            mode = "team"
        if mode not in ("native", "team"):
            raise ValueError(f"mode must be 'native' or 'team', got {mode!r}")
        if self.defunct:
            raise RuntimeError(
                f"provider {self.id} is defunct; cannot start new runs"
            )
        self.assert_not_suspended(action="start new runs")
        if reasoning_effort:
            normalized_effort = normalize_reasoning_effort(reasoning_effort)
            if normalized_effort is None:
                allowed = ", ".join(self.reasoning_effort_options)
                raise ValueError(f"reasoning_effort must be one of: {allowed}")
            reasoning_effort = normalized_effort

        # OpenAI is a generic endpoint kind: the valid model set is defined by
        # the remote endpoint (varies per deployment), not by a BA-owned
        # catalog. So we do NOT hard-validate the model — the endpoint rejects
        # unknown models with a clear error. We only require one to be set;
        # the provider record's default_model / custom_models seed the dropdown.
        if not model:
            raise ValueError("openai provider requires a model")
        if mode == "team" and not self.supports_manager_mode:
            raise NotImplementedError(
                f"{self.KIND} provider does not support team mode."
            )
        # `fork` is gated by the class-level capability. OpenAI supports it by
        # copying BA-owned history in runner_better_agent, but keep the defensive gate
        # so per-record capability overrides can still disable it cleanly.
        if fork and not self.supports_fork:
            raise NotImplementedError(
                f"{self.KIND} provider does not support fork."
            )

        run_dir = _runs_root() / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        runner_mode = "manager" if mode == "team" else mode
        from session_manager import manager as _sm
        import user_prefs
        _sess_rec = _sm.get(app_session_id) or {}
        _worker_sess_rec = _sm.get(worker_agent_session_id) if worker_agent_session_id else {}
        from permission import resolve_for_run as _resolve_perm
        _permission = _resolve_perm(
            sess_rec=_sess_rec,
            worker_sess_rec=_worker_sess_rec,
            is_worker=is_worker,
            fallback_kind=self.KIND,
        )
        input_payload = {
            "prompt": prompt,
            "images": images or [],
            "files": files or [],
            "cwd": cwd,
            "model": model,
            "reasoning_effort": reasoning_effort,
            "permission": _permission,
            "session_id": session_id,
            "mode": runner_mode,
            "source": source or "",
            "app_session_id": app_session_id,
            "active_capability_ids": [
                str(cid)
                for cid in (_sess_rec.get("active_capability_ids") or [])
                if str(cid or "").strip()
            ],
            "disallowed_tools": disallowed_tools or [],
            "setting_sources": setting_sources or [],
            "backend_url": backend_url or "",
            "internal_token": internal_token or "",
            "fork": bool(fork),
            "supervised": bool(supervised),
            "supervisor_agent_session_id": supervisor_agent_session_id,
            "worker_agent_session_id": worker_agent_session_id,
            "mssg_sender_session_id": mssg_sender_session_id,
            "browser_harness_enabled": bool(browser_harness_enabled),
            "open_file_panel_enabled": bool(open_file_panel_enabled),
            "bare_config": bool(_sess_rec.get("bare_config")),
            "working_mode": _sess_rec.get("working_mode"),
            "worker_working_mode": (_worker_sess_rec or {}).get("working_mode"),
            "context_strategy": user_prefs.get_context_strategy(),
            "continuation_chain": continuation_chain or [],
            "provider_run_config": normalize_provider_run_config(provider_run_config),
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
        (run_dir / "input.json").write_text(json.dumps(input_payload), encoding="utf-8")

        from containment import containment
        containment().create(run_id)
        stdout_fp = (run_dir / "stdout.log").open("ab")
        stderr_fp = (run_dir / "stderr.log").open("ab")
        try:
            env = self.build_env()
            if extra_env:
                env.update(extra_env)
            env.update(build_better_agent_run_env(
                backend_url=backend_url,
                internal_token=internal_token,
                app_session_id=app_session_id,
                cwd=cwd,
                model=model,
                provider_id=self.id,
                bare_config=bool(_sess_rec.get("bare_config")),
                user_facing=bool(open_file_panel_enabled) and not bool(_sess_rec.get("bare_config")),
                disabled_builtin_extensions=input_payload["disabled_builtin_extensions"],
            ))
            popen = provider_runtime.popen_runner(
                runner_argv(run_dir, dev_script=_RUNNER_PATH, kind="openai"),
                run_dir=run_dir,
                project_cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=stdout_fp,
                stderr=stderr_fp,
                cwd=cwd,
                env=env,
                **_process_control().detach_spawn_kwargs(),
                **containment().spawn_kwargs(run_id),
            )
        except Exception:
            stdout_fp.close()
            stderr_fp.close()
            containment().teardown(run_id)
            raise
        finally:
            stdout_fp.close()
            stderr_fp.close()
        containment().after_spawn(run_id, popen.pid)

        logger.info(
            "spawned Better Agent runner pid=%d mode=%s run_id=%s",
            popen.pid, mode, run_id,
        )

        rs = RunState(
            run_id=run_id,
            run_dir=run_dir,
            popen=popen,
            mode=mode,
            app_session_id=app_session_id,
            queue=queue,
            started_at=datetime.now().isoformat(),
            # In supervisor mode, worker turns persist to the worker BC,
            # not the supervisor's app_session_id. Mirrors ClaudeProvider.
            persist_to=worker_agent_session_id or app_session_id,
            target_message_id=target_message_id,
            turn_run_id=turn_run_id,
            lifecycle_msg_id=lifecycle_msg_id,
        )
        persist_seed_or_terminate(self._write_backend_state, rs)

        return rs

    def start_run(self, **spawn_kwargs) -> None:
        spawn_kwargs["lifecycle_msg_id"] = spawn_kwargs.get("lifecycle_msg_id")
        loop = spawn_kwargs["loop"]
        run_id = spawn_kwargs["run_id"]
        if self._lifecycle is None:
            self._lifecycle = RunLifecycleCoordinator(loop)
        task = schedule_loop_task(
            loop,
            self._admit_and_spawn(spawn_kwargs),
            name=f"openai-admit-spawn-{run_id[:8]}",
        )
        if task is not None:
            self._lifecycle_spawn_tasks.add(task)
            task.add_done_callback(self._lifecycle_spawn_tasks.discard)
            self._track_run_start_receipt(run_id, task)

    async def _admit_and_spawn(self, spawn_kwargs: dict) -> None:
        lifecycle = self._lifecycle
        if lifecycle is None:
            raise RuntimeError("OpenAI lifecycle coordinator is unavailable")
        run_id = spawn_kwargs["run_id"]
        admission = await lifecycle.admit(run_id)
        if not admission.accepted or admission.token is None:
            if admission.outcome is LifecycleOutcome.DUPLICATE:
                raise RuntimeError(f"duplicate OpenAI run id: {run_id}")
            raise RuntimeError(f"OpenAI run admission rejected: {admission.outcome.value}")
        token = admission.token
        rs = None
        record = None
        published_record = None
        try:
            rs = await run_provider_io_phase_off_loop(
                "openai_spawn_seed", functools.partial(self._spawn_run, **spawn_kwargs)
            )
            record = OpenAILifecycleRecord(
                run_id=run_id,
                cleanup_nonce=uuid.uuid4().hex,
                pid=int(rs.popen.pid),
                run_dir=str(rs.run_dir),
            )
            published = await lifecycle.publish(token, record)
            if not published.accepted:
                raise RuntimeError(f"OpenAI run publish rejected: {published.outcome.value}")
            published_record = record
            rs.lifecycle_token = token
            rs.lifecycle_record = record
            self._lifecycle_runs[record.cleanup_nonce] = rs
            self._publish_started_run(run_id, rs)
            await self._bootstrap_run(rs)
        except BaseException:
            if published_record is not None and rs is not None:
                await self._cleanup_failed_published_run(
                    lifecycle, token, published_record, rs
                )
            else:
                if rs is not None:
                    await self._cleanup_unpublished_failure(
                        lifecycle, token, rs, phase="spawn_rollback"
                    )
                else:
                    try:
                        await lifecycle.rollback(token)
                    except BaseException:
                        logger.exception(
                            "failed to roll back unspawned OpenAI run %s", run_id
                        )
            raise

    async def _cleanup_failed_published_run(
        self, lifecycle, token, record: OpenAILifecycleRecord, rs: RunState,
    ) -> None:
        if bool(getattr(rs, "recovered_attach", False)):
            self._lifecycle_runs.pop(record.cleanup_nonce, None)
            self._recovery_pending_states.pop(rs.run_id, None)
            super()._cleanup_run(rs.run_id)
            await lifecycle.retire(token, record)
            return
        try:
            await run_provider_io_phase_off_loop(
                "openai_bootstrap_failure_terminate", terminate_failed_run_process, rs
            )
        except BaseException:
            logger.exception("failed to terminate bootstrap-failed OpenAI run %s", rs.run_id)
        try:
            await run_provider_io_phase_off_loop(
                "openai_bootstrap_failure_cleanup", self._cleanup_lifecycle_artifacts, rs
            )
        except BaseException:
            logger.exception("failed to clean bootstrap-failed OpenAI run %s", rs.run_id)
        try:
            await lifecycle.retire(token, record)
        except BaseException:
            logger.exception("failed to retire bootstrap-failed OpenAI run %s", rs.run_id)

    async def _cleanup_unpublished_failure(
        self, lifecycle, token, rs: RunState, *, phase: str,
    ) -> None:
        if bool(getattr(rs, "recovered_attach", False)):
            self._recovery_pending_states.pop(rs.run_id, None)
            await lifecycle.rollback(token)
            return
        try:
            await run_provider_io_phase_off_loop(
                f"openai_{phase}_terminate", terminate_failed_run_process, rs
            )
        except BaseException:
            logger.exception("failed to terminate unpublished OpenAI run %s", rs.run_id)
        try:
            await run_provider_io_phase_off_loop(
                f"openai_{phase}_cleanup", self._cleanup_lifecycle_artifacts, rs
            )
        except BaseException:
            logger.exception("failed to clean unpublished OpenAI run %s", rs.run_id)
        try:
            await lifecycle.rollback(token)
        except BaseException:
            logger.exception("failed to roll back unpublished OpenAI run %s", rs.run_id)

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
                    parsed = json.loads(await run_provider_io_phase_off_loop("bootstrap_read", Path.read_text, state_path, "utf-8"))
                    if parsed.get("session_id"):
                        runner_state = parsed
                        break
                except (json.JSONDecodeError, OSError):
                    pass

            # Runner is dead — enter regardless of state.json existing.
            # state.json with null session_id + dead runner is a pre-run
            # failure (e.g. invalid --resume target, missing cwd); the
            # old `and not state_path.exists()` gate would spin forever.
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
        try:
            await run_provider_io_phase_off_loop("backend_state_commit", self._write_backend_state, rs)
        except Exception as exc:
            await self._emit_early_failure(
                rs, f"bootstrap persistence failed: {exc}", cleanup=False
            )
            raise
        if (
            self._runs.get(rs.run_id) is not rs
            or bool(getattr(rs, "cancelled", False))
            or bool(getattr(rs, "turn_finalized", False))
        ):
            return

        # 2) Emit session_discovered
        try:
            rs.queue.put_nowait(StreamEvent("session_discovered", {"session_id": session_id}))
        except Exception:
            logger.exception("failed to enqueue session_discovered")

        # 3) Start the polling tailer on session_events.jsonl. Reuse
        # GeminiJsonlTailer verbatim — it is provider-agnostic; only
        # the file path differs. No subclass needed.
        from jsonl_tailer import GeminiJsonlTailer

        # Single-slot holder tagging the just-dispatched StreamEvent with its
        # tailer cursor (see `_on_cursor`). Safe as a single slot: dispatch
        # and the matching on_cursor_advance fire back-to-back, synchronously,
        # from the same tailer read loop with no interleaving dispatch in
        # between (see jsonl_tailer.JsonlEventTailer.run).
        _pending_cursor_event: list = [None]

        def _dispatch_to_queue(event: dict, _rs: RunState = rs) -> None:
            try:
                stream_event = runner_event_to_stream_event(event)
                _pending_cursor_event[0] = stream_event
                _rs.queue.put_nowait(stream_event)
            except Exception:
                logger.exception(
                    "GeminiJsonlTailer dispatch: put_nowait failed for run %s",
                    _rs.run_id,
                )

        def _on_cursor(n: int, _rs: RunState = rs) -> None:
            # Called synchronously from the tailer's read loop, so this MUST
            # stay non-blocking. `processed_line` is the eager READ cursor
            # (cheap in-memory update; this is what the deterministic drain
            # polls) — it is NOT persisted here. Tag the just-dispatched
            # event with this cursor value so the consumer can persist it
            # via `ack_applied_cursor` only once the event is actually
            # applied to the render tree (never at read/enqueue time).
            _rs.processed_line = n
            pending = _pending_cursor_event[0]
            if pending is not None:
                pending.cursor = n
                _pending_cursor_event[0] = None

        rs.tailer = GeminiJsonlTailer(
            path=events_path,
            start_offset=rs.processed_line,
            dispatch=_dispatch_to_queue,
            on_cursor_advance=_on_cursor,
        )
        rs.tailer_task = asyncio.get_event_loop().create_task(
            rs.tailer.run(),
            name=f"openai-tailer-{rs.run_id[:8]}",
        )

        # 4) Schedule completion watcher
        rs.complete_task = asyncio.get_event_loop().create_task(
            self._watch_complete(rs),
            name=f"openai-complete-{rs.run_id[:8]}",
        )

    # ------------------------------------------------------------------
    # _watch_complete
    # ------------------------------------------------------------------
    async def _watch_complete(self, rs: RunState) -> None:
        complete_path = rs.run_dir / "complete.json"
        try:
            while True:
                if await path_exists_off_loop(complete_path):
                    break
                # INVARIANT: process death MUST end this loop. If the
                # runner is SIGKILLed (OOM, manual kill, OS) it never
                # writes complete.json — the old "complete.json AND
                # process dead" condition would spin forever, leaving
                # the turn stuck in flight forever. Breaking on
                # process-dead alone lets `_emit_complete_from_file`'s
                # built-in fallback (`error="runner exited without
                # writing complete.json"`) synthesize the error
                # complete event. A short grace window lets a normal
                # exit's complete.json land before we synthesize.
                if not await popen_is_running_off_loop(rs.popen):
                    loop = asyncio.get_event_loop()
                    grace_end = loop.time() + (_TAIL_POLL_INTERVAL * 6)
                    while not await path_exists_off_loop(complete_path) and loop.time() < grace_end:
                        await asyncio.sleep(_TAIL_POLL_INTERVAL)
                    break
                await asyncio.sleep(_TAIL_POLL_INTERVAL)

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
                        "openai tailer did not exit in time for %s", rs.run_id,
                    )
                except Exception:
                    logger.exception(
                        "openai tailer task failed for %s", rs.run_id,
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
        if complete_path.exists():
            try:
                payload = json.loads(complete_path.read_text(encoding="utf-8"))
            except Exception:
                logger.exception("failed to parse complete.json for %s", rs.run_id)
        try:
            rs.queue.put_nowait(StreamEvent("complete", payload))
        except Exception:
            logger.exception("failed to enqueue complete for %s", rs.run_id)

    # ------------------------------------------------------------------
    # _emit_early_failure
    # ------------------------------------------------------------------
    async def _emit_early_failure(
        self, rs: RunState, msg: str, *, cleanup: bool = True,
    ) -> None:
        logger.warning("openai bootstrap failure for %s: %s", rs.run_id, msg)
        try:
            rs.queue.put_nowait(StreamEvent("error", {"error": msg}))
            rs.queue.put_nowait(StreamEvent("complete", {
                "success": False, "error": msg,
                "session_id": None, "token_usage": None,
            }))
        except Exception:
            logger.exception("failed to enqueue early failure for %s", rs.run_id)
        if cleanup:
            self._cleanup_run(rs.run_id)

    # _backend_state_path / _read_backend_state inherited from
    # AbstractStreamingProvider. is_running / cancel_all / active_runs /
    # runs_for_session / _cleanup_run / cancel_run all inherited.

    def _write_backend_state(self, rs: RunState) -> None:
        """Provider-specific backend_state.json contents.
        Mirrors `GeminiProvider._write_backend_state` (run_id /
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
            "started_at": rs.started_at,
            "session_id": rs.session_id,
            "jsonl_path": str(rs.run_dir / "session_events.jsonl"),
            # Durable resume cursor: `applied_line`, NOT the eager read
            # cursor `processed_line` — see RunState.applied_line.
            "processed_line": rs.applied_line,
            "cancelled": rs.cancelled,
            "target_message_id": rs.target_message_id,
            "turn_run_id": rs.turn_run_id,
            "lifecycle_msg_id": rs.lifecycle_msg_id,
            "provider_id": self.id,
            "provider_kind": self.KIND,
            "ingestion_version": OPENAI_INGESTION_VERSION,
        }
        try:
            _atomic_write_json(self._backend_state_path(rs), data)
            if rs.session_id:
                import spawn_ledger
                spawn_ledger.record_discovered(rs.session_id)
        except Exception:
            logger.exception("failed to write backend_state.json for %s", rs.run_id)
            raise

    async def _flush_cursor_ledger(self, rs: RunState) -> None:
        """Block until `cursor_ledger_worker` has written this run's
        latest known cursor to `backend_state.json`, once a drain
        concludes — crash recovery must see the true final cursor, not
        whatever was last coalesced. Off-loop so the event loop itself
        never blocks on the write."""
        from cursor_ledger_worker import worker as cursor_ledger_worker
        await asyncio.to_thread(cursor_ledger_worker.flush_now, rs.run_id)

    def ack_applied_cursor(self, run_id: str, cursor: Optional[int]) -> None:
        if cursor is None:
            return
        rs = self._runs.get(run_id)
        if rs is None or cursor <= rs.applied_line:
            return
        rs.applied_line = cursor
        from cursor_ledger_worker import worker as cursor_ledger_worker
        cursor_ledger_worker.note(run_id, lambda: self._write_backend_state(rs))

    def attach_recovered_run(
        self,
        *,
        desc: dict,
        queue: asyncio.Queue,
        loop: asyncio.AbstractEventLoop,
    ) -> RecoveryAttachReceipt:
        """Re-attach a still-running detached Better Agent runner after a
        backend restart.

        `recover_in_flight` only classifies the on-disk run. This method
        rebuilds the provider-side RunState and restarts the same
        `session_events.jsonl` tailer/completion watcher used by a live
        spawn, so events emitted after the backend restart are streamed into
        the recovered turn immediately instead of waiting for a later cold
        replay.
        """
        run_id = str(desc.get("run_id") or "")
        pid = desc.get("pid")
        if (
            not run_id
            or not pid
            or run_id in self._runs
            or run_id in self._recovery_pending_states
        ):
            return RecoveryAttachReceipt(None, lambda: False)
        try:
            runner_pid = int(pid)
        except (TypeError, ValueError):
            return RecoveryAttachReceipt(None, lambda: False)
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
            applied_line=processed_line,
            started_at=desc.get("started_at") or datetime.now().isoformat(),
            cancelled=bool(desc.get("cancelled", False)),
            persist_to=desc.get("persist_to") or desc.get("app_session_id") or "",
            target_message_id=desc.get("target_message_id"),
            turn_run_id=desc.get("turn_run_id"),
            lifecycle_msg_id=desc.get("lifecycle_msg_id"),
        )
        rs.recovered_attach = True
        if self._lifecycle is None:
            self._lifecycle = RunLifecycleCoordinator(loop)
        self._recovery_pending_states[run_id] = rs
        task = schedule_loop_task(
            loop,
            self._admit_recovered_run(rs),
            name=f"openai-recover-bootstrap-{run_id[:8]}",
        )
        if task is not None:
            self._lifecycle_spawn_tasks.add(task)

            def done(completed: asyncio.Task) -> None:
                self._lifecycle_spawn_tasks.discard(completed)

            task.add_done_callback(done)
        if task is None:
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
            raise RuntimeError("OpenAI lifecycle coordinator is unavailable")
        admission = await lifecycle.admit(rs.run_id)
        if not admission.accepted or admission.token is None:
            self._recovery_pending_states.pop(rs.run_id, None)
            if admission.outcome is LifecycleOutcome.SHUTDOWN:
                await run_provider_io_phase_off_loop(
                    "openai_recovery_shutdown_terminate",
                    terminate_failed_run_process,
                    rs,
                )
                await run_provider_io_phase_off_loop(
                    "openai_recovery_shutdown_cleanup",
                    self._cleanup_lifecycle_artifacts,
                    rs,
                )
                return
            raise RuntimeError(
                f"OpenAI recovered run admission rejected: {admission.outcome.value}"
            )
        token = admission.token
        record = OpenAILifecycleRecord(
            run_id=rs.run_id,
            cleanup_nonce=uuid.uuid4().hex,
            pid=int(rs.popen.pid),
            run_dir=str(rs.run_dir),
        )
        published_record = None
        try:
            await run_provider_io_phase_off_loop(
                "openai_recovery_seed", self._write_backend_state, rs
            )
            published = await lifecycle.publish(token, record)
            if not published.accepted:
                raise RuntimeError(
                    f"OpenAI recovered run publish rejected: {published.outcome.value}"
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
                self._recovery_pending_states.pop(rs.run_id, None)
                await self._cleanup_unpublished_failure(
                    lifecycle, token, rs, phase="recovery_rollback"
                )
            raise

    def _post_cancel_hook(self, rs: RunState) -> None:
        """Wake the tailer's stop_event so it exits its poll-sleep
        promptly rather than waiting up to _POLL_INTERVAL."""
        if rs.tailer is not None:
            try:
                rs.tailer.stop()
            except Exception:
                pass

    def steer_run(self, run_id: str, prompt: str, images: Optional[list] = None) -> bool:
        """Append a steering message for a live OpenAI turn.

        Chat Completions has no mid-token native steering primitive, but because
        BA owns the loop we can cleanly append the user's steer payload as the
        next user message before the next model round. This works during
        tool-heavy/long-running turns and avoids provider-CLI hacks.
        """
        rs = self._runs.get(run_id)
        images = images or []
        if rs is None or rs.popen.poll() is not None or (not prompt.strip() and not images):
            return False
        state_path = rs.run_dir / "state.json"
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        if not state.get("session_id"):
            return False
        inbox = rs.run_dir / "steer.jsonl"
        try:
            with inbox.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"prompt": prompt, "images": images}) + "\n")
                f.flush()
                os.fsync(f.fileno())
            return True
        except OSError:
            logger.exception("openai steer_run failed for %s", run_id)
            return False

    # ------------------------------------------------------------------
    # recover_in_flight
    # ------------------------------------------------------------------
    def recover_in_flight(
        self,
        loop: Optional[asyncio.AbstractEventLoop] = None,
        run_id_filter: Optional[set[str]] = None,
    ) -> list[dict]:
        """Mirror `GeminiProvider.recover_in_flight`'s descriptor shape
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
                    "openai recover_in_flight: live orphan %s (pid=%s) "
                    "still running; re-attaching for recovery",
                    child.name, pid,
                )

            if not live_orphan and not has_complete_json:
                try:
                    terminal_path = child / "terminal.json"
                    if terminal_path.exists():
                        complete = json.loads(terminal_path.read_text(encoding="utf-8"))
                    else:
                        complete = {
                            "success": False,
                            "session_id": bs.get("session_id") or rs_disk.get("session_id"),
                            "error": "runner died before completion (recovered at startup)",
                            "token_usage": None,
                            "finished_at": datetime.now().isoformat(),
                        }
                    complete_path.write_text(json.dumps(complete, indent=2), encoding="utf-8")
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
                "mode": bs.get("mode") or rs_disk.get("mode") or "native",
                "provider_id": bs.get("provider_id") or self.id,
                "provider_kind": bs.get("provider_kind") or self.KIND,
                "ingestion_version": bs.get("ingestion_version"),
                "target_message_id": bs.get("target_message_id"),
                "turn_run_id": bs.get("turn_run_id"),
                "lifecycle_msg_id": bs.get("lifecycle_msg_id"),
                "recovered_as": "live_orphan" if live_orphan else "dead_orphan",
            })

        return recovered

    # ------------------------------------------------------------------
    # prune_old_runs
    # ------------------------------------------------------------------
    def prune_old_runs(self, max_age_days: int = 7) -> int:
        return prune_old_completed_runs(max_age_days)

    # ------------------------------------------------------------------
    # run_headless — direct one-shot Chat Completions call.
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
        """Run one tool-less OpenAI completion and return a Claude-shaped
        headless envelope.

        `fork=True` copies BA-owned OpenAI history to a fresh sid before the
        prompt is appended, preserving composer guarantees that the
        source session is not mutated. `no_tools` is accepted for parity — this
        path never sends tools.
        """
        self.assert_not_suspended(action="run headless work")
        del cwd, no_tools
        rec = self.record
        base_url = str(rec.get("base_url") or "").strip()
        api_key = str(rec.get("api_key") or "").strip()
        model = str(rec.get("default_model") or "").strip()
        if not base_url or not api_key or not model:
            logger.error("OpenAIProvider.run_headless: base_url/api_key/default_model missing")
            return None

        try:
            import runner_better_agent as _ro
            parent_sid = resume_sid or session_id
            if fork:
                sid, messages = _ro._load_history_for_run(parent_sid, fork=True)
            else:
                sid, messages = _ro._load_history(session_id or resume_sid)
            if session_id and not resume_sid and sid != session_id:
                sid = session_id
            if not messages or messages[0].get("role") != "system":
                messages.insert(0, {"role": "system", "content": _ro._SYSTEM_PROMPT})
            messages.append({"role": "user", "content": prompt})

            text, usage = await _openai_headless_completion(
                base_url=base_url,
                api_key=api_key,
                model=model,
                messages=messages,
                timeout_s=timeout or _HEADLESS_TIMEOUT_S,
            )
            messages.append({"role": "assistant", "content": text})
            _ro._save_history(sid, messages)
            mapped_usage = {
                "input_tokens": int((usage or {}).get("prompt_tokens") or 0),
                "output_tokens": int((usage or {}).get("completion_tokens") or 0),
                "cache_read_input_tokens": int(((usage or {}).get("prompt_tokens_details") or {}).get("cached_tokens") or 0),
                "total_tokens": int((usage or {}).get("total_tokens") or 0),
            }
            return {
                "result": text,
                "session_id": sid,
                "usage": mapped_usage,
                "total_cost_usd": 0.0,
                "is_error": False,
            }
        except Exception:
            logger.exception("OpenAIProvider.run_headless failed")
            return None

    # ------------------------------------------------------------------
    # Rate-limit parsing — unblocks the orchestrator's rate-limit retry
    # loop (turn_manager). Without this, a 429 from the Chat Completions
    # endpoint raises AttributeError at turn_manager's parse_rate_limit
    # call site and aborts the turn instead of retrying.
    # ------------------------------------------------------------------
    _OPENAI_RATE_LIMIT_KEYWORDS = (
        "capacity",
    )
    # Long-reset quota exhaustion (e.g. Sakana's "Subscription window is
    # exceeded") vs a short per-minute throttle: the orchestrator clamps
    # the wait to 600s either way, but the reset time is surfaced to the
    # UI as retrying_until, so keep it honest.
    def parse_rate_limit(
        self, error: Optional[str], events: list[dict],
    ) -> Optional[datetime]:
        corpus = build_corpus(error, events, self._extract_text_for_rate_limit)
        return parse_provider_rate_limit(
            "openai", corpus, extra_keywords=self._OPENAI_RATE_LIMIT_KEYWORDS,
        )

    # ------------------------------------------------------------------
    # rewind — we simulate rewind by clearing the session_id so the
    # NEXT turn starts a fresh Chat Completions history.
    # ------------------------------------------------------------------
    async def rewind(self, app_sid: str, message_uuid: str) -> None:
        del message_uuid
        from session_manager import manager as session_manager
        session_manager.set_agent_sid(app_sid, "native", None)
        session_manager.set_agent_sid(app_sid, "manager", None)
