"""CopilotProvider — `Provider` implementation for GitHub's Copilot CLI.

Drives the `copilot` binary (a.k.a. `gh copilot`) via a detached
`runner_copilot.py` subprocess per turn. The runner spawns
`copilot -p <prompt> --allow-all-tools`, tails Copilot's own structured
event log at `<config_dir>/session-state/<sessionId>.jsonl`, normalizes
those events to Claude jsonl shape, and writes `session_events.jsonl`.
This provider tails that file and pushes events onto the orchestrator
queue — identical to the SessionEventsProvider path, which CopilotProvider
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
from typing import ClassVar, Optional

from cli_paths import resolve_cli_binary
from provider_session_events import SessionEventsProvider

logger = logging.getLogger(__name__)

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
# displayed position in the picker (after GPT minis, before the Gemini models).
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


class CopilotProvider(SessionEventsProvider):
    """GitHub Copilot CLI provider. Native-mode only: Copilot has no
    non-interactive fork primitive, no in-process SDK MCP registration
    (manager mode), no mid-turn steering, and no reasoning-effort flag.
    Reuses SessionEventsProvider's RunState, tailer bootstrap, completion
    watcher, and disk recovery — only the runner binary and env differ."""

    KIND: ClassVar[str] = "copilot"

    supports_fork: ClassVar[bool] = False
    supports_manager_mode: ClassVar[bool] = False
    # Copilot has no rewind primitive, but we simulate one the way the family
    # does: clear the stored provider session id so the next turn starts
    # a fresh CLI session.
    supports_rewind: ClassVar[bool] = True
    rewind_requires_agent_identity: ClassVar[bool] = False
    supports_steering: ClassVar[bool] = False
    supports_native_subagents: ClassVar[bool] = False
    supports_reasoning_effort: ClassVar[bool] = False

    def build_env(self) -> dict[str, str]:
        self.require_runtime_credential()
        env = os.environ.copy()
        # Copilot reads ~/.copilot by default (overridable via --config-dir
        # in the runner). Clear Claude env so a concurrently-configured
        # Claude provider can't leak into the Copilot subprocess.
        env.pop("CLAUDE_CONFIG_DIR", None)
        env.pop("ANTHROPIC_API_KEY", None)
        env.pop("ANTHROPIC_BASE_URL", None)
        env.pop("ANTHROPIC_AUTH_TOKEN", None)
        env.pop("CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING", None)
        return self.finalize_env(env)

    def _start_run(
        self,
        *,
        loop: asyncio.AbstractEventLoop,
        queue: asyncio.Queue,
        internal_token: Optional[str] = None,
        extra_env: Optional[dict[str, str]] = None,
        _execution,
        **_unused: Any,
    ) -> bool:
        del _unused
        self.start_session_events_execution(
            execution=_execution,
            loop=loop,
            queue=queue,
            internal_token=internal_token,
            extra_env=extra_env,
        )
        return True

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
