from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from better_agent_sdk import Client


def create_router(_context):
    router = APIRouter()

    @router.get("/health")
    def health() -> dict[str, bool]:
        return {"ok": True}

    @router.post("/sessions/search")
    def search_sessions(body: dict[str, Any]) -> dict[str, Any]:
        return Client().call_internal(
            "/api/internal/ask-ui/search-sessions",
            body,
            timeout=24 * 60 * 60,
        )

    @router.post("/ask/ensure")
    def ensure_ask(body: dict[str, Any] | None = None) -> dict[str, Any]:
        return Client().call_internal(
            "/api/internal/ask-ui/ensure",
            body or {},
            timeout=10.0,
        )

    return router
