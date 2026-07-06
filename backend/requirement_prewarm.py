"""Startup warm-up for the get-requirements processor.

Warms the processor's provisioned base session off the query path, so a spec
version bump or backend restart is paid here instead of inside a query's
dispatch budget. Fired fire-and-forget from backend startup.
"""
from __future__ import annotations

import logging
from typing import Any

import requirement_context

logger = logging.getLogger(__name__)


async def run_requirements_prewarm(reason: str = "manual") -> dict[str, Any]:
    """Fail-soft: the requirements extension may be inactive or the provider
    unavailable; a skipped prewarm only means the first query pays the warm."""
    try:
        from provisioning.config import resolve_config
        from provisioning.manager import ensure_warm_base

        spec = requirement_context.get_requirements_processor_spec()
        cfg = resolve_config(spec)
        base_session_id = await ensure_warm_base(spec, cfg)
        return {"success": True, "base_session_id": base_session_id}
    except Exception as exc:
        logger.info("requirements processor base prewarm skipped (%s): %s", reason, exc)
        return {"success": False, "error": str(exc)}
