from __future__ import annotations

import asyncio
import json
from urllib.parse import quote

import extension_backend_loader
import extension_store
import marketplace_auth
import marketplace_protocol_transport
import password_manager
from env_compat import get_env
from fastapi import HTTPException
from marketplace_protocol import PROTOCOL_HASH


def _response_json(response) -> dict:
    try:
        payload = json.loads(bytes(response.body))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=502, detail="marketplace backend returned invalid JSON"
        ) from exc
    if not 200 <= response.status_code < 300:
        detail = payload.get("detail") if isinstance(payload, dict) else None
        raise HTTPException(
            status_code=response.status_code,
            detail=str(detail or "marketplace request failed"),
        )
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=502, detail="marketplace backend returned an invalid response"
        )
    return payload


async def _invoke(
    path: str,
    *,
    method: str = "GET",
    body: dict | None = None,
) -> dict:
    body_bytes = (
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if body is not None
        else b""
    )
    response = await extension_backend_loader.invoke_extension_backend(
        extension_store.MARKETPLACE_EXTENSION_ID,
        path,
        method=method,
        body_bytes=body_bytes,
        # This call has no incoming Request to derive base_url from (unlike
        # dispatch_extension_backend_request). Without it, the spawned
        # extension subprocess's own self-referential Client() calls (e.g.
        # its credential-store lookup) fall back to the hardcoded
        # http://localhost:8000 default — wrong on any backend not bound to
        # that exact port. Same fallback session_event_extensions.py uses.
        base_url=get_env("BETTER_CLAUDE_BACKEND_URL", "http://localhost:8000"),
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


async def install_exact(
    extension_id: str,
    metadata: dict,
    *,
    expected_version: str,
    entitlement_token: str = "",
) -> dict:
    metadata_id = str(metadata.get("extension_id") or metadata.get("id") or "")
    metadata_version = str(metadata.get("version") or "")
    if metadata_id != extension_id or metadata_version != expected_version:
        raise HTTPException(
            status_code=409,
            detail="marketplace target does not match approved action",
        )
    prepared = await asyncio.to_thread(
        extension_store.prepare_marketplace_install,
        extension_id,
        metadata,
    )
    return await install_preview(
        extension_id,
        str(prepared["preview_token"]),
        entitlement_token=entitlement_token,
    )


async def update_exact(
    extension_id: str,
    metadata: dict,
    *,
    expected_version: str,
) -> dict:
    return await asyncio.to_thread(
        extension_store.apply_marketplace_update_metadata,
        extension_id,
        metadata,
        expected_version=expected_version,
    )


async def uninstall(extension_id: str) -> None:
    await asyncio.to_thread(
        extension_store.require_extension_source,
        extension_id,
        "marketplace",
    )
    await asyncio.to_thread(
        extension_store.uninstall,
        extension_id,
        required_source_type="marketplace",
    )


async def _protocol_access_token() -> str:
    ready = await _invoke("auth/protocol-ready", method="POST")
    if ready != {"authenticated": True}:
        raise HTTPException(status_code=401, detail="marketplace login required")
    try:
        raw = await asyncio.to_thread(
            password_manager.get_service_password,
            marketplace_auth.service_name(),
            marketplace_auth.AUTH_ACCOUNT,
        )
        tokens = json.loads(raw)
    except (password_manager.PasswordManagerError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=401, detail="marketplace login required"
        ) from exc
    access_token = tokens.get("access_token") if isinstance(tokens, dict) else None
    if not isinstance(access_token, str) or not access_token:
        raise HTTPException(status_code=401, detail="marketplace login required")
    return access_token


async def _protocol_request(
    operation: str,
    path_values: dict[str, str],
    body: dict,
) -> dict:
    access_token = await _protocol_access_token()
    return await asyncio.to_thread(
        marketplace_protocol_transport.request,
        operation,
        path_values,
        access_token=access_token,
        body=body,
    )


def protocol_origin() -> str:
    return marketplace_protocol_transport.origin()


async def protocol_pair_context(pair_token: str) -> dict:
    return await _protocol_request(
        "pair_context",
        {},
        {
            "pair_token": pair_token,
            "protocol_hash": PROTOCOL_HASH,
        },
    )


async def protocol_pair(body: dict) -> dict:
    return await _protocol_request("pair_redeem", {}, body)


async def protocol_pair_reject(pair_token: str) -> dict:
    return await _protocol_request(
        "pair_reject",
        {},
        {"pair_token": pair_token, "protocol_hash": PROTOCOL_HASH},
    )


async def protocol_challenges(device_id: str) -> dict:
    return await _protocol_request(
        "device_challenges",
        {"device_id": device_id},
        {"protocol_hash": PROTOCOL_HASH},
    )


async def protocol_lease(device_id: str, body: dict) -> dict:
    return await _protocol_request(
        "lease",
        {"device_id": device_id},
        body,
    )


async def protocol_fence(device_id: str, action_id: str, body: dict) -> dict:
    return await _protocol_request(
        "fence",
        {"device_id": device_id, "action_id": action_id},
        body,
    )


async def protocol_ack(device_id: str, action_id: str, body: dict) -> dict:
    return await _protocol_request(
        "ack",
        {"device_id": device_id, "action_id": action_id},
        body,
    )


async def protocol_projection(device_id: str, body: dict) -> dict:
    return await _protocol_request(
        "projection",
        {"device_id": device_id},
        body,
    )


async def protocol_revoke(device_id: str, body: dict) -> dict:
    return await _protocol_request(
        "revoke",
        {"device_id": device_id},
        body,
    )


async def protocol_action_metadata(
    action_id: str,
    terminal_capability: str,
) -> dict:
    return await _protocol_request(
        "action_metadata",
        {"action_id": action_id},
        {
            "protocol_hash": PROTOCOL_HASH,
            "terminal_capability": terminal_capability,
        },
    )
