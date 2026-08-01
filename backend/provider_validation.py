"""Pure provider/model/permission validation and resolution helpers.

No route handlers and no dependency on the app's coordinator/broadcast
machinery live here — only stateless (or config_store/session-backed)
validation logic reused by provider routes, session routes, and worker
provisioning alike.
"""
from __future__ import annotations

from fastapi import HTTPException, Request

import auth_routes
import config_store
import extension_store
import runtime_profile
from hot_path_executor import hot_path
from i18n import t
from reasoning_effort import normalize_reasoning_effort
from session_helpers import session_lite


def is_loopback_request(request: Request) -> bool:
    """Server-side desktop/loopback gate for OAuth login/logout. The CLI
    opens a browser and binds a localhost callback, so the user's browser
    must share the backend's machine; a remote authenticated client must
    not trigger a server-side browser spawn or wipe credentials.

    Delegates to the canonical loopback check in auth_routes (handles the
    full 127.0.0.0/8, ::1, and IPv4-mapped IPv6 forms) rather than a
    divergent local set."""
    return auth_routes._is_loopback_request(request)


def provider_auth_result_response(result: dict):
    """`_start` already broadcasts on every state change, so the route does
    not rebroadcast — it only maps spawn errors to HTTP statuses."""
    error = result.get("error")
    if error == "busy":
        raise HTTPException(status_code=409, detail="A login/logout is already in progress.")
    if error == "binary_missing":
        raise HTTPException(status_code=409, detail="Provider CLI not found.")
    if error == "spawn_failed":
        raise HTTPException(status_code=409, detail="Login could not start (transport unavailable).")
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail="Login could not start.")
    return {"login_state": result.get("state")}


def provider_not_suspended(provider_id: str | None, *, action: str = "use provider") -> None:
    if provider_id and config_store.provider_suspended(provider_id):
        raise HTTPException(
            status_code=409,
            detail=t("error.provider_suspended", action=action),
        )


def api_reasoning_effort(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return ""
    effort = normalize_reasoning_effort(value)
    if effort is None:
        raise HTTPException(
            status_code=400,
            detail="reasoning_effort must be one of: none, minimal, low, medium, high, xhigh",
        )
    return effort


def api_optional_provision_prompt(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(status_code=400, detail="provision_prompt must be a non-empty string")
    return value


def api_optional_pool_affinity_key(value: object) -> str:
    if value in (None, ""):
        return ""
    key = str(value).strip()
    if not key:
        return ""
    if len(key) > 200:
        raise HTTPException(status_code=400, detail="pool_affinity_key must be at most 200 characters")
    return key


_REQUIREMENTS_PROCESSOR_PROFILE = "requirements_processor"


def api_optional_provisioned_tool_profile(value: object, body: dict | None = None) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise HTTPException(status_code=400, detail="provisioned_tool_profile must be a string")
    profile = value.strip()
    if not profile:
        return profile
    if profile == _REQUIREMENTS_PROCESSOR_PROFILE:
        if is_authorized_provisioned_tool_profile(body, profile):
            return profile
        raise HTTPException(
            status_code=400,
            detail="requirements_processor profile is reserved for get-requirements processor dispatch",
        )
    raise HTTPException(status_code=400, detail="unsupported provisioned_tool_profile")


def is_authorized_provisioned_tool_profile(body: dict | None, profile: str) -> bool:
    if not isinstance(body, dict):
        return False
    client_delegation_id = body.get("client_delegation_id")
    if not isinstance(client_delegation_id, str):
        return False
    from provisioning.dispatch import is_authorized_tool_profile_dispatch
    return is_authorized_tool_profile_dispatch(client_delegation_id, profile)


def _resolved_provider_record(provider_id: str | None) -> dict | None:
    """Resolve provider_id to its record, falling back to the active default
    provider when id is absent or the named provider is not found."""
    record = config_store.get_provider(provider_id) if provider_id else None
    if record is None:
        active = config_store.get_default_provider()
        record = config_store.get_provider(active["id"]) if active else None
    return record


def provider_reasoning_effort(
    provider_id: str | None,
    effort: str | None,
    runner: str | None = None,
    model: str = "",
) -> str | None:
    if effort is None:
        return None
    if not effort:
        return ""
    record = _resolved_provider_record(provider_id)
    options = runtime_profile.reasoning_efforts(
        record or {}, runner, model=model,
    )
    if effort not in options:
        name = (record or {}).get("name") or provider_id or "active provider"
        raise HTTPException(
            status_code=400,
            detail=f"{name} does not support reasoning_effort={effort!r}",
        )
    return effort


def inherited_reasoning_effort(
    provider_id: str | None,
    effort: str | None,
    runner: str | None = None,
    model: str = "",
) -> str:
    """Resolve the target provider record, then fit the inherited effort onto it.

    Explicitly requested efforts still go through provider_reasoning_effort so a
    caller asking for an unsupported value gets a 400 instead of a silent
    downgrade.
    """
    record = _resolved_provider_record(provider_id)
    return runtime_profile.fit_reasoning_effort(record, effort, runner, model=model)


def provider_runner(provider_id: str | None, runner: object = None) -> str:
    record = _resolved_provider_record(provider_id)
    if record is None:
        raise HTTPException(status_code=400, detail="provider does not exist")
    try:
        return runtime_profile.resolve_runner(record, runner)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def api_permission(value: object) -> dict | None:
    """Coerce an incoming permission body value. None = absent (no change /
    inherit at creation). Empty object/string = explicit "inherit default"
    (clear any per-session override). Otherwise must be a dict."""
    if value is None:
        return None
    if (isinstance(value, str) and not value.strip()) or value == {}:
        return {}
    if not isinstance(value, dict):
        raise HTTPException(status_code=400, detail="permission must be an object")
    return value


def provider_permission(
    provider_id: str | None,
    value: dict | None,
    runner: str | None = None,
) -> dict | None:
    """Strictly validate a permission dict against the provider's native
    options. Returns normalized dict, {} (inherit default), or None (absent)."""
    if value is None:
        return None
    if not value:
        return {}
    record = _resolved_provider_record(provider_id)
    name = (record or {}).get("name") or provider_id or "active provider"
    from permission import permission_axes_for_kind
    runtime_kind = runtime_profile.runtime_kind(record or {}, runner)
    options = permission_axes_for_kind(runtime_kind)
    if not options:
        raise HTTPException(
            status_code=400, detail=f"{name} has no permission options"
        )
    norm: dict[str, str] = {}
    for axis, allowed in options.items():
        raw = value.get(axis)
        if not isinstance(raw, str) or raw not in allowed:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{name} permission {axis}={raw!r} is not one of: "
                    f"{', '.join(allowed)}"
                ),
            )
        norm[axis] = raw
    return norm


def provider_for_required_model(provider_id: str | None) -> dict:
    provider = config_store.get_provider(provider_id) if provider_id else config_store.get_default_provider()
    if provider_id and not provider:
        if config_store.provider_suspended(provider_id):
            raise HTTPException(
                status_code=409,
                detail=t("error.provider_suspended", action="create sessions"),
            )
        raise HTTPException(status_code=404, detail="provider not found")
    if not provider:
        raise HTTPException(status_code=400, detail="no active provider configured")
    provider_not_suspended(provider.get("id"), action="create sessions")
    return provider


def profile_prefill_model(provider_id: str | None, runner: object = None) -> str:
    """Model prefill chain when a caller supplies no explicit model:
    the (provider, runner) profile's last-used model, then its default
    model. "" when the provider has no live profile."""
    import user_prefs

    if not provider_id:
        return ""
    defaults = config_store.provider_execution_defaults(provider_id, runner)
    profile_id = defaults["runtime_profile_id"]
    if profile_id:
        last = str(user_prefs.get_last_models().get(profile_id) or "").strip()
        if last:
            return last
    return str(defaults["default_model"] or "").strip()


def required_model_from_body_or_provider(body: dict, provider: dict) -> str:
    import models as models_mod

    model = str(body.get("model") or "").strip()
    if model:
        validate_provider_model(str(provider.get("id") or "").strip() or None, model)
        return model
    provider_id = str(provider.get("id") or "").strip() or None
    available = models_mod.available_models(provider_id)
    default_model = profile_prefill_model(provider_id, body.get("runner"))
    name = provider.get("name") or provider.get("id") or "provider"
    if not default_model:
        raise HTTPException(status_code=400, detail=f"{name} has no default model configured")
    if default_model and default_model in available:
        return default_model
    for candidate in available:
        candidate = str(candidate or "").strip()
        if candidate:
            return candidate
    raise HTTPException(status_code=400, detail=f"{name} has no default model configured")


def validate_provider_model(
    provider_id: str | None, model: str, include_retired: bool = False,
) -> None:
    if not model:
        return
    import models as models_mod
    available = set(
        models_mod.available_models_including_retired(provider_id)
        if include_retired
        else models_mod.available_models(provider_id)
    )
    if model in available:
        return
    provider = (
        config_store.get_provider(provider_id)
        if provider_id
        else config_store.get_default_provider()
    ) or {}
    name = provider.get("name") or provider_id
    if not available:
        raise HTTPException(
            status_code=400,
            detail=f"{name} has no known models; explicit model={model!r} is not allowed",
        )
    raise HTTPException(
        status_code=400,
        detail=f"{name} does not support model={model!r}",
    )


async def resolve_provider_id_ref(provider_ref: str) -> str:
    ref = str(provider_ref or "").strip()
    if not ref:
        return ""
    try:
        import asyncio
        provider = await asyncio.to_thread(config_store.resolve_provider_ref, ref)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not provider:
        raise HTTPException(status_code=400, detail="provider_id does not exist")
    return str(provider.get("id") or "").strip()


async def resolve_auto_search_provider_id(
    body: dict,
    caller_session_id: str,
) -> str:
    requested = str((body or {}).get("provider_id") or "").strip()
    if requested.upper() == "ANY":
        return ""
    if requested:
        return await resolve_provider_id_ref(requested)
    caller = await session_lite(caller_session_id)
    return str((caller or {}).get("provider_id") or "").strip()


async def validate_optional_run_selector(
    sender_session_id: str,
    provider_id: str,
    model: str,
) -> None:
    if not provider_id and not model:
        return
    sender = await session_lite(sender_session_id)
    resolved_provider_id = (
        provider_id
        or str((sender or {}).get("provider_id") or "").strip()
        or None
    )
    resolved_model = model
    if not resolved_model and provider_id:
        resolved_model = await hot_path.run(
            "communication.validate_run_selector.profile_prefill_model",
            profile_prefill_model,
            provider_id,
        )
        if not resolved_model:
            provider = await hot_path.run(
                "communication.validate_run_selector.get_provider",
                config_store.get_provider,
                provider_id,
            ) or {}
            name = provider.get("name") or provider_id
            raise HTTPException(
                status_code=400,
                detail=f"{name} has no default model configured",
            )
    if not resolved_model:  # pragma: no cover - defensive; L344 early-return + L353 block guarantee resolved_model is truthy here
        resolved_model = str((sender or {}).get("model") or "").strip()
    await hot_path.run(
        "communication.validate_run_selector.validate_provider_model",
        validate_provider_model,
        resolved_provider_id,
        resolved_model,
    )


def validate_provider_default_reasoning_effort(
    provider_record: dict, effort: str | None,
) -> str:
    parsed = api_reasoning_effort(effort)
    if not parsed:
        return ""
    options = config_store.reasoning_effort_options_for_provider(provider_record)
    if parsed not in options:
        name = provider_record.get("name") or provider_record.get("kind") or "provider"
        raise HTTPException(
            status_code=400,
            detail=f"{name} does not support reasoning_effort={parsed!r}",
        )
    return parsed


def api_extra_mcp_servers(value: object) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise HTTPException(status_code=400, detail="mcp_servers must be a list")
    names = []
    for item in value:
        name = str(item).strip()
        if not name:
            raise HTTPException(status_code=400, detail="mcp_servers entries must be non-empty strings")
        names.append(name)
    names = list(dict.fromkeys(names))
    known = extension_store.all_extension_mcp_server_names()
    unknown = [name for name in names if name not in known]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"unknown extension MCP servers: {', '.join(unknown)}",
        )
    return names


def api_disallowed_tools(value: object) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise HTTPException(status_code=400, detail="disallowed_tools must be a list")
    tools = []
    for item in value:
        tool = str(item).strip()
        if not tool:
            raise HTTPException(status_code=400, detail="disallowed_tools entries must be non-empty strings")
        tools.append(tool)
    return list(dict.fromkeys(tools))


def api_disabled_builtin_extensions(value: object) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise HTTPException(status_code=400, detail="disabled_builtin_extensions must be a list")
    extensions = []
    for item in value:
        extension_id = str(item).strip()
        if not extension_id:
            raise HTTPException(status_code=400, detail="disabled_builtin_extensions entries must be non-empty strings")
        extensions.append(extension_id)
    return list(dict.fromkeys(extensions))
