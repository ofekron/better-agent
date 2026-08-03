from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PreparedMcpPrewarm:
    ready_map: dict[str, str | dict[str, Any]]
    status: dict[str, dict[str, str | None]]


def prepare_runtime_mcp_prewarm(
    inputs: dict[str, Any],
    app_session_id: str,
    *,
    bound_seconds: float,
) -> PreparedMcpPrewarm:
    from extension_store import runtime_mcp_prewarm_targets
    from mcp_prewarm import supervisor

    targets = runtime_mcp_prewarm_targets(inputs)
    if not targets:
        return PreparedMcpPrewarm(ready_map={}, status={})

    async def ready(target: dict[str, Any]) -> tuple[str, Any]:
        fingerprint = supervisor.compute_fingerprint(
            target["real_config"], target["extension_record"]
        )
        try:
            result = await supervisor.ensure_daemon_ready(
                app_session_id,
                target["extension_id"],
                target["server_name"],
                target["real_config"],
                fingerprint,
                bound_seconds=bound_seconds,
            )
        except Exception:
            logger.exception(
                "warm_pool readiness failed for %s/%s",
                target["extension_id"],
                target["server_name"],
            )
            return target["server_name"], None
        return target["server_name"], result

    async def gather() -> list[tuple[str, Any]]:
        return await asyncio.gather(*(ready(target) for target in targets))

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        results = asyncio.run(gather())
    else:
        raise RuntimeError("warm_pool preparation must run off the event loop")

    failed = [name for name, result in results if result is None or not result.ready]
    if failed:
        raise RuntimeError(
            "warm_pool MCP server failed to become ready: " + ", ".join(sorted(failed))
        )
    return PreparedMcpPrewarm(
        ready_map={name: result.ready_map_value() for name, result in results if result},
        status={name: {"status": "ready", "error": None} for name, _result in results},
    )
