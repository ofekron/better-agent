from __future__ import annotations

import json
import urllib.parse
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from better_agent_sdk import BetterAgentError, Client

_CORE_PREFIX = "/api/internal/provider-config-sync"


def _core_path(path: str) -> str:
    safe_path = urllib.parse.quote(path, safe="/")
    return f"{_CORE_PREFIX}/{safe_path}" if safe_path else _CORE_PREFIX


async def _proxy(request: Request, path: str = "") -> JSONResponse:
    raw_body = await request.body()
    try:
        status, raw = Client().request_internal(
            request.method,
            _core_path(path),
            body=raw_body or None,
            query=request.url.query,
            timeout=60.0,
        )
    except BetterAgentError as exc:
        raise HTTPException(status_code=502, detail=f"provider config sync core unreachable: {exc}") from exc
    if not raw:
        return JSONResponse({}, status_code=status)
    try:
        payload: Any = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=502, detail="provider config sync core returned non-JSON") from exc
    return JSONResponse(payload, status_code=status)


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
