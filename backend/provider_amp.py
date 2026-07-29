"""AmpProvider — `Provider` implementation for Sourcegraph's Amp CLI.

Drives the `amp` binary via a detached `runner_amp.py` subprocess per
turn. The runner spawns `amp -x --stream-json` (or `amp threads
continue <threadId> -x --stream-json` to resume), parses Amp's
Claude-Code-compatible stream-json from stdout, normalizes it to
Claude jsonl shape, and writes `session_events.jsonl`. This provider
tails that file and pushes events onto the orchestrator queue —
identical to the SessionEventsProvider path, which AmpProvider subclasses to
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
from typing import ClassVar, Optional

from cli_paths import resolve_cli_binary
from provider_session_events import SessionEventsProvider

logger = logging.getLogger(__name__)

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


class AmpProvider(SessionEventsProvider):
    uses_managed_api_key = True
    """Sourcegraph Amp CLI provider. Fork is supported natively
    (`amp threads fork`); everything else is native-mode only: no
    in-process SDK MCP registration (manager mode), no mid-turn
    steering, no reasoning-effort flag. Amp DOES run its own internal
    subagents (Task tool), but not through Better Agent's native
    subagent integration. Reuses SessionEventsProvider's RunState, tailer
    bootstrap, completion watcher, and disk recovery — only the runner
    binary and env differ."""

    KIND: ClassVar[str] = "amp"

    supports_fork: ClassVar[bool] = True
    supports_manager_mode: ClassVar[bool] = False
    # Amp has no rewind primitive; simulate like Copilot by
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
        record = self.runtime_record()
        api_key = str(record.get("api_key") or "").strip()
        if api_key:
            env["AMP_API_KEY"] = api_key
        base_url = str(record.get("base_url") or "").strip()
        if base_url:
            env["AMP_URL"] = base_url
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
