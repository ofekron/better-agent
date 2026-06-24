"""On-demand requirements cache refresh.

Strategy A moved extraction from a background ``user_message_done`` prewarm
into the query path — ``requirement_context.get_processed_requirements``
calls ``prepare_requirements_context`` itself. What remains here is the
startup one-shot warm (builds the initial cache so the first query after a
restart isn't cold) plus the lock-busy guard.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import requirement_context

logger = logging.getLogger(__name__)


async def run_requirements_prewarm(reason: str = "manual") -> dict[str, Any]:
    result = await asyncio.to_thread(
        requirement_context.prepare_requirements_context,
        exclude_latest_prompt=False,
        allowed_unhandled_prompts=0,
    )
    if not _is_prephase_lock_busy(result):
        return result
    logger.info("requirements prewarm skipped: prephase lock busy reason=%s", reason)
    return {
        "success": False,
        "skipped": True,
        "reason": "prephase_lock_busy",
        "freshness": result.get("freshness"),
        "sync": result.get("sync"),
    }


def _is_prephase_lock_busy(result: dict[str, Any]) -> bool:
    freshness = result.get("freshness")
    if not isinstance(freshness, dict):
        return False
    unit_sync = freshness.get("unit_sync")
    if not isinstance(unit_sync, dict):
        return False
    error = unit_sync.get("error")
    return isinstance(error, str) and "requirement unit extraction already running" in error
