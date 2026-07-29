from __future__ import annotations

from dataclasses import dataclass

from codex_execution_common import ExecutionContractError
from provider_manifest import session_events_family_kinds


@dataclass(frozen=True)
class SessionEventsExecutionStrategy:
    kind: str
    cli_command: str | None
    config_default: str
    config_files: tuple[str, ...]
    input_fields: frozenset[str]
    permission: bool = False
    resume_kind: str = ""


_COMMON_INPUT_FIELDS = frozenset({
    "app_session_id",
    "backend_url",
    "browser_harness_enabled",
    "cwd",
    "files",
    "images",
    "internal_token",
    "mode",
    "model",
    "prompt",
    "provider_id",
    "provider_kind",
    "runner",
    "session_id",
    "source",
    "target_message_id",
    "turn_run_id",
    "user_facing",
    "worker_agent_session_id",
})
_POLICY_FIELDS = frozenset({
    "active_capability_ids",
    "bare_config",
    "capability_contexts",
    "coordination_enabled",
    "disabled_builtin_extensions",
    "disabled_builtin_tools",
    "disabled_runtime_skills",
    "extra_mcp_servers",
    "integrations_enabled",
    "provider_run_config",
    "required_mcp_server_names",
    "resolved_harness_run_config",
    "team_orchestration_enabled",
})
_PROJECTION_FIELDS = frozenset({
    "context_strategy",
    "provisioned_tool_profile",
    "worker_working_mode",
    "working_mode",
})


def _fields(*extra: str) -> frozenset[str]:
    return frozenset({
        *_COMMON_INPUT_FIELDS,
        *_POLICY_FIELDS,
        *_PROJECTION_FIELDS,
        *extra,
    })


_STRATEGIES = {
    "amp": SessionEventsExecutionStrategy(
        "amp",
        "amp",
        ".config/amp",
        ("settings.json",),
        _fields("fork"),
        permission=True,
    ),
    "copilot": SessionEventsExecutionStrategy(
        "copilot",
        "copilot",
        ".copilot",
        (),
        _fields("config_dir"),
        resume_kind="copilot",
    ),
    "cursor": SessionEventsExecutionStrategy(
        "cursor",
        "cursor-agent",
        ".cursor",
        ("cli-config.json", "mcp.json"),
        _fields(),
        permission=True,
    ),
    "kimi": SessionEventsExecutionStrategy(
        "kimi",
        "kimi",
        ".kimi",
        ("config.toml", "kimi.json", "mcp.json"),
        _fields(),
    ),
    "openai": SessionEventsExecutionStrategy(
        "openai",
        None,
        "",
        (),
        _fields(
            "continuation_chain",
            "disallowed_tools",
            "fork",
            "mssg_sender_session_id",
            "reasoning_effort",
            "setting_sources",
            "supervised",
            "supervisor_agent_session_id",
        ),
        permission=True,
        resume_kind="openai",
    ),
    "opencode": SessionEventsExecutionStrategy(
        "opencode",
        "opencode",
        ".",
        (
            ".config/opencode/opencode.jsonc",
            ".local/share/opencode/opencode.db",
        ),
        _fields(
            "fork",
            "mssg_sender_session_id",
            "reasoning_effort",
        ),
        permission=True,
    ),
    "pi": SessionEventsExecutionStrategy(
        "pi",
        "pi",
        ".",
        (".pi/agent/auth.json", ".pi/agent/models.json"),
        _fields("fork", "reasoning_effort", "supervised"),
        permission=True,
        resume_kind="pi",
    ),
    "qwen": SessionEventsExecutionStrategy(
        "qwen",
        "qwen",
        ".qwen",
        ("settings.json", "oauth_creds.json", "output-language.md"),
        _fields("provider_mode", "reasoning_effort", "supervised"),
        permission=True,
    ),
}


def external_execution_kinds() -> frozenset[str]:
    return session_events_family_kinds() - {"agy"}


def strategy_for(kind: str) -> SessionEventsExecutionStrategy:
    strategy = _STRATEGIES.get(kind)
    if strategy is None or kind not in external_execution_kinds():
        raise ExecutionContractError("unsupported session-events execution kind")
    return strategy


__all__ = [
    "SessionEventsExecutionStrategy",
    "external_execution_kinds",
    "strategy_for",
]
