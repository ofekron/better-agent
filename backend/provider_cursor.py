"""CursorProvider — `Provider` implementation for Cursor's `cursor-agent` CLI.

Drives the `cursor-agent` binary via a detached `runner_cursor.py`
subprocess per turn. The runner spawns
`cursor-agent --print --output-format stream-json --stream-partial-output`,
normalizes the CLI's stream-json events to Claude jsonl shape, and writes
`session_events.jsonl`. This provider tails that file and pushes events onto
the orchestrator queue — identical to the SessionEventsProvider path, which
CursorProvider subclasses to reuse RunState / bootstrap / tailer /
completion watcher / disk recovery (recovery_family="session_events").

Auth: `cursor-agent login` (browser OAuth) or the CURSOR_API_KEY env var.
Resume: the stream init event's session_id is the chatId accepted by
`cursor-agent --resume <chatId>`.

Permission (fail closed): mode "force" → `-f/--force` (allow commands unless
explicitly denied); any other/unknown mode runs the CLI's default headless
approval behavior. See runner_cursor.CURSOR_PERMISSION_MODES.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
from typing import ClassVar, Optional

from cli_paths import resolve_cli_binary
from provider_session_events import SessionEventsProvider

logger = logging.getLogger(__name__)

# Install command per Cursor's CLI docs (cursor.com/docs/cli/installation):
#   curl https://cursor.com/install -fsS | bash
CURSOR_INSTALL_COMMAND = "curl https://cursor.com/install -fsS | bash"

# Cold-start models for the Cursor CLI. Cursor's catalog is served
# per-account (agent.v1.GetUsableModels — no static list ships in the CLI
# bundle), so this seed is best-effort: "auto" is Cursor's router;
# gpt-5 / sonnet-4 / sonnet-4-thinking are the CLI's own `--model` help
# examples (cursor-agent 2025.11.25); the rest mirror Cursor's published
# model docs. `fetch_cursor_models` (parses `cursor-agent models`) is the
# authority once the CLI is installed and authenticated.
CURSOR_MODELS = [
    "auto",
    "composer-1",
    "sonnet-4.5",
    "sonnet-4.5-thinking",
    "sonnet-4",
    "sonnet-4-thinking",
    "gpt-5",
    "gpt-5-codex",
    "opus-4.1",
]

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\x1b\].*?(?:\x07|\x1b\\)")
_MODEL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._\-]*$")
_MODEL_LINE_NOISE = ("checking", "error", "usage", "available", "model")


def _dedupe_preserve_order(seq: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in seq:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def parse_cursor_models_output(text: str) -> list[str]:
    """Parse model ids out of `cursor-agent models` output.

    Tolerant of decoration: strips ANSI sequences, bullet/selection markers,
    and annotation suffixes; keeps only tokens shaped like model ids.
    Returns [] when fewer than 2 ids parse (treated as failure so callers
    keep the prior cache / static seed)."""
    models: list[str] = []
    for raw_line in _ANSI_RE.sub("", text).splitlines():
        line = raw_line.strip().lstrip("•-*>✓ \t").strip()
        if not line:
            continue
        token = line.split()[0].strip().rstrip(",")
        lowered = token.lower()
        if any(lowered.startswith(noise) for noise in _MODEL_LINE_NOISE):
            continue
        if _MODEL_ID_RE.match(token):
            models.append(token)
    deduped = _dedupe_preserve_order(models)
    return deduped if len(deduped) >= 2 else []


def fetch_cursor_models() -> list[str]:
    """List models from the installed `cursor-agent` CLI.

    Returns [] on any failure (CLI missing, not authenticated, output shape
    unparseable) so the caller keeps the prior cache and falls back to the
    static CURSOR_MODELS seed."""
    cursor_bin = resolve_cli_binary("cursor-agent")
    if not cursor_bin:
        return []
    try:
        proc = subprocess.run(
            [cursor_bin, "models"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    return parse_cursor_models_output(proc.stdout)


class CursorProvider(SessionEventsProvider):
    """Cursor CLI provider. Native-mode only: cursor-agent has no
    non-interactive fork primitive, no in-process SDK MCP registration
    (manager mode), no mid-turn steering, and no reasoning-effort flag.
    Reuses SessionEventsProvider's RunState, tailer bootstrap, completion watcher,
    and disk recovery — only the runner binary, env, and permission model
    differ."""

    KIND: ClassVar[str] = "cursor"

    supports_fork: ClassVar[bool] = False
    supports_manager_mode: ClassVar[bool] = False
    # No CLI rewind primitive; simulated — clear the stored
    # provider session id so the next turn starts a fresh chat.
    supports_rewind: ClassVar[bool] = True
    rewind_requires_agent_identity: ClassVar[bool] = False
    supports_steering: ClassVar[bool] = False
    supports_native_subagents: ClassVar[bool] = False
    supports_reasoning_effort: ClassVar[bool] = False

    def build_env(self) -> dict[str, str]:
        self.require_runtime_credential()
        env = os.environ.copy()
        # cursor-agent authenticates via its own login store or
        # CURSOR_API_KEY (passed through untouched). Clear Claude env so a
        # concurrently-configured Claude provider can't leak into the
        # subprocess.
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
            # cursor-agent's --print mode "has access to all tools, including
            # write and bash" with no proven disable path — fail closed when
            # the caller demanded a text-only run.
            logger.error("CursorProvider.run_headless: no_tools requested but unsupported")
            return None
        if fork:
            logger.warning("Cursor provider ignores fork flag in run_headless")
        cursor_bin = resolve_cli_binary("cursor-agent")
        if not cursor_bin:
            logger.error("CursorProvider.run_headless: `cursor-agent` CLI not found")
            return None
        cmd = [cursor_bin, "--print", "--output-format", "json"]
        resume_target = resume_sid or session_id
        if resume_target:
            cmd += ["--resume", resume_target]
        cmd += ["--", prompt]
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
            logger.error("CursorProvider.run_headless: `cursor-agent` CLI not found")
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
                "CursorProvider.run_headless: exited %s; stderr=%r",
                proc.returncode, stderr_bytes[:500],
            )
            return None
        stdout = stdout_bytes.decode(errors="replace").strip()
        result_text = stdout
        result_sid: Optional[str] = None
        # `--output-format json` emits one result object:
        # {"type":"result","subtype":"success","is_error":...,"result":...,
        #  "session_id":...} (verified from the CLI bundle emitter).
        try:
            raw = json.loads(stdout)
        except json.JSONDecodeError:
            raw = None
        if isinstance(raw, dict):
            if raw.get("is_error") or raw.get("subtype") != "success":
                logger.error("CursorProvider.run_headless: result error: %r", raw)
                return None
            result_text = str(raw.get("result") or "")
            sid = raw.get("session_id")
            result_sid = sid if isinstance(sid, str) and sid else None
        return {
            "result": result_text,
            "session_id": result_sid,
            "usage": {},
            "total_cost_usd": 0.0,
        }
