"""Per-spec config resolution.

Model / provider / reasoning-effort come from the app-settings single source
of truth — `config_store.resolve_internal_llm(spec.task_key)` — so a
provisioned session honors the same per-task configuration the user set in
the UI. Everything else (cwd, dispatch, run-mode, session-id pins, http
token) is resolved here with a per-spec env overlay.

Env overlay (per spec `env_prefix`, e.g. `REQ_ANALYSIS` / `SESSION_SEARCH`),
overriding app-settings where set:
  {PREFIX}_MODEL, {PREFIX}_PROVIDER_ID, {PREFIX}_REASONING_EFFORT,
  {PREFIX}_CWD, {PREFIX}_RUN_MODE (fork|direct), {PREFIX}_DISPATCH
  (http|in_process), {PREFIX}_ON_NO_FORK (error|fallback_native),
  {PREFIX}_NODE_ID (target node; "primary" runs locally), {PREFIX}_PROVISIONED_SESSION_ID,
  {PREFIX}_CALLER_SESSION_ID, {PREFIX}_WORKER_DESCRIPTION, {PREFIX}_BACKEND_URL,
  {PREFIX}_INTERNAL_TOKEN.

`PROVISIONED_SESSION_ID` pins a specific base (skips the registry); a pinned
base that turns out dirty raises rather than being rotated.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from paths import ba_home
from env_compat import get_env

from provisioning.spec import ProvisionedSessionSpec


@dataclass
class ProvisionedConfig:
    cwd: str
    model: str
    provider_id: str
    reasoning_effort: str
    run_mode: str            # "fork" | "direct"
    dispatch: str            # "http" | "in_process"
    on_no_fork: str          # "error" | "fallback_native"
    node_id: str             # target node; "primary" runs locally
    backend_url: str
    internal_token: str
    provisioned_session_id: str | None   # env pin, else None
    caller_session_id: str | None        # env pin, else None
    worker_description: str


def resolve_config(
    spec: ProvisionedSessionSpec, *, model: str | None = None
) -> ProvisionedConfig:
    custom = spec.build_config(model=model)
    if custom is not None:
        return custom
    resolved = _resolve_task(spec)

    model = model or _env(spec, "MODEL") or resolved.get("model") or spec.default_model
    if not model:
        raise RuntimeError(f"{spec.key or spec.name or 'provisioned session'} has no model configured")
    provider_id = _env(spec, "PROVIDER_ID") or resolved.get("provider_id") or ""
    reasoning_effort = _env(spec, "REASONING_EFFORT")
    if reasoning_effort is None:
        reasoning_effort = resolved.get("reasoning_effort") or ""

    run_mode = _resolve_run_mode(spec, provider_id)
    return ProvisionedConfig(
        cwd=(
            _env(spec, "CWD")
            or spec.default_cwd
            or str(Path(os.getcwd()).expanduser().resolve())
        ),
        model=model,
        provider_id=provider_id,
        reasoning_effort=reasoning_effort,
        run_mode=run_mode,
        dispatch=_choice(spec, "DISPATCH", spec.dispatch, ("http", "in_process")),
        on_no_fork=_choice(spec, "ON_NO_FORK", spec.on_no_fork, ("error", "fallback_native")),
        node_id=(_env(spec, "NODE_ID") or spec.node_id or "primary"),
        backend_url=(
            _env(spec, "BACKEND_URL")
            or get_env("BETTER_CLAUDE_BACKEND_URL")
            or "http://localhost:8000"
        ).strip(),
        internal_token=_resolve_internal_token(spec),
        provisioned_session_id=_env(spec, "PROVISIONED_SESSION_ID"),
        caller_session_id=_env(spec, "CALLER_SESSION_ID"),
        worker_description=_env(spec, "WORKER_DESCRIPTION") or spec.name,
    )


def _resolve_task(spec: ProvisionedSessionSpec) -> dict:
    """app-settings resolution for `spec.task_key`. Empty (headless/standalone
    or unknown task) — caller falls back to spec defaults / env."""
    if not spec.task_key:
        return {}
    try:
        import config_store
    except Exception:
        return {}
    try:
        return config_store.resolve_internal_llm(spec.task_key)
    except Exception:
        return {}


def _env(spec: ProvisionedSessionSpec, suffix: str) -> str | None:
    value = os.environ.get(f"{spec.env_prefix}_{suffix}")
    return value.strip() if value else None


def _choice(
    spec: ProvisionedSessionSpec, suffix: str, default: str, allowed: tuple[str, ...]
) -> str:
    raw = _env(spec, suffix)
    if not raw:
        return default
    value = raw.lower()
    if value not in allowed:
        raise RuntimeError(f"{spec.env_prefix}_{suffix} must be one of {allowed}")
    return value


def _resolve_run_mode(spec: ProvisionedSessionSpec, provider_id: str) -> str:
    requested = (_env(spec, "RUN_MODE") or spec.run_mode).lower()
    if requested not in ("fork", "direct"):
        raise RuntimeError(f"{spec.env_prefix}_RUN_MODE must be 'fork' or 'direct'")
    if requested == "direct":
        return requested
    if not provider_supports_fork(provider_id):
        raise RuntimeError(
            f"{spec.env_prefix} fork run_mode requires a fork-capable provider "
            f"(provider {provider_id!r} does not support fork)"
        )
    return requested


def provider_supports_fork(provider_id: str) -> bool:
    import config_store

    if not provider_id:
        state = config_store.list_providers()
        provider_id = str(state.get("default_provider_id") or "")
        if not provider_id:
            return False
    provider = config_store.resolve_provider_ref(provider_id)
    return bool(provider and provider.get("supports_fork"))


def _resolve_internal_token(spec: ProvisionedSessionSpec) -> str:
    return (
        _env(spec, "INTERNAL_TOKEN")
        or _read_internal_token()
        or get_env("BETTER_CLAUDE_INTERNAL_TOKEN")
    )


def _read_internal_token() -> str:
    try:
        return (ba_home() / "internal_token").read_text(encoding="utf-8").strip()
    except OSError:
        return ""
