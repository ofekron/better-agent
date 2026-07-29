"""PiProvider — `Provider` implementation for the pi coding agent CLI
(badlogic/pi-mono, npm `@mariozechner/pi-coding-agent`).

Drives the `pi` binary via a detached `runner_pi.py` subprocess per turn.
The runner spawns `pi --mode json -p` (prompt on stdin), normalizes pi's
JSON event stream to Claude jsonl shape, and writes
`<run_dir>/session_events.jsonl`. This provider tails that file and pushes
events onto the orchestrator queue — identical to the SessionEventsProvider path,
which PiProvider subclasses for RunState / bootstrap / tailer / recovery
(session-events recovery family).

Auth: pi reads provider API keys from env vars (ANTHROPIC_API_KEY,
OPENAI_API_KEY, OPENROUTER_API_KEY, …) or OAuth/API-key records the user
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
from typing import ClassVar, Optional

from cli_paths import resolve_cli_binary
from provider_session_events import SessionEventsProvider

logger = logging.getLogger(__name__)

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


class PiProvider(SessionEventsProvider):
    """pi coding agent CLI provider. Fork is native (`pi --fork`); rewind is
    simulated the way Copilot does it (clear the stored provider session
    id so the next turn starts fresh). No manager mode (no in-process SDK MCP
    registration), no mid-turn steering, no native subagents. Reasoning
    effort maps to pi's `--thinking` levels. Reuses SessionEventsProvider's
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
