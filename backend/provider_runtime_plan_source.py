from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping

from codex_execution_common import ExecutionContractError
from provider_runtime_capability_model import (
    frozen_json,
    normalize_plan,
)
from provider_runtime_plan_hydration import (
    RUNNER_OPERATION_BROKER_REF as _RUNNER_OPERATION_BROKER_REF,
    apply_runtime_hydration,
    capture_runtime_hydration,
)
from provider_manifest import artifact_family_kinds


_FAMILIES = artifact_family_kinds()
_SECRET_KEY_RE = re.compile(
    r"(^|_)(api_?key|auth|authorization|credential|password|secret|token)($|_)",
)
_SECRET_VALUE_RE = re.compile(
    r"(?i)(^|[-_])(api[-_]?key|authorization|auth|credential|password|secret|token)"
    r"($|=)",
)


def _json_copy(value: Any, *, label: str) -> Any:
    try:
        return json.loads(
            json.dumps(
                value,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
        )
    except (TypeError, ValueError) as exc:
        raise ExecutionContractError(f"{label} must be JSON-compatible") from exc


def _secret_ref(path: str, *, kind: str = "runtime_value") -> dict[str, str]:
    return {
        "kind": kind,
        "path_sha256": hashlib.sha256(path.encode("utf-8")).hexdigest(),
    }


def _secret_free(
    value: Any,
    *,
    path: str,
    hydration: dict[str, str] | None = None,
) -> Any:
    if type(value) is dict:
        clean: dict[str, Any] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise ExecutionContractError("runtime plan keys must be strings")
            normalized = key.lower().replace("-", "_")
            item_path = f"{path}.{key}" if path else key
            if _SECRET_KEY_RE.search(normalized) and not normalized.endswith(
                ("_ref", "_refs"),
            ):
                reference = _secret_ref(item_path)
                capture_runtime_hydration(hydration, reference, item)
                clean[f"{key}_ref"] = reference
                continue
            clean[key] = _secret_free(
                item,
                path=item_path,
                hydration=hydration,
            )
        return clean
    if type(value) is list:
        return [
            _secret_free(
                item,
                path=f"{path}[{index}]",
                hydration=hydration,
            )
            for index, item in enumerate(value)
        ]
    if type(value) is str and _SECRET_VALUE_RE.search(value):
        reference = _secret_ref(path)
        capture_runtime_hydration(hydration, reference, value)
        return reference
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float and value == value and value not in (
        float("inf"),
        float("-inf"),
    ):
        return value
    raise ExecutionContractError("runtime plan must be JSON-compatible")


def _validated_source(path: str | Path, *, directory: bool) -> Path:
    candidate = Path(path)
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ExecutionContractError("runtime capability source is unavailable") from exc
    if (directory and not resolved.is_dir()) or (
        not directory and not resolved.is_file()
    ):
        raise ExecutionContractError("runtime capability source has invalid type")
    return resolved


def selected_runtime_skill_sources(
    cwd: str,
    bare_config: bool,
    disabled: list[str] | None,
) -> dict[str, Path]:
    import installation_profile

    if bare_config or not installation_profile.integrations_enabled():
        return {}
    import runtime_skills

    selected = runtime_skills._filter_disabled(
        runtime_skills._discover_skills(cwd),
        disabled,
    )
    sources: dict[str, Path] = {}
    for skill in selected:
        name = str(skill.get("name") or "").strip()
        if not name or name in sources:
            raise ExecutionContractError("runtime skill selection is invalid")
        sources[name] = _validated_source(skill.get("dir") or "", directory=True)
    return sources


def selected_runtime_agent_sources(
    provider_kind: str,
    bare_config: bool,
) -> dict[str, Path]:
    import installation_profile
    import runtime_agents

    if (
        bare_config
        or provider_kind not in runtime_agents.SUPPORTED_PROVIDERS
        or not installation_profile.integrations_enabled()
    ):
        return {}
    sources: dict[str, Path] = {}
    for entry in runtime_agents._discover_agents():
        source = entry.get(provider_kind)
        if not source:
            continue
        path = _validated_source(source, directory=False)
        if path.name in sources:
            raise ExecutionContractError("runtime agent selection is ambiguous")
        sources[path.name] = path
    return sources


def _setting_projection(
    extension_id: str,
    schema: list[dict[str, Any]],
    values: Mapping[str, Any],
    hydration: dict[str, str] | None,
) -> dict[str, Any]:
    projected: dict[str, Any] = {}
    for item in schema:
        key = str(item.get("key") or "").strip()
        if not key:
            continue
        if item.get("type") == "secret":
            reference = {
                "kind": "extension_setting",
                "extension_id": extension_id,
                "key_sha256": hashlib.sha256(key.encode("utf-8")).hexdigest(),
            }
            capture_runtime_hydration(
                hydration,
                reference,
                values.get(key, item.get("default")),
            )
            projected[f"{key}_ref"] = reference
            continue
        if key in values:
            projected[key] = _secret_free(
                values[key],
                path=f"extensions.{extension_id}.settings.{key}",
                hydration=hydration,
            )
        elif "default" in item:
            projected[key] = _secret_free(
                item["default"],
                path=f"extensions.{extension_id}.settings.{key}",
                hydration=hydration,
            )
    return projected


def _extension_projection(
    record: Mapping[str, Any],
    overlays: Mapping[str, Any],
    hydration: dict[str, str] | None,
) -> dict[str, Any] | None:
    import extension_store

    manifest = record.get("manifest")
    if type(manifest) is not dict:
        return None
    extension_id = str(manifest.get("id") or "").strip()
    if not extension_id or record.get("enabled") is not True:
        return None
    entrypoints = manifest.get("entrypoints")
    entrypoints = entrypoints if type(entrypoints) is dict else {}
    settings_schema = entrypoints.get("settings")
    settings_schema = settings_schema if type(settings_schema) is list else []
    # Turn-dispatch hot path: use the non-probing accessor. Secret refs are
    # emitted as opaque placeholders below and resolved later by the extension's
    # own loopback config read (extension_store.resolve_all_settings) — this
    # projection never needs `secret_present`, so it must not touch the OS
    # keychain here.
    setting_values = extension_store.get_extension_setting_values(extension_id)
    setting_values = setting_values.get("values") if type(setting_values) is dict else {}
    setting_values = setting_values if type(setting_values) is dict else {}
    mcp_items = entrypoints.get("mcp")
    mcp_items = mcp_items if type(mcp_items) is list else []
    predicates = [
        {
            "name": str(item.get("name") or ""),
            "predicate": _secret_free(
                item.get("predicate") or {},
                path=f"extensions.{extension_id}.mcp.predicate",
                hydration=hydration,
            ),
        }
        for item in mcp_items
        if type(item) is dict and item.get("predicate")
    ]
    package_fingerprint = extension_store.runtime_package_content_fingerprint(
        dict(record),
    )
    if package_fingerprint is not None and (
        type(package_fingerprint) is not str or not package_fingerprint
    ):
        raise ExecutionContractError("extension package identity is invalid")
    return {
        "id": extension_id,
        "version": str(manifest.get("version") or ""),
        "generation": str(record.get("generation") or ""),
        "revision": record.get("revision"),
        "package_fingerprint": package_fingerprint,
        "permission_grants": _secret_free(
            extension_store.permission_grants(dict(record)),
            path=f"extensions.{extension_id}.permission_grants",
            hydration=hydration,
        ),
        "mcp_predicates": predicates,
        "settings": _setting_projection(
            extension_id,
            settings_schema,
            setting_values,
            hydration,
        ),
        "setting_overlays": _secret_free(
            overlays.get(extension_id) or {},
            path=f"extensions.{extension_id}.setting_overlays",
            hydration=hydration,
        ),
    }


def _server_transport(configs: list[Mapping[str, Any]]) -> str:
    for config in configs:
        transport = str(config.get("transport") or "").lower()
        if transport in {"http", "sdk", "sse", "stdio"}:
            return transport
        url = str(config.get("url") or "")
        if url:
            return "sse" if transport == "sse" else "http"
    return "stdio"


def _tool_names(config: Mapping[str, Any]) -> list[str]:
    raw = config.get("tool_names")
    if not isinstance(raw, list):
        raw = config.get("tools")
    if not isinstance(raw, list):
        return []
    return sorted({
        str(item).strip()
        for item in raw
        if isinstance(item, str) and str(item).strip()
    })


def _contains_unavailable_runtime_secret(value: Any) -> bool:
    if type(value) is list:
        return any(_contains_unavailable_runtime_secret(item) for item in value)
    if type(value) is not dict:
        return False
    if (
        set(value) == {"kind", "path_sha256"}
        and value.get("kind") == "runtime_value"
    ):
        return True
    if (
        set(value) == {"kind", "extension_id", "key_sha256"}
        and value.get("kind") == "extension_setting"
    ):
        return True
    return any(
        _contains_unavailable_runtime_secret(item)
        for item in value.values()
    )


def _config_without_tool_metadata(
    config: Mapping[str, Any],
    *,
    path: str,
) -> dict:
    stripped = {
        key: value
        for key, value in config.items()
        if key not in {"tool_names", "tools"}
    }
    env = stripped.get("env")
    env = dict(env) if type(env) is dict else None
    if env is not None:
        extension_id = str(
            env.get("BETTER_AGENT_EXTENSION_ID")
            or env.get("BETTER_CLAUDE_EXTENSION_ID")
            or ""
        ).strip()
        for key in (
            "BETTER_AGENT_INTERNAL_TOKEN",
            "BETTER_CLAUDE_INTERNAL_TOKEN",
        ):
            if not env.get(key):
                continue
            if not extension_id:
                raise ExecutionContractError(
                    "runtime MCP secret lacks typed authority",
                )
            env.pop(key)
            env[f"{key}_ref"] = {
                "kind": "extension_identity",
                "extension_id": extension_id,
            }
        stripped["env"] = env
    clean = _secret_free(stripped, path=path)
    if _contains_unavailable_runtime_secret(clean):
        raise ExecutionContractError(
            "runtime MCP secret lacks typed authority",
        )
    return clean


def _explicit_mcp_configs(inputs: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    provider_config = inputs.get("provider_run_config")
    if type(provider_config) is not dict:
        return {}
    raw = provider_config.get("mcp_servers")
    if type(raw) is not dict:
        return {}
    return {
        name: config
        for name, config in raw.items()
        if type(name) is str and name and type(config) is dict
    }


def _effective_mcp_configs(
    inputs: dict[str, Any],
    *,
    explicit: dict[str, dict[str, Any]],
    runtime: dict[str, dict[str, Any]],
    launcher: dict[str, dict[str, Any]],
    integrations_enabled: bool,
) -> dict[str, dict[str, Any]]:
    from builtin_mcp_config import with_builtin_mcp_servers

    return with_builtin_mcp_servers(
        inputs,
        {"mcp_servers": explicit},
        runtime_broker=_RUNNER_OPERATION_BROKER_REF,
        integrations_enabled=integrations_enabled,
        runtime_mcp_servers=runtime,
        launcher_mcp_servers=launcher,
        include_tool_metadata=True,
    )["mcp_servers"]


def _provider_runner(inputs: Mapping[str, Any], provider_kind: str) -> str:
    from provider_manifest import default_runner_for, runner_choices_for

    raw = inputs.get("runner")
    runner = default_runner_for(provider_kind) if raw in (None, "") else raw
    if type(runner) is not str or runner not in runner_choices_for(provider_kind):
        raise ExecutionContractError("invalid provider runtime runner")
    return runner


def _structural_provider_runtime_plan(
    inputs: dict[str, Any],
    provider_kind: str,
) -> dict[str, dict[str, Any]]:
    if type(inputs) is not dict or provider_kind not in _FAMILIES:
        raise ExecutionContractError("invalid provider runtime plan source")
    frozen_inputs = _json_copy(inputs, label="provider runtime inputs")
    # The per-run operation-broker address only exists once the runner starts
    # its host; extension server configs built here carry this placeholder so
    # `hydrate_runner_operation_broker` swaps in the real address runner-side
    # (snapshotting the backend's own — absent — env here would freeze an
    # explicit empty broker into every brokered server env).
    frozen_inputs["runtime_broker"] = _RUNNER_OPERATION_BROKER_REF
    bare = bool(frozen_inputs.get("bare_config"))
    user_facing = bool(frozen_inputs.get("user_facing"))

    import extension_store
    import harness_run_projection
    import installation_profile

    ready_map = frozen_inputs.get("_mcp_prewarm_ready")
    warm_pool_names = (
        set(ready_map)
        if isinstance(ready_map, dict)
        else extension_store.runtime_mcp_warm_pool_server_names(frozen_inputs)
    )

    explicit = _explicit_mcp_configs(frozen_inputs)
    runner = (
        _provider_runner(frozen_inputs, provider_kind)
        if provider_kind == "claude"
        else None
    )
    if provider_kind != "claude":
        runtime = extension_store.runtime_mcp_server_configs(
            frozen_inputs,
            user_facing=user_facing,
            bare=bare,
        )
        launcher = extension_store.native_mcp_launcher_server_configs(
            frozen_inputs,
            user_facing=user_facing,
            bare=bare,
        )
        effective = _effective_mcp_configs(
            frozen_inputs,
            explicit=explicit,
            runtime=runtime,
            launcher=launcher,
            integrations_enabled=installation_profile.integrations_enabled(),
        )
        delivery_maps = {"effective": effective}
    else:
        runtime = extension_store.runtime_mcp_server_configs(
            frozen_inputs,
            user_facing=user_facing,
            bare=bare,
        )
        launcher = extension_store.native_mcp_launcher_server_configs(
            frozen_inputs,
            user_facing=user_facing,
            bare=bare,
        )
        if runner == "better_agent_runner":
            effective = _effective_mcp_configs(
                frozen_inputs,
                explicit=explicit,
                runtime=runtime,
                launcher=launcher,
                integrations_enabled=(
                    installation_profile.integrations_enabled()
                ),
            )
            delivery_maps = {"effective": effective}
        else:
            native = extension_store.native_mcp_server_configs(
                frozen_inputs,
                user_facing=user_facing,
                bare=bare,
            )
            delivery_maps = {
                "explicit": explicit,
                "runtime": runtime,
                "native": native,
                "launcher": launcher,
            }
    names = sorted({
        name
        for configs in delivery_maps.values()
        for name in configs
    })
    servers: list[dict[str, Any]] = []
    all_tools: set[str] = set()
    for name in names:
        variants = {
            delivery: configs[name]
            for delivery, configs in delivery_maps.items()
            if name in configs
        }
        tool_names = sorted({
            tool_name
            for config in variants.values()
            for tool_name in _tool_names(config)
        })
        all_tools.update(tool_names)
        servers.append({
            "name": name,
            "transport": _server_transport(list(variants.values())),
            "config": {
                delivery: _config_without_tool_metadata(
                    config,
                    path=f"mcp_servers.{name}.{delivery}",
                )
                for delivery, config in variants.items()
            },
            "tool_names": tool_names,
            "prewarm": {
                "eligible": name in warm_pool_names,
                "readiness_required": name in warm_pool_names,
            },
        })
    for key in ("resolved_tool_names", "tool_names"):
        raw_tools = frozen_inputs.get(key)
        if isinstance(raw_tools, list):
            all_tools.update(
                str(item).strip()
                for item in raw_tools
                if isinstance(item, str) and str(item).strip()
            )

    launcher_projection = harness_run_projection.launcher_projection(frozen_inputs)
    overlays = launcher_projection.get("extension_setting_overlays")
    overlays = overlays if type(overlays) is dict else {}
    extensions = [
        projected
        for record in extension_store.list_extensions(include_hidden=True)
        if (
            projected := _extension_projection(record, overlays, None)
        ) is not None
    ]
    extensions.sort(key=lambda item: item["id"])
    native_grants = extension_store.resolve_native_mcp_servers_for_context(
        project_path=frozen_inputs.get("cwd"),
        session_id=frozen_inputs.get("app_session_id"),
        root_id=frozen_inputs.get("root_id"),
        turn_id=frozen_inputs.get("turn_run_id"),
    )
    harness = frozen_inputs.get("resolved_harness_run_config")
    harness = harness if type(harness) is dict else {}
    resolved_plan = normalize_plan({
        "harness": harness,
        "tools": sorted(all_tools),
        "mcp_servers": servers,
    })
    if _contains_unavailable_runtime_secret(resolved_plan):
        raise ExecutionContractError(
            "runtime plan secret lacks typed authority",
        )
    extension_state = {
        "store_fingerprint": list(extension_store.store_fingerprint()),
        "settings_fingerprint": list(
            extension_store.extension_settings_fingerprint(),
        ),
        "native_grants": _secret_free(
            native_grants,
            path="native_grants",
        ),
        "extensions": extensions,
    }
    installation_decisions = {
        "profile": _secret_free(
            installation_profile.load(),
            path="installation.profile",
        ),
        "capabilities": _secret_free(
            installation_profile.capabilities(),
            path="installation.capabilities",
        ),
    }
    return {
        "resolved_plan": resolved_plan,
        "extension_state": json.loads(frozen_json(
            extension_state,
            label="extension capability state",
        )),
        "installation_decisions": json.loads(frozen_json(
            installation_decisions,
            label="installation profile decisions",
        )),
    }


def structural_provider_runtime_plan(
    inputs: dict[str, Any],
    provider_kind: str,
) -> dict[str, dict[str, Any]]:
    return _structural_provider_runtime_plan(
        inputs,
        provider_kind,
    )


def hydrate_frozen_provider_runtime_plan(
    plan: Mapping[str, Any],
) -> dict[str, Any]:
    frozen = normalize_plan(
        _json_copy(plan, label="runtime capability plan"),
    )
    references: dict[str, dict[str, str]] = {}

    def collect(value: Any) -> None:
        if (
            type(value) is dict
            and set(value) == {"kind", "extension_id"}
            and value.get("kind") == "extension_identity"
        ):
            references[value["extension_id"]] = value
            return
        if type(value) is list:
            for item in value:
                collect(item)
            return
        if type(value) is dict:
            for item in value.values():
                collect(item)

    collect(frozen)
    hydration: dict[str, str] = {}
    if references:
        import extension_token_registry

        for extension_id, reference in references.items():
            capture_runtime_hydration(
                hydration,
                reference,
                extension_token_registry.mint(extension_id),
            )
    return apply_runtime_hydration(frozen, hydration)


def hydrate_runner_operation_broker(
    plan: Mapping[str, Any],
    address: str,
) -> dict[str, Any]:
    prefix, separator, target = (
        address.partition(":")
        if type(address) is str
        else ("", "", "")
    )
    if (
        not separator
        or prefix not in {"unix", "pipe"}
        or not target
    ):
        raise ExecutionContractError("runner operation broker is unavailable")

    def hydrate(value: Any) -> Any:
        if value == _RUNNER_OPERATION_BROKER_REF:
            return address
        if type(value) is list:
            return [hydrate(item) for item in value]
        if type(value) is dict:
            return {key: hydrate(item) for key, item in value.items()}
        return value

    return hydrate(_json_copy(plan, label="runtime capability plan"))


__all__ = [
    "hydrate_frozen_provider_runtime_plan",
    "hydrate_runner_operation_broker",
    "selected_runtime_agent_sources",
    "selected_runtime_skill_sources",
    "structural_provider_runtime_plan",
]
