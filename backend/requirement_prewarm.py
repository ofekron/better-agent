"""Startup warm-up for the get-requirements processor.

Warms the processor's provisioned base session off the query path, so a spec
version bump or backend restart is paid here instead of inside a query's
dispatch budget. Fired fire-and-forget from backend startup.
"""
from __future__ import annotations

import logging
import asyncio
from typing import Any

import requirement_context

logger = logging.getLogger(__name__)
_projection_ready_task: asyncio.Task[None] | None = None
_projection_ready_loop: asyncio.AbstractEventLoop | None = None


async def ensure_requirements_projection_ready() -> None:
    global _projection_ready_task, _projection_ready_loop
    loop = asyncio.get_running_loop()
    task = _projection_ready_task
    failed = bool(
        task is not None
        and task.done()
        and (task.cancelled() or task.exception() is not None)
    )
    if task is None or failed or _projection_ready_loop is not loop:
        task = loop.create_task(
            asyncio.to_thread(requirement_context.prewarm_requirements_read_model),
            name="requirements-projection-ready",
        )
        _projection_ready_task = task
        _projection_ready_loop = loop
    await asyncio.shield(task)


def reset_requirements_projection_readiness() -> None:
    global _projection_ready_task, _projection_ready_loop
    _projection_ready_task = None
    _projection_ready_loop = None


async def run_requirements_prewarm(reason: str = "manual") -> dict[str, Any]:
    """Fail-soft: the requirements extension may be inactive or the provider
    unavailable; a skipped prewarm only means the first query pays the warm."""
    try:
        from provisioning.config import resolve_config
        from provisioning.manager import ensure_warm_base

        async def warm_processor() -> str:
            def resolve_processor():
                spec = requirement_context.get_requirements_processor_spec()
                return spec, resolve_config(spec)

            spec, cfg = await asyncio.to_thread(resolve_processor)
            return await ensure_warm_base(spec, cfg)

        _, base_session_id = await asyncio.gather(
            ensure_requirements_projection_ready(),
            warm_processor(),
        )
        return {"success": True, "base_session_id": base_session_id}
    except Exception as exc:
        logger.info("requirements processor base prewarm skipped (%s): %s", reason, exc)
        return {"success": False, "error": str(exc)}
