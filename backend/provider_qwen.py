"""QwenProvider — `Provider` implementation for Alibaba's Qwen Code CLI.

Qwen Code is a fork of Gemini CLI: identical run flags (`-o stream-json`,
`--approval-mode plan|default|auto-edit|yolo`, `-m`, `-r/--resume`,
`--include-directories`) but a DIFFERENT stream-json emitter — qwen 0.10+
emits Claude-Code-compatible messages (`system/init`, `assistant`,
`user`, `result` with `message.content` block lists), verified against
the installed @qwen-code/qwen-code bundle's StreamJsonOutputAdapter.

This provider subclasses GeminiProvider and reuses its RunState, tailer
bootstrap (`_bootstrap_run`), completion watcher, backend-state writer,
disk recovery (`recover_in_flight` / `attach_recovered_run`), rate-limit
parsing skeleton, and simulated rewind. Only the runner script, env,
auth routing, and model catalog differ. The runner writes Claude-shaped
`session_events.jsonl`, so recovery_family="gemini" replay applies.

Auth (from the CLI source's AUTH_ENV_MAPPINGS):
  - subscription → `--auth-type qwen-oauth` (free tier, device-flow OAuth,
    creds in ~/.qwen/oauth_creds.json; models: coder-model / vision-model)
  - api_key      → `--auth-type openai` (OPENAI_API_KEY / OPENAI_BASE_URL;
    DashScope keys use the OpenAI-compatible endpoint; default model
    qwen3-coder-plus)

Registration this module still needs (files owned elsewhere):
  provider_manifest.SPECS entry (kind="qwen", runner_module="runner_qwen",
  recovery_family="gemini", installable=True, hosts_ui_mcp=True),
  models.py cold-start/refresh dispatch, provider_setup installer
  (`npm install -g @qwen-code/qwen-code`), permission._AXES["qwen"]
  (gemini-style {"mode": ...}), and frontend setup template + i18n.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import ClassVar, Optional

import config_store
import provider_runtime
import user_prefs
from cli_paths import resolve_cli_binary
from containment import containment
from extension_run_policy import (
    disabled_builtin_extensions_for_run,
    disabled_builtin_tools_for_run,
    disabled_runtime_skills_for_run,
)
from proc_control import process_control as _process_control
from provider import build_better_agent_run_env, runner_argv, schedule_loop_task
from provider_gemini import GeminiProvider, RunState
from provider_run_config import normalize_provider_run_config
from runs_dir import runs_root as _runs_root

logger = logging.getLogger(__name__)

_RUNNER_PATH = Path(__file__).parent / "runner_qwen.py"

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
# single esbuild bundle (<pkg>/cli.js, not gemini's chunk-*.js layout).
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
    # the catalog (parity with fetch_gemini_models).
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


class QwenProvider(GeminiProvider):
    uses_managed_api_key = True
    """Qwen Code CLI provider. Native-mode only: like Gemini, qwen's CLI
    has no non-interactive fork primitive, no in-process SDK MCP
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

    # Extends the inherited gemini keyword set with qwen/DashScope quota
    # phrasing; `parse_rate_limit` is inherited and reads this attribute
    # via `self`, so the override applies without copying the method.
    _GEMINI_RATE_LIMIT_KEYWORDS = (
        GeminiProvider._GEMINI_RATE_LIMIT_KEYWORDS
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
        rec = self.record
        if rec.get("mode") == "api_key":
            api_key = rec.get("api_key")
            base_url = rec.get("base_url")
            if api_key:
                env["OPENAI_API_KEY"] = str(api_key)
            if base_url:
                env["OPENAI_BASE_URL"] = str(base_url)
        return self.finalize_env(env)

    # ------------------------------------------------------------------
    # start_run — copilot-style override of the gemini template: same
    # run-dir protocol and bootstrap, but qwen's runner script, no
    # gemini-subscription block (qwen-oauth subscriptions ARE supported),
    # and the provider record mode forwarded for --auth-type routing.
    # ------------------------------------------------------------------
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
        if reasoning_effort:
            raise NotImplementedError("qwen provider does not support reasoning effort.")
        if mode == "team" and not self.supports_manager_mode:
            raise NotImplementedError("qwen provider does not support team mode.")
        if fork and not self.supports_fork:
            raise NotImplementedError("qwen provider does not support fork.")

        available = _dedupe_preserve_order(self.available_models() + QWEN_MODELS)
        if model and model not in available:
            raise ValueError(
                f"model {model!r} is not available for the Qwen provider. "
                f"Available: {', '.join(available)}. "
                f"This session's model was likely set while a different "
                f"provider was active."
            )

        run_dir = _runs_root() / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        from session_manager import manager as _sm
        _sess_rec = _sm.get(app_session_id) or {}
        _worker_sess_rec = _sm.get(worker_agent_session_id) if worker_agent_session_id else {}
        from permission import resolve_for_run as _resolve_perm
        # Qwen shares gemini's single-axis approval-mode vocabulary
        # verbatim (the runner maps auto_edit → auto-edit), so the
        # gemini axis is the canonical permission shape until
        # permission._AXES gains a first-class "qwen" entry.
        _permission = _resolve_perm(
            sess_rec=_sess_rec,
            worker_sess_rec=_worker_sess_rec,
            is_worker=is_worker,
            fallback_kind="gemini",
        )
        input_payload = {
            "prompt": prompt,
            "images": images or [],
            "files": files or [],
            "cwd": cwd,
            "model": model,
            "reasoning_effort": reasoning_effort,
            "permission": _permission,
            "session_id": session_id,
            "mode": mode,
            "source": source or "",
            "app_session_id": app_session_id,
            "provider_id": self.id,
            "provider_mode": self.record.get("mode", "subscription"),
            "backend_url": backend_url or "",
            "internal_token": "",
            "supervised": bool(supervised),
            "worker_agent_session_id": worker_agent_session_id,
            "browser_harness_enabled": bool(browser_harness_enabled),
            "open_file_panel_enabled": bool(open_file_panel_enabled),
            "bare_config": bool(_sess_rec.get("bare_config")),
            "working_mode": _sess_rec.get("working_mode"),
            "worker_working_mode": (_worker_sess_rec or {}).get("working_mode"),
            "context_strategy": user_prefs.get_context_strategy(),
            "provider_run_config": normalize_provider_run_config(provider_run_config),
            "capability_contexts": capability_contexts or [],
            "target_message_id": target_message_id,
            "turn_run_id": turn_run_id,
            "provisioned_tool_profile": str(provisioned_tool_profile or "").strip(),
            "disabled_builtin_tools": disabled_builtin_tools_for_run(
                session_record=_sess_rec, worker_record=_worker_sess_rec,
            ),
            "disabled_runtime_skills": disabled_runtime_skills_for_run(
                session_record=_sess_rec, worker_record=_worker_sess_rec,
            ),
            "disabled_builtin_extensions": (
                disabled_builtin_extensions_for_run(
                    disabled_builtin_extensions,
                    session_record=_sess_rec,
                    worker_record=_worker_sess_rec,
                )
            ),
        }
        (run_dir / "input.json").write_text(json.dumps(input_payload), encoding="utf-8")

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
                run_id=run_id,
                app_session_id=app_session_id,
                cwd=cwd,
                model=model,
                provider_id=self.id,
                bare_config=bool(_sess_rec.get("bare_config")),
                user_facing=bool(open_file_panel_enabled) and not bool(_sess_rec.get("bare_config")),
                disabled_builtin_extensions=input_payload["disabled_builtin_extensions"],
            ))
            popen = provider_runtime.popen_runner(
                runner_argv(run_dir, dev_script=_RUNNER_PATH, kind="qwen"),
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

        logger.info(
            "spawned qwen runner pid=%d mode=%s run_id=%s", popen.pid, mode, run_id,
        )

        rs = RunState(
            run_id=run_id,
            run_dir=run_dir,
            popen=popen,
            mode=mode,
            app_session_id=app_session_id,
            queue=queue,
            started_at=datetime.now().isoformat(),
            persist_to=worker_agent_session_id or app_session_id,
            target_message_id=target_message_id,
            turn_run_id=turn_run_id,
        )
        self._runs[run_id] = rs
        self._write_backend_state(rs)
        schedule_loop_task(
            loop,
            self._bootstrap_run(rs),
            name=f"qwen-bootstrap-{run_id[:8]}",
        )

    # ------------------------------------------------------------------
    # run_headless — one-shot `qwen -o json`. Qwen's `-o json` prints the
    # Claude-shaped result message ({type:"result", result, session_id,
    # usage, ...}), not gemini's {session_id, response, stats} envelope.
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
