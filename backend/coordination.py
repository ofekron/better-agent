from __future__ import annotations

import asyncio
import json
import os
import secrets
import stat
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from json_store import write_json_durable
from paths import bc_home
from portable_lock import lock_ex, unlock

_DEFAULT_LOCK_LEASE_SECONDS = 3 * 60
_MIN_LOCK_LEASE_SECONDS = 5.0
_MAX_LOCK_LEASE_SECONDS = 15 * 60
_MULTI_LOCK_POLL_SECONDS = 0.1
_DEFAULT_MULTI_LOCK_TIMEOUT_SECONDS = 10.0
_MAX_MULTI_LOCK_TIMEOUT_SECONDS = 60.0
_LOCK_STORE_SCHEMA_VERSION = 1
_LOCK_STORE_DIRNAME = "coordination"
_LOCK_STORE_FILENAME = "locks.json"
_LOCK_STORE_LOCK_FILENAME = "locks.json.lock"
_OWNER_FIELD_MAX_CHARS = 512
_OWNER_KEYS = (
    "principal_extension_id",
    "app_session_id",
    "cwd",
    "provider_id",
    "source",
    "pid",
)
_TRUSTED_OWNER_MATCH_KEYS = (
    "principal_extension_id",
    "app_session_id",
    "cwd",
)

_locks: dict[str, dict[str, Any]] = {}
_locks_guard = asyncio.Lock()


def _now() -> float:
    return time.time()


def _lock_store_dir() -> Path:
    return bc_home() / _LOCK_STORE_DIRNAME


def _lock_store_path() -> Path:
    return _lock_store_dir() / _LOCK_STORE_FILENAME


def _lock_store_lock_path() -> Path:
    return _lock_store_dir() / _LOCK_STORE_LOCK_FILENAME


def _assert_regular_or_missing(path: Path) -> None:
    try:
        st = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise RuntimeError(f"coordination lock store path is not a regular file: {path}")


@contextmanager
def _locked_store():
    store_dir = _lock_store_dir()
    store_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = _lock_store_lock_path()
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(lock_path, flags, 0o600)
    try:
        lock_ex(fd)
        _assert_regular_or_missing(_lock_store_path())
        yield
    finally:
        unlock(fd)
        os.close(fd)


def _sync_locks_mirror(records: dict[str, dict[str, Any]]) -> None:
    _locks.clear()
    _locks.update({str(key): dict(value) for key, value in records.items()})


def _load_locks_unlocked() -> None:
    path = _lock_store_path()
    if not path.exists():
        _sync_locks_mirror({})
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"failed to parse coordination lock store: {path}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"coordination lock store has invalid root shape: {path}")
    if data.get("schema_version") != _LOCK_STORE_SCHEMA_VERSION:
        raise RuntimeError(f"coordination lock store has unsupported schema version: {path}")
    records = data.get("locks")
    if not isinstance(records, dict):
        raise RuntimeError(f"coordination lock store has invalid locks shape: {path}")
    normalized: dict[str, dict[str, Any]] = {}
    for raw_key, raw_rec in records.items():
        key = str(raw_key or "").strip()
        if not key:
            continue
        if not isinstance(raw_rec, dict):
            raise RuntimeError(f"coordination lock record has invalid shape for key: {key}")
        normalized[key] = dict(raw_rec)
    _sync_locks_mirror(normalized)


def _save_locks_unlocked() -> None:
    write_json_durable(
        _lock_store_path(),
        {
            "schema_version": _LOCK_STORE_SCHEMA_VERSION,
            "locks": _locks,
        },
    )


def _save_locks_if_changed_unlocked(changed: bool) -> None:
    if changed:
        _save_locks_unlocked()


def _clear_for_tests() -> None:
    with _locked_store():
        _sync_locks_mirror({})
        _save_locks_unlocked()


def _drop_memory_for_tests() -> None:
    _locks.clear()


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


def _clamp_lease(lease_seconds: float | int | None) -> float | None:
    if lease_seconds is None:
        return float(_DEFAULT_LOCK_LEASE_SECONDS)
    try:
        lease = float(lease_seconds)
    except (TypeError, ValueError):
        return None
    if lease <= 0:
        return None
    return min(max(lease, _MIN_LOCK_LEASE_SECONDS), float(_MAX_LOCK_LEASE_SECONDS))


def _remaining_seconds(rec: dict[str, Any], now: float) -> int:
    return max(0, int(float(rec.get("expires_at") or 0) - now))


def _expire_locks(now: float, keys: list[str] | None = None) -> bool:
    changed = False
    scan_keys = list(_locks.keys()) if keys is None else keys
    for key in scan_keys:
        rec = _locks.get(key)
        if rec and float(rec.get("expires_at") or 0) <= now:
            _locks.pop(key, None)
            changed = True
    return changed


def _held_by_other(key: str, token: str) -> dict[str, Any] | None:
    rec = _locks.get(key)
    if not rec:
        return None
    if token and secrets.compare_digest(str(rec.get("holder_token") or ""), token):
        return None
    return rec


def _normalize_owner(owner: dict[str, Any] | None) -> dict[str, str]:
    if not isinstance(owner, dict):
        return {}
    normalized: dict[str, str] = {}
    for field in _OWNER_KEYS:
        value = owner.get(field)
        if value is None:
            continue
        text = str(value).strip()
        if not text:
            continue
        normalized[field] = text[:_OWNER_FIELD_MAX_CHARS]
    return normalized


def _same_trusted_owner(rec: dict[str, Any], owner: dict[str, str]) -> bool:
    rec_owner = rec.get("owner") if isinstance(rec.get("owner"), dict) else {}
    for field in _TRUSTED_OWNER_MATCH_KEYS:
        left = str(rec_owner.get(field) or "").strip()
        right = str(owner.get(field) or "").strip()
        if not left or not right or left != right:
            return False
    rec_provider = str(rec_owner.get("provider_id") or "").strip()
    owner_provider = str(owner.get("provider_id") or "").strip()
    if rec_provider and owner_provider and rec_provider != owner_provider:
        return False
    return True


def _lock_record(token: str, expires_at: float, owner: dict[str, str], created_at: float) -> dict[str, Any]:
    created_at_epoch = time.time()
    return {
        "holder_token": token,
        "expires_at": expires_at,
        "owner": dict(owner),
        "created_at": created_at,
        "renewed_at": created_at,
        "created_at_epoch": created_at_epoch,
        "renewed_at_epoch": created_at_epoch,
    }


def _holder_snapshot(rec: dict[str, Any], now: float) -> dict[str, Any]:
    owner = rec.get("owner") if isinstance(rec.get("owner"), dict) else {}
    created_at = float(rec.get("created_at") or now)
    renewed_at = float(rec.get("renewed_at") or created_at)
    return {
        "owner": dict(owner),
        "created_at": float(rec.get("created_at_epoch") or created_at),
        "renewed_at": float(rec.get("renewed_at_epoch") or renewed_at),
        "age_seconds": max(0, round(now - created_at, 3)),
        "renewed_age_seconds": max(0, round(now - renewed_at, 3)),
    }


def _blocked_payload(lock_key: str, rec: dict[str, Any], now: float) -> dict[str, Any]:
    return {
        "key": lock_key,
        "expires_in_seconds": _remaining_seconds(rec, now),
        "holder": _holder_snapshot(rec, now),
    }


def _success_payload(
    *,
    key: str,
    keys: list[str],
    token: str,
    now: float,
    waited: bool = False,
    waited_seconds: float = 0.0,
    waited_keys: set[str] | None = None,
) -> dict[str, Any]:
    expiries = [float(_locks[item].get("expires_at") or now) for item in keys if item in _locks]
    expires_in = max(0, int(min(expiries) - now)) if expiries else 0
    precise_waited_keys = sorted(waited_keys or set())
    return {
        "success": True,
        "key": key,
        "keys": keys,
        "holder_token": token,
        "expires_in_seconds": expires_in,
        "waited": waited,
        "waited_seconds": round(waited_seconds, 3),
        "waited_keys": precise_waited_keys,
        "blocked_keys": precise_waited_keys,
    }


async def _release_keys(keys: list[str], holder_token: str) -> dict[str, Any]:
    if not holder_token:
        return {"success": False, "error": "holder_token_required"}

    async with _locks_guard:
        with _locked_store():
            _load_locks_unlocked()
            now = _now()
            changed = _expire_locks(now, keys)
            locked = [key for key in keys if key in _locks]
            if not locked:
                _save_locks_if_changed_unlocked(changed)
                return {"success": False, "error": "not_locked"}

            for key in locked:
                rec = _locks[key]
                if not secrets.compare_digest(str(rec.get("holder_token") or ""), holder_token):
                    _save_locks_if_changed_unlocked(changed)
                    return {"success": False, "error": "invalid_holder_token", "key": key}

            for key in locked:
                _locks.pop(key, None)
            changed = True
            _save_locks_unlocked()

    return {"success": True, "released": True, "key": keys[0], "keys": keys}


async def _release_owned_keys(keys: list[str], owner: dict[str, str]) -> dict[str, Any]:
    if not owner:
        return {"success": False, "error": "owner_required"}
    async with _locks_guard:
        with _locked_store():
            _load_locks_unlocked()
            now = _now()
            changed = _expire_locks(now)
            candidate_keys = keys or list(_locks.keys())
            released: list[str] = []
            blocked: list[str] = []
            for lock_key in candidate_keys:
                rec = _locks.get(lock_key)
                if not rec:
                    continue
                if _same_trusted_owner(rec, owner):
                    released.append(lock_key)
                else:
                    blocked.append(lock_key)
            if blocked:
                _save_locks_if_changed_unlocked(changed)
                return {"success": False, "error": "not_lock_owner", "blocked_keys": sorted(blocked)}
            for lock_key in released:
                _locks.pop(lock_key, None)
                changed = True
            _save_locks_if_changed_unlocked(changed)
    return {"success": True, "released": True, "key": released[0] if released else "", "keys": released}


async def _list_owned_locks(owner: dict[str, str]) -> dict[str, Any]:
    if not owner:
        return {"success": False, "error": "owner_required"}
    async with _locks_guard:
        with _locked_store():
            _load_locks_unlocked()
            now = _now()
            changed = _expire_locks(now)
            locks = [
                {
                    "key": lock_key,
                    "expires_in_seconds": _remaining_seconds(rec, now),
                    "holder": _holder_snapshot(rec, now),
                }
                for lock_key, rec in sorted(_locks.items())
                if _same_trusted_owner(rec, owner)
            ]
            _save_locks_if_changed_unlocked(changed)
    return {"success": True, "locks": locks, "keys": [item["key"] for item in locks]}


async def _validate_keys(keys: list[str], holder_token: str) -> dict[str, Any]:
    if not holder_token:
        return {"success": False, "error": "holder_token_required"}
    async with _locks_guard:
        with _locked_store():
            _load_locks_unlocked()
            now = _now()
            changed = _expire_locks(now, keys)
            for lock_key in keys:
                rec = _locks.get(lock_key)
                if not rec:
                    _save_locks_if_changed_unlocked(changed)
                    return {"success": False, "error": "not_locked", "key": lock_key, "keys": keys}
                if not secrets.compare_digest(str(rec.get("holder_token") or ""), holder_token):
                    _save_locks_if_changed_unlocked(changed)
                    return {"success": False, "error": "invalid_holder_token", "key": lock_key, "keys": keys}
            result = _success_payload(key=keys[0], keys=keys, token=holder_token, now=now)
            _save_locks_if_changed_unlocked(changed)
            return result


async def _reattach_keys(keys: list[str], owner: dict[str, str]) -> dict[str, Any]:
    if not owner:
        return {"success": False, "error": "owner_required"}
    async with _locks_guard:
        with _locked_store():
            _load_locks_unlocked()
            now = _now()
            changed = _expire_locks(now, keys)
            tokens: dict[str, str] = {}
            for lock_key in keys:
                rec = _locks.get(lock_key)
                if not rec:
                    _save_locks_if_changed_unlocked(changed)
                    return {"success": False, "error": "not_locked", "key": lock_key, "keys": keys}
                if not _same_trusted_owner(rec, owner):
                    blocked = _blocked_payload(lock_key, rec, now)
                    _save_locks_if_changed_unlocked(changed)
                    return {
                        "success": False,
                        "error": "locked",
                        "key": lock_key,
                        "keys": keys,
                        "blocked_keys": [lock_key],
                        "expires_in_seconds": blocked["expires_in_seconds"],
                        "holder": blocked["holder"],
                    }
                tokens[lock_key] = str(rec.get("holder_token") or "")
            unique_tokens = {token for token in tokens.values() if token}
            if len(unique_tokens) != 1:
                result = {
                    "success": True,
                    "key": keys[0],
                    "keys": keys,
                    "holder_tokens_by_key": tokens,
                    "expires_in_seconds": min(_remaining_seconds(_locks[item], now) for item in keys),
                    "waited": False,
                    "waited_seconds": 0.0,
                    "waited_keys": [],
                    "blocked_keys": [],
                }
                _save_locks_if_changed_unlocked(changed)
                return result
            result = _success_payload(key=keys[0], keys=keys, token=next(iter(unique_tokens)), now=now)
            _save_locks_if_changed_unlocked(changed)
            return result


async def _renew_keys(
    keys: list[str],
    holder_token: str,
    owner: dict[str, str],
    lease_seconds: float,
) -> dict[str, Any]:
    async with _locks_guard:
        with _locked_store():
            _load_locks_unlocked()
            now = _now()
            changed = _expire_locks(now, keys)
            for lock_key in keys:
                rec = _locks.get(lock_key)
                if not rec:
                    _save_locks_if_changed_unlocked(changed)
                    return {"success": False, "error": "not_locked", "key": lock_key, "keys": keys}
                token_matches = bool(holder_token) and secrets.compare_digest(
                    str(rec.get("holder_token") or ""), holder_token
                )
                owner_matches = bool(owner) and _same_trusted_owner(rec, owner)
                if not token_matches and not owner_matches:
                    blocked = _blocked_payload(lock_key, rec, now)
                    _save_locks_if_changed_unlocked(changed)
                    return {
                        "success": False,
                        "error": "invalid_holder_token" if holder_token else "not_lock_owner",
                        "key": lock_key,
                        "keys": keys,
                        "blocked_keys": [lock_key],
                        "expires_in_seconds": blocked["expires_in_seconds"],
                        "holder": blocked["holder"],
                    }
            expires_at = now + lease_seconds
            tokens = {str(_locks[item].get("holder_token") or "") for item in keys}
            renewed_at_epoch = time.time()
            for lock_key in keys:
                _locks[lock_key]["expires_at"] = expires_at
                _locks[lock_key]["renewed_at"] = now
                _locks[lock_key]["renewed_at_epoch"] = renewed_at_epoch
            changed = True
            if len(tokens) == 1:
                result = _success_payload(key=keys[0], keys=keys, token=next(iter(tokens)), now=now)
                _save_locks_unlocked()
                return result
            result = {
                "success": True,
                "key": keys[0],
                "keys": keys,
                "holder_tokens_by_key": {item: str(_locks[item].get("holder_token") or "") for item in keys},
                "expires_in_seconds": int(lease_seconds),
                "waited": False,
                "waited_seconds": 0.0,
                "waited_keys": [],
                "blocked_keys": [],
            }
            _save_locks_unlocked()
            return result


async def _acquire_keys(
    keys: list[str],
    timeout_seconds: float,
    lease_seconds: float,
    owner: dict[str, str],
) -> dict[str, Any]:
    token = secrets.token_urlsafe(32)
    acquired: set[str] = set()
    waited_keys: set[str] = set()
    start = _now()
    deadline = start + timeout_seconds
    waited = False

    while True:
        async with _locks_guard:
            with _locked_store():
                _load_locks_unlocked()
                now = _now()
                changed = _expire_locks(now, keys)
                acquired = {
                    lock_key
                    for lock_key in acquired
                    if (
                        (rec := _locks.get(lock_key))
                        and secrets.compare_digest(str(rec.get("holder_token") or ""), token)
                    )
                }
                blocked: dict[str, Any] | None = None
                blocked_keys: set[str] = set()
                for lock_key in sorted(keys):
                    rec = _held_by_other(lock_key, token)
                    if rec:
                        blocked_keys.add(lock_key)
                        if blocked is None:
                            blocked = _blocked_payload(lock_key, rec, now)
                        continue
                    if lock_key not in acquired:
                        acquired_at = _now()
                        _locks[lock_key] = _lock_record(token, acquired_at + lease_seconds, owner, acquired_at)
                        acquired.add(lock_key)
                        changed = True

                if blocked:
                    waited = True
                    waited_keys.update(blocked_keys)

                if not blocked:
                    result = _success_payload(
                        key=keys[0],
                        keys=keys,
                        token=token,
                        now=now,
                        waited=waited,
                        waited_seconds=_now() - start,
                        waited_keys=waited_keys,
                    )
                    _save_locks_unlocked()
                    return result

                if now >= deadline:
                    for acquired_key in acquired:
                        rec = _locks.get(acquired_key)
                        if rec and secrets.compare_digest(str(rec.get("holder_token") or ""), token):
                            _locks.pop(acquired_key, None)
                            changed = True
                    result = {
                        "success": False,
                        "error": "timeout",
                        "key": blocked["key"],
                        "keys": keys,
                        "locked_keys": sorted(acquired),
                        "blocked_keys": sorted(waited_keys or blocked_keys),
                        "expires_in_seconds": blocked["expires_in_seconds"],
                        "holder": blocked["holder"],
                    }
                    _save_locks_unlocked()
                    return result

                _save_locks_if_changed_unlocked(changed)

        await asyncio.sleep(min(_MULTI_LOCK_POLL_SECONDS, max(0, deadline - _now())))


def _normalize_op(
    *,
    op: str,
    release: bool,
    renew: bool,
    validate: bool,
    reattach: bool,
    owned: bool,
) -> str:
    normalized = (op or "").strip().lower().replace("-", "_")
    if normalized:
        return normalized
    if release and owned:
        return "release_owned"
    if release:
        return "release"
    if renew:
        return "renew"
    if validate:
        return "validate"
    if reattach:
        return "reattach"
    if owned:
        return "list_owned"
    return "acquire"


async def lock_ops(
    *,
    key: str,
    keys: list[str] | None = None,
    release: bool = False,
    holder_token: str = "",
    timeout_seconds: float | int | None = None,
    lease_seconds: float | int | None = None,
    renew: bool = False,
    validate: bool = False,
    reattach: bool = False,
    owned: bool = False,
    op: str = "",
    owner: dict[str, Any] | None = None,
) -> dict[str, Any]:
    key, normalized_keys = _normalize_keys(key, keys)
    holder_token = (holder_token or "").strip()
    normalized_owner = _normalize_owner(owner)
    operation = _normalize_op(
        op=op,
        release=release,
        renew=renew,
        validate=validate,
        reattach=reattach,
        owned=owned,
    )

    if operation == "list_owned":
        return await _list_owned_locks(normalized_owner)
    if operation == "release_owned":
        return await _release_owned_keys(normalized_keys, normalized_owner)
    if not normalized_keys:
        return {"success": False, "error": "key_required"}

    if operation == "release":
        return await _release_keys(normalized_keys, holder_token)
    if operation == "validate":
        return await _validate_keys(normalized_keys, holder_token)
    if operation == "reattach":
        return await _reattach_keys(normalized_keys, normalized_owner)
    if operation not in {"acquire", "renew"}:
        return {"success": False, "error": "invalid_op"}

    lease = _clamp_lease(lease_seconds)
    if lease is None:
        return {"success": False, "error": "invalid_lease_seconds"}
    if operation == "renew":
        return await _renew_keys(normalized_keys, holder_token, normalized_owner, lease)

    if len(normalized_keys) > 1 or timeout_seconds is not None:
        timeout = _clamp_timeout(timeout_seconds)
        if timeout is None:
            return {"success": False, "error": "invalid_timeout_seconds"}
        return await _acquire_keys(normalized_keys, timeout, lease, normalized_owner)

    async with _locks_guard:
        with _locked_store():
            _load_locks_unlocked()
            now = _now()
            changed = _expire_locks(now, [key])
            rec = _locks.get(key)

            if rec:
                blocked = _blocked_payload(key, rec, now)
                _save_locks_if_changed_unlocked(changed)
                return {
                    "success": False,
                    "error": "locked",
                    "key": key,
                    "blocked_keys": [key],
                    "expires_in_seconds": blocked["expires_in_seconds"],
                    "holder": blocked["holder"],
                }

            token = secrets.token_urlsafe(32)
            created_at = now
            expires_at = now + lease
            _locks[key] = _lock_record(token, expires_at, normalized_owner, created_at)
            result = _success_payload(key=key, keys=[key], token=token, now=now)
            _save_locks_unlocked()
            return result
