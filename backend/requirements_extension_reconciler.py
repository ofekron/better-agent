from __future__ import annotations

import asyncio
import json
import logging

import extension_backend_loader
import extension_store
from event_bus import BusEvent, bus
from provider_runtime_facts import EVENT_TYPE as PROVIDER_RUNTIME_CHANGED

logger = logging.getLogger(__name__)

_SUBSCRIBER = "requirements_extension_reconciler"
_reconcile_lock: asyncio.Lock | None = None
_shutting_down = True


def _lock() -> asyncio.Lock:
    global _reconcile_lock
    if _reconcile_lock is None:
        _reconcile_lock = asyncio.Lock()
    return _reconcile_lock


async def _invoke(reason: str) -> dict:
    response = await extension_backend_loader.invoke_core_backend_contract(
        "requirements.reconcile",
        body_bytes=json.dumps(
            {"reason": reason},
            separators=(",", ":"),
        ).encode("utf-8"),
    )
    try:
        payload = json.loads(bytes(response.body))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("requirements reconcile returned invalid JSON") from exc
    if response.status_code >= 400:
        raise RuntimeError(
            f"requirements reconcile failed with status {response.status_code}"
        )
    if not isinstance(payload, dict) or not isinstance(payload.get("status"), str):
        raise RuntimeError("requirements reconcile returned an invalid result")
    return payload


async def reconcile(reason: str) -> dict:
    if reason not in {"startup", "provider_runtime"}:
        raise ValueError("unsupported requirements reconciliation reason")
    async with _lock():
        if _shutting_down:
            return {"status": "shutting_down"}
        return await _invoke(reason)


async def _on_provider_runtime_changed(_event: BusEvent) -> None:
    await reconcile("provider_runtime")


async def bind_and_reconcile() -> dict:
    global _shutting_down
    _shutting_down = False
    bus.unsubscribe(_SUBSCRIBER)
    bus.subscribe(
        PROVIDER_RUNTIME_CHANGED,
        _on_provider_runtime_changed,
        name=_SUBSCRIBER,
        bind_current_loop=True,
    )
    return await reconcile("startup")


async def shutdown() -> None:
    global _reconcile_lock, _shutting_down
    _shutting_down = True
    bus.unsubscribe(_SUBSCRIBER)
    if _reconcile_lock is not None:
        async with _reconcile_lock:
            pass
    extension_id = extension_store.extension_id_for_role("requirements")
    if extension_id is not None:
        extension_backend_loader.evict_persistent_backend(extension_id)
    _reconcile_lock = None
