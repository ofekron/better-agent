from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from better_agent_sdk import Client


class SessionBridgeBackendClient:
    def __init__(self, client: Client | None = None) -> None:
        self._client = client or Client()

    def resolve_delegation(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._client.invoke_capability(
            "session-bridge", "delegation.resolve", payload, timeout=10.0,
        )


def _raise_loopback_error(result: dict[str, Any]) -> None:
    if result.get("success") is not False:
        return
    status = result.get("status")
    if not isinstance(status, int) or status < 400 or status > 599:
        status = 500
    raise HTTPException(status_code=status, detail=result.get("error") or "session bridge request failed")


def create_router(_context):
    router = APIRouter()

    @router.get("/health")
    def health() -> dict[str, bool]:
        return {"ok": True}

    @router.post("/delegate/{delegation_id}/resolve")
    def resolve_delegation(delegation_id: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = dict(body or {})
        payload["delegation_id"] = delegation_id
        result = SessionBridgeBackendClient().resolve_delegation(payload)
        _raise_loopback_error(result)
        return result

    return router
