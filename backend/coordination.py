from __future__ import annotations

import asyncio
import secrets
import time
from typing import Any

_LOCK_TTL_SECONDS = 3 * 60

_locks: dict[str, dict[str, Any]] = {}
_locks_guard = asyncio.Lock()


def _now() -> float:
    return time.monotonic()


async def lock_ops(
    *,
    key: str,
    release: bool = False,
    holder_token: str = "",
) -> dict[str, Any]:
    key = (key or "").strip()
    holder_token = (holder_token or "").strip()
    if not key:
        return {"success": False, "error": "key_required"}

    async with _locks_guard:
        now = _now()
        rec = _locks.get(key)
        if rec and float(rec.get("expires_at") or 0) <= now:
            rec = None
            _locks.pop(key, None)

        if release:
            if not holder_token:
                return {"success": False, "error": "holder_token_required"}
            if not rec:
                return {"success": False, "error": "not_locked"}
            if not secrets.compare_digest(str(rec.get("holder_token") or ""), holder_token):
                return {"success": False, "error": "invalid_holder_token"}
            _locks.pop(key, None)
            return {"success": True, "released": True, "key": key}

        if rec:
            return {
                "success": False,
                "error": "locked",
                "key": key,
                "expires_in_seconds": max(0, int(float(rec["expires_at"]) - now)),
            }

        token = secrets.token_urlsafe(32)
        expires_at = now + _LOCK_TTL_SECONDS
        _locks[key] = {"holder_token": token, "expires_at": expires_at}
        return {
            "success": True,
            "key": key,
            "holder_token": token,
            "expires_in_seconds": _LOCK_TTL_SECONDS,
        }
