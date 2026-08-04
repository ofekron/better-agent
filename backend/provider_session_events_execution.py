from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger(__name__)


from cli_paths import resolve_cli_binary
from codex_execution_common import ExecutionContractError
from env_compat import get_env
from execution_template import PreparedExecution
from extension_run_policy import (
    disabled_runtime_skills_for_run,
    resolve_extension_run_policy,
)
from paths import ba_home, resolve_provider_config_dir, user_home
from provider_family_execution_runtime import prepare_family_execution
from provider_family_launch_attestation import (
    FamilyLaunchAttestation,
    capture_cli_launch,
    capture_config_scope,
    capture_runner_launch,
)
from provider_family_runtime_capabilities import (
    snapshot_family_runtime_capabilities,
)
from provider_manifest import (
    artifact_family_kinds,
    runner_module_for,
)
from provider_runtime_plan_source import (
    selected_runtime_agent_sources,
    selected_runtime_skill_sources,
    structural_provider_runtime_plan,
)
from provider_session_events_execution_strategy import (
    SessionEventsExecutionStrategy,
    external_execution_kinds,
    strategy_for,
)
import user_prefs


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


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _normalize_mode(provider: Any, arguments: Mapping[str, Any]) -> str:
    mode = arguments.get("mode")
    if mode == "manager":
        mode = "team"
    if mode not in {"native", "team"}:
        raise ValueError(f"mode must be 'native' or 'team', got {mode!r}")
    if mode == "team" and not provider.supports_manager_mode:
        raise NotImplementedError(
            f"{provider.KIND} provider does not support team mode.",
        )
    fork = bool(arguments.get("fork"))
    if fork and not provider.supports_fork:
        raise NotImplementedError(
            f"{provider.KIND} provider does not support fork.",
        )
    if fork and provider.KIND in {"amp", "opencode", "pi"} and not arguments.get(
        "session_id",
    ):
        raise ValueError(
            f"{provider.KIND} fork requires an existing session id",
        )
    return mode


def _mode_projection(
    strategy: SessionEventsExecutionStrategy,
    canonical_mode: str,
) -> dict[str, str]:
    return {
        "canonical_mode": canonical_mode,
        "mode": (
            "manager"
            if strategy.kind == "openai" and canonical_mode == "team"
            else canonical_mode
        ),
    }


def _normalize_reasoning(
    provider: Any,
    arguments: Mapping[str, Any],
) -> str | None:
    effort = str(arguments.get("reasoning_effort") or "").strip()
    if not effort:
        return "" if provider.supports_reasoning_effort else None
    if not provider.supports_reasoning_effort:
        raise NotImplementedError(
            f"{provider.KIND} provider does not support reasoning effort.",
        )
    if provider.KIND == "openai":
        from reasoning_effort import normalize_reasoning_effort

        normalized = normalize_reasoning_effort(effort)
        if normalized is None:
            allowed = ", ".join(provider.reasoning_effort_options)
            raise ValueError(f"reasoning_effort must be one of: {allowed}")
        return normalized
    if effort not in provider.reasoning_effort_options:
        allowed = ", ".join(provider.reasoning_effort_options)
        raise ValueError(
            f"reasoning_effort {effort!r} is not valid for the "
            f"{provider.KIND} provider. Options: {allowed}.",
        )
    return effort


def _normalize_model(provider: Any, arguments: Mapping[str, Any]) -> str:
    kind = provider.KIND
    model = str(arguments.get("model") or "").strip()
    if kind == "openai":
        if not model:
            raise ValueError("openai provider requires a model")
        return model
    if kind == "copilot":
        from provider_copilot import _normalize_copilot_model

        model = _normalize_copilot_model(model)
    if kind == "kimi":
        from provider_kimi import KIMI_MODELS, fetch_kimi_models

        available = _dedupe(
            provider.available_models() + fetch_kimi_models() + KIMI_MODELS,
        )
    else:
        module = __import__(f"provider_{kind}", fromlist=["_"])
        constants = {
            "amp": "AMP_MODELS",
            "copilot": "COPILOT_MODELS",
            "cursor": "CURSOR_MODELS",
            "opencode": "OPENCODE_MODELS",
            "pi": "PI_MODELS",
            "qwen": "QWEN_MODELS",
        }
        available = _dedupe(
            provider.available_models()
            + list(getattr(module, constants[kind])),
        )
    if kind == "pi":
        from provider_pi import _model_allowed

        valid = not model or _model_allowed(model, available)
    else:
        valid = not model or model in available
    if not valid:
        raise ValueError(
            f"model {model!r} is not available for the {kind} provider. "
            f"Available: {', '.join(available)}.",
        )
    return model


def _session_records(arguments: Mapping[str, Any]) -> tuple[dict, dict]:
    from session_manager import manager

    session_id = str(arguments["app_session_id"])
    worker_id = str(arguments.get("worker_agent_session_id") or "")
    session = manager.get_fields(session_id, RUN_CONFIG_SESSION_FIELDS)
    worker = (
        manager.get_fields(worker_id, RUN_CONFIG_SESSION_FIELDS)
        if worker_id
        else {}
    )
    return session, worker


def _runner_input(
    provider: Any,
    strategy: SessionEventsExecutionStrategy,
    arguments: Mapping[str, Any],
    authority: Mapping[str, Any],
) -> dict[str, Any]:
    canonical_mode = _normalize_mode(provider, arguments)
    model = _normalize_model(provider, arguments)
    reasoning_effort = _normalize_reasoning(provider, arguments)
    session, worker = _session_records(arguments)
    policy = resolve_extension_run_policy(
        resolved_harness_run_config=arguments.get(
            "resolved_harness_run_config",
        ),
        session_record=session,
        worker_record=worker,
        provider_kind=provider.KIND,
        provider_run_config=arguments.get("provider_run_config"),
        capability_contexts=arguments.get("capability_contexts"),
        disabled_builtin_extensions=arguments.get(
            "disabled_builtin_extensions",
        ),
    )
    permission = None
    if strategy.permission:
        from permission import resolve_for_run

        permission = resolve_for_run(
            sess_rec=session,
            worker_sess_rec=worker,
            is_worker=bool(arguments.get("is_worker")),
            fallback_kind=provider.KIND,
        )
    import extension_store
    import installation_profile

    integrations_enabled = installation_profile.integrations_enabled()
    values = {
        **arguments,
        **policy,
        **_mode_projection(strategy, canonical_mode),
        "app_session_id": str(arguments["app_session_id"]),
        "backend_url": (
            str(arguments.get("backend_url") or "").strip()
            or get_env("BETTER_CLAUDE_BACKEND_URL").strip()
            or "http://localhost:8000"
        ),
        "browser_harness_enabled": bool(
            arguments.get("browser_harness_enabled"),
        ),
        "context_strategy": user_prefs.get_context_strategy(),
        "files": list(arguments.get("files") or []),
        "images": list(arguments.get("images") or []),
        "internal_token": "",
        "integrations_enabled": integrations_enabled,
        "model": model,
        "permission": permission,
        "provider_id": provider.id,
        "provider_kind": provider.KIND,
        "provider_mode": str(authority.get("mode") or "subscription"),
        "reasoning_effort": reasoning_effort,
        "runner": str(authority.get("runner") or ""),
        "source": str(arguments.get("source") or ""),
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
        "user_facing": bool(arguments.get("user_facing")),
        "working_mode": session.get("working_mode"),
        "worker_working_mode": worker.get("working_mode"),
        "provisioned_tool_profile": str(
            arguments.get("provisioned_tool_profile") or "",
        ).strip(),
        "disabled_runtime_skills": disabled_runtime_skills_for_run(
            session_record=session,
            worker_record=worker,
        ),
    }
    values["required_mcp_server_names"] = sorted(
        extension_store.required_profile_mcp_server_names(values),
    )
    if strategy.kind == "copilot":
        values["config_dir"] = str(
            authority.get("config_dir") or "",
        )
    payload = {
        key: values.get(key)
        for key in strategy.input_fields
    }
    if set(payload) != set(strategy.input_fields):
        raise ExecutionContractError("incomplete session-events runner input")
    return payload


def _config_root(
    authority: Mapping[str, Any],
    strategy: SessionEventsExecutionStrategy,
) -> Path:
    if strategy.kind == "openai":
        root = ba_home() / "better_agent_sessions"
    elif strategy.kind in {"opencode", "pi"}:
        root = user_home()
    else:
        configured = (
            authority.get("config_dir")
            if strategy.kind == "copilot"
            else None
        )
        root = resolve_provider_config_dir(
            str(configured or strategy.config_default),
        )
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve(strict=True)


def _resume_path(
    strategy: SessionEventsExecutionStrategy,
    root: Path,
    runner_input: Mapping[str, Any],
) -> Path | None:
    session_id = str(runner_input.get("session_id") or "").strip()
    if not session_id:
        return None
    if strategy.resume_kind == "openai":
        import re

        safe = re.sub(r"[^A-Za-z0-9_-]", "_", session_id)
        candidate = root / f"{safe}.json"
        return candidate if candidate.is_file() else None
    if strategy.resume_kind == "copilot":
        current = root / "session-state" / session_id / "events.jsonl"
        legacy = root / "session-state" / f"{session_id}.jsonl"
        if current.is_file():
            return current
        return legacy if legacy.is_file() else None
    if strategy.resume_kind == "pi":
        from runner_pi import find_session_file_for_sid

        return find_session_file_for_sid(session_id)
    return None


def _launch_attestation(
    authority: Mapping[str, Any],
    strategy: SessionEventsExecutionStrategy,
    runner_input: Mapping[str, Any],
    run_dir: Path,
) -> FamilyLaunchAttestation:
    root = _config_root(authority, strategy)
    resume = _resume_path(strategy, root, runner_input)
    config_paths = tuple(root / value for value in strategy.config_files)
    if resume is not None and not resume.resolve(strict=True).is_relative_to(root):
        raise ExecutionContractError("resume authority escapes config root")
    config = capture_config_scope(
        root_path=root,
        config_paths=config_paths,
        resume_path=resume,
    )
    module = runner_module_for(strategy.kind)
    runner_path = Path(__file__).resolve().parent / f"{module}.py"
    runner = capture_runner_launch(
        run_dir=run_dir,
        executable_path=sys.executable,
        runner_entry=runner_path,
        runner_kind=strategy.kind,
        runner_module=module,
        frozen=bool(getattr(sys, "frozen", False)),
    )
    executable = (
        sys.executable
        if strategy.cli_command is None
        else resolve_cli_binary(strategy.cli_command)
    )
    if not executable:
        raise RuntimeError(
            f"{strategy.cli_command} CLI not found on PATH",
        )
    launch = FamilyLaunchAttestation.capture(
        family=strategy.kind,
        runner=runner,
        downstream=capture_cli_launch(
            logical_command=strategy.kind,
            launcher_path=executable,
            search_path=os.environ.get("PATH"),
            platform=sys.platform,
            command_processor=os.environ.get("COMSPEC"),
        ),
        config=config,
    )
    ok, attest_reason = launch.attest_with_reason()
    if not ok:
        logger.error("session-events launch authority attestation failed: reason=%s", attest_reason)
        raise ExecutionContractError(
            f"session-events launch authority changed during preparation (reason: {attest_reason})",
        )

    return launch


def prepare_session_events_execution(
    provider: Any,
    start_arguments: Mapping[str, Any],
) -> PreparedExecution:
    if (
        provider.KIND not in artifact_family_kinds()
        or provider.KIND not in external_execution_kinds()
        or type(start_arguments) is not dict
    ):
        raise ExecutionContractError("invalid session-events preparation")
    strategy = strategy_for(provider.KIND)
    run_id = str(start_arguments.get("run_id") or "")
    if not run_id:
        raise ExecutionContractError("session-events run id is unavailable")
    authority = provider.execution_authority_record(dict(start_arguments))
    runner_input = _runner_input(
        provider,
        strategy,
        start_arguments,
        authority,
    )
    from runs_dir import runs_root

    launch = _launch_attestation(
        authority,
        strategy,
        runner_input,
        runs_root() / run_id,
    )
    projection = structural_provider_runtime_plan(
        runner_input,
        provider.KIND,
    )
    capabilities = snapshot_family_runtime_capabilities(
        family=provider.KIND,
        skill_sources=selected_runtime_skill_sources(
            str(runner_input["cwd"]),
            bool(runner_input["bare_config"]),
            runner_input["disabled_runtime_skills"],
        ),
        agent_sources=selected_runtime_agent_sources(
            provider.KIND,
            bool(runner_input["bare_config"]),
        ),
        resolved_plan=projection["resolved_plan"],
        extension_state=projection["extension_state"],
        installation_decisions=projection["installation_decisions"],
    )
    native_sid_compatibility = None
    if runner_input.get("runner") == "better_agent_runner":
        from native_sid_compatibility import (
            derive_better_agent_runner_native_sid_compatibility,
        )

        native_sid_compatibility = (
            derive_better_agent_runner_native_sid_compatibility(
                app_session_id=str(start_arguments["app_session_id"]),
                worker_session_id=start_arguments.get(
                    "worker_agent_session_id"
                ),
            )
        )
    return prepare_family_execution(
        authority,
        # The execution template's frozen "model" argument must match what
        # runner_input actually uses: _normalize_model can rewrite it (e.g.
        # Copilot's retired-model fallbacks collapse to "auto"), and the
        # template would otherwise freeze the pre-normalization value,
        # tripping validate_recovery_input's exact-match check at run start.
        start_arguments={**start_arguments, "model": runner_input["model"]},
        runner_input=runner_input,
        launch=launch,
        capabilities=capabilities,
        native_sid_compatibility=native_sid_compatibility,
    )


__all__ = [
    "SessionEventsExecutionStrategy",
    "external_execution_kinds",
    "prepare_session_events_execution",
    "strategy_for",
]
