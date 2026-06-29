"""Per-extension internal-loopback tokens.

Each extension that holds the ``internal_loopback`` permission gets its OWN
stable secret, minted once and persisted under ``ba_home()``. The backend
derives the calling extension's identity from this token — it never trusts a
self-asserted ``X-Extension-Id`` header. This makes cross-extension and
builtin impersonation impossible: an extension can only ever act as the
identity its token maps to.

Tokens are stable across restarts/recovery (no rotation), so a respawned
extension subprocess re-receives the same token via env and keeps working
without any disk-reload/retry dance. Rotation happens only on reinstall.
"""
from __future__ import annotations

import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path

from paths import ba_home

_LOCK = threading.Lock()
# Cache keyed by the registry file's fingerprint (path, mtime_ns, size) so a
# process that switches BETTER_AGENT_HOME (tests) never serves a stale map, AND
# an out-of-process write (reinstall rotation, another node/process minting)
# invalidates the cache instead of being silently invisible until restart.
_cache_key: tuple[str, int, int] | None = None
_cache: dict[str, str] | None = None
_last_fingerprint_check = 0.0
_FINGERPRINT_TTL_SECONDS = 0.25


def _path() -> Path:
    return ba_home() / "extension_tokens.json"


def _fingerprint(path: Path) -> tuple[str, int, int]:
    try:
        st = path.stat()
        return (str(path), st.st_mtime_ns, st.st_size)
    except OSError:
        return (str(path), -1, -1)


def _load_locked() -> dict[str, str]:
    global _cache_key, _cache, _last_fingerprint_check
    path = _path()
    now = time.monotonic()
    if (
        _cache is not None
        and _cache_key is not None
        and now - _last_fingerprint_check < _FINGERPRINT_TTL_SECONDS
    ):
        return _cache
    key = _fingerprint(path)
    _last_fingerprint_check = now
    if _cache is not None and _cache_key == key:
        return _cache
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        data = {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}
    except (FileNotFoundError, ValueError, OSError):
        data = {}
    _cache_key, _cache = key, data
    return data


def _persist_locked(data: dict[str, str]) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def mint(extension_id: str) -> str:
    """Return the extension's stable token, creating+persisting it on first use."""
    extension_id = (extension_id or "").strip()
    if not extension_id:
        raise ValueError("extension_id required to mint an internal token")
    with _LOCK:
        data = dict(_load_locked())
        token = data.get(extension_id)
        if not token:
            token = secrets.token_urlsafe(32)
            data[extension_id] = token
            _persist_locked(data)
            global _cache, _cache_key, _last_fingerprint_check
            _cache, _cache_key = data, _fingerprint(_path())
            _last_fingerprint_check = time.monotonic()
        return token


def resolve(token: str | None) -> str | None:
    """Reverse-map a token to its extension id, or None. Constant-time compare."""
    if not token:
        return None
    with _LOCK:
        data = _load_locked()
        for ext_id, tok in data.items():
            if hmac.compare_digest(token, tok):
                return ext_id
    return None


def revoke(extension_id: str) -> None:
    """Drop an extension's token (e.g. on uninstall) so it stops authenticating."""
    extension_id = (extension_id or "").strip()
    if not extension_id:
        return
    with _LOCK:
        data = dict(_load_locked())
        if data.pop(extension_id, None) is not None:
            _persist_locked(data)
            global _cache, _cache_key, _last_fingerprint_check
            _cache, _cache_key = data, _fingerprint(_path())
            _last_fingerprint_check = time.monotonic()
