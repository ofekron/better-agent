"""CopilotProvider — `Provider` implementation for GitHub's Copilot CLI.

Drives the `copilot` binary (a.k.a. `gh copilot`) via a detached
`runner_copilot.py` subprocess per turn. The runner spawns
`copilot -p <prompt> --allow-all-tools`, tails Copilot's own structured
event log at `<config_dir>/session-state/<sessionId>.jsonl`, normalizes
those events to Claude jsonl shape, and writes `session_events.jsonl`.
This provider tails that file and pushes events onto the orchestrator
queue — identical to the GeminiProvider path, which CopilotProvider
subclass reuses for RunState / bootstrap / tailer / recovery.

Auth: Copilot CLI authenticates via GitHub OAuth (`gh auth login` or an
interactive `copilot` login). There is no API-key mode, so the provider
record is subscription-only.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from pathlib import Path
from typing import ClassVar, Optional

import config_store
import user_prefs
from cli_paths import resolve_cli_binary
from containment import containment
from provider import build_better_agent_run_env, create_loop_task, runner_argv
from provider_gemini import GeminiProvider, RunState
from provider_run_config import normalize_provider_run_config
from proc_control import process_control as _process_control
from runs_dir import runs_root as _runs_root

logger = logging.getLogger(__name__)

_RUNNER_PATH = Path(__file__).parent / "runner_copilot.py"

# Models the `copilot` CLI accepts for `--model`, taken verbatim from the
# CLI's `--help` choices list (copilot-cli 0.0.395, 2026-03). The CLI
# rejects anything outside this set, so the static seed doubles as the
# `start_run` validator. `fetch_copilot_models` re-parses `--help` so the
# catalog tracks CLI upgrades without a code change.
COPILOT_MODELS = [
    "gpt-5.2-codex",
    "gpt-5.2",
    "gpt-5.1-codex-max",
    "gpt-5.1-codex",
    "gpt-5.1",
    "gpt-5.1-codex-mini",
    "gpt-5",
    "gpt-5-mini",
    "gpt-4.1",
    "claude-sonnet-4.5",
    "claude-opus-4.5",
    "claude-haiku-4.5",
    "claude-sonnet-4",
    "gemini-3-pro-preview",
]


def fetch_copilot_models() -> list[str]:
    """Parse the installed `copilot` CLI's `--model` choices out of
    `--help`. Returns [] on any failure (CLI missing, choices block not
    located, post-filter list too small) so the caller keeps the prior
    cache and falls back to the static COPILOT_MODELS seed."""
    import re

    copilot_bin = resolve_cli_binary("copilot")
    if not copilot_bin:
        return []
    try:
        proc = subprocess.run(
            [copilot_bin, "--help"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []

    # The --model help line looks like:
    #   --model <model>  Set the AI model ... (choices: "a", "b", "c-3")
    # Parse the parenthesized choices list with a bracket counter so a
    # future choice containing a stray ')' inside a quoted string can't
    # truncate the match.
    text = proc.stdout
    head = re.search(r"--model\s+<[^>]+>[\s\S]*?\(choices:\s*", text)
    if not head:
        return []
    # The head match already consumed the choices group's opening `(`,
    # so the scan starts inside the parens at depth 1 and ends at the
    # matching `)`. Bracket-counted (not a regex) so a future quoted
    # choice containing `)` can't truncate the list.
    depth = 1
    end = -1
    start = head.end()
    for i in range(start, len(text)):
        c = text[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                end = i
                break
    if end < 0:
        return []
    body = text[start:end]
    models = re.findall(r'"([^"]+)"', body)
    # Drop obvious non-chat families the CLI may mix in later.
    filtered = [m for m in models if not m.startswith(("gemma-", "o1-", "text-"))]
    if len(filtered) < 3:
        return []
    return filtered


class CopilotProvider(GeminiProvider):
    """GitHub Copilot CLI provider. Native-mode only: Copilot has no
    non-interactive fork primitive, no in-process SDK MCP registration
    (manager mode), no mid-turn steering, and no reasoning-effort flag.
    Reuses GeminiProvider's RunState, tailer bootstrap, completion
    watcher, and disk recovery — only the runner binary and env differ."""

    KIND: ClassVar[str] = "copilot"

    supports_fork: ClassVar[bool] = False
    supports_manager_mode: ClassVar[bool] = False
    # Copilot has no rewind primitive, but we simulate one the way Gemini
    # does: clear the stored provider session id so the next turn starts
    # a fresh CLI session.
    supports_rewind: ClassVar[bool] = True
    rewind_requires_agent_identity: ClassVar[bool] = False
    supports_steering: ClassVar[bool] = False
    supports_native_subagents: ClassVar[bool] = False
    supports_reasoning_effort: ClassVar[bool] = False

    def build_env(self) -> dict[str, str]:
        env = os.environ.copy()
        # Copilot reads ~/.copilot by default (overridable via --config-dir
        # in the runner). Clear Claude env so a concurrently-configured
        # Claude provider can't leak into the Copilot subprocess.
        env.pop("CLAUDE_CONFIG_DIR", None)
        env.pop("ANTHROPIC_API_KEY", None)
        env.pop("ANTHROPIC_BASE_URL", None)
        env.pop("ANTHROPIC_AUTH_TOKEN", None)
        env.pop("CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING", None)
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
        del disallowed_tools, setting_sources
        del supervised, supervisor_agent_session_id, mssg_sender_session_id, is_worker
        del continuation_chain
        if mode == "manager":
            mode = "team"
        if mode not in ("native", "team"):
            raise ValueError(f"mode must be 'native' or 'team', got {mode!r}")
        if self.defunct:
            raise RuntimeError(f"provider {self.id} is defunct; cannot start new runs")
        if reasoning_effort:
            raise NotImplementedError("copilot provider does not support reasoning effort.")
        if mode == "team":
            raise NotImplementedError("copilot provider does not support team mode.")
        if fork:
            raise NotImplementedError("copilot provider does not support fork.")

        available = self.available_models()
        if model and model not in available:
            raise ValueError(
                f"model {model!r} is not available for the Copilot provider. "
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
            "app_session_id": app_session_id,
            "backend_url": backend_url or "",
            "internal_token": internal_token or "",
            "provider_id": self.id,
            "config_dir": self.record.get("config_dir", ""),
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
            "disabled_builtin_tools": config_store.get_disabled_builtin_tools(),
            "disabled_builtin_extensions": (
                disabled_builtin_extensions
                if disabled_builtin_extensions is not None
                else config_store.get_disabled_builtin_extensions()
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
            popen = subprocess.Popen(
                runner_argv(run_dir, dev_script=_RUNNER_PATH, kind="copilot"),
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
        rs.bootstrap_task = create_loop_task(
            loop,
            self._bootstrap_run(rs),
            name=f"copilot-bootstrap-{run_id[:8]}",
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
        if no_tools:
            # Copilot runs with --allow-all-tools; no proven disable path —
            # fail closed when the caller demanded a text-only run.
            logger.error("CopilotProvider.run_headless: no_tools requested but unsupported")
            return None
        if fork:
            logger.warning("Copilot provider ignores fork flag in run_headless")
        copilot_bin = resolve_cli_binary("copilot")
        if not copilot_bin:
            logger.error("CopilotProvider.run_headless: `copilot` CLI not found")
            return None
        # -s/--silent: agent response only, no stats banner → clean text.
        cmd = [copilot_bin, "-s"]
        resume_target = resume_sid or session_id
        if resume_target:
            cmd += ["--resume", resume_target]
        cmd += ["-p", prompt, "--allow-all-tools"]
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
            logger.error("CopilotProvider.run_headless: `copilot` CLI not found")
            return None

        try:
            kw = {"timeout": timeout} if timeout else {}
            stdout_bytes, stderr_bytes = await proc.communicate(**kw)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return None
        if proc.returncode != 0:
            logger.error(
                "CopilotProvider.run_headless: exited %s; stderr=%r",
                proc.returncode, stderr_bytes[:500],
            )
            return None
        return {
            "result": stdout_bytes.decode(errors="replace").strip(),
            "session_id": None,
            "usage": {},
            "total_cost_usd": 0.0,
        }
