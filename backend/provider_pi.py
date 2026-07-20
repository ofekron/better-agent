"""PiProvider — `Provider` implementation for the pi coding agent CLI
(badlogic/pi-mono, npm `@mariozechner/pi-coding-agent`).

Drives the `pi` binary via a detached `runner_pi.py` subprocess per turn.
The runner spawns `pi --mode json -p` (prompt on stdin), normalizes pi's
JSON event stream to Claude jsonl shape, and writes
`<run_dir>/session_events.jsonl`. This provider tails that file and pushes
events onto the orchestrator queue — identical to the GeminiProvider path,
which PiProvider subclasses for RunState / bootstrap / tailer / recovery
(gemini recovery family).

Auth: pi reads provider API keys from env vars (ANTHROPIC_API_KEY,
OPENAI_API_KEY, GEMINI_API_KEY, …) or OAuth/API-key records the user
stores via its interactive `/login` in `~/.pi/agent/auth.json`. The CLI is
the credential authority; Better Agent passes nothing through.

Models are `provider/id` pairs (e.g. `anthropic/claude-sonnet-4-6`), with
an optional `:<thinking>` suffix pi resolves natively. Reasoning effort
maps to pi's `--thinking off|minimal|low|medium|high|xhigh`.
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

_RUNNER_PATH = Path(__file__).parent / "runner_pi.py"

# Cold-start models for the pi CLI, as `provider/id` pairs. Every entry is
# present in pi-coding-agent 0.73.1's bundled model catalog
# (@mariozechner/pi-ai dist/models.generated.js) — verified 2026-07-09.
# Availability still depends on which providers the user has credentials
# for; the CLI is the final authority. `fetch_pi_models` re-parses the
# installed CLI's `--list-models` output so the catalog tracks both CLI
# upgrades and the user's actual logins.
PI_MODELS = [
    "anthropic/claude-opus-4-7",
    "anthropic/claude-opus-4-6",
    "anthropic/claude-sonnet-4-6",
    "anthropic/claude-haiku-4-5",
    "openai/gpt-5.5",
    "openai/gpt-5.4",
    "openai/gpt-5.4-mini",
    "openai/gpt-5.3-codex",
    "google/gemini-3.1-pro-preview",
    "google/gemini-3-flash-preview",
    "google/gemini-2.5-pro",
    "google/gemini-2.5-flash",
    "openai-codex/gpt-5.5",
    "openai-codex/gpt-5.4",
]


def _dedupe_preserve_order(seq: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in seq:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _parse_pi_list_models(text: str) -> list[str]:
    """Parse `pi --list-models` table output into `provider/id` strings.

    Output shape (aligned columns, header first):
        provider   model                 context  max-out  thinking  images
        anthropic  claude-sonnet-4-6     200K     64K      yes       yes
    Returns [] when pi reports no authenticated providers."""
    if "No models available" in text:
        return []
    models: list[str] = []
    for i, line in enumerate(text.splitlines()):
        parts = line.split()
        if len(parts) < 2:
            continue
        if i == 0 and parts[0] == "provider" and parts[1] == "model":
            continue
        provider, model_id = parts[0], parts[1]
        models.append(f"{provider}/{model_id}")
    return _dedupe_preserve_order(models)


def fetch_pi_models() -> list[str]:
    """Live model list from the installed `pi` CLI (`--list-models`).

    Returns [] on any failure (CLI missing, no authenticated providers,
    output shape changed, post-parse list too small) so the caller keeps the
    prior cache and falls back to the static PI_MODELS seed."""
    pi_bin = resolve_cli_binary("pi")
    if not pi_bin:
        return []
    try:
        proc = subprocess.run(
            [pi_bin, "--list-models"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    parsed = _parse_pi_list_models(proc.stdout)
    return parsed if len(parsed) >= 3 else []


def _strip_thinking_suffix(model: str) -> str:
    """pi accepts `provider/id:<thinking>`; validation compares the bare id."""
    base, sep, suffix = model.rpartition(":")
    if sep and suffix in PiProvider.reasoning_effort_options:
        return base
    return model


def _model_allowed(model: str, available: list[str]) -> bool:
    """A model is spawnable when it is in the known catalog, or is an
    explicit `provider/id` pair (covers user-defined custom providers in
    pi's ~/.pi/agent/models.json, which no catalog fetch can see)."""
    bare = _strip_thinking_suffix(model)
    return bare in available or "/" in bare


class PiProvider(GeminiProvider):
    """pi coding agent CLI provider. Fork is native (`pi --fork`); rewind is
    simulated the way Gemini/Copilot do it (clear the stored provider session
    id so the next turn starts fresh). No manager mode (no in-process SDK MCP
    registration), no mid-turn steering, no native subagents. Reasoning
    effort maps to pi's `--thinking` levels. Reuses GeminiProvider's
    RunState, tailer bootstrap, completion watcher, and disk recovery — only
    the runner binary, env, and capability surface differ."""

    KIND: ClassVar[str] = "pi"

    supports_fork: ClassVar[bool] = True
    supports_manager_mode: ClassVar[bool] = False
    supports_rewind: ClassVar[bool] = True
    rewind_requires_agent_identity: ClassVar[bool] = False
    supports_steering: ClassVar[bool] = False
    supports_native_subagents: ClassVar[bool] = False
    supports_reasoning_effort: ClassVar[bool] = True
    reasoning_effort_options: ClassVar[tuple[str, ...]] = (
        "off", "minimal", "low", "medium", "high", "xhigh",
    )
    default_reasoning_effort: ClassVar[str] = ""
    # `--no-tools` provably disables every tool for a one-shot run.
    supports_headless_no_tools: ClassVar[bool] = True

    def build_env(self) -> dict[str, str]:
        self.require_runtime_credential()
        env = os.environ.copy()
        # pi reads its own state from ~/.pi and provider API keys from env.
        # Clear Claude-harness env so a concurrently-configured Claude
        # provider can't steer the pi subprocess; provider API-key envs stay
        # because pi legitimately authenticates through them.
        env.pop("CLAUDE_CONFIG_DIR", None)
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
        del supervisor_agent_session_id, mssg_sender_session_id
        del continuation_chain
        if mode == "manager":
            mode = "team"
        if mode not in ("native", "team"):
            raise ValueError(f"mode must be 'native' or 'team', got {mode!r}")
        if self.defunct:
            raise RuntimeError(f"provider {self.id} is defunct; cannot start new runs")
        self.assert_not_suspended(action="start new runs")
        if mode == "team":
            raise NotImplementedError("pi provider does not support team mode.")
        if fork and not self.supports_fork:
            raise NotImplementedError("pi provider does not support fork.")
        if fork and not session_id:
            raise ValueError("fork requires an existing pi session id")
        if reasoning_effort and reasoning_effort not in self.reasoning_effort_options:
            raise ValueError(
                f"reasoning_effort {reasoning_effort!r} is not valid for the pi "
                f"provider. Options: {', '.join(self.reasoning_effort_options)}."
            )

        model = str(model or "").strip()
        available = _dedupe_preserve_order(self.available_models() + PI_MODELS)
        if model and not _model_allowed(model, available):
            raise ValueError(
                f"model {model!r} is not available for the pi provider. "
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
            "reasoning_effort": reasoning_effort or "",
            "permission": _permission,
            "session_id": session_id,
            "fork": bool(fork),
            "supervised": bool(supervised),
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
                runner_argv(run_dir, dev_script=_RUNNER_PATH, kind=self.KIND),
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
            name=f"pi-bootstrap-{run_id[:8]}",
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
        pi_bin = resolve_cli_binary("pi")
        if not pi_bin:
            logger.error("PiProvider.run_headless: `pi` CLI not found")
            return None

        import runner_pi
        cmd: list[str] = [pi_bin, "--mode", "text", "-p"]
        if no_tools:
            cmd += ["--no-tools"]
        resume_target = str(resume_sid or session_id or "").strip()
        if resume_target:
            prior = runner_pi.find_session_file_for_sid(resume_target)
            if prior is None:
                logger.error(
                    "PiProvider.run_headless: session file for %r not found",
                    resume_target,
                )
                return None
            cmd += (["--fork", str(prior)] if fork else ["--session", str(prior)])
        else:
            if fork:
                logger.warning("pi provider ignores fork flag without a resume target")
            # One-shot with no continuation target: don't persist a session.
            cmd += ["--no-session"]

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
            logger.error("PiProvider.run_headless: `pi` CLI not found")
            return None

        try:
            communicate = proc.communicate(input=prompt.encode("utf-8"))
            if timeout:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(communicate, timeout)
            else:
                stdout_bytes, stderr_bytes = await communicate
        except asyncio.TimeoutError:
            logger.error("PiProvider.run_headless: timeout after %ss", timeout)
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            return None

        if proc.returncode != 0:
            logger.error(
                "PiProvider.run_headless: exited %s; stderr=%r",
                proc.returncode, stderr_bytes[:500],
            )
            return None
        return {
            "result": stdout_bytes.decode(errors="replace").strip(),
            "session_id": None,
            "usage": {},
            "total_cost_usd": 0.0,
        }
