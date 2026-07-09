from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from better_agent_sdk import BetterAgentError, Client

_ACTIONS = {
    ("GET", ""): "state.get",
    ("GET", "capability-picker"): "capability-picker.get",
    ("GET", "settings"): "settings.get",
    ("PATCH", "settings"): "settings.patch",
    ("GET", "repository"): "repository.get",
    ("POST", "repository/init"): "repository.init",
    ("POST", "repository/load"): "repository.load",
    ("POST", "repository/sync"): "repository.sync",
    ("PUT", "file"): "file.put",
    ("POST", "file/restore"): "file.restore",
    ("DELETE", "capability"): "capability.delete",
    ("POST", "capability"): "capability.create",
    ("POST", "capability/transfer"): "capability.transfer",
    ("POST", "apply"): "apply",
    ("POST", "auto-sync"): "auto-sync",
    ("POST", "unified-capability-item"): "unified-item.upsert",
    ("DELETE", "unified-capability-item"): "unified-item.remove",
}


async def _proxy(request: Request, path: str = "") -> JSONResponse:
    action = _ACTIONS.get((request.method, path.strip("/")))
    if action is None:
        raise HTTPException(status_code=404, detail="unknown provider config sync action")
    payload: dict[str, Any]
    if request.method == "GET":
        payload = dict(request.query_params)
    else:
        raw_body = await request.body()
        if not raw_body:
            payload = {}
        else:
            try:
                decoded = await request.json()
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="request body must be valid JSON") from exc
            if not isinstance(decoded, dict):
                raise HTTPException(status_code=400, detail="request body must be an object")
            payload = decoded
    try:
        result = Client().invoke_capability(
            "provider-config-sync",
            action,
            payload,
            timeout=60.0,
        )
    except BetterAgentError as exc:
        raise HTTPException(status_code=502, detail=f"provider config sync core unreachable: {exc}") from exc
    return JSONResponse(result)


def create_router(_context):
    router = APIRouter()

    @router.get("/health")
    def health() -> dict[str, bool]:
        return {"ok": True}

    @router.api_route("/", methods=["GET"])
    async def root(request: Request) -> JSONResponse:
        return await _proxy(request)

    @router.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def any_provider_config_sync_route(request: Request, path: str) -> JSONResponse:
        return await _proxy(request, path)

    return router
