from __future__ import annotations

import json
import sys
from typing import Any, Callable


def main() -> int:
    try:
        _apply_posix_limits()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    import requirement_context

    actions: dict[str, Callable[..., dict[str, Any]]] = {
        "unit_rg": requirement_context.search_requirements,
        "unit_fts": requirement_context.search_requirement_units_fts,
        "unit_vector": requirement_context.search_requirement_units_vector,
        "index_sql": requirement_context.run_native_index_sql,
    }
    request = json.load(sys.stdin)
    action = request.get("action")
    kwargs = request.get("kwargs")
    if action not in actions or not isinstance(kwargs, dict):
        raise ValueError("invalid requirements search worker request")
    result = actions[action](**kwargs)
    json.dump(result, sys.stdout, ensure_ascii=False, separators=(",", ":"))
    return 0


def _apply_posix_limits() -> None:
    if sys.platform == "win32":
        return
    import os
    import resource

    cpu_seconds = int(os.environ["BETTER_AGENT_SEARCH_CPU_SECONDS"])
    inherited_cpu_soft, inherited_cpu_hard = resource.getrlimit(resource.RLIMIT_CPU)
    cpu_soft_limit = _bounded_limit(cpu_seconds, inherited_cpu_hard)
    cpu_hard_limit = _bounded_limit(cpu_seconds + 1, inherited_cpu_hard)
    _set_posix_limit(
        resource,
        resource.RLIMIT_CPU,
        "CPU",
        cpu_soft_limit,
        cpu_hard_limit,
        inherited_cpu_soft,
        inherited_cpu_hard,
    )


def _bounded_limit(configured: int, inherited_hard: int) -> int:
    import resource

    if inherited_hard == resource.RLIM_INFINITY:
        return configured
    return min(configured, inherited_hard)


def _set_posix_limit(
    resource_module: Any,
    limit: int,
    resource_name: str,
    requested_soft: int,
    requested_hard: int,
    inherited_soft: int,
    inherited_hard: int,
) -> None:
    try:
        resource_module.setrlimit(limit, (requested_soft, requested_hard))
    except (OSError, ValueError) as exc:
        limit_name = _resource_limit_name(resource_module, limit)
        requested = _format_limit_pair(
            resource_module, requested_soft, requested_hard
        )
        inherited = _format_limit_pair(
            resource_module, inherited_soft, inherited_hard
        )
        raise RuntimeError(
            f"failed to apply POSIX {resource_name} limit ({limit_name}): "
            f"requested {requested}; inherited {inherited}; system error: {exc}"
        ) from exc


def _resource_limit_name(resource_module: Any, limit: int) -> str:
    for name, value in vars(resource_module).items():
        if name.startswith("RLIMIT_") and value == limit:
            return name
    return f"resource limit {limit}"


def _format_limit_pair(resource_module: Any, soft: int, hard: int) -> str:
    return (
        f"soft={_format_limit_value(resource_module, soft)} "
        f"hard={_format_limit_value(resource_module, hard)}"
    )


def _format_limit_value(resource_module: Any, value: int) -> str:
    if value == resource_module.RLIM_INFINITY or value < 0:
        return "unlimited"
    return f"{value}s"


if __name__ == "__main__":
    raise SystemExit(main())
