from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar, Optional

from extension_run_policy import (
    resolve_extension_run_policy,
)
import user_prefs
from cli_paths import resolve_cli_binary
from containment import containment
from env_compat import get_env
from execution_artifact_io import bind_execution_input
from provider import build_better_agent_run_env, schedule_loop_task
from provider_family_execution_runtime import (
    cleanup_failed_family_execution,
    install_family_execution_payload,
    prepare_family_execution,
    release_staged_family_execution,
    resolve_family_execution_payload,
)
from provider_family_launch_attestation import (
    FamilyLaunchAttestation,
    capture_cli_launch,
    capture_config_scope,
    capture_runner_launch,
)
from provider_runtime_plan_source import (
    hydrate_frozen_provider_runtime_plan,
    selected_runtime_skill_sources,
    structural_provider_runtime_plan,
)
from provider_family_runtime_capabilities import (
    snapshot_family_runtime_capabilities,
)
from mcp_prewarm.preparation import prepare_runtime_mcp_prewarm
from provider_session_events import SessionEventsProvider, RunState
from proc_control import process_control as _process_control
from runs_dir import runs_root as _runs_root
from paths import resolve_provider_config_dir

logger = logging.getLogger(__name__)

_RUNNER_PATH = Path(__file__).parent / "runner_agy.py"

AGY_MODELS = [
    "Gemini 3.5 Flash (Medium)",
    "Gemini 3.5 Flash (High)",
    "Gemini 3.5 Flash (Low)",
    "Gemini 3.1 Pro (Low)",
    "Gemini 3.1 Pro (High)",
    "Claude Sonnet 4.6 (Thinking)",
    "Claude Opus 4.6 (Thinking)",
    "GPT-OSS 120B (Medium)",
]

RUN_CONFIG_SESSION_FIELDS = (
    "active_capability_ids",
    "bare_config",
    "disabled_builtin_extensions",
    "disabled_builtin_tools",
    "disabled_runtime_skills",
    "extra_mcp_servers",
    "harness_profile_id",
    "orchestration_mode",
    "permission",
    "provider_id",
    "working_mode",
)


def fetch_agy_models() -> list[str]:
    agy_bin = resolve_cli_binary("agy")
    if not agy_bin:
        return list(AGY_MODELS)
    try:
        proc = subprocess.run(
            [agy_bin, "models"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return list(AGY_MODELS)
    if proc.returncode != 0:
        return list(AGY_MODELS)
    out: list[str] = []
    for line in proc.stdout.splitlines():
        token = line.strip().lstrip("*-•").strip()
        if token and token not in out and not token.startswith("-"):
            out.append(token)
    return out if out else list(AGY_MODELS)


class AgyProvider(SessionEventsProvider):
    KIND: ClassVar[str] = "agy"

    supports_fork: ClassVar[bool] = False
    supports_manager_mode: ClassVar[bool] = True
    supports_rewind: ClassVar[bool] = False
    rewind_requires_agent_identity: ClassVar[bool] = True
    supports_semantic_alter: ClassVar[bool] = True
    supports_steering: ClassVar[bool] = False
    supports_native_subagents: ClassVar[bool] = True
    supports_reasoning_effort: ClassVar[bool] = False

    def build_env(self) -> dict[str, str]:
        self.require_runtime_credential()
        env = os.environ.copy()
        env.pop("CLAUDE_CONFIG_DIR", None)
        env.pop("ANTHROPIC_API_KEY", None)
        env.pop("ANTHROPIC_BASE_URL", None)
        env.pop("ANTHROPIC_AUTH_TOKEN", None)
        env.pop("CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING", None)
        return self.finalize_env(env)

    def prepare_run(self, **start_arguments: Any):
        runner_input = self._build_runner_input(start_arguments)
        run_id = start_arguments["run_id"]
        run_dir = _runs_root() / run_id
        agy_binary = resolve_cli_binary("agy")
        if not agy_binary:
            raise RuntimeError("agy CLI not found on PATH")
        authority = self.execution_authority_record(start_arguments)
        config_root = resolve_provider_config_dir(
            str(authority.get("config_dir") or ".gemini/antigravity-cli"),
        )
        config_root.mkdir(parents=True, exist_ok=True)
        resume_path = (
            config_root
            / "conversations"
            / f"{start_arguments['session_id']}.db"
            if start_arguments.get("session_id")
            else None
        )
        if resume_path is not None and not resume_path.is_file():
            resume_path = None
        config_scope = capture_config_scope(
            root_path=config_root,
            config_paths=(config_root / "settings.json",),
            resume_path=resume_path,
        )
        settings_path = config_root / "settings.json"
        if settings_path.is_file():
            try:
                agy_settings = json.loads(
                    settings_path.read_text(encoding="utf-8"),
                )
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError("AGY settings are unreadable") from exc
            if type(agy_settings) is not dict:
                raise RuntimeError("AGY settings must contain an object")
        else:
            agy_settings = {}
        launch = FamilyLaunchAttestation.capture(
            family=self.KIND,
            runner=capture_runner_launch(
                run_dir=run_dir,
                executable_path=sys.executable,
                runner_entry=_RUNNER_PATH,
                runner_kind=self.KIND,
                runner_module="runner_agy",
                frozen=bool(getattr(sys, "frozen", False)),
            ),
            downstream=capture_cli_launch(
                logical_command=self.KIND,
                launcher_path=agy_binary,
                search_path=os.environ.get("PATH"),
                platform=sys.platform,
                command_processor=os.environ.get("COMSPEC"),
            ),
            config=config_scope,
        )
        ok, attest_reason = launch.attest_with_reason()
        if not ok:
            logger.error("AGY launch authority attestation failed during preparation: reason=%s", attest_reason)
            raise RuntimeError(f"AGY launch authority changed during preparation (reason: {attest_reason})")

        prewarm = prepare_runtime_mcp_prewarm(
            runner_input,
            str(start_arguments["app_session_id"]),
            bound_seconds=8.0,
        )
        runner_input["_mcp_prewarm_ready"] = prewarm.ready_map
        projection = structural_provider_runtime_plan(
            runner_input,
            self.KIND,
        )
        capabilities = snapshot_family_runtime_capabilities(
            family=self.KIND,
            skill_sources=selected_runtime_skill_sources(
                runner_input["cwd"],
                bool(runner_input["bare_config"]),
                runner_input["disabled_runtime_skills"],
            ),
            agent_sources={},
            resolved_plan=projection["resolved_plan"],
            extension_state=projection["extension_state"],
            installation_decisions=projection["installation_decisions"],
            prewarm_results=prewarm.status,
        )
        runner_input["agy_config_root"] = str(config_root)
        runner_input["agy_settings"] = agy_settings
        return prepare_family_execution(
            authority,
            start_arguments=start_arguments,
            runner_input=runner_input,
            launch=launch,
            capabilities=capabilities,
        )

    def _build_runner_input(
        self,
        start_arguments: dict[str, Any],
    ) -> dict[str, Any]:
        mode = start_arguments["mode"]
        if mode == "manager":
            mode = "team"
        if mode not in ("native", "team"):
            raise ValueError(f"mode must be 'native' or 'team', got {mode!r}")
        if start_arguments.get("reasoning_effort"):
            raise NotImplementedError(
                "agy provider does not support reasoning effort.",
            )
        if mode == "team" and not self.supports_manager_mode:
            raise NotImplementedError("agy provider does not support team mode.")
        if start_arguments.get("fork"):
            raise NotImplementedError("agy provider does not support fork.")
        model = start_arguments.get("model")
        import models as models_mod

        available = models_mod.available_models(self.id)
        if model and model not in available:
            raise ValueError(
                f"model {model!r} is not available for the AGY provider. "
                f"Available: {', '.join(available)}."
            )

        from session_manager import manager as session_manager

        app_session_id = start_arguments["app_session_id"]
        worker_session_id = start_arguments.get("worker_agent_session_id")
        session_record = session_manager.get_fields(
            app_session_id,
            RUN_CONFIG_SESSION_FIELDS,
        )
        worker_record = (
            session_manager.get_fields(
                worker_session_id,
                RUN_CONFIG_SESSION_FIELDS,
            )
            if worker_session_id
            else {}
        )
        from permission import resolve_for_run

        permission = resolve_for_run(
            sess_rec=session_record,
            worker_sess_rec=worker_record,
            is_worker=bool(worker_session_id),
            fallback_kind=self.KIND,
        )
        run_policy = resolve_extension_run_policy(
            resolved_harness_run_config=start_arguments.get(
                "resolved_harness_run_config",
            ),
            session_record=session_record,
            worker_record=worker_record,
            provider_kind=self.KIND,
            provider_run_config=start_arguments.get("provider_run_config"),
            capability_contexts=start_arguments.get("capability_contexts"),
            disabled_builtin_extensions=start_arguments.get(
                "disabled_builtin_extensions",
            ),
        )
        bare = bool(run_policy["bare_config"])
        backend_url = (
            start_arguments.get("backend_url")
            or get_env("BETTER_CLAUDE_BACKEND_URL")
            or "http://localhost:8000"
        )
        import extension_store
        import installation_profile

        integrations_enabled = installation_profile.integrations_enabled()
        payload = {
            key: value
            for key, value in start_arguments.items()
            if key not in {"extra_env", "internal_token"}
        }
        payload.update({
            "backend_url": backend_url,
            "internal_token": "",
            # See provider_claude.py's matching flag: `internal_token` is
            # always blank here (the runner subprocess mints the real one
            # moments later), so without this, `requires_backend_auth`
            # extension MCP servers outside extension_store's
            # `_BROKERED_MCP_EXTENSION_IDS` allowlist are silently excluded
            # from the frozen plan regardless of harness-profile selection.
            "extension_mcp_launcher_context": True,
            "mode": mode,
            "provider_id": self.id,
            "provider_kind": self.KIND,
            "permission": permission,
            "bare_config": bare,
            "working_mode": session_record.get("working_mode"),
            "worker_working_mode": worker_record.get("working_mode"),
            "context_strategy": user_prefs.get_context_strategy(),
            "team_orchestration_enabled": (
                integrations_enabled
                and extension_store.is_extension_runtime_ready(
                    extension_store.extension_id_for_role("team-orchestration"),
                )
            ),
            "coordination_enabled": (
                integrations_enabled
                and extension_store.is_extension_runtime_ready(
                    extension_store.BUILTIN_COORDINATION_EXTENSION_ID,
                )
            ),
        })
        payload.update(run_policy)
        return payload

    def _install_execution_payloads(self, execution, run_dir: Path) -> None:
        install_family_execution_payload(execution, run_dir)

    def _cleanup_failed_execution_payloads(
        self,
        execution,
        run_dir: Path,
    ) -> None:
        cleanup_failed_family_execution(execution, run_dir)

    def _release_execution_authority(self, execution) -> None:
        release_staged_family_execution(execution)

    def _start_run(
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
        user_facing: bool = False,
        working_mode: Optional[str] = None,
        extra_env: Optional[dict[str, str]] = None,
        continuation_chain: Optional[list[str]] = None,
        provider_run_config: Optional[dict] = None,
        capability_contexts: Optional[list[dict]] = None,
        target_message_id: Optional[str] = None,
        resolved_harness_run_config: Optional[dict] = None,
        turn_run_id: Optional[str] = None,
        disabled_builtin_extensions: Optional[list[str]] = None,
        provisioned_tool_profile: str = "",
        _execution,
    ) -> bool:
        del disallowed_tools, setting_sources
        del supervised, supervisor_agent_session_id, mssg_sender_session_id, is_worker
        del continuation_chain
        run_dir = _runs_root() / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        launch, capabilities = resolve_family_execution_payload(
            _execution.artifact,
            run_dir,
        )
        input_payload = _execution.artifact.runtime_policy.get("runner_input")
        if type(input_payload) is not dict:
            raise RuntimeError("frozen AGY runner input is unavailable")
        hydrated_plan = hydrate_frozen_provider_runtime_plan(
            capabilities.plan,
        )
        runtime_hydration = {
            "capability_plan": hydrated_plan,
            "prewarm_status": capabilities.prewarm_status,
            "skill_dirs": {
                name: str(path)
                for name, path in capabilities.skill_dirs.items()
            },
        }
        from runs_dir import atomic_write_json

        atomic_write_json(
            run_dir / "input.json",
            bind_execution_input(_execution.artifact, input_payload),
        )
        from execution_spawn_authority import consume_execution_spawn_authority

        consume_execution_spawn_authority(_execution.artifact, run_dir)

        containment().create(run_id)
        stdout_fp = (run_dir / "stdout.log").open("ab")
        stderr_fp = (run_dir / "stderr.log").open("ab")
        try:
            with launch.open_runner() as pinned:
                # Env is built after runner materialization: the runtime
                # bootstrap issued inside build_better_agent_run_env is a
                # short single-use lease that must start at spawn time.
                env = self.finalize_run_env(
                    self.build_env(),
                    run_id=run_id,
                    app_session_id=app_session_id,
                    resolved_harness_run_config=input_payload.get(
                        "resolved_harness_run_config",
                    ),
                )
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
                    bare_config=bool(input_payload.get("bare_config")),
                    user_facing=bool(input_payload.get("user_facing"))
                    and not bool(input_payload.get("bare_config")),
                    disabled_builtin_extensions=input_payload["disabled_builtin_extensions"],
                    runtime_hydration=runtime_hydration,
                ))
                pass_fds = (
                    {"pass_fds": pinned.pass_fds}
                    if os.name != "nt" and pinned.pass_fds
                    else {}
                )
                popen = subprocess.Popen(
                    list(pinned.argv),
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_fp,
                    stderr=stderr_fp,
                    cwd=cwd,
                    env=env,
                    **pass_fds,
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
            started_at=datetime.now(timezone.utc).isoformat(),
            persist_to=worker_agent_session_id or app_session_id,
            target_message_id=target_message_id,
            turn_run_id=turn_run_id,
        )
        self._runs[run_id] = rs
        self._write_backend_state(rs)
        schedule_loop_task(
            loop,
            self._bootstrap_run(rs),
            name=f"agy-bootstrap-{run_id[:8]}",
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
            # No proven tool-disable flag — fail closed rather than run
            # a tool-capable CLI when the caller demanded text-only.
            logger.error("AgyProvider.run_headless: no_tools requested but unsupported")
            return None
        if fork:
            logger.warning("AGY provider ignores fork flag in run_headless")
        agy_bin = resolve_cli_binary("agy")
        if not agy_bin:
            logger.error("AgyProvider.run_headless: `agy` CLI not found")
            return None
        cmd = [agy_bin]
        if resume_sid or session_id:
            cmd += ["--conversation", resume_sid or session_id or ""]
        cmd += ["-p"]
        cmd.append(prompt)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self.build_env(),
            cwd=cwd,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=timeout,
            ) if timeout else await proc.communicate()
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return None
        if proc.returncode != 0:
            logger.error(
                "AgyProvider.run_headless: exited %s; stderr=%r",
                proc.returncode, stderr_bytes[:500],
            )
            return None
        return {
            "result": stdout_bytes.decode(errors="replace").strip(),
            "session_id": None,
            "usage": {},
            "total_cost_usd": 0.0,
        }

    async def rewind(self, app_sid: str, message_uuid: str) -> None:
        raise NotImplementedError("agy provider does not support rewind.")
