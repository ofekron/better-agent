from __future__ import annotations

import asyncio
import json
from urllib.parse import quote

from fastapi import HTTPException

import extension_backend_loader
import extension_store


def _response_json(response) -> dict:
    try:
        payload = json.loads(bytes(response.body))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=502, detail="marketplace backend returned invalid JSON") from exc
    if not 200 <= response.status_code < 300:
        detail = payload.get("detail") if isinstance(payload, dict) else None
        raise HTTPException(status_code=response.status_code, detail=str(detail or "marketplace request failed"))
    if not isinstance(payload, dict):
        raise HTTPException(status_code=502, detail="marketplace backend returned an invalid response")
    return payload


async def _invoke(path: str, *, method: str = "GET") -> dict:
    response = await extension_backend_loader.invoke_extension_backend(
        extension_store.MARKETPLACE_EXTENSION_ID,
        path,
        method=method,
    )
    return _response_json(response)


async def prepare_install(extension_id: str) -> dict:
    metadata = await _invoke(f"metadata/{quote(extension_id, safe='')}")
    return await asyncio.to_thread(
        extension_store.prepare_marketplace_install,
        extension_id,
        metadata,
    )


async def install_preview(
    extension_id: str,
    preview_token: str,
    *,
    entitlement_token: str = "",
) -> dict:
    return await asyncio.to_thread(
        extension_store.install_marketplace_preview,
        extension_id,
        preview_token,
        entitlement_token=entitlement_token,
    )


async def install_direct(extension_id: str, *, entitlement_token: str = "") -> dict:
    prepared = await prepare_install(extension_id)
    return await install_preview(
        extension_id,
        str(prepared["preview_token"]),
        entitlement_token=entitlement_token,
    )


async def uninstall(extension_id: str) -> None:
    await asyncio.to_thread(
        extension_store.require_extension_source,
        extension_id,
        "marketplace",
    )
    await _invoke(f"extensions/{quote(extension_id, safe='')}/uninstall", method="POST")
    await asyncio.to_thread(
        extension_store.uninstall,
        extension_id,
        required_source_type="marketplace",
    )
