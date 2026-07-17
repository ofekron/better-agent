"""KimiProvider — `Provider` implementation for Moonshot AI's Kimi CLI.

Drives the `kimi` binary (kimi-cli, a Python package installed via
`uv tool install kimi-cli`) through a detached `runner_kimi.py`
subprocess per turn. The runner spawns
`kimi --print --output-format stream-json --session <sid>`, normalizes
Kimi's stream-json output (kosong `Message` JSON lines) to Claude jsonl
shape, and writes `session_events.jsonl`. This provider tails that file
and pushes events onto the orchestrator queue — identical to the
GeminiProvider path, which KimiProvider subclasses for RunState /
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
import subprocess
import uuid
from pathlib import Path
from typing import ClassVar, Optional

import config_store
from extension_run_policy import disabled_builtin_extensions_for_run
import user_prefs
from cli_paths import resolve_cli_binary
from containment import containment
from provider import build_better_agent_run_env, persist_seed_or_terminate, runner_argv
import provider_runtime
from provider_gemini import GeminiProvider, RunState
from provider_run_config import normalize_provider_run_config
from proc_control import process_control as _process_control
from runs_dir import runs_root as _runs_root

logger = logging.getLogger(__name__)

_RUNNER_PATH = Path(__file__).parent / "runner_kimi.py"

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


class KimiProvider(GeminiProvider):
    """Moonshot Kimi CLI provider. Native-mode only: kimi-cli has no
    non-interactive fork primitive, no in-process SDK MCP registration
    (manager mode), no mid-turn steering, and no reasoning-effort flag
    (`--thinking` is a boolean toggle, not an effort ladder). Print mode
    implicitly runs `--yolo` (kimi-cli auto-approves every action in
    non-interactive mode), so there is no per-tool approval round-trip.
    Reuses GeminiProvider's RunState, tailer bootstrap, completion
    watcher, and disk recovery — only the runner binary and env differ."""

    KIND: ClassVar[str] = "kimi"

    supports_fork: ClassVar[bool] = False
    supports_manager_mode: ClassVar[bool] = False
    # Kimi has no rewind primitive, but we simulate one the way Gemini
    # does: clear the stored provider session id so the next turn starts
    # a fresh CLI session.
    supports_rewind: ClassVar[bool] = True
    rewind_requires_agent_identity: ClassVar[bool] = False
    supports_steering: ClassVar[bool] = False
    supports_native_subagents: ClassVar[bool] = False
    supports_reasoning_effort: ClassVar[bool] = False

    def build_env(self) -> dict[str, str]:
        env = os.environ.copy()
        # Kimi reads ~/.kimi and KIMI_* env vars natively; pass them
        # through. Clear Claude env so a concurrently-configured Claude
        # provider can't leak into the Kimi subprocess.
        env.pop("CLAUDE_CONFIG_DIR", None)
        env.pop("ANTHROPIC_API_KEY", None)
        env.pop("ANTHROPIC_BASE_URL", None)
        env.pop("ANTHROPIC_AUTH_TOKEN", None)
        env.pop("CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING", None)
        return env

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
        if reasoning_effort:
            raise NotImplementedError("kimi provider does not support reasoning effort.")
        if mode == "team":
            raise NotImplementedError("kimi provider does not support team mode.")
        if fork:
            raise NotImplementedError("kimi provider does not support fork.")

        model = str(model or "").strip()
        available = _dedupe_preserve_order(
            self.available_models() + fetch_kimi_models() + KIMI_MODELS
        )
        if model and model not in available:
            raise ValueError(
                f"model {model!r} is not available for the Kimi provider. "
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
            "provisioned_tool_profile": str(provisioned_tool_profile or "").strip(),
            "disabled_builtin_tools": config_store.get_disabled_builtin_tools(),
            "disabled_builtin_extensions": (
                disabled_builtin_extensions_for_run(
                    disabled_builtin_extensions,
                    session_record=session_record,
                    worker_record=worker_record,
                )
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
                interacts_with_user=bool(open_file_panel_enabled) and not bool(session_record.get("bare_config")),
                disabled_builtin_extensions=input_payload["disabled_builtin_extensions"],
            ))
            popen = provider_runtime.popen_runner(
                runner_argv(run_dir, dev_script=_RUNNER_PATH, kind="kimi"),
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
        persist_seed_or_terminate(self._write_backend_state, rs)
        return rs

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
