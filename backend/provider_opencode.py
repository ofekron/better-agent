"""OpencodeProvider — `Provider` implementation for the OpenCode CLI.

Drives the `opencode` binary (npm `opencode-ai`) via a detached
`runner_opencode.py` subprocess per turn. The runner spawns
`opencode run --format json` with the prompt on stdin, normalizes the
raw JSON events streamed on stdout to Claude jsonl shape, and writes
`session_events.jsonl`. This provider tails that file and pushes events
onto the orchestrator queue — identical to the GeminiProvider path,
which OpencodeProvider subclasses to reuse RunState / bootstrap /
tailer / completion watcher / disk recovery.

Auth: `opencode auth login` (per-provider OAuth or API keys, stored in
`~/.local/share/opencode/auth.json`) or provider API-key env vars.
The bundled `opencode/*-free` models work with no credentials at all,
so a fresh install is immediately usable.

Session state lives in OpenCode's own shared data dir
(`$XDG_DATA_HOME/opencode`, default `~/.local/share/opencode`) — it is
deliberately NOT isolated per run, because resume (`-s <sid>`) and fork
(`--fork`) must find the session across turns and backend restarts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
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
import provider_runtime
from provider_gemini import GeminiProvider, RunState
from provider_run_config import normalize_provider_run_config
from proc_control import process_control as _process_control
from runs_dir import runs_root as _runs_root

logger = logging.getLogger(__name__)

_RUNNER_PATH = Path(__file__).parent / "runner_opencode.py"

# Cold-start models for the OpenCode CLI. These are the credential-free
# `opencode/*` (OpenCode Zen) ids returned by `opencode models` on a
# fresh opencode 1.17.18 install (verified 2026-07-09) — usable with zero
# auth, so a cold start always has a working model. Users with configured
# provider credentials get their full catalog via `fetch_opencode_models`
# (the daily refresh re-runs `opencode models`).
OPENCODE_MODELS = [
    "opencode/big-pickle",
    "opencode/deepseek-v4-flash-free",
    "opencode/hy3-free",
    "opencode/mimo-v2.5-free",
    "opencode/nemotron-3-ultra-free",
    "opencode/north-mini-code-free",
]

# Every valid opencode model id is `provider/model` (the CLI's own -m
# format). Anything else in `opencode models` output is log noise.
_MODEL_LINE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._:-]*$")


def parse_opencode_models(text: str) -> list[str]:
    """Parse `opencode models` output: one `provider/model` id per line."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or not _MODEL_LINE_RE.match(line):
            continue
        if line in seen:
            continue
        seen.add(line)
        out.append(line)
    return out


def fetch_opencode_models() -> list[str]:
    """Run the installed `opencode` CLI's `models` command and parse the
    catalog. Returns [] on any failure (CLI missing, non-zero exit, no
    parseable ids) so the caller keeps the prior cache and falls back to
    the static OPENCODE_MODELS seed."""
    opencode_bin = resolve_cli_binary("opencode")
    if not opencode_bin:
        return []
    try:
        proc = subprocess.run(
            [opencode_bin, "models"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    return parse_opencode_models(proc.stdout)


def _dedupe_preserve_order(seq: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in seq:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


class OpencodeProvider(GeminiProvider):
    """OpenCode CLI provider. Native-mode only (no in-process SDK MCP
    registration → no manager mode, no mid-turn steering), but with a
    real non-interactive fork primitive (`--fork`) and per-run reasoning
    effort via `--variant`. Reuses GeminiProvider's RunState, tailer
    bootstrap, completion watcher, and disk recovery — only the runner
    binary, env, and capability surface differ."""

    KIND: ClassVar[str] = "opencode"

    # `opencode run -s <sid> --fork` forks the session before continuing —
    # a real headless fork primitive (verified: fork run reports a new
    # sessionID while the source session is left intact).
    supports_fork: ClassVar[bool] = True
    supports_manager_mode: ClassVar[bool] = False
    # No rewind primitive; simulated the Gemini way — clear the stored
    # provider session id so the next turn starts a fresh CLI session.
    supports_rewind: ClassVar[bool] = True
    rewind_requires_agent_identity: ClassVar[bool] = False
    supports_steering: ClassVar[bool] = False
    supports_native_subagents: ClassVar[bool] = False
    # `--variant` = provider-specific reasoning effort. The CLI help names
    # high/max/minimal; unknown variants are ignored by models that don't
    # support them (verified `--variant high` on a live run).
    supports_reasoning_effort: ClassVar[bool] = True
    reasoning_effort_options: ClassVar[tuple[str, ...]] = ("minimal", "high", "max")
    default_reasoning_effort: ClassVar[str] = ""

    def build_env(self) -> dict[str, str]:
        self.require_runtime_credential()
        env = os.environ.copy()
        # OpenCode reads $XDG_DATA_HOME/opencode (default
        # ~/.local/share/opencode) — nothing to configure. Clear Claude
        # env so a concurrently-configured Claude provider can't leak
        # into the opencode subprocess.
        env.pop("CLAUDE_CONFIG_DIR", None)
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
        del supervised, supervisor_agent_session_id
        del continuation_chain
        if mode == "manager":
            mode = "team"
        if mode not in ("native", "team"):
            raise ValueError(f"mode must be 'native' or 'team', got {mode!r}")
        if self.defunct:
            raise RuntimeError(f"provider {self.id} is defunct; cannot start new runs")
        self.assert_not_suspended(action="start new runs")
        if reasoning_effort and reasoning_effort not in self.reasoning_effort_options:
            raise ValueError(
                f"reasoning effort {reasoning_effort!r} is not supported by the "
                f"OpenCode provider. Allowed: {', '.join(self.reasoning_effort_options)}."
            )
        if mode == "team":
            raise NotImplementedError("opencode provider does not support team mode.")
        if fork and not session_id:
            raise ValueError("opencode fork requires a session id to fork from.")

        available = _dedupe_preserve_order(self.available_models() + OPENCODE_MODELS)
        if model and model not in available:
            raise ValueError(
                f"model {model!r} is not available for the OpenCode provider. "
                f"Available: {', '.join(available)}."
            )

        run_dir = _runs_root() / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        from session_manager import manager as _sm
        session_record = _sm.get(app_session_id) or {}
        worker_record = _sm.get(worker_agent_session_id) if worker_agent_session_id else {}
        from permission import resolve_for_run as _resolve_perm
        _permission = _resolve_perm(
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
            "reasoning_effort": reasoning_effort,
            "permission": _permission,
            "session_id": session_id,
            "fork": bool(fork),
            "mode": mode,
            "source": source or "",
            "app_session_id": app_session_id,
            "backend_url": backend_url or "",
            "internal_token": "",
            "provider_id": self.id,
            "browser_harness_enabled": bool(browser_harness_enabled),
            "open_file_panel_enabled": bool(open_file_panel_enabled),
            "worker_agent_session_id": worker_agent_session_id,
            "mssg_sender_session_id": mssg_sender_session_id,
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
        (run_dir / "input.json").write_text(
            json.dumps(input_payload), encoding="utf-8"
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
                run_id=run_id,
                app_session_id=app_session_id,
                cwd=cwd,
                model=model,
                provider_id=self.id,
                bare_config=bool(session_record.get("bare_config")),
                user_facing=bool(open_file_panel_enabled) and not bool(session_record.get("bare_config")),
                disabled_builtin_extensions=input_payload["disabled_builtin_extensions"],
            ))
            popen = provider_runtime.popen_runner(
                runner_argv(run_dir, dev_script=_RUNNER_PATH, kind="opencode"),
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
            "spawned opencode runner pid=%d mode=%s run_id=%s",
            popen.pid, mode, run_id,
        )

        from datetime import datetime as _dt
        rs = RunState(
            run_id=run_id,
            run_dir=run_dir,
            popen=popen,
            mode=mode,
            app_session_id=app_session_id,
            queue=queue,
            started_at=_dt.now().isoformat(),
            persist_to=worker_agent_session_id or app_session_id,
            target_message_id=target_message_id,
            turn_run_id=turn_run_id,
        )
        self._runs[run_id] = rs
        self._write_backend_state(rs)
        schedule_loop_task(
            loop,
            self._bootstrap_run(rs),
            name=f"opencode-bootstrap-{run_id[:8]}",
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
            # OPENCODE_PERMISSION deny is verified for built-ins only;
            # user-configured MCP tools would still be reachable. Fail
            # closed when the caller demanded a guaranteed tool-less run.
            logger.error("OpencodeProvider.run_headless: no_tools requested but unsupported")
            return None
        opencode_bin = resolve_cli_binary("opencode")
        if not opencode_bin:
            logger.error("OpencodeProvider.run_headless: `opencode` CLI not found")
            return None
        resume_target = resume_sid or session_id
        if fork and not resume_target:
            logger.error("OpencodeProvider.run_headless: fork requires a session id")
            return None
        cmd = [opencode_bin, "run", "--format", "json"]
        if cwd:
            # Pin the project dir explicitly — the bun-built CLI resolves
            # its directory from $PWD, not the subprocess spawn cwd.
            cmd += ["--dir", cwd]
        if resume_target:
            cmd += ["-s", resume_target]
            if fork:
                cmd += ["--fork"]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self.build_env(),
                cwd=cwd,
                limit=16 * 1024 * 1024,
            )
        except FileNotFoundError:
            logger.error("OpencodeProvider.run_headless: `opencode` CLI not found")
            return None

        try:
            communicate = proc.communicate(prompt.encode("utf-8"))
            if timeout:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    communicate, timeout=timeout,
                )
            else:
                stdout_bytes, stderr_bytes = await communicate
        except asyncio.TimeoutError:
            logger.error("OpencodeProvider.run_headless: timeout after %ss", timeout)
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.communicate()
            return None
        if proc.returncode != 0:
            logger.error(
                "OpencodeProvider.run_headless: exited %s; stderr=%r",
                proc.returncode, (stderr_bytes or b"")[:500],
            )
            return None

        texts: list[str] = []
        discovered_sid: Optional[str] = None
        usage: dict[str, int] = {}
        from runner_opencode import _sum_tokens
        for line in stdout_bytes.decode(errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            if not discovered_sid and event.get("sessionID"):
                discovered_sid = str(event["sessionID"])
            part = event.get("part") if isinstance(event.get("part"), dict) else {}
            if event.get("type") == "text" and part.get("text"):
                texts.append(str(part["text"]))
            elif event.get("type") == "step_finish":
                usage = _sum_tokens(usage, part.get("tokens"))
        return {
            "result": "\n".join(texts).strip(),
            "session_id": discovered_sid,
            "usage": usage,
            "total_cost_usd": 0.0,
        }
