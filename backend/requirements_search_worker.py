from __future__ import annotations

import json
import sys
from typing import Any, Callable

def main() -> int:
    _apply_posix_limits()
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

    memory_bytes = int(os.environ["BETTER_AGENT_SEARCH_MEMORY_BYTES"])
    cpu_seconds = int(os.environ["BETTER_AGENT_SEARCH_CPU_SECONDS"])
    _, inherited_memory_hard = resource.getrlimit(resource.RLIMIT_AS)
    _, inherited_cpu_hard = resource.getrlimit(resource.RLIMIT_CPU)
    memory_limit = _bounded_limit(memory_bytes, inherited_memory_hard)
    cpu_soft_limit = _bounded_limit(cpu_seconds, inherited_cpu_hard)
    cpu_hard_limit = _bounded_limit(cpu_seconds + 1, inherited_cpu_hard)
    resource.setrlimit(resource.RLIMIT_AS, (memory_limit, memory_limit))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_soft_limit, cpu_hard_limit))


def _bounded_limit(configured: int, inherited_hard: int) -> int:
    import resource

    if inherited_hard == resource.RLIM_INFINITY:
        return configured
    return min(configured, inherited_hard)


if __name__ == "__main__":
    raise SystemExit(main())
