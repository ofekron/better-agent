"""GrokProvider — `Provider` implementation for xAI's Grok Build CLI.

Drives the `grok` binary (installed from https://x.ai/cli/install.sh /
install.ps1; ships as `xai-grok-pager` internally) through a detached
`runner_grok.py` subprocess per turn. The runner spawns
`grok -p ... --output-format streaming-json`, normalizes grok's
text/thought/end/error event stream to Claude jsonl shape, and writes
`session_events.jsonl`. This provider tails that file and pushes events
onto the orchestrator queue — identical to the GeminiProvider path,
which GrokProvider subclasses for RunState / bootstrap / tailer /
completion watcher / disk recovery.

Wire format note: grok's headless streaming-json surfaces ONLY
`text`/`thought` deltas plus `end`/`error` (verified against
crates/codegen/xai-grok-pager/src/headless.rs in the grok-build repo) —
no `tool_use` event is emitted, so tool calls are invisible to this
integration; only the agent's running commentary/answer streams through.

Sessions: grok separates create (`-s <uuid>`, must NOT already exist)
from resume (`-r <id>`, must exist) — unlike kimi's single `--session`
upsert flag. A fresh turn (no incoming session_id) pre-generates a
uuid4 and passes `-s` so the id is known before the CLI even starts
(mirrors kimi's immediate-persist pattern); a follow-up turn passes
`-r <session_id>`; fork passes `-r <session_id> --fork-session` and the
NEW forked id is discovered from the `end` event's `sessionId`. `-c`
(continue most recent session in cwd) is intentionally never used —
Better Agent always tracks its own explicit session id, so relying on
the CLI's own "most recent in cwd" heuristic would be ambiguous under
concurrent turns.

Auth: `XAI_API_KEY` (api_key mode) or the CLI's own `grok login` /
device-code OAuth cache under `GROK_HOME` (default `~/.grok`,
subscription mode — ambient, not per-account env-selectable, so
`credential_config_env=None` on the manifest spec, consistent with how
kimi/qwen route a single shared login through ambient env/config-dir
rather than a spawn-time-selected directory).

Permission: always `--yolo` (unattended automation), matching every
other Better Agent runner. Grok's CLI does expose finer-grained
`--permission-mode` / `--allow` / `--deny` rule syntax, but Better
Agent has no per-tool allow/deny UI for any provider yet, so this
mirrors kimi's implicit-yolo model rather than adding a bespoke rule
surface for one provider.

Registration this module still needs (files owned elsewhere):
  provider_manifest.SPECS entry (kind="grok", runner_module="runner_grok",
  recovery_family="gemini", installable=True, hosts_ui_mcp=True),
  models.py cold-start/refresh dispatch, provider_setup installer
  (curl|bash / irm|iex), runner_errors auth/session rules, and frontend
  setup template + i18n.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import ClassVar, Optional

import config_store
import provider_runtime
import user_prefs
from cli_paths import resolve_cli_binary
from containment import containment
from extension_run_policy import disabled_builtin_extensions_for_run
from proc_control import process_control as _process_control
from provider import build_better_agent_run_env, persist_seed_or_terminate, runner_argv
from provider_gemini import GeminiProvider, RunState
from provider_run_config import normalize_provider_run_config
from runs_dir import runs_root as _runs_root

logger = logging.getLogger(__name__)

_RUNNER_PATH = Path(__file__).parent / "runner_grok.py"

# Cold-start model seed. Grok has no compiled-in model catalog (the CLI's
# `grok models` subcommand queries the server), so this is only the
# flagship model documented in the CLI's own --help/docs.
GROK_MODELS = ["grok-build"]


def _dedupe_preserve_order(seq: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in seq:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def fetch_grok_models() -> list[str]:
    """Query the installed grok CLI's `grok models` subcommand for the
    live catalog (prints `  * <id> (default)` / `  - <id>` lines after an
    auth-status banner). Requires the CLI to be authenticated; returns []
    on any failure (CLI missing, timeout, non-zero exit, empty parse) so
    the caller keeps the prior cache and GROK_MODELS covers cold start."""
    grok_bin = resolve_cli_binary("grok")
    if not grok_bin:
        return []
    try:
        proc = subprocess.run(
            [grok_bin, "models"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    out: list[str] = []
    for line in proc.stdout.splitlines():
        stripped = line.strip()
        if not (stripped.startswith("-") or stripped.startswith("*")):
            continue
        token = stripped.lstrip("*-").strip()
        if token.endswith("(default)"):
            token = token[: -len("(default)")].strip()
        if token and token not in out:
            out.append(token)
    return out


class GrokProvider(GeminiProvider):
    """xAI Grok Build CLI provider. Native-mode only: grok has no
    in-process SDK MCP registration (manager mode) and no mid-turn
    steering. Fork is real (`--fork-session` with `-r`), reasoning
    effort is real (`--reasoning-effort`), rewind is simulated (clear
    the stored provider session id) the way Gemini/Kimi/Qwen do."""

    KIND: ClassVar[str] = "grok"

    supports_fork: ClassVar[bool] = True
    supports_manager_mode: ClassVar[bool] = False
    supports_rewind: ClassVar[bool] = True
    rewind_requires_agent_identity: ClassVar[bool] = False
    supports_steering: ClassVar[bool] = False
    supports_native_subagents: ClassVar[bool] = False
    supports_reasoning_effort: ClassVar[bool] = True
    # Canonical levels from `--reasoning-effort`/`--effort` (`max` is an
    # alias of `xhigh` in the CLI; both are exposed so either round-trips).
    reasoning_effort_options: ClassVar[tuple[str, ...]] = (
        "none", "minimal", "low", "medium", "high", "xhigh", "max",
    )
    default_reasoning_effort: ClassVar[str] = "medium"

    # ------------------------------------------------------------------
    # Env — clear foreign-provider vars; route api_key-mode credentials
    # through XAI_API_KEY (the CLI's headless auth env, takes precedence
    # over `grok login` cached creds). GROK_DISABLE_AUTOUPDATER pairs
    # with the runner's `--no-auto-update` flag.
    # ------------------------------------------------------------------
    def build_env(self) -> dict[str, str]:
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
        env["GROK_DISABLE_AUTOUPDATER"] = "1"
        rec = self.record
        if rec.get("mode") == "api_key":
            api_key = rec.get("api_key")
            if api_key:
                env["XAI_API_KEY"] = str(api_key)
        return env

    # ------------------------------------------------------------------
    # start_run — same run-dir protocol/bootstrap as the rest of the
    # gemini family; only the runner script, env, and grok-specific
    # session/fork fields differ.
    # ------------------------------------------------------------------
    def _spawn_run(
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
        del supervised, supervisor_agent_session_id, mssg_sender_session_id, is_worker
        del continuation_chain
        if mode == "manager":
            mode = "team"
        if mode not in ("native", "team"):
            raise ValueError(f"mode must be 'native' or 'team', got {mode!r}")
        if self.defunct:
            raise RuntimeError(f"provider {self.id} is defunct; cannot start new runs")
        self.assert_not_suspended(action="start new runs")
        if mode == "team" and not self.supports_manager_mode:
            raise NotImplementedError("grok provider does not support team mode.")
        if fork and not session_id:
            raise ValueError("grok fork requires an existing session id to fork from")
        if reasoning_effort and reasoning_effort not in self.reasoning_effort_options:
            raise ValueError(
                f"reasoning_effort {reasoning_effort!r} is not supported by the "
                f"Grok provider. Available: {', '.join(self.reasoning_effort_options)}."
            )

        # NEVER inline-fetch here (models.py contract: fetch_grok_models is a
        # subprocess+network call, owned by the daily background refresher).
        # Validate against the cached catalog + static seed only, same as
        # qwen (whose fetch is likewise excluded from the spawn-time check).
        available = _dedupe_preserve_order(self.available_models() + GROK_MODELS)
        if model and model not in available:
            raise ValueError(
                f"model {model!r} is not available for the Grok provider. "
                f"Available: {', '.join(available)}. "
                f"This session's model was likely set while a different "
                f"provider was active."
            )

        run_dir = _runs_root() / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        from session_manager import manager as _sm
        _sess_rec = _sm.get(app_session_id) or {}
        _worker_sess_rec = _sm.get(worker_agent_session_id) if worker_agent_session_id else {}
        input_payload = {
            "prompt": prompt,
            "images": images or [],
            "files": files or [],
            "cwd": cwd,
            "model": model,
            "reasoning_effort": reasoning_effort,
            "session_id": session_id,
            "fork": bool(fork),
            "mode": mode,
            "source": source or "",
            "app_session_id": app_session_id,
            "provider_id": self.id,
            "provider_mode": self.record.get("mode", "subscription"),
            "backend_url": backend_url or "",
            "internal_token": internal_token or "",
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
            "disabled_builtin_tools": config_store.get_disabled_builtin_tools(),
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
                app_session_id=app_session_id,
                cwd=cwd,
                model=model,
                provider_id=self.id,
                bare_config=bool(_sess_rec.get("bare_config")),
                user_facing=bool(open_file_panel_enabled) and not bool(_sess_rec.get("bare_config")),
                disabled_builtin_extensions=input_payload["disabled_builtin_extensions"],
            ))
            popen = provider_runtime.popen_runner(
                runner_argv(run_dir, dev_script=_RUNNER_PATH, kind="grok"),
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
            "spawned grok runner pid=%d mode=%s run_id=%s", popen.pid, mode, run_id,
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
        persist_seed_or_terminate(self._write_backend_state, rs)
        return rs

    # ------------------------------------------------------------------
    # run_headless — one-shot `grok -p ... --output-format json`.
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
        grok_bin = resolve_cli_binary("grok")
        if not grok_bin:
            logger.error("GrokProvider.run_headless: `grok` CLI not found")
            return None
        if no_tools:
            # No documented read-only/plan mode for grok headless — fail
            # closed when the caller demanded a text-only run.
            logger.error("GrokProvider.run_headless: no_tools requested but unsupported")
            return None
        cmd: list[str] = [grok_bin, "-p", prompt, "--output-format", "json", "--yolo", "--no-auto-update"]
        resume_target = resume_sid or session_id
        if resume_target:
            cmd += ["-r", resume_target]
            if fork:
                cmd += ["--fork-session"]
        elif fork:
            logger.warning("Grok provider ignores fork flag in run_headless without a session id")

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
            logger.error("GrokProvider.run_headless: `grok` CLI not found")
            return None
        except Exception:
            logger.exception("GrokProvider.run_headless: spawn failed")
            return None

        try:
            kw = {"timeout": timeout} if timeout else {}
            stdout_bytes, stderr_bytes = await proc.communicate(**kw)
        except asyncio.TimeoutError:
            logger.error("GrokProvider.run_headless: timeout after %ss", timeout)
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            return None

        if proc.returncode != 0:
            logger.error(
                "GrokProvider.run_headless: exited %s; stderr=%r",
                proc.returncode, stderr_bytes[:500],
            )
            return None

        stdout = stdout_bytes.decode(errors="replace").strip()
        if not stdout:
            return None
        try:
            raw = json.loads(stdout)
        except json.JSONDecodeError:
            logger.error("GrokProvider.run_headless: not JSON: %r", stdout[:500])
            return None
        if raw.get("type") == "error":
            logger.error("GrokProvider.run_headless: run failed: %s", raw.get("message"))
            return None
        from runner_grok import usage_from_grok_event
        return {
            "result": raw.get("text") or "",
            "session_id": raw.get("sessionId"),
            "usage": usage_from_grok_event(raw),
            "total_cost_usd": raw.get("total_cost_usd") or 0.0,
        }
