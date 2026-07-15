"""Pre-send advisory collection.

Core-side seam: before a prompt is sent, the frontend asks core for
advisories; core fans out to every active extension declaring a
``hooks.pre_send_advisory`` backend path and returns the normalized,
validated advisories. Core knows nothing about provider quota
mechanics — extensions own that. An advisory is a signal, never a
gate: any extension failure, timeout, or malformed response is
dropped and must not block sending.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import extension_store
from extension_backend_loader import invoke_extension_backend
from fastapi import HTTPException

log = logging.getLogger(__name__)

_SEVERITIES = {"info", "warn"}
_MAX_TEXT_CHARS = 500
_MAX_ADVISORIES_PER_EXTENSION = 5
# Snappy ceiling for the send hot path, enforced INSIDE the loader as the
# roundtrip timeout (single timeout owner — an outer wait_for would race the
# loader's own per-route timeout, abandon the shielded roundtrip mid-flight,
# and bypass timeout accounting). The extension route reads a cached
# provider_status (60s TTL), so a warm cache returns in milliseconds; a
# cold-cache miss just yields no advisory and the next send picks up the
# warmed cache. Kept under the frontend FETCH_TIMEOUT so a slow extension is
# abandoned here before the frontend gives up.
_PER_EXTENSION_TIMEOUT_SECONDS = 2.0


def _normalize_advisory(raw: Any, extension_id: str) -> dict[str, Any] | None:
    """Validate one extension-supplied advisory; None when malformed."""
    if not isinstance(raw, dict):
        return None
    title = str(raw.get("title") or "").strip()
    if not title:
        return None
    severity = str(raw.get("severity") or "warn").strip()
    if severity not in _SEVERITIES:
        return None
    advisory: dict[str, Any] = {
        "extension_id": extension_id,
        "title": title[:_MAX_TEXT_CHARS],
        "severity": severity,
    }
    detail = str(raw.get("detail") or "").strip()
    if detail:
        advisory["detail"] = detail[:_MAX_TEXT_CHARS]
    usage_percent = raw.get("usage_percent")
    if isinstance(usage_percent, (int, float)) and not isinstance(usage_percent, bool):
        advisory["usage_percent"] = max(0.0, min(100.0, float(usage_percent)))
    resets_at = str(raw.get("resets_at") or "").strip()
    if resets_at:
        advisory["resets_at"] = resets_at[:64]
    source = str(raw.get("source") or "").strip()
    if source:
        advisory["source"] = source[:_MAX_TEXT_CHARS]
    return advisory


async def _collect_one(extension_id: str, path: str, body_bytes: bytes) -> list[dict[str, Any]]:
    try:
        response = await invoke_extension_backend(
            extension_id,
            path,
            body_bytes=body_bytes,
            timeout_ceiling=_PER_EXTENSION_TIMEOUT_SECONDS,
        )
        if response.status_code != 200:
            return []
        payload = json.loads(response.body or b"{}")
    except HTTPException as exc:
        # Defined loader outcome (504 timeout, 503 busy/unavailable) — an
        # expected advisory miss, not a core fault; no traceback needed.
        log.warning(
            "pre_send_advisory: extension %s advisory call failed (HTTP %s: %s); skipping",
            extension_id,
            exc.status_code,
            exc.detail,
        )
        return []
    except Exception:
        log.warning(
            "pre_send_advisory: extension %s advisory call failed; skipping",
            extension_id,
            exc_info=True,
        )
        return []
    raw_items = payload.get("advisories") if isinstance(payload, dict) else None
    if not isinstance(raw_items, list):
        return []
    out: list[dict[str, Any]] = []
    for raw in raw_items[:_MAX_ADVISORIES_PER_EXTENSION]:
        advisory = _normalize_advisory(raw, extension_id)
        if advisory:
            out.append(advisory)
    return out


async def collect_pre_send_advisories(
    app_session_id: str,
    provider_id: str,
    provider_kind: str,
    config_dir: str,
    model: str,
    provider_mode: str = "",
    provider_base_url: str = "",
    provider_name: str = "",
) -> list[dict[str, Any]]:
    hooks = extension_store.pre_send_advisory_hooks()
    if not hooks:
        return []
    body_bytes = json.dumps(
        {
            "app_session_id": app_session_id,
            "provider_id": provider_id,
            "provider_kind": provider_kind,
            "config_dir": config_dir,
            "model": model,
            "provider_mode": provider_mode,
            "provider_base_url": provider_base_url,
            "provider_name": provider_name,
        }
    ).encode()
    results = await asyncio.gather(
        *(_collect_one(extension_id, path, body_bytes) for extension_id, path in hooks)
    )
    return [advisory for items in results for advisory in items]
