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
from provider import build_better_agent_run_env, schedule_loop_task, runner_argv
import provider_runtime
from provider_gemini import GeminiProvider, RunState
from provider_run_config import normalize_provider_run_config
from proc_control import process_control as _process_control
from runs_dir import runs_root as _runs_root

logger = logging.getLogger(__name__)

_RUNNER_PATH = Path(__file__).parent / "runner_copilot.py"

# Cold-start models for the GitHub Copilot CLI. `auto` is a first-class
# `--model` value; the remaining IDs mirror the current built-in model catalog
# exposed by `copilot help config` (Copilot CLI 1.0.65, 2026-06). Some IDs may
# still be rejected by a user's subscription tier; the CLI is the final
# entitlement authority. `fetch_copilot_models` re-parses the installed CLI's
# help text so the catalog tracks CLI upgrades without a code change.
COPILOT_MODELS = [
    "auto",
    "claude-sonnet-4.6",
    "claude-sonnet-4.5",
    "claude-haiku-4.5",
    "claude-fable-5",
    "claude-opus-4.8",
    "claude-opus-4.7",
    "claude-opus-4.6",
    "claude-opus-4.6-fast",
    "claude-opus-4.5",
    "gpt-5.5",
    "gpt-5.4",
    "gpt-5.3-codex",
    "gpt-5.4-mini",
    "gpt-5-mini",
    "mai-code-1-flash-picker",
    "gemini-3.1-pro-preview",
    "gemini-3.5-flash",
]

# Current Copilot config help omits this available picker-only model, but the
# interactive picker persists this id and `--model` accepts it. Keep it near its
# displayed position in the picker (after GPT minis, before Gemini).
_COPILOT_PICKER_EXTRA_MODELS_AFTER = {
    "gpt-5-mini": ["mai-code-1-flash-picker"],
}

# Model ids from the old Copilot CLI catalog that are now rejected by current
# CLI releases. Existing provider records/sessions may still carry these (e.g.
# as default_model), so remap them to Copilot's supported automatic routing
# rather than spawning a CLI process guaranteed to fail.
_COPILOT_RETIRED_MODEL_FALLBACKS = {
    "gpt-5.2-codex": "auto",
    "gpt-5.2": "auto",
    "gpt-5.1-codex-max": "auto",
    "gpt-5.1-codex": "auto",
    "gpt-5.1": "auto",
    "gpt-5.1-codex-mini": "auto",
    "gpt-5": "auto",
    "gpt-4.1": "auto",
    "claude-sonnet-4": "auto",
    "gemini-3-pro-preview": "auto",
}


def _dedupe_preserve_order(seq: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in seq:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _filter_copilot_model_ids(models: list[str]) -> list[str]:
    """Keep chat/agent model ids and drop obvious non-chat families the CLI may
    mix into generic help in the future."""
    filtered = [
        m.strip()
        for m in models
        if m.strip() and not m.startswith(("gemma-", "o1-", "text-"))
    ]
    return _dedupe_preserve_order(filtered)


def _copilot_config_model_slug(label: str) -> str:
    """Convert `copilot help config` display labels to `--model` ids."""
    if label == "MAI-Code-1-Flash":
        # The interactive picker stores/uses this internal id; the display
        # label itself is rejected by `--model`.
        return "mai-code-1-flash-picker"
    return label


def _insert_copilot_picker_extras(models: list[str]) -> list[str]:
    out: list[str] = []
    inserted: set[str] = set()
    for model in models:
        out.append(model)
        for extra in _COPILOT_PICKER_EXTRA_MODELS_AFTER.get(model, []):
            out.append(extra)
            inserted.add(extra)
    for extras in _COPILOT_PICKER_EXTRA_MODELS_AFTER.values():
        for extra in extras:
            if extra not in inserted:
                out.append(extra)
    return out


def _parse_copilot_config_models(text: str) -> list[str]:
    """Parse the model bullet list from `copilot help config`.

    Copilot CLI 1.x removed `(choices: ...)` from `--help`; the maintained
    built-in catalog now appears in the config help under the `model` setting:
        `model`: AI model to use ...
          - "claude-sonnet-4.6"
          - "gpt-5.4"
    """
    import re

    models: list[str] = []
    in_model_section = False
    for line in text.splitlines():
        if re.match(r"\s*`model`:\s", line):
            in_model_section = True
            continue
        if in_model_section and re.match(r"\s*`[^`]+`:\s", line):
            break
        if not in_model_section:
            continue
        m = re.match(r'\s*-\s+"([^"]+)"\s*$', line)
        if m:
            models.append(_copilot_config_model_slug(m.group(1)))
    return _filter_copilot_model_ids(
        _insert_copilot_picker_extras(["auto", *models])
    ) if models else []


def _parse_copilot_help_choices(text: str) -> list[str]:
    """Parse the legacy `(choices: ...)` list from `copilot --help`."""
    import re

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
    models = re.findall(r'"([^"]+)"', text[start:end])
    return _filter_copilot_model_ids(["auto", *models]) if models else []


def _normalize_copilot_model(model: Optional[str]) -> str:
    value = str(model or "").strip()
    return _COPILOT_RETIRED_MODEL_FALLBACKS.get(value, value)


def fetch_copilot_models() -> list[str]:
    """Parse the installed `copilot` CLI's model catalog from help output.

    Returns [] on any failure (CLI missing, help shape changed, post-filter list
    too small) so the caller keeps the prior cache and falls back to the static
    COPILOT_MODELS seed.
    """
    copilot_bin = resolve_cli_binary("copilot")
    if not copilot_bin:
        return []

    commands = ([copilot_bin, "help", "config"], [copilot_bin, "--help"])
    parsers = (_parse_copilot_config_models, _parse_copilot_help_choices)
    for cmd, parser in zip(commands, parsers):
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if proc.returncode != 0:
            continue
        parsed = parser(proc.stdout)
        if len(parsed) >= 3:
            return parsed
    return []


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

        model = _normalize_copilot_model(model)
        available = _dedupe_preserve_order(self.available_models() + COPILOT_MODELS)
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
            "source": source or "",
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
            popen = provider_runtime.popen_runner(
                runner_argv(run_dir, dev_script=_RUNNER_PATH, kind="copilot"),
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
