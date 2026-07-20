"""AmpProvider — `Provider` implementation for Sourcegraph's Amp CLI.

Drives the `amp` binary via a detached `runner_amp.py` subprocess per
turn. The runner spawns `amp -x --stream-json` (or `amp threads
continue <threadId> -x --stream-json` to resume), parses Amp's
Claude-Code-compatible stream-json from stdout, normalizes it to
Claude jsonl shape, and writes `session_events.jsonl`. This provider
tails that file and pushes events onto the orchestrator queue —
identical to the GeminiProvider path, which AmpProvider subclasses to
reuse RunState / bootstrap / tailer / completion watcher / recovery.

Auth: `amp login` stores an API key locally; AMP_API_KEY overrides it
per process. A provider record may carry `api_key` (routed into the
subprocess env, never logged) and `base_url` (AMP_URL, for
enterprise/self-hosted servers). NOTE: Amp's execute mode (`amp -x`)
requires paid credits — Amp Free is interactive-only (server enforces
with a 402).

Session continuation: the thread id (`T-<uuid>`) is the stream-json
`session_id`. Fork is real (`amp threads fork <id>` prints a new
thread id), so supports_fork=True.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from pathlib import Path
from typing import ClassVar, Optional

import config_store
from extension_run_policy import disabled_builtin_extensions_for_run
import user_prefs
from cli_paths import resolve_cli_binary
from containment import containment
from provider import build_better_agent_run_env, schedule_loop_task, runner_argv
import provider_runtime
from provider_gemini import GeminiProvider, RunState
from provider_run_config import normalize_provider_run_config
from proc_control import process_control as _process_control
from runs_dir import runs_root as _runs_root

logger = logging.getLogger(__name__)

_RUNNER_PATH = Path(__file__).parent / "runner_amp.py"

# Amp auto-selects the underlying LLM; the only selectable knobs are the
# agent mode (`-m smart|rush|free` — controls model + system prompt +
# tool selection) and the Sonnet toggle (`--use-sonnet`). These selectors
# are the model catalog: "auto" = Amp's default (Opus 4.5, mode smart).
# There is no per-LLM `--model` flag and no live catalog to probe
# (verified against amp 0.0.1765051277 --help, 2026-07-09).
AMP_MODELS = [
    "auto",
    "smart",
    "rush",
    "free",
    "sonnet",
]


def fetch_amp_models() -> list[str]:
    """Amp exposes no model-list surface (no `--model`, no catalog
    endpoint); the static selector list above is the whole catalog."""
    return list(AMP_MODELS)


class AmpProvider(GeminiProvider):
    uses_managed_api_key = True
    """Sourcegraph Amp CLI provider. Fork is supported natively
    (`amp threads fork`); everything else is native-mode only: no
    in-process SDK MCP registration (manager mode), no mid-turn
    steering, no reasoning-effort flag. Amp DOES run its own internal
    subagents (Task tool), but not through Better Agent's native
    subagent integration. Reuses GeminiProvider's RunState, tailer
    bootstrap, completion watcher, and disk recovery — only the runner
    binary and env differ."""

    KIND: ClassVar[str] = "amp"

    supports_fork: ClassVar[bool] = True
    supports_manager_mode: ClassVar[bool] = False
    # Amp has no rewind primitive; simulate like Gemini/Copilot by
    # clearing the stored thread id so the next turn starts fresh.
    supports_rewind: ClassVar[bool] = True
    rewind_requires_agent_identity: ClassVar[bool] = False
    supports_steering: ClassVar[bool] = False
    supports_native_subagents: ClassVar[bool] = False
    supports_reasoning_effort: ClassVar[bool] = False

    def build_env(self) -> dict[str, str]:
        self.require_runtime_credential()
        env = os.environ.copy()
        # Amp reads ~/.config/amp + AMP_API_KEY/AMP_URL. Clear Claude env
        # so a concurrently-configured Claude provider can't leak into
        # the Amp subprocess.
        env.pop("CLAUDE_CONFIG_DIR", None)
        env.pop("ANTHROPIC_API_KEY", None)
        env.pop("ANTHROPIC_BASE_URL", None)
        env.pop("ANTHROPIC_AUTH_TOKEN", None)
        env.pop("CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING", None)
        record = self.record
        api_key = str(record.get("api_key") or "").strip()
        if api_key:
            env["AMP_API_KEY"] = api_key
        base_url = str(record.get("base_url") or "").strip()
        if base_url:
            env["AMP_URL"] = base_url
        return env

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
        del disallowed_tools, setting_sources
        del supervised, supervisor_agent_session_id, mssg_sender_session_id
        del continuation_chain
        if mode == "manager":
            mode = "team"
        if mode not in ("native", "team"):
            raise ValueError(f"mode must be 'native' or 'team', got {mode!r}")
        if self.defunct:
            raise RuntimeError(f"provider {self.id} is defunct; cannot start new runs")
        self.assert_not_suspended(action="start new runs")
        if reasoning_effort:
            raise NotImplementedError("amp provider does not support reasoning effort.")
        if mode == "team":
            raise NotImplementedError("amp provider does not support team mode.")
        if fork and not session_id:
            raise ValueError("amp fork requires an existing thread id to fork from")

        model = str(model or "").strip()
        available = list(dict.fromkeys(self.available_models() + AMP_MODELS))
        if model and model not in available:
            raise ValueError(
                f"model {model!r} is not available for the Amp provider. "
                f"Available: {', '.join(available)}."
            )

        run_dir = _runs_root() / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        from session_manager import manager as _sm
        session_record = _sm.get(app_session_id) or {}
        worker_record = _sm.get(worker_agent_session_id) if worker_agent_session_id else {}
        from permission import resolve_for_run as _resolve_perm
        permission = _resolve_perm(
            sess_rec=session_record,
            worker_sess_rec=worker_record,
            is_worker=is_worker,
            fallback_kind=self.KIND,
        )
        input_payload = {
            "prompt": prompt,
            "images": images or [],
            "files": files or [],
            "cwd": cwd,
            "model": model,
            "permission": permission,
            "session_id": session_id,
            "fork": bool(fork),
            "mode": mode,
            "source": source or "",
            "app_session_id": app_session_id,
            "backend_url": backend_url or "",
            "internal_token": internal_token or "",
            "provider_id": self.id,
            "browser_harness_enabled": bool(browser_harness_enabled),
            "open_file_panel_enabled": bool(open_file_panel_enabled),
            "worker_agent_session_id": worker_agent_session_id,
            "bare_config": bool(session_record.get("bare_config")),
            "working_mode": session_record.get("working_mode"),
            "worker_working_mode": (worker_record or {}).get("working_mode"),
            "context_strategy": user_prefs.get_context_strategy(),
            "capability_contexts": capability_contexts or [],
            "target_message_id": target_message_id,
            "turn_run_id": turn_run_id,
            "provisioned_tool_profile": str(provisioned_tool_profile or "").strip(),
            "disabled_builtin_tools": config_store.get_disabled_builtin_tools(),
            "disabled_builtin_extensions": (
                disabled_builtin_extensions_for_run(
                    disabled_builtin_extensions,
                    session_record=session_record,
                    worker_record=worker_record,
                )
            ),
            "provider_run_config": normalize_provider_run_config(provider_run_config),
        }
        (run_dir / "input.json").write_text(
            __import__("json").dumps(input_payload), encoding="utf-8"
        )

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
                bare_config=bool(session_record.get("bare_config")),
                user_facing=bool(open_file_panel_enabled) and not bool(session_record.get("bare_config")),
                disabled_builtin_extensions=input_payload["disabled_builtin_extensions"],
            ))
            popen = provider_runtime.popen_runner(
                runner_argv(run_dir, dev_script=_RUNNER_PATH, kind="amp"),
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

        rs = RunState(
            run_id=run_id,
            run_dir=run_dir,
            popen=popen,
            mode=mode,
            app_session_id=app_session_id,
            queue=queue,
            started_at=__import__("datetime").datetime.now().isoformat(),
            persist_to=worker_agent_session_id or app_session_id,
            target_message_id=target_message_id,
            turn_run_id=turn_run_id,
        )
        self._runs[run_id] = rs
        self._write_backend_state(rs)
        schedule_loop_task(
            loop,
            self._bootstrap_run(rs),
            name=f"amp-bootstrap-{run_id[:8]}",
        )

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
        if no_tools:
            # Amp exposes no proven way to disable every tool for one
            # execute-mode run — fail closed when the caller demanded a
            # text-only run.
            logger.error("AmpProvider.run_headless: no_tools requested but unsupported")
            return None
        amp_bin = resolve_cli_binary("amp")
        if not amp_bin:
            logger.error("AmpProvider.run_headless: `amp` CLI not found")
            return None
        resume_target = resume_sid or session_id
        if fork and resume_target:
            from runner_amp import parse_fork_thread_id
            fork_proc = await asyncio.create_subprocess_exec(
                amp_bin, "threads", "fork", resume_target,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self.build_env(),
                cwd=cwd,
            )
            out_bytes, err_bytes = await fork_proc.communicate()
            forked = parse_fork_thread_id(
                out_bytes.decode(errors="replace") + "\n" + err_bytes.decode(errors="replace")
            )
            if fork_proc.returncode != 0 or not forked:
                logger.error(
                    "AmpProvider.run_headless: fork of %s failed: %r",
                    resume_target, err_bytes[:500],
                )
                return None
            resume_target = forked

        # Execute mode prints only the last assistant message — clean text.
        cmd: list[str] = [amp_bin]
        if resume_target:
            cmd += ["threads", "continue", resume_target]
        cmd += ["-x"]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self.build_env(),
                cwd=cwd,
            )
        except FileNotFoundError:
            logger.error("AmpProvider.run_headless: `amp` CLI not found")
            return None

        async def _communicate() -> tuple[bytes, bytes]:
            return await proc.communicate(input=prompt.encode("utf-8"))

        try:
            if timeout:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    _communicate(), timeout=timeout,
                )
            else:
                stdout_bytes, stderr_bytes = await _communicate()
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return None
        result_text = stdout_bytes.decode(errors="replace").strip()
        if proc.returncode != 0 or not result_text:
            logger.error(
                "AmpProvider.run_headless: exited %s; stderr=%r",
                proc.returncode, stderr_bytes[:500],
            )
            return None
        return {
            "result": result_text,
            "session_id": resume_target,
            "usage": {},
            "total_cost_usd": 0.0,
        }
