"""OpencodeProvider — `Provider` implementation for the OpenCode CLI.

Drives the `opencode` binary (npm `opencode-ai`) via a detached
`runner_opencode.py` subprocess per turn. The runner spawns
`opencode run --format json` with the prompt on stdin, normalizes the
raw JSON events streamed on stdout to Claude jsonl shape, and writes
`session_events.jsonl`. This provider tails that file and pushes events
onto the orchestrator queue — identical to the SessionEventsProvider path,
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
from typing import ClassVar, Optional

from cli_paths import resolve_cli_binary
from provider_session_events import SessionEventsProvider

logger = logging.getLogger(__name__)

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


class OpencodeProvider(SessionEventsProvider):
    """OpenCode CLI provider. Native-mode only (no in-process SDK MCP
    registration → no manager mode, no mid-turn steering), but with a
    real non-interactive fork primitive (`--fork`) and per-run reasoning
    effort via `--variant`. Reuses SessionEventsProvider's RunState, tailer
    bootstrap, completion watcher, and disk recovery — only the runner
    binary, env, and capability surface differ."""

    KIND: ClassVar[str] = "opencode"

    # `opencode run -s <sid> --fork` forks the session before continuing —
    # a real headless fork primitive (verified: fork run reports a new
    # sessionID while the source session is left intact).
    supports_fork: ClassVar[bool] = True
    supports_manager_mode: ClassVar[bool] = False
    # No rewind primitive; simulated the family way — clear the stored
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
