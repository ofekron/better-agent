from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter

from better_agent_sdk import BetterAgentError, Client

logger = logging.getLogger(__name__)


def create_router(_context):
    router = APIRouter()

    @router.get("/health")
    def health() -> dict[str, bool]:
        return {"ok": True}

    @router.post("/sessions/search")
    def search_sessions(body: dict[str, Any]) -> dict[str, Any]:
        try:
            return Client().invoke_capability(
                "ask",
                "sessions.search",
                body,
                timeout=24 * 60 * 60,
            )
        except BetterAgentError:
            logger.exception("ask sessions.search: capability call failed")
            return {"results": [], "reasoning": "", "error": "search_failed"}

    @router.post("/ask/ensure")
    def ensure_ask(body: dict[str, Any] | None = None) -> dict[str, Any]:
        return Client().invoke_capability(
            "ask",
            "ensure",
            body or {},
            timeout=10.0,
        )

    return router
