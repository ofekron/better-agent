from __future__ import annotations

import asyncio
import secrets
import time
from typing import Any

_LOCK_TTL_SECONDS = 3 * 60
_MULTI_LOCK_POLL_SECONDS = 0.1
_DEFAULT_MULTI_LOCK_TIMEOUT_SECONDS = 10.0
_MAX_MULTI_LOCK_TIMEOUT_SECONDS = 60.0

_locks: dict[str, dict[str, Any]] = {}
_locks_guard = asyncio.Lock()


def _now() -> float:
    return time.monotonic()


def _normalize_keys(key: str, keys: list[str] | None) -> tuple[str, list[str]]:
    if keys is None:
        normalized_key = (key or "").strip()
        return normalized_key, [normalized_key] if normalized_key else []

    normalized_keys: list[str] = []
    seen: set[str] = set()
    for raw_key in keys:
        normalized_key = str(raw_key or "").strip()
        if not normalized_key:
            continue
        if normalized_key in seen:
            continue
        seen.add(normalized_key)
        normalized_keys.append(normalized_key)
    return normalized_keys[0] if len(normalized_keys) == 1 else "", normalized_keys


def _clamp_timeout(timeout_seconds: float | int | None) -> float | None:
    if timeout_seconds is None:
        return _DEFAULT_MULTI_LOCK_TIMEOUT_SECONDS
    try:
        timeout = float(timeout_seconds)
    except (TypeError, ValueError):
        return None
    if timeout <= 0:
        return 0
    return min(timeout, _MAX_MULTI_LOCK_TIMEOUT_SECONDS)


def _expire_locks(now: float, keys: list[str]) -> None:
    for key in keys:
        rec = _locks.get(key)
        if rec and float(rec.get("expires_at") or 0) <= now:
            _locks.pop(key, None)


def _held_by_other(key: str, token: str) -> dict[str, Any] | None:
    rec = _locks.get(key)
    if not rec:
        return None
    if secrets.compare_digest(str(rec.get("holder_token") or ""), token):
        return None
    return rec


async def _release_keys(keys: list[str], holder_token: str) -> dict[str, Any]:
    if not holder_token:
        return {"success": False, "error": "holder_token_required"}

    async with _locks_guard:
        now = _now()
        _expire_locks(now, keys)
        locked = [key for key in keys if key in _locks]
        if not locked:
            return {"success": False, "error": "not_locked"}

        for key in locked:
            rec = _locks[key]
            if not secrets.compare_digest(str(rec.get("holder_token") or ""), holder_token):
                return {"success": False, "error": "invalid_holder_token", "key": key}

        for key in locked:
            _locks.pop(key, None)

    return {"success": True, "released": True, "key": keys[0], "keys": keys}


async def _acquire_keys(keys: list[str], timeout_seconds: float) -> dict[str, Any]:
    token = secrets.token_urlsafe(32)
    expires_at = _now() + _LOCK_TTL_SECONDS
    acquired: set[str] = set()
    start = _now()
    deadline = start + timeout_seconds
    waited = False

    while True:
        async with _locks_guard:
            now = _now()
            _expire_locks(now, keys)
            blocked: dict[str, Any] | None = None
            for lock_key in sorted(keys):
                rec = _held_by_other(lock_key, token)
                if rec:
                    if blocked is None:
                        blocked = {
                            "key": lock_key,
                            "expires_in_seconds": max(0, int(float(rec["expires_at"]) - now)),
                        }
                    continue
                if lock_key not in acquired:
                    _locks[lock_key] = {"holder_token": token, "expires_at": expires_at}
                    acquired.add(lock_key)

            if blocked:
                waited = True

            if not blocked:
                return {
                    "success": True,
                    "key": keys[0],
                    "keys": keys,
                    "holder_token": token,
                    "expires_in_seconds": _LOCK_TTL_SECONDS,
                    "waited": waited,
                    "waited_seconds": round(_now() - start, 3),
                }

            if now >= deadline:
                for acquired_key in acquired:
                    rec = _locks.get(acquired_key)
                    if rec and secrets.compare_digest(str(rec.get("holder_token") or ""), token):
                        _locks.pop(acquired_key, None)
                return {
                    "success": False,
                    "error": "timeout",
                    "key": blocked["key"],
                    "keys": keys,
                    "locked_keys": sorted(acquired),
                    "expires_in_seconds": blocked["expires_in_seconds"],
                }

        await asyncio.sleep(min(_MULTI_LOCK_POLL_SECONDS, max(0, deadline - _now())))


async def lock_ops(
    *,
    key: str,
    keys: list[str] | None = None,
    release: bool = False,
    holder_token: str = "",
    timeout_seconds: float | int | None = None,
) -> dict[str, Any]:
    key, normalized_keys = _normalize_keys(key, keys)
    holder_token = (holder_token or "").strip()
    if not normalized_keys:
        return {"success": False, "error": "key_required"}

    if len(normalized_keys) > 1:
        if release:
            return await _release_keys(normalized_keys, holder_token)
        timeout = _clamp_timeout(timeout_seconds)
        if timeout is None:
            return {"success": False, "error": "invalid_timeout_seconds"}
        return await _acquire_keys(normalized_keys, timeout)

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
            "waited": False,
            "waited_seconds": 0.0,
        }
