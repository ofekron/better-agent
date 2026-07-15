from __future__ import annotations

import os


def agent_env_name(legacy_name: str) -> str:
    if not legacy_name.startswith("BETTER_CLAUDE_"):
        raise ValueError(f"legacy env name must start with BETTER_CLAUDE_: {legacy_name!r}")
    return "BETTER_AGENT_" + legacy_name.removeprefix("BETTER_CLAUDE_")


def get_env(legacy_name: str, default: str = "") -> str:
    return os.environ.get(agent_env_name(legacy_name)) or os.environ.get(legacy_name) or default


def get_env_stripped(legacy_name: str, default: str = "") -> str:
    return get_env(legacy_name, default).strip()


def require_env(legacy_name: str) -> str:
    value = get_env_stripped(legacy_name)
    if not value:
        raise RuntimeError(f"{agent_env_name(legacy_name)} or {legacy_name} is required")
    return value


def dual_env(legacy_name: str, value: object) -> dict[str, str]:
    rendered = str(value)
    return {
        agent_env_name(legacy_name): rendered,
        legacy_name: rendered,
    }


def dual_env_many(values: dict[str, object]) -> dict[str, str]:
    env: dict[str, str] = {}
    for legacy_name, value in values.items():
        env.update(dual_env(legacy_name, value))
    return env


def better_agent_home_env() -> dict[str, str]:
    return dual_env("BETTER_CLAUDE_HOME", get_env("BETTER_CLAUDE_HOME"))


def better_agent_runtime_env() -> dict[str, str]:
    env = better_agent_home_env()
    for key in ("TMPDIR", "TEMP", "TMP"):
        value = os.environ.get(key)
        if value is not None:
            env[key] = value
    return env
