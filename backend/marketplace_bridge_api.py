from __future__ import annotations

import asyncio
import hmac
from collections.abc import Awaitable, Callable
from typing import Annotated, Literal

from fastapi import APIRouter, Header, HTTPException, Path
from pydantic import BaseModel, ConfigDict, Field

import marketplace_bridge

router = APIRouter(tags=["marketplace-bridge"])
_internal_token: Callable[[], str] | None = None
_broadcast: Callable[[int], Awaitable[None]] | None = None
_broadcast_extensions: Callable[[], Awaitable[None]] | None = None


class PairActivation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: str = Field(min_length=43, max_length=43, pattern=r"^[A-Za-z0-9_-]{43}$")
    version: Literal[1]


def configure(
    internal_token: Callable[[], str],
    broadcast: Callable[[int], Awaitable[None]],
    broadcast_extensions: Callable[[], Awaitable[None]],
) -> None:
    global _internal_token, _broadcast, _broadcast_extensions
    _internal_token = internal_token
    _broadcast = broadcast
    _broadcast_extensions = broadcast_extensions


async def start() -> None:
    if _broadcast is None or _broadcast_extensions is None:
        raise RuntimeError("marketplace bridge API is not configured")
    await marketplace_bridge.bridge.start(_broadcast, _broadcast_extensions)


async def stop() -> None:
    await marketplace_bridge.bridge.stop()


def _require_internal(token: str) -> None:
    expected = _internal_token() if _internal_token is not None else ""
    if not expected or not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=403, detail="invalid internal token")


def _bridge_error(exc: ValueError) -> HTTPException:
    message = str(exc)
    if "not found" in message:
        status = 404
    elif "invalid" in message:
        status = 400
    else:
        status = 409
    return HTTPException(status_code=status, detail=str(exc))


@router.get("/api/marketplace-bridge")
async def get_marketplace_bridge():
    try:
        return await asyncio.to_thread(marketplace_bridge.bridge.snapshot)
    except ValueError as exc:
        raise _bridge_error(exc) from exc


@router.post("/api/internal/marketplace-bridge/pair")
async def activate_marketplace_pair(
    body: PairActivation,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    _require_internal(x_internal_token)
    try:
        return await marketplace_bridge.bridge.activate_pair(body.intent, body.version)
    except ValueError as exc:
        raise _bridge_error(exc) from exc


@router.post("/api/marketplace-bridge/intents/{intent_id}/approve")
async def approve_marketplace_intent(
    intent_id: Annotated[
        str,
        Path(
            min_length=37,
            max_length=39,
            pattern=r"^(?:pair_[a-f0-9]{32}|baact_[a-f0-9]{32})$",
        ),
    ],
):
    try:
        return await marketplace_bridge.bridge.approve(intent_id)
    except ValueError as exc:
        raise _bridge_error(exc) from exc


@router.post("/api/marketplace-bridge/intents/{intent_id}/reject")
async def reject_marketplace_intent(
    intent_id: Annotated[
        str,
        Path(
            min_length=37,
            max_length=39,
            pattern=r"^(?:pair_[a-f0-9]{32}|baact_[a-f0-9]{32})$",
        ),
    ],
):
    try:
        return await marketplace_bridge.bridge.reject(intent_id)
    except ValueError as exc:
        raise _bridge_error(exc) from exc


@router.delete("/api/marketplace-bridge/devices/{device_id}")
async def revoke_marketplace_device(
    device_id: Annotated[
        str,
        Path(
            min_length=38,
            max_length=38,
            pattern=r"^badvc_[a-f0-9]{32}$",
        ),
    ],
):
    try:
        return await marketplace_bridge.bridge.revoke(device_id)
    except marketplace_bridge.MarketplaceBridgeError as exc:
        raise _bridge_error(exc) from exc
