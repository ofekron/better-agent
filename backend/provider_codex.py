"""CodexProvider — `Provider` implementation for OpenAI's Codex CLI.

Spawns `runner_codex.py` as a detached subprocess per run. The runner
captures turn completion from Codex app-server. The provider tails Codex's
native rollout JSONL and normalizes those events onto the orchestrator queue.

Subscription-only (no API key mode). The Codex CLI is expected to be
installed and authenticated via ChatGPT sign-in.
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
from typing import Any, ClassVar, Optional

from provider import (
    Provider,
    RecoveredPopen,
    live_recovery_pid,
    StreamEvent,
    _file_byte_size,
    await_line_tailer_drained,
    build_better_agent_run_env,
    path_exists_off_loop,
    popen_is_running_off_loop,
    schedule_loop_task,
    runner_argv,
)
import config_store
from extension_run_policy import disabled_builtin_extensions_for_run
from provider_run_config import normalize_provider_run_config
from reasoning_effort import CODEX_REASONING_EFFORTS, DEFAULT_REASONING_EFFORT
from proc_control import process_control as _process_control
from runs_dir import (
    atomic_write_json as _atomic_write_json,
    iter_run_dirs,
    pid_alive as _pid_alive,
    prune_old_completed_runs,
    reap_run_dir as _reap_run_dir,
    runs_root as _runs_root,
)
from ingestion_versions import CODEX_INGESTION_VERSION, marker_matches_current
from codex_normalize import _codex_terminal_state
from codex_usage import token_usage_from_codex_usage

logger = logging.getLogger(__name__)


_RUNNER_PATH = Path(__file__).parent / "runner_codex.py"
_TAIL_POLL_INTERVAL = 0.05

# Official Codex models for when no cache exists yet.
# Checked 2026-07-09 against https://developers.openai.com/codex/models.
# The daily refresh via `fetch_codex_models()` keeps this current.
CODEX_MODELS = [
    "gpt-5.6",
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "gpt-5.5",
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5.3-codex-spark",
]


def _run_start_byte(bs: dict, rs_disk: dict) -> int:
    for value in (
        rs_disk.get("pre_query_byte_offset"),
        bs.get("pre_query_byte_offset"),
        bs.get("processed_byte_offset"),
    ):
        try:
            if value is not None:
                return int(value)
        except (TypeError, ValueError):
            continue
    return 0


def _rollout_terminal_from_byte(path: Path, start_byte: int) -> Optional[bool]:
    try:
        with path.open("rb") as file:
            file.seek(max(0, start_byte))
            terminal: Optional[bool] = None
            for raw in file:
                if not raw.endswith(b"\n"):
                    break
                try:
                    row = json.loads(raw.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    continue
                state = _codex_terminal_state(row)
                if state is not None:
                    terminal = state
            return terminal
    except OSError:
        return None


def _complete_path_success(path: Path) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    return payload.get("success") is True


def _manager_event_count_for_target(persist_to: str, target_message_id: Optional[str]) -> int:
    if not persist_to or not target_message_id:
        return 0
    try:
        from session_manager import manager as session_manager
        sess = session_manager.get(persist_to) or {}
    except Exception:
        logger.debug("failed to read target message for Codex insert_at", exc_info=True)
        return 0
    for msg in sess.get("messages") or []:
        if msg.get("id") == target_message_id:
            return len(msg.get("events") or [])
    return 0


def _read_codex_rollout_complete(
    jsonl_path: Optional[str],
    *,
    start_byte: int,
    session_id: Optional[str],
) -> Optional[dict[str, Any]]:
    if not jsonl_path:
        return None
    path = Path(jsonl_path)
    if not path.exists():
        return None

    payload: Optional[dict[str, Any]] = None
    try:
        with path.open("rb") as f:
            f.seek(max(0, start_byte))
            for raw in f:
                if not raw.endswith(b"\n"):
                    break
                try:
                    event = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                event_type = event.get("type")
                if event_type == "turn.completed":
                    payload = {
                        "success": True,
                        "session_id": session_id,
                        "error": None,
                        "token_usage": token_usage_from_codex_usage(event.get("usage")),
                        "finished_at": datetime.now().isoformat(),
                    }
                elif event_type == "turn.failed":
                    err_data = event.get("error") if isinstance(event.get("error"), dict) else {}
                    message = err_data.get("message") or "turn failed"
                    from continuation import normalize_context_overflow_error
                    payload = {
                        "success": False,
                        "session_id": session_id,
                        "error": normalize_context_overflow_error(message) or message,
                        "token_usage": None,
                        "finished_at": datetime.now().isoformat(),
                    }
    except OSError:
        return None
    return payload


def _read_run_rollout_complete(run_dir: Path, session_id: Optional[str]) -> Optional[dict[str, Any]]:
    bs: dict = {}
    rs_disk: dict = {}
    for target, path in (
        (bs, run_dir / "backend_state.json"),
        (rs_disk, run_dir / "state.json"),
    ):
        if not path.exists():
            continue
        try:
            target.update(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    sid = session_id or bs.get("session_id") or rs_disk.get("session_id")
    jsonl_path = bs.get("jsonl_path") or rs_disk.get("jsonl_path") or rs_disk.get("rollout_path")
    if sid and not jsonl_path:
        from codex_native import resolve_rollout_path
        resolved = resolve_rollout_path(sid)
        jsonl_path = str(resolved) if resolved else None
    return _read_codex_rollout_complete(
        jsonl_path,
        start_byte=_run_start_byte(bs, rs_disk),
        session_id=sid,
    )


def read_codex_run_rollout_events(run_dir: Path) -> list[dict[str, Any]]:
    bs: dict = {}
    rs_disk: dict = {}
    for target, path in (
        (bs, run_dir / "backend_state.json"),
        (rs_disk, run_dir / "state.json"),
    ):
        if not path.exists():
            continue
        try:
            target.update(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue

    sid = bs.get("session_id") or rs_disk.get("session_id") or run_dir.name
    jsonl_path = bs.get("jsonl_path") or rs_disk.get("jsonl_path") or rs_disk.get("rollout_path")
    if sid and not jsonl_path:
        from codex_native import resolve_rollout_path
        resolved = resolve_rollout_path(str(sid))
        jsonl_path = str(resolved) if resolved else None
    if not jsonl_path:
        return []

    from codex_native import normalize_rollout_file
    wrapped, _ = normalize_rollout_file(
        Path(jsonl_path),
        start_byte=_run_start_byte(bs, rs_disk),
        namespace=str(sid),
    )
    return wrapped


def fetch_codex_models() -> list[str]:
    """Parse `codex debug models` output and return visible model slugs.

    Returns the cold-start list on failure (CLI not installed, parse
    error, etc.) so the model dropdown always has something.
    """
    import subprocess as _sp

    from cli_paths import resolve_cli_binary

    codex_bin = resolve_cli_binary("codex")
    if not codex_bin:
        return list(CODEX_MODELS)

    try:
        proc = _sp.run(
            [codex_bin, "debug", "models"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, _sp.TimeoutExpired):
        return list(CODEX_MODELS)

    if proc.returncode != 0:
        return list(CODEX_MODELS)

    try:
        import json as _json

        data = _json.loads(proc.stdout)
        models = [
            m["slug"]
            for m in data.get("models", [])
            if m.get("visibility") != "hide" and m.get("slug")
        ]
        return models if len(models) >= 2 else list(CODEX_MODELS)
    except (json.JSONDecodeError, KeyError, TypeError):
        return list(CODEX_MODELS)


# ============================================================================
# RunState — per-run bookkeeping (mirrors GeminiProvider.RunState)
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
    jsonl_path: Optional[Path] = None
    processed_line: int = 0
    processed_byte_offset: int = 0
    tailer: Optional["object"] = None
    tailer_task: Optional[asyncio.Task] = None
    child_tailers: dict[str, "object"] = field(default_factory=dict)
    child_tailer_tasks: dict[str, asyncio.Task] = field(default_factory=dict)
    child_setup_tasks: dict[str, asyncio.Task] = field(default_factory=dict)
    child_sources: dict[str, dict] = field(default_factory=dict)
    child_terminal_events: dict[str, asyncio.Event] = field(default_factory=dict)
    child_terminal_states: dict[str, bool] = field(default_factory=dict)
    complete_task: Optional[asyncio.Task] = None
    started_at: str = ""
    cancelled: bool = False
    persist_to: str = ""
    target_message_id: Optional[str] = None
    turn_run_id: Optional[str] = None
    backend_state_flush_task: Optional[asyncio.Task] = None
    backend_state_flush_dirty: bool = False


# ============================================================================
# CodexProvider
# ============================================================================
class CodexProvider(Provider):
    """Drives OpenAI's `codex` CLI via detached `runner_codex.py`
    subprocesses. Events are read from Codex's native rollout JSONL and
    pushed onto the orchestrator queue."""

    KIND: ClassVar[str] = "codex"
    # `--runner-kind` for the frozen entrypoint dispatch and `runner_argv`.
    # Subclasses that reuse this runner but need their own frozen dispatch
    # (Fugu) override this.
    RUNNER_KIND: ClassVar[str] = "codex"
    # CLI binary the codex runner resolves and spawns in app-server mode.
    CODEX_BINARY: ClassVar[str] = "codex"
    # Optional Codex profile selected via `-p`. App-server does not support
    # profiles in current Codex; provider-specific app-server selection should
    # use CODEX_CONFIG_OVERRIDES instead.
    CODEX_PROFILE: ClassVar[Optional[str]] = None
    CODEX_CONFIG_OVERRIDES: ClassVar[tuple[str, ...]] = ()

    # Codex forks via the app-server `thread/fork`, which branches a
    # previous session's rollout into a new, isolated thread.
    supports_fork: ClassVar[bool] = True
    supports_manager_mode: ClassVar[bool] = True
    # Codex CLI has no rewind primitive, but we simulate by clearing
    # session_id so the next turn starts fresh.
    supports_rewind: ClassVar[bool] = True
    rewind_requires_agent_identity: ClassVar[bool] = False
    supports_steering: ClassVar[bool] = True
    supports_native_subagents: ClassVar[bool] = True
    supports_reasoning_effort: ClassVar[bool] = True
    # `-s read-only` confines the one-shot run to a read-only sandbox.
    supports_headless_no_tools: ClassVar[bool] = True
    reasoning_effort_options: ClassVar[tuple[str, ...]] = CODEX_REASONING_EFFORTS
    default_reasoning_effort: ClassVar[str] = DEFAULT_REASONING_EFFORT

    def recovered_startup_activity(self, desc: dict) -> list[str]:
        path_raw = desc.get("jsonl_path") or desc.get("rollout_path")
        if not isinstance(path_raw, str) or not path_raw:
            return []
        try:
            start_byte = max(0, int(desc.get("pre_query_byte_offset") or 0))
        except (TypeError, ValueError):
            start_byte = 0
        seen: list[str] = []
        try:
            with Path(path_raw).open("rb") as file:
                file.seek(start_byte)
                for raw in file:
                    if not raw.endswith(b"\n"):
                        break
                    try:
                        row = json.loads(raw.decode("utf-8", errors="replace"))
                    except json.JSONDecodeError:
                        continue
                    row_type = row.get("type")
                    payload = row.get("payload")
                    activity = (
                        row_type
                        if row_type in ("task_started", "turn_context")
                        else payload.get("type")
                        if row_type == "event_msg" and isinstance(payload, dict)
                        else None
                    )
                    if activity in (
                        "task_started",
                        "turn_context",
                        "user_message",
                    ) and activity not in seen:
                        seen.append(activity)
        except OSError:
            logger.debug(
                "failed reading recovered Codex startup activity path=%s",
                path_raw,
                exc_info=True,
            )
        return seen

    def __init__(self, record: dict) -> None:
        super().__init__(record)
        self._runs: dict[str, RunState] = {}

    def codex_config_overrides(self, *, model: Optional[str]) -> list[str]:
        del model
        return list(self.CODEX_CONFIG_OVERRIDES)

    # ------------------------------------------------------------------
    # Env — minimal for Codex (subscription mode, no API keys)
    # ------------------------------------------------------------------
    def build_env(self) -> dict[str, str]:
        self.require_runtime_credential()
        env = os.environ.copy()
        # Clear other provider env vars so they don't interfere.
        env.pop("CLAUDE_CONFIG_DIR", None)
        env.pop("ANTHROPIC_API_KEY", None)
        env.pop("ANTHROPIC_BASE_URL", None)
        env.pop("CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING", None)
        # Isolate this account's credential store: point CODEX_HOME at the
        # record's config_dir so two codex records with distinct config_dirs
        # log in as distinct accounts. A record on the shared default (no
        # config_dir / `~/.codex`) yields no override, leaving any ambient
        # CODEX_HOME the user exported at backend launch untouched.
        cred = config_store.provider_credential_env(self.record)
        if cred:
            env[cred[0]] = cred[1]
        return env

    # ------------------------------------------------------------------
    # start_run
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
        if mode == "manager":
            mode = "team"
        if mode not in ("native", "team"):
            raise ValueError(f"mode must be 'native' or 'team', got {mode!r}")
        if self.defunct:
            raise RuntimeError(
                f"provider {self.id} is defunct; cannot start new runs"
            )
        self.assert_not_suspended(action="start new runs")

        if mode == "team" and not self.supports_manager_mode:
            raise NotImplementedError(
                f"{self.KIND} provider does not support team mode."
            )
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
        request_user_input_enabled = (
            bool(open_file_panel_enabled)
            and not bool(_sess_rec.get("bare_config"))
            and not bool(worker_agent_session_id)
            and not bool(supervisor_agent_session_id)
            and not bool(is_worker)
        )

        input_payload = {
            "prompt": prompt,
            "images": images or [],
            "cwd": cwd,
            "model": model,
            "reasoning_effort": reasoning_effort,
            "permission": _permission,
            "session_id": session_id,
            "mode": runner_mode,
            "source": source or "",
            "provider_kind": self.KIND,
            "codex_binary": self.CODEX_BINARY,
            "codex_profile": self.CODEX_PROFILE,
            "codex_config_overrides": self.codex_config_overrides(model=model),
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
            "request_user_input_enabled": request_user_input_enabled,
            "bare_config": bool(_sess_rec.get("bare_config")),
            "working_mode": _sess_rec.get("working_mode"),
            "worker_working_mode": (_worker_sess_rec or {}).get("working_mode"),
            "context_strategy": user_prefs.get_context_strategy(),
            "continuation_chain": continuation_chain or [],
            "provider_run_config": normalize_provider_run_config(provider_run_config),
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
            popen = subprocess.Popen(
                runner_argv(run_dir, dev_script=_RUNNER_PATH, kind=self.RUNNER_KIND),
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
            "spawned codex runner pid=%d mode=%s run_id=%s",
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
            persist_to=worker_agent_session_id or app_session_id,
            target_message_id=target_message_id,
            turn_run_id=turn_run_id,
        )
        self._runs[run_id] = rs
        self._write_backend_state(rs)

        schedule_loop_task(
            loop,
            self._bootstrap_run(rs),
            name=f"codex-bootstrap-{run_id[:8]}",
        )

    # ------------------------------------------------------------------
    # _bootstrap_run
    # ------------------------------------------------------------------
    async def _bootstrap_run(self, rs: RunState) -> None:
        state_path = rs.run_dir / "state.json"
        complete_path = rs.run_dir / "complete.json"

        runner_state: Optional[dict] = None
        while True:
            if await path_exists_off_loop(state_path):
                try:
                    parsed = json.loads(state_path.read_text(encoding="utf-8"))
                    if parsed.get("session_id"):
                        runner_state = parsed
                        break
                except (json.JSONDecodeError, OSError):
                    pass

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
        jsonl_path_str = runner_state.get("jsonl_path") or runner_state.get("rollout_path")
        if not jsonl_path_str:
            from codex_native import resolve_rollout_path_polled
            resolved = await resolve_rollout_path_polled(session_id)
            jsonl_path_str = str(resolved) if resolved else ""
        if not jsonl_path_str:
            await self._emit_early_failure(
                rs, "codex state missing native rollout path"
            )
            return
        rs.jsonl_path = Path(jsonl_path_str)

        try:
            start_byte = int(runner_state.get("pre_query_byte_offset") or 0)
        except (TypeError, ValueError):
            start_byte = 0
        backend_state = self._read_backend_state(rs)
        if backend_state:
            try:
                recovered_byte = int(backend_state.get("processed_byte_offset") or 0)
            except (TypeError, ValueError):
                recovered_byte = 0
            start_byte = max(start_byte, recovered_byte)
            child_sources = backend_state.get("child_sources")
            if isinstance(child_sources, dict):
                rs.child_sources = {
                    str(k): v for k, v in child_sources.items()
                    if isinstance(v, dict)
                }
        rs.processed_byte_offset = start_byte
        self._write_backend_state(rs)

        try:
            rs.queue.put_nowait(StreamEvent("session_discovered", {"session_id": session_id}))
        except Exception:
            logger.exception("failed to enqueue session_discovered")

        from codex_native import CodexRolloutTailer

        def _dispatch_to_queue(event: dict, _rs: RunState = rs) -> None:
            try:
                _rs.queue.put_nowait(StreamEvent("agent_message", event))
            except Exception:
                logger.exception(
                    "CodexJsonlTailer dispatch: put_nowait failed for run %s",
                    _rs.run_id,
                )
            self._schedule_child_sources(_rs, event)

        def _on_cursor(n: int, _rs: RunState = rs) -> None:
            _rs.processed_byte_offset = n
            self._schedule_backend_state_flush(_rs)

        def _on_context_update(
            context_window: Optional[int],
            context_tokens: Optional[int],
            _rs: RunState = rs,
        ) -> None:
            payload = {}
            if isinstance(context_window, int):
                payload["context_window"] = context_window
            if isinstance(context_tokens, int):
                payload["context_tokens"] = context_tokens
            if not payload:
                return
            try:
                _rs.queue.put_nowait(StreamEvent("context_usage", payload))
            except Exception:
                logger.exception(
                    "CodexJsonlTailer context update failed for run %s",
                    _rs.run_id,
                )

        def _on_lifecycle_update(kind: str, _rs: RunState = rs) -> None:
            try:
                _rs.queue.put_nowait(StreamEvent("run_activity", {"kind": kind}))
            except Exception:
                logger.exception(
                    "CodexJsonlTailer lifecycle update failed for run %s",
                    _rs.run_id,
                )

        rs.tailer = CodexRolloutTailer(
            path=rs.jsonl_path,
            start_byte=start_byte,
            namespace=session_id,
            dispatch=_dispatch_to_queue,
            on_cursor_advance=_on_cursor,
            on_context_update=_on_context_update,
            on_lifecycle_update=_on_lifecycle_update,
        )
        rs.tailer_task = asyncio.get_event_loop().create_task(
            rs.tailer.run(),
            name=f"codex-tailer-{rs.run_id[:8]}",
        )
        for source_key, source in list(rs.child_sources.items()):
            child_id = source.get("agent_id") or source.get("child_id") or source_key
            task = asyncio.get_event_loop().create_task(
                self._ensure_child_tailer(rs, source_key, str(child_id), source, None),
                name=f"codex-child-recover-{source_key[:8]}",
            )
            rs.child_setup_tasks[source_key] = task
            def _done(_task: asyncio.Task, _source_key: str = source_key) -> None:
                rs.child_setup_tasks.pop(_source_key, None)
                if _task.cancelled():
                    return
                exc = _task.exception()
                if exc is not None:
                    logger.error(
                        "codex recovered child setup failed for %s",
                        _source_key,
                        exc_info=(type(exc), exc, exc.__traceback__),
                    )

            task.add_done_callback(_done)

        rs.complete_task = asyncio.get_event_loop().create_task(
            self._watch_complete(rs),
            name=f"codex-complete-{rs.run_id[:8]}",
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

            # Deterministic drain: the Codex rollout is appended by the CLI
            # before complete.json is written, so wait until the tailer's
            # byte cursor covers the file as it stands now. A fixed sleep
            # guess let `complete` overtake trailing rollout lines when the
            # poll tailer lagged — the turn loop then broke and the lines
            # never reached the render tree (stale-content grabs).
            if rs.jsonl_path is not None:
                await await_line_tailer_drained(
                    path=Path(rs.jsonl_path),
                    get_cursor=lambda: rs.processed_byte_offset,
                    run_id=rs.run_id,
                    count_fn=_file_byte_size,
                )
            await self._wait_child_setup(rs)
            if rs.tailer is not None:
                await rs.tailer.drain_available()
            await self._wait_child_setup(rs)
            if (
                not rs.cancelled
                and await asyncio.to_thread(_complete_path_success, complete_path)
            ):
                await self._wait_child_tailers_terminal(rs)
            if rs.tailer is not None:
                rs.tailer.stop()
            if rs.tailer_task is not None:
                try:
                    await asyncio.wait_for(rs.tailer_task, timeout=2.0)
                except asyncio.TimeoutError:
                    logger.warning(
                        "codex tailer did not exit in time for %s", rs.run_id,
                    )
                except Exception:
                    logger.exception(
                        "codex tailer task failed for %s", rs.run_id,
                    )
            for tailer in rs.child_tailers.values():
                try:
                    tailer.stop()
                except Exception:
                    logger.debug("codex child tailer stop failed", exc_info=True)
            for task in rs.child_tailer_tasks.values():
                try:
                    await asyncio.wait_for(task, timeout=2.0)
                except asyncio.TimeoutError:
                    logger.warning(
                        "codex child tailer did not exit in time for %s",
                        rs.run_id,
                    )
                except Exception:
                    logger.exception(
                        "codex child tailer task failed for %s", rs.run_id,
                    )
            await self._flush_backend_state_async(rs)
            await self._emit_complete_from_file(rs, complete_path)
        finally:
            self._cleanup_run(rs.run_id)

    async def _wait_child_setup(self, rs: RunState) -> None:
        while rs.child_setup_tasks:
            await asyncio.gather(
                *list(rs.child_setup_tasks.values()),
                return_exceptions=True,
            )

    async def _wait_child_tailers_terminal(self, rs: RunState) -> None:
        while True:
            await self._wait_child_setup(rs)
            if rs.cancelled:
                return
            if not rs.child_tailers:
                return
            wait_tasks: list[asyncio.Task] = []
            for source_key, tailer in list(rs.child_tailers.items()):
                await tailer.drain_available()
                if source_key in rs.child_terminal_states:
                    continue
                event = rs.child_terminal_events.setdefault(source_key, asyncio.Event())
                wait_tasks.append(asyncio.create_task(event.wait()))
            if not wait_tasks:
                return
            try:
                await asyncio.wait(wait_tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in wait_tasks:
                    if not task.done():
                        task.cancel()

    def _schedule_child_sources(
        self,
        rs: RunState,
        event: dict,
        *,
        parent_source_key: str = "",
        parent_delegation_id: str = "",
    ) -> None:
        from codex_native import codex_subagent_sources_from_event

        for discovered in codex_subagent_sources_from_event(event):
            source_key = discovered["source_key"]
            if parent_source_key:
                source_key = f"{parent_source_key}::{source_key}"
            if source_key in rs.child_setup_tasks or source_key in rs.child_tailers:
                continue
            child_id = discovered["child_id"]
            insert_at = _manager_event_count_for_target(
                rs.persist_to or rs.app_session_id,
                rs.target_message_id,
            ) + 1
            source = {
                **discovered,
                "source_key": source_key,
                "delegation_id": f"codex_subagent_{source_key}",
                "insert_at": insert_at,
            }
            if parent_source_key:
                source["parent_source_key"] = parent_source_key
                source["parent_delegation_id"] = parent_delegation_id
            task = asyncio.get_running_loop().create_task(
                self._ensure_child_tailer(rs, source_key, child_id, source, event),
                name=f"codex-child-tailer-{source_key[:8]}",
            )
            rs.child_setup_tasks[source_key] = task

            def _done(_task: asyncio.Task, _source_key: str = source_key) -> None:
                rs.child_setup_tasks.pop(_source_key, None)
                if _task.cancelled():
                    return
                exc = _task.exception()
                if exc is not None:
                    logger.error(
                        "codex child setup failed for %s",
                        _source_key,
                        exc_info=(type(exc), exc, exc.__traceback__),
                    )

            task.add_done_callback(_done)

    async def _ensure_child_tailer(
        self,
        rs: RunState,
        source_key: str,
        child_id: str,
        source_hint: Optional[dict],
        parent_event: Optional[dict],
    ) -> None:
        if source_key in rs.child_tailers:
            return
        from codex_native import CodexRolloutTailer
        from codex_native import codex_subagent_delegation_id
        from codex_native import codex_subagent_rollout_start_byte
        from codex_native import normalize_rollout_file
        from codex_native import resolve_rollout_path_polled

        source = rs.child_sources.get(source_key) or source_hint or {}
        path_str = source.get("jsonl_path")
        path = Path(path_str) if path_str else await resolve_rollout_path_polled(child_id)
        if path is None:
            return
        parent_tool_use_id = str(source.get("parent_tool_use_id") or "")
        try:
            source_start_byte = int(source.get("start_byte") or 0)
        except (TypeError, ValueError):
            source_start_byte = 0
        if source_start_byte <= 0:
            source_start_byte = codex_subagent_rollout_start_byte(path)
        try:
            tail_start_byte = int(source.get("processed_byte_offset") or 0)
        except (TypeError, ValueError):
            tail_start_byte = 0
        if tail_start_byte <= 0:
            tail_start_byte = source_start_byte
        delegation_id = str(
            source.get("delegation_id")
            or codex_subagent_delegation_id(
                child_id,
                parent_tool_use_id=parent_tool_use_id,
            )
        )
        try:
            insert_at = int(source.get("insert_at"))
        except (TypeError, ValueError):
            insert_at = _manager_event_count_for_target(
                rs.persist_to or rs.app_session_id,
                rs.target_message_id,
            ) + (1 if parent_event is not None else 0)
        rs.child_sources[source_key] = {
            "agent_id": child_id,
            "source_key": source_key,
            "parent_tool_use_id": parent_tool_use_id,
            "jsonl_path": str(path),
            "start_byte": source_start_byte,
            "processed_byte_offset": tail_start_byte,
            "delegation_id": delegation_id,
            "insert_at": insert_at,
        }
        parent_source_key = str(source.get("parent_source_key") or "")
        parent_delegation_id = str(source.get("parent_delegation_id") or "")
        if parent_source_key:
            rs.child_sources[source_key]["parent_source_key"] = parent_source_key
            rs.child_sources[source_key]["parent_delegation_id"] = parent_delegation_id
        terminal_event = rs.child_terminal_events.setdefault(source_key, asyncio.Event())
        existing_terminal = await asyncio.to_thread(
            _rollout_terminal_from_byte,
            path,
            source_start_byte,
        )
        if existing_terminal is not None:
            rs.child_terminal_states[source_key] = existing_terminal
            terminal_event.set()
        self._write_backend_state(rs)

        if tail_start_byte > source_start_byte:
            historical, _ = await asyncio.to_thread(
                normalize_rollout_file,
                path,
                start_byte=source_start_byte,
                namespace=child_id,
                end_byte=tail_start_byte,
            )
            for wrapped in historical:
                event = wrapped.get("data") if isinstance(wrapped, dict) else None
                if isinstance(event, dict):
                    self._schedule_child_sources(
                        rs,
                        event,
                        parent_source_key=source_key,
                        parent_delegation_id=delegation_id,
                    )

        if parent_event is not None:
            try:
                rs.queue.put_nowait(StreamEvent("worker_start", {
                    "delegation_id": delegation_id,
                    "worker_session_id": child_id,
                    "worker_description": f"Codex subagent {child_id}",
                    "panel_kind": "worker",
                    "started_at": datetime.now(timezone.utc).isoformat(),
                    "insert_at": insert_at,
                    "is_new": False,
                    "instructions_preview": "",
                    "run_mode": "codex_subagent",
                    "parent_delegation_id": parent_delegation_id or None,
                }))
            except Exception:
                logger.exception("failed to enqueue codex subagent panel")

        def _dispatch_child(
            event: dict,
            _rs: RunState = rs,
            _did: str = delegation_id,
            _parent_tool_use_id: str = parent_tool_use_id,
        ) -> None:
            event_parent_tool_use_id = event.get("parent_tool_use_id")
            if (
                _parent_tool_use_id
                and event_parent_tool_use_id
                and event_parent_tool_use_id != _parent_tool_use_id
            ):
                return
            try:
                _rs.queue.put_nowait(StreamEvent("worker_event", {
                    "delegation_id": _did,
                    "event": {"type": "agent_message", "data": event},
                }))
            except Exception:
                logger.exception(
                    "Codex child tailer dispatch failed for run %s",
                    _rs.run_id,
                )
            self._schedule_child_sources(
                _rs,
                event,
                parent_source_key=source_key,
                parent_delegation_id=_did,
            )

        def _on_child_cursor(n: int, _rs: RunState = rs, _source_key: str = source_key) -> None:
            _rs.child_sources.setdefault(_source_key, {})["processed_byte_offset"] = n
            self._schedule_backend_state_flush(_rs)

        def _on_child_terminal(
            terminal_state: bool,
            _rs: RunState = rs,
            _source_key: str = source_key,
        ) -> None:
            _rs.child_terminal_states[_source_key] = terminal_state
            _rs.child_terminal_events.setdefault(_source_key, asyncio.Event()).set()

        tailer = CodexRolloutTailer(
            path=path,
            start_byte=tail_start_byte,
            namespace=child_id,
            dispatch=_dispatch_child,
            on_cursor_advance=_on_child_cursor,
            on_terminal_update=_on_child_terminal,
        )
        task = asyncio.get_event_loop().create_task(
            tailer.run(),
            name=f"codex-child-tailer-{child_id[:8]}",
        )
        rs.child_tailers[source_key] = tailer
        rs.child_tailer_tasks[source_key] = task

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
        else:
            recovered_payload = _read_run_rollout_complete(rs.run_dir, rs.session_id)
            if recovered_payload is not None:
                payload = recovered_payload
        if getattr(rs, "cancelled", False):
            payload["success"] = False
            payload["error"] = "cancelled"
        # Codex's runner can't see token_count (it's in the rollout, tailed
        # here), so stamp the context window captured during tailing onto the
        # complete envelope — turn_manager routes it to set_context_window,
        # mirroring how Claude/Gemini carry it in complete.json.
        if rs.tailer is not None:
            window = getattr(rs.tailer.normalizer, "context_window", None)
            if window is not None:
                payload["context_window"] = window
            tokens = getattr(rs.tailer.normalizer, "context_tokens", None)
            if tokens is not None:
                payload["context_tokens"] = tokens
        try:
            rs.queue.put_nowait(StreamEvent("complete", payload))
        except Exception:
            logger.exception("failed to enqueue complete for %s", rs.run_id)

    # ------------------------------------------------------------------
    # _emit_early_failure
    # ------------------------------------------------------------------
    async def _emit_early_failure(self, rs: RunState, msg: str) -> None:
        logger.warning("codex bootstrap failure for %s: %s", rs.run_id, msg)
        try:
            rs.queue.put_nowait(StreamEvent("error", {"error": msg}))
            rs.queue.put_nowait(StreamEvent("complete", {
                "success": False, "error": msg,
                "session_id": None, "token_usage": None,
            }))
        except Exception:
            logger.exception("failed to enqueue early failure for %s", rs.run_id)
        self._cleanup_run(rs.run_id)

    def _write_backend_state(self, rs: RunState) -> None:
        data = {
            "run_id": rs.run_id,
            "app_session_id": rs.app_session_id,
            "persist_to": rs.persist_to or rs.app_session_id,
            "mode": rs.mode,
            "runner_pid": rs.popen.pid,
            "started_at": rs.started_at,
            "session_id": rs.session_id,
            "jsonl_path": str(rs.jsonl_path) if rs.jsonl_path else None,
            "processed_line": rs.processed_line,
            "processed_byte_offset": rs.processed_byte_offset,
            "cancelled": rs.cancelled,
            "target_message_id": rs.target_message_id,
            "turn_run_id": rs.turn_run_id,
            "ingestion_version": CODEX_INGESTION_VERSION,
            "provider_id": self.id,
            "runner": self.record.get("runner"),
            "child_sources": rs.child_sources,
        }
        try:
            _atomic_write_json(self._backend_state_path(rs), data)
            if rs.session_id:
                import spawn_ledger
                spawn_ledger.record_discovered(rs.session_id)
        except Exception:
            logger.exception("failed to write backend_state.json for %s", rs.run_id)

    def _schedule_backend_state_flush(self, rs: RunState) -> None:
        rs.backend_state_flush_dirty = True
        task = rs.backend_state_flush_task
        if task is not None and not task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._write_backend_state(rs)
            rs.backend_state_flush_dirty = False
            return
        rs.backend_state_flush_task = loop.create_task(
            self._coalesced_backend_state_flush(rs),
            name=f"codex-state-flush-{rs.run_id[:8]}",
        )

    async def _coalesced_backend_state_flush(self, rs: RunState) -> None:
        try:
            await self._flush_backend_state_async(rs)
        finally:
            if rs.backend_state_flush_task is asyncio.current_task():
                rs.backend_state_flush_task = None

    async def _flush_backend_state_async(self, rs: RunState) -> None:
        while rs.backend_state_flush_dirty:
            rs.backend_state_flush_dirty = False
            await asyncio.to_thread(self._write_backend_state, rs)

    def attach_recovered_run(
        self,
        *,
        desc: dict,
        queue: asyncio.Queue,
        loop: asyncio.AbstractEventLoop,
    ) -> bool:
        run_id = str(desc.get("run_id") or "")
        pid = live_recovery_pid(desc)
        if not run_id or not pid or run_id in self._runs:
            return False
        try:
            runner_pid = int(pid)
        except (TypeError, ValueError):
            return False

        child_sources = desc.get("child_sources")
        if not isinstance(child_sources, dict):
            child_sources = {}

        try:
            processed_byte_offset = int(desc.get("processed_byte_offset") or 0)
        except (TypeError, ValueError):
            processed_byte_offset = 0

        rs = RunState(
            run_id=run_id,
            run_dir=_runs_root() / run_id,
            popen=RecoveredPopen(runner_pid),
            mode=desc.get("mode") or "native",
            app_session_id=desc.get("app_session_id") or "",
            queue=queue,
            session_id=desc.get("session_id"),
            jsonl_path=Path(desc["jsonl_path"]) if desc.get("jsonl_path") else None,
            processed_byte_offset=processed_byte_offset,
            child_sources={
                str(k): v for k, v in child_sources.items()
                if isinstance(v, dict)
            },
            started_at=desc.get("started_at") or datetime.now().isoformat(),
            cancelled=bool(desc.get("cancelled", False)),
            persist_to=desc.get("persist_to") or desc.get("app_session_id") or "",
            target_message_id=desc.get("target_message_id"),
            turn_run_id=desc.get("turn_run_id"),
        )
        self._runs[run_id] = rs
        self._write_backend_state(rs)
        schedule_loop_task(
            loop,
            self._bootstrap_run(rs),
            name=f"codex-recover-bootstrap-{run_id[:8]}",
        )
        return True

    def _post_cancel_hook(self, rs: RunState) -> None:
        if rs.tailer is not None:
            try:
                rs.tailer.stop()
            except Exception:
                pass
        for event in rs.child_terminal_events.values():
            event.set()
        for tailer in rs.child_tailers.values():
            try:
                tailer.stop()
            except Exception:
                pass

    def steer_run(self, run_id: str, prompt: str, images: Optional[list] = None) -> bool:
        rs = self._runs.get(run_id)
        images = images or []
        if rs is None or rs.popen.poll() is not None or (not prompt.strip() and not images):
            return False
        state_path = rs.run_dir / "state.json"
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        if not state.get("session_id") or not state.get("turn_id"):
            return False
        inbox = rs.run_dir / "steer.jsonl"
        with inbox.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"prompt": prompt, "images": images}) + "\n")
            f.flush()
            os.fsync(f.fileno())
        return True

    # ------------------------------------------------------------------
    # recover_in_flight
    # ------------------------------------------------------------------
    def recover_in_flight(
        self,
        loop: Optional[asyncio.AbstractEventLoop] = None,
        run_id_filter: Optional[set[str]] = None,
    ) -> list[dict]:
        del loop

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

            try:
                processed_byte_offset = int(bs.get("processed_byte_offset") or 0)
            except (TypeError, ValueError):
                processed_byte_offset = 0
            session_id = bs.get("session_id") or rs_disk.get("session_id")
            jsonl_path = bs.get("jsonl_path") or rs_disk.get("jsonl_path")
            if session_id and not jsonl_path:
                from codex_native import resolve_rollout_path
                resolved = resolve_rollout_path(session_id)
                jsonl_path = str(resolved) if resolved else None

            cli_pid_raw = rs_disk.get("cli_pid") or bs.get("cli_pid")
            try:
                cli_pid = int(cli_pid_raw) if cli_pid_raw else None
            except (TypeError, ValueError):
                cli_pid = None
            # Wrapper dead but the codex CLI still alive and writing its
            # rollout jsonl → still running; re-attach. Corroborate via the
            # persisted byte offset + freshness to reject a recycled pid.
            from runs_dir import cli_liveness_corroborated
            orphaned_cli = (
                not alive
                and not has_complete_json
                and cli_liveness_corroborated(
                    cli_pid, jsonl_path, bs.get("jsonl_inode"), processed_byte_offset,
                )
            )
            recovered_as = (
                "live_orphan" if (live_orphan or orphaned_cli) else "dead_orphan"
            )

            if live_orphan:
                # Still-running detached runner: emit it (alive=True) so
                # integrate_recovered_runs re-registers the live turn
                # (running pill) and finalizes from the native rollout jsonl
                # when complete.json appears. The prior "left alone; will
                # complete on its own" continue detached it permanently —
                # nothing re-hooked it until a later backend restart.
                logger.info(
                    "codex recover_in_flight: live orphan %s (pid=%s) "
                    "still running; re-attaching for recovery",
                    child.name, pid,
                )
            elif orphaned_cli:
                logger.info(
                    "codex recover_in_flight: wrapper dead but CLI live for "
                    "%s (cli_pid=%s) — re-attaching to the running CLI",
                    child.name, cli_pid,
                )
            elif not has_complete_json:
                recovered_payload = _read_run_rollout_complete(
                    child,
                    bs.get("session_id") or rs_disk.get("session_id"),
                )
                if recovered_payload is not None:
                    try:
                        _atomic_write_json(complete_path, recovered_payload)
                        has_complete_json = True
                        recovered_as = "completed_from_rollout"
                    except Exception:
                        logger.exception(
                            "failed to write recovered complete.json for %s",
                            child.name,
                        )
                if not has_complete_json:
                    try:
                        complete_path.write_text(json.dumps({
                            "success": False,
                            "session_id": bs.get("session_id") or rs_disk.get("session_id"),
                            "error": "runner died before completion (recovered at startup)",
                            "token_usage": None,
                            "finished_at": datetime.now().isoformat(),
                        }, indent=2), encoding="utf-8")
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
                "cli_pid": cli_pid,
                "orphaned_cli": bool(orphaned_cli),
                "has_complete_json": has_complete_json,
                "session_id": session_id,
                "jsonl_path": jsonl_path,
                "app_session_id": bs.get("app_session_id") or rs_disk.get("app_session_id"),
                "persist_to": bs.get("persist_to") or bs.get("app_session_id"),
                "started_at": bs.get("started_at") or rs_disk.get("started_at") or "",
                "processed_line": processed_line,
                "processed_byte_offset": processed_byte_offset,
                "cancelled": bool(bs.get("cancelled", False)),
                "mode": bs.get("mode") or rs_disk.get("mode") or "native",
                "provider_id": bs.get("provider_id") or self.id,
                "provider_kind": self.KIND,
                "ingestion_version": bs.get("ingestion_version"),
                "target_message_id": bs.get("target_message_id"),
                "turn_run_id": bs.get("turn_run_id"),
                "child_sources": bs.get("child_sources")
                if isinstance(bs.get("child_sources"), dict) else {},
                "recovered_as": recovered_as,
            })

        return recovered

    # ------------------------------------------------------------------
    # prune_old_runs
    # ------------------------------------------------------------------
    def prune_old_runs(self, max_age_days: int = 7) -> int:
        return prune_old_completed_runs(max_age_days)

    # ------------------------------------------------------------------
    # run_headless
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
        self.assert_not_suspended(action="run headless work")
        from cli_paths import resolve_cli_binary

        codex_bin = resolve_cli_binary("codex")
        if not codex_bin:
            logger.error("CodexProvider.run_headless: `codex` CLI not found")
            return None

        cmd: list[str] = [codex_bin, "exec", "--skip-git-repo-check"]
        if no_tools:
            # Read-only sandbox: the model can read context but cannot
            # write files or run mutating shell commands — pure text out.
            cmd += ["-s", "read-only"]
        else:
            cmd += [
                "--dangerously-bypass-approvals-and-sandbox",
                "-s", "danger-full-access",
            ]
        resume_target = resume_sid or session_id
        if resume_target:
            cmd += ["resume", resume_target, prompt]
        else:
            cmd.append(prompt)

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
            logger.error("CodexProvider.run_headless: `codex` CLI not found")
            return None
        except Exception:
            logger.exception("CodexProvider.run_headless: spawn failed")
            return None

        try:
            communicate = proc.communicate()
            if timeout:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    communicate,
                    timeout=timeout,
                )
            else:
                stdout_bytes, stderr_bytes = await communicate
        except asyncio.TimeoutError:
            logger.error("CodexProvider.run_headless: timeout after %ss", timeout)
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            return None

        if proc.returncode != 0:
            logger.error(
                "CodexProvider.run_headless: exited %s; stderr=%r",
                proc.returncode, stderr_bytes[:500],
            )
            return None

        stdout = stdout_bytes.decode(errors="replace").strip()
        if not stdout:
            return None
        # Codex without --json outputs plain text
        return {
            "result": stdout,
            "session_id": None,
            "usage": {},
            "total_cost_usd": 0.0,
        }

    # ------------------------------------------------------------------
    # Rate-limit parsing
    # ------------------------------------------------------------------
    _CODEX_RATE_LIMIT_KEYWORDS = (
        "rate limit", "quota exceeded", "resource exhausted",
        "status: 429", "error 429", "too many requests",
        "usage limit", "capacity",
    )

    def parse_rate_limit(
        self, error: Optional[str], events: list[dict],
    ) -> Optional[datetime]:
        texts: list[str] = []
        if error:
            texts.append(error[-2000:] if len(error) > 2000 else error)
        extracted = self._extract_text_for_rate_limit(events)
        if extracted:
            texts.append(extracted)
        corpus = "\n".join(texts).lower()
        if not corpus:
            return None

        if not any(kw in corpus for kw in self._CODEX_RATE_LIMIT_KEYWORDS):
            return None

        return None

    # ------------------------------------------------------------------
    # rewind — simulate by clearing session_id
    # ------------------------------------------------------------------
    async def rewind(self, app_sid: str, message_uuid: str) -> None:
        from session_manager import manager as session_manager
        session_manager.set_agent_sid(app_sid, "native", None)
        session_manager.set_agent_sid(app_sid, "manager", None)
