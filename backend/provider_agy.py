from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
from pathlib import Path
from typing import ClassVar, Optional

import config_store
from extension_run_policy import (
    disabled_builtin_extensions_for_run,
    disabled_builtin_tools_for_run,
    disabled_runtime_skills_for_run,
)
import user_prefs
from cli_paths import resolve_cli_binary
from containment import containment
from provider import build_better_agent_run_env, schedule_loop_task, runner_argv
from provider_gemini import GeminiProvider, RunState
from provider_run_config import normalize_provider_run_config
from proc_control import process_control as _process_control
from runs_dir import runs_root as _runs_root

logger = logging.getLogger(__name__)

_RUNNER_PATH = Path(__file__).parent / "runner_agy.py"

AGY_MODELS = [
    "Gemini 3.5 Flash (Medium)",
    "Gemini 3.5 Flash (High)",
    "Gemini 3.5 Flash (Low)",
    "Gemini 3.1 Pro (Low)",
    "Gemini 3.1 Pro (High)",
    "Claude Sonnet 4.6 (Thinking)",
    "Claude Opus 4.6 (Thinking)",
    "GPT-OSS 120B (Medium)",
]


def fetch_agy_models() -> list[str]:
    agy_bin = resolve_cli_binary("agy")
    if not agy_bin:
        return list(AGY_MODELS)
    try:
        proc = subprocess.run(
            [agy_bin, "models"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return list(AGY_MODELS)
    if proc.returncode != 0:
        return list(AGY_MODELS)
    out: list[str] = []
    for line in proc.stdout.splitlines():
        token = line.strip().lstrip("*-•").strip()
        if token and token not in out and not token.startswith("-"):
            out.append(token)
    return out if out else list(AGY_MODELS)


class AgyProvider(GeminiProvider):
    KIND: ClassVar[str] = "agy"

    supports_fork: ClassVar[bool] = False
    supports_manager_mode: ClassVar[bool] = False
    supports_rewind: ClassVar[bool] = False
    rewind_requires_agent_identity: ClassVar[bool] = True
    supports_semantic_alter: ClassVar[bool] = True
    supports_steering: ClassVar[bool] = False
    supports_native_subagents: ClassVar[bool] = True
    supports_reasoning_effort: ClassVar[bool] = False

    def build_env(self) -> dict[str, str]:
        self.require_runtime_credential()
        env = os.environ.copy()
        env.pop("CLAUDE_CONFIG_DIR", None)
        env.pop("ANTHROPIC_API_KEY", None)
        env.pop("ANTHROPIC_BASE_URL", None)
        env.pop("ANTHROPIC_AUTH_TOKEN", None)
        env.pop("CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING", None)
        return self.finalize_env(env)

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
        del supervised, supervisor_agent_session_id, mssg_sender_session_id, is_worker
        del continuation_chain
        if mode == "manager":
            mode = "team"
        if mode not in ("native", "team"):
            raise ValueError(f"mode must be 'native' or 'team', got {mode!r}")
        if self.defunct:
            raise RuntimeError(f"provider {self.id} is defunct; cannot start new runs")
        self.assert_not_suspended(action="start new runs")
        if reasoning_effort:
            raise NotImplementedError("agy provider does not support reasoning effort.")
        if mode == "team":
            raise NotImplementedError("agy provider does not support team mode.")
        if fork:
            raise NotImplementedError("agy provider does not support fork.")

        available = self.available_models()
        if model and model not in available:
            raise ValueError(
                f"model {model!r} is not available for the AGY provider. "
                f"Available: {', '.join(available)}."
            )

        run_dir = _runs_root() / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        from session_manager import manager as _sm
        session_record = _sm.get(app_session_id) or {}
        worker_record = _sm.get(worker_agent_session_id) if worker_agent_session_id else {}
        input_payload = {
            "prompt": prompt,
            "images": images or [],
            "files": files or [],
            "cwd": cwd,
            "model": model,
            "session_id": session_id,
            "mode": mode,
            "source": source or "",
            "app_session_id": app_session_id,
            "active_capability_ids": [
                str(cid)
                for cid in (session_record.get("active_capability_ids") or [])
                if str(cid or "").strip()
            ],
            "backend_url": backend_url or "",
            "internal_token": "",
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
            "disabled_builtin_tools": disabled_builtin_tools_for_run(
                session_record=session_record, worker_record=worker_record,
            ),
            "disabled_runtime_skills": disabled_runtime_skills_for_run(
                session_record=session_record, worker_record=worker_record,
            ),
            "disabled_builtin_extensions": (
                disabled_builtin_extensions_for_run(
                    disabled_builtin_extensions,
                    session_record=session_record,
                    worker_record=worker_record,
                )
            ),
            "provider_run_config": normalize_provider_run_config(provider_run_config),
        }
        (run_dir / "input.json").write_text(json.dumps(input_payload), encoding="utf-8")

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
                run_id=run_id,
                app_session_id=app_session_id,
                cwd=cwd,
                model=model,
                provider_id=self.id,
                bare_config=bool(session_record.get("bare_config")),
                user_facing=bool(open_file_panel_enabled) and not bool(session_record.get("bare_config")),
                disabled_builtin_extensions=input_payload["disabled_builtin_extensions"],
            ))
            popen = subprocess.Popen(
                runner_argv(run_dir, dev_script=_RUNNER_PATH, kind="agy"),
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
            name=f"agy-bootstrap-{run_id[:8]}",
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
            # No proven tool-disable flag — fail closed rather than run
            # a tool-capable CLI when the caller demanded text-only.
            logger.error("AgyProvider.run_headless: no_tools requested but unsupported")
            return None
        if fork:
            logger.warning("AGY provider ignores fork flag in run_headless")
        agy_bin = resolve_cli_binary("agy")
        if not agy_bin:
            logger.error("AgyProvider.run_headless: `agy` CLI not found")
            return None
        cmd = [agy_bin]
        if resume_sid or session_id:
            cmd += ["--conversation", resume_sid or session_id or ""]
        cmd += ["-p"]
        cmd.append(prompt)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self.build_env(),
            cwd=cwd,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=timeout,
            ) if timeout else await proc.communicate()
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return None
        if proc.returncode != 0:
            logger.error(
                "AgyProvider.run_headless: exited %s; stderr=%r",
                proc.returncode, stderr_bytes[:500],
            )
            return None
        return {
            "result": stdout_bytes.decode(errors="replace").strip(),
            "session_id": None,
            "usage": {},
            "total_cost_usd": 0.0,
        }

    async def rewind(self, app_sid: str, message_uuid: str) -> None:
        raise NotImplementedError("agy provider does not support rewind.")
