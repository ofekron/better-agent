"""QwenProvider — `Provider` implementation for Alibaba's Qwen Code CLI.

Qwen Code carries gemini-cli ancestry: the same run flags (`-o stream-json`,
`--approval-mode plan|default|auto-edit|yolo`, `-m`, `-r/--resume`,
`--include-directories`) but a DIFFERENT stream-json emitter — qwen 0.10+
emits Claude-Code-compatible messages (`system/init`, `assistant`,
`user`, `result` with `message.content` block lists), verified against
the installed @qwen-code/qwen-code bundle's StreamJsonOutputAdapter.

This provider subclasses SessionEventsProvider and reuses its RunState, tailer
bootstrap (`_bootstrap_run`), completion watcher, backend-state writer,
disk recovery (`recover_in_flight` / `attach_recovered_run`), rate-limit
parsing skeleton, and simulated rewind. Only the runner script, env,
auth routing, and model catalog differ. The runner writes Claude-shaped
`session_events.jsonl`, so recovery_family="session_events" replay applies.

Auth (from the CLI source's AUTH_ENV_MAPPINGS):
  - subscription → `--auth-type qwen-oauth` (free tier, device-flow OAuth,
    creds in ~/.qwen/oauth_creds.json; models: coder-model / vision-model)
  - api_key      → `--auth-type openai` (OPENAI_API_KEY / OPENAI_BASE_URL;
    DashScope keys use the OpenAI-compatible endpoint; default model
    qwen3-coder-plus)

Registration this module still needs (files owned elsewhere):
  provider_manifest.SPECS entry (kind="qwen", runner_module="runner_qwen",
  recovery_family="session_events", installable=True, hosts_ui_mcp=True),
  models.py cold-start/refresh dispatch, provider_setup installer
  (`npm install -g @qwen-code/qwen-code`), permission._AXES["qwen"]
  (single-axis {"mode": ...}), and frontend setup template + i18n.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import ClassVar, Optional

from cli_paths import resolve_cli_binary
from provider_session_events import SessionEventsProvider

logger = logging.getLogger(__name__)

# Cold-start model catalog. First two are the qwen-oauth (subscription)
# aliases the CLI's ModelRegistry hardcodes (QWEN_OAUTH_MODELS); the rest
# are the DashScope/OpenAI-compatible ids present in the CLI bundle's
# model-limit table (api_key mode). `coder-model` is the CLI's
# DEFAULT_QWEN_MODEL (currently aliased to qwen3.5-plus, 1M context).
QWEN_MODELS = [
    "coder-model",
    "vision-model",
    "qwen3-coder-plus",
    "qwen3-coder-flash",
    "qwen3.5-plus",
    "qwen3-max",
    "qwen3-vl-plus",
]


# --------------------------------------------------------------------
# Bundle scraper — daily refresh path for the catalog. Qwen ships a
# single esbuild bundle (<pkg>/cli.js).
# The authoritative subscription list is the QWEN_OAUTH_MODELS array;
# api-key ids come from DEFAULT_MODELS plus the qwen3* literals in the
# model-limits table.
# --------------------------------------------------------------------
_QWEN_OAUTH_BLOCK_RE = re.compile(
    r"QWEN_OAUTH_MODELS\s*=\s*\[(.*?)\];", re.DOTALL
)
_QWEN_ID_RE = re.compile(r'id:\s*"([^"]+)"')
_QWEN_LITERAL_RE = re.compile(r'"(qwen3[a-z0-9.\-]*)"')
_QWEN_EXCLUDE_PATTERNS = [
    re.compile(p) for p in [r"embedding", r"-tts(\b|$)", r"-audio", r"-omni"]
]


def _resolve_qwen_bundle() -> Optional[Path]:
    """Locate the installed qwen CLI's bundled cli.js. Returns None when
    the CLI is not on PATH or the resolved entry has no cli.js sibling."""
    qwen = resolve_cli_binary("qwen")
    if not qwen:
        return None
    real = Path(qwen).resolve()
    if real.name == "cli.js":
        return real
    for cand in (real.parent / "cli.js", real.parent.parent / "cli.js"):
        if cand.is_file():
            return cand
    return None


def fetch_qwen_models() -> list[str]:
    """Scrape the installed qwen CLI bundle for its model catalog.

    Returns `[]` on CLI-missing / parse-failure / integrity-check failure
    so the caller keeps the prior cache and the QWEN_MODELS static seed
    covers cold start."""
    bundle = _resolve_qwen_bundle()
    if bundle is None:
        logger.warning("fetch_qwen_models: qwen CLI bundle not found")
        return []
    try:
        text = bundle.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        logger.warning("fetch_qwen_models: cannot read %s", bundle)
        return []

    models: list[str] = []
    seen: set[str] = set()

    def _add(mid: str) -> None:
        mid = mid.strip()
        if not mid or mid in seen:
            return
        if any(p.search(mid) for p in _QWEN_EXCLUDE_PATTERNS):
            return
        seen.add(mid)
        models.append(mid)

    oauth_block = _QWEN_OAUTH_BLOCK_RE.search(text)
    if oauth_block:
        for m in _QWEN_ID_RE.finditer(oauth_block.group(1)):
            _add(m.group(1))
    for m in _QWEN_LITERAL_RE.finditer(text):
        # Skip regex-source fragments (e.g. `qwen3-coder-.`) — real model
        # ids never end with a dot or dash.
        if not m.group(1).endswith((".", "-")):
            _add(m.group(1))

    # Integrity check — guard against a bundle reshape silently nuking
    # the catalog.
    if len(models) < 3:
        logger.warning(
            "fetch_qwen_models: post-filter list has %d entries — "
            "treating as parse failure", len(models),
        )
        return []
    return models


def _dedupe_preserve_order(seq: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in seq:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


class QwenProvider(SessionEventsProvider):
    uses_managed_api_key = True
    """Qwen Code CLI provider. Native-mode only: qwen's CLI has no
    non-interactive fork primitive, no in-process SDK MCP
    registration (manager mode), no mid-turn steering, and no
    reasoning-effort flag. Rewind is simulated (clear stored sid)."""

    KIND: ClassVar[str] = "qwen"

    supports_fork: ClassVar[bool] = False
    supports_manager_mode: ClassVar[bool] = False
    supports_rewind: ClassVar[bool] = True
    rewind_requires_agent_identity: ClassVar[bool] = False
    supports_steering: ClassVar[bool] = False
    supports_native_subagents: ClassVar[bool] = False
    supports_reasoning_effort: ClassVar[bool] = False

    # Extends the inherited keyword set with qwen/DashScope quota
    # phrasing; `parse_rate_limit` is inherited and reads this attribute
    # via `self`, so the override applies without copying the method.
    _RATE_LIMIT_KEYWORDS = (
        SessionEventsProvider._RATE_LIMIT_KEYWORDS
        + ("insufficient_quota", "allocated quota", "throttling.ratequota")
    )

    # ------------------------------------------------------------------
    # Env — clear foreign-provider vars; route api_key-mode credentials
    # through the OPENAI_* vars qwen's `--auth-type openai` reads
    # (AUTH_ENV_MAPPINGS in the CLI source).
    # ------------------------------------------------------------------
    def build_env(self) -> dict[str, str]:
        self.require_runtime_credential()
        env = os.environ.copy()
        for key in (
            "CLAUDE_CONFIG_DIR",
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_BASE_URL",
            "ANTHROPIC_AUTH_TOKEN",
            "CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING",
            "GEMINI_CLI_HOME",
            "GEMINI_API_KEY",
            "GOOGLE_API_KEY",
            "CODEX_HOME",
            "OPENAI_API_KEY",
            "OPENAI_BASE_URL",
            "OPENAI_MODEL",
        ):
            env.pop(key, None)
        rec = self.runtime_record()
        if rec.get("mode") == "api_key":
            api_key = rec.get("api_key")
            base_url = rec.get("base_url")
            if api_key:
                env["OPENAI_API_KEY"] = str(api_key)
            if base_url:
                env["OPENAI_BASE_URL"] = str(base_url)
        return self.finalize_env(env)

    # ------------------------------------------------------------------
    # start_run — copilot-style override of the family template: same
    # run-dir protocol and bootstrap, but qwen's runner script and the
    # provider record mode forwarded for --auth-type routing.
    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    # run_headless — one-shot `qwen -o json`. Qwen's `-o json` prints the
    # Claude-shaped result message ({type:"result", result, session_id,
    # usage, ...}), not a {session_id, response, stats} envelope.
    # ------------------------------------------------------------------
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
        qwen_bin = resolve_cli_binary("qwen")
        if not qwen_bin:
            logger.error("QwenProvider.run_headless: `qwen` CLI not found")
            return None
        from runner_qwen import resolve_auth_type
        cmd: list[str] = [
            qwen_bin,
            "--auth-type", resolve_auth_type(self.record.get("mode", "subscription")),
            "-o", "json",
        ]
        if no_tools:
            # Plan mode = read-only; the model cannot run mutating tools.
            cmd += ["--approval-mode", "plan"]
        resume_target = resume_sid or session_id
        if resume_target:
            cmd += ["-r", resume_target]
        if fork:
            logger.warning("Qwen provider ignores fork flag in run_headless")
        cmd.append(prompt)

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
            logger.error("QwenProvider.run_headless: `qwen` CLI not found")
            return None
        except Exception:
            logger.exception("QwenProvider.run_headless: spawn failed")
            return None

        try:
            kw = {"timeout": timeout} if timeout else {}
            stdout_bytes, stderr_bytes = await proc.communicate(**kw)
        except asyncio.TimeoutError:
            logger.error("QwenProvider.run_headless: timeout after %ss", timeout)
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            return None

        if proc.returncode != 0:
            logger.error(
                "QwenProvider.run_headless: exited %s; stderr=%r",
                proc.returncode, stderr_bytes[:500],
            )
            return None

        stdout = stdout_bytes.decode(errors="replace").strip()
        if not stdout:
            return None
        try:
            raw = json.loads(stdout)
        except json.JSONDecodeError:
            logger.error("QwenProvider.run_headless: not JSON: %r", stdout[:500])
            return None
        if raw.get("is_error"):
            err = raw.get("error") or {}
            logger.error(
                "QwenProvider.run_headless: run failed: %s",
                err.get("message") if isinstance(err, dict) else err,
            )
            return None
        from runner_qwen import usage_from_result
        return {
            "result": raw.get("result") or "",
            "session_id": raw.get("session_id"),
            "usage": usage_from_result(raw),
            "total_cost_usd": 0.0,
            # Pass through provider-native fields for callers that want them.
            "stats": raw.get("stats") or {},
        }
