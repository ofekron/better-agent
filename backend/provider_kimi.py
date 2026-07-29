"""KimiProvider — `Provider` implementation for Moonshot AI's Kimi CLI.

Drives the `kimi` binary (kimi-cli, a Python package installed via
`uv tool install kimi-cli`) through a detached `runner_kimi.py`
subprocess per turn. The runner spawns
`kimi --print --output-format stream-json --session <sid>`, normalizes
Kimi's stream-json output (kosong `Message` JSON lines) to Claude jsonl
shape, and writes `session_events.jsonl`. This provider tails that file
and pushes events onto the orchestrator queue — identical to the
SessionEventsProvider path, which KimiProvider subclasses for RunState /
bootstrap / tailer / completion watcher / disk recovery.

Sessions: `kimi --session <id>` creates the session when the id does not
exist yet (kimi-cli cli/__init__.py), so the runner pre-generates a
uuid4 for fresh turns and the same id resumes later turns.

Auth: Kimi CLI authenticates via the managed Moonshot account configured
in `~/.kimi/config.toml` (interactive `/login` device flow) or via a
`KIMI_API_KEY` / `KIMI_BASE_URL` env override (platform.moonshot.ai API
key). The provider passes the ambient env through untouched and never
logs or persists key material.

Models: `-m/--model` selects a model KEY from the user's
`~/.kimi/config.toml` `[models]` table (the CLI rejects anything else).
`fetch_kimi_models` parses that table, so the catalog always mirrors
what the installed CLI will actually accept; `KIMI_MODELS` is only the
cold-start seed for the managed kimi-code subscription default.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from pathlib import Path
from typing import ClassVar, Optional

from cli_paths import resolve_cli_binary
from provider_session_events import SessionEventsProvider

logger = logging.getLogger(__name__)

# Cold-start model KEYS for the Kimi CLI. `-m` accepts only keys present in
# the user's `~/.kimi/config.toml` [models] table; the managed kimi-code
# login provisions exactly one ("kimi-code/kimi-for-coding", kimi-cli 0.75,
# 2026-07). API-key users add their own keys (conventionally the Moonshot
# platform model ids of the kimi-k2 family). `fetch_kimi_models` re-parses
# the installed config so the catalog tracks the user's real setup.
KIMI_MODELS = [
    "kimi-code/kimi-for-coding",
    "kimi-k2-thinking",
    "kimi-k2-thinking-turbo",
    "kimi-k2-turbo-preview",
    "kimi-k2-0905-preview",
    "kimi-latest",
]


def _dedupe_preserve_order(seq: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in seq:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def kimi_config_file() -> Path:
    """Default kimi-cli config path (kimi_cli.config.get_config_file)."""
    return Path.home() / ".kimi" / "config.toml"


def fetch_kimi_models(config_file: Optional[Path] = None) -> list[str]:
    """Parse the `[models]` table of the kimi-cli config TOML.

    The CLI's `-m` flag only accepts keys of that table (kimi-cli
    config.py validates `default_model in models`), so the config file is
    the authoritative catalog for the installed CLI. Returns the model
    keys with `default_model` first; [] on any failure (missing file,
    malformed TOML, empty table) so the caller keeps the prior cache and
    falls back to the static KIMI_MODELS seed.
    """
    import tomllib

    path = config_file or kimi_config_file()
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError, ValueError):
        return []
    models_table = data.get("models")
    if not isinstance(models_table, dict) or not models_table:
        return []
    keys = [str(k) for k in models_table.keys()]
    default = str(data.get("default_model") or "")
    if default in keys:
        keys.remove(default)
        keys.insert(0, default)
    return _dedupe_preserve_order(keys)


class KimiProvider(SessionEventsProvider):
    """Moonshot Kimi CLI provider. Native-mode only: kimi-cli has no
    non-interactive fork primitive, no in-process SDK MCP registration
    (manager mode), no mid-turn steering, and no reasoning-effort flag
    (`--thinking` is a boolean toggle, not an effort ladder). Print mode
    implicitly runs `--yolo` (kimi-cli auto-approves every action in
    non-interactive mode), so there is no per-tool approval round-trip.
    Reuses SessionEventsProvider's RunState, tailer bootstrap, completion
    watcher, and disk recovery — only the runner binary and env differ."""

    KIND: ClassVar[str] = "kimi"

    supports_fork: ClassVar[bool] = False
    supports_manager_mode: ClassVar[bool] = False
    # Kimi has no rewind primitive, but we simulate one the way the family
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
        # Kimi reads ~/.kimi and KIMI_* env vars natively; pass them
        # through. Clear Claude env so a concurrently-configured Claude
        # provider can't leak into the Kimi subprocess.
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
            # kimi's print mode implicitly runs --yolo with the full tool
            # set; there is no proven disable path — fail closed when the
            # caller demanded a text-only run.
            logger.error("KimiProvider.run_headless: no_tools requested but unsupported")
            return None
        if fork:
            logger.warning("Kimi provider ignores fork flag in run_headless")
        kimi_bin = resolve_cli_binary("kimi")
        if not kimi_bin:
            logger.error("KimiProvider.run_headless: `kimi` CLI not found")
            return None
        # --session with an unknown id creates that session, so a fresh
        # headless run pre-generates its sid and can report it back.
        sid = resume_sid or session_id or str(uuid.uuid4())
        # --quiet = --print --output-format text --final-message-only.
        cmd = [kimi_bin, "--quiet", "--session", sid, "--command", prompt]
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
            logger.error("KimiProvider.run_headless: `kimi` CLI not found")
            return None

        try:
            if timeout:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout
                )
            else:
                stdout_bytes, stderr_bytes = await proc.communicate()
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return None
        if proc.returncode != 0:
            logger.error(
                "KimiProvider.run_headless: exited %s; stderr=%r",
                proc.returncode, stderr_bytes[:500],
            )
            return None
        return {
            "result": stdout_bytes.decode(errors="replace").strip(),
            "session_id": sid,
            "usage": {},
            "total_cost_usd": 0.0,
        }
