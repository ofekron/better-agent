"""HMAC request signing for the runtime's internal API (/api/internal/*).

Single source of truth for BOTH sides of the exchange: producers attach
headers from ``sign(...)``; the auth gate checks them with ``verify(...)``
/ ``match_signature(...)``. The signing key is the caller's internal
secret (core runner token, per-extension token, or ambient credential) —
the secret itself never travels on the wire.

Canonical string v1::

    BA1\n{METHOD_UPPER}\n{path}\n{sha256_hex(body or b"")}\n{nonce}\n{timestamp}
"""

from __future__ import annotations

import hashlib
import hmac
import os
import threading
import time
from typing import Iterable, Mapping, Optional

HEADER_NONCE = "X-Internal-Nonce"
HEADER_TIMESTAMP = "X-Internal-Timestamp"
HEADER_SIGNATURE = "X-Internal-Signature"

DEFAULT_SKEW_SECONDS = 300

_VERSION = "BA1"
_SIG_PREFIX = "v1="
_NONCE_HEX_LEN = 32  # 16 random bytes, hex-encoded
_SIG_HEX_LEN = 64  # HMAC-SHA256, hex-encoded
_HEX_DIGITS = frozenset("0123456789abcdef")


def canonical_string(
    method: str, path: str, body: bytes | None, nonce: str, timestamp: int
) -> str:
    body_hash = hashlib.sha256(body or b"").hexdigest()
    return f"{_VERSION}\n{method.upper()}\n{path}\n{body_hash}\n{nonce}\n{timestamp}"


def _signature_hex(key: str, canonical: str) -> str:
    return hmac.new(
        key.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def sign(key: str, method: str, path: str, body: bytes | None) -> dict[str, str]:
    """Signature headers for one internal request. ``key`` is the caller's
    internal secret; it is never included in the returned headers."""
    if not key:
        raise ValueError("internal request signing requires a non-empty key")
    nonce = os.urandom(16).hex()
    timestamp = int(time.time())
    signature = _signature_hex(key, canonical_string(method, path, body, nonce, timestamp))
    return {
        HEADER_NONCE: nonce,
        HEADER_TIMESTAMP: str(timestamp),
        HEADER_SIGNATURE: _SIG_PREFIX + signature,
    }


class NonceCache:
    """Thread-safe replay guard. A nonce is accepted exactly once and
    remembered for ``ttl_seconds`` (2x the verification skew window, so a
    replay can never outlive its timestamp validity). Prunes on insert."""

    def __init__(self, ttl_seconds: float = 2.0 * DEFAULT_SKEW_SECONDS) -> None:
        self._ttl = float(ttl_seconds)
        self._lock = threading.Lock()
        self._expiry_by_nonce: dict[str, float] = {}

    def check_and_record(self, nonce: str) -> bool:
        """True (and records the nonce) if unseen; False on replay."""
        now = time.monotonic()
        with self._lock:
            expired = [n for n, exp in self._expiry_by_nonce.items() if exp <= now]
            for n in expired:
                del self._expiry_by_nonce[n]
            if nonce in self._expiry_by_nonce:
                return False
            self._expiry_by_nonce[nonce] = now + self._ttl
            return True


def _is_hex(value: str) -> bool:
    return bool(value) and all(ch in _HEX_DIGITS for ch in value)


def _lowered(headers: Mapping[str, str]) -> dict[str, str]:
    return {str(k).lower(): str(v) for k, v in headers.items()}


def has_signature(headers: Mapping[str, str]) -> bool:
    return bool(_lowered(headers).get(HEADER_SIGNATURE.lower()))


def match_signature(
    keys: Iterable[str],
    method: str,
    path: str,
    body: bytes | None,
    headers: Mapping[str, str],
    *,
    skew_seconds: int = DEFAULT_SKEW_SECONDS,
    nonce_cache: NonceCache,
) -> Optional[str]:
    """The candidate key whose signature matches this request, or None.

    Fail-closed: missing/malformed headers, timestamp outside the skew
    window, no matching key, or a replayed nonce all yield None. The nonce
    is recorded only after a signature matches, so unauthenticated floods
    cannot fill the cache."""
    lowered = _lowered(headers)
    nonce = lowered.get(HEADER_NONCE.lower(), "")
    timestamp_raw = lowered.get(HEADER_TIMESTAMP.lower(), "")
    signature_raw = lowered.get(HEADER_SIGNATURE.lower(), "")
    if len(nonce) != _NONCE_HEX_LEN or not _is_hex(nonce):
        return None
    if not timestamp_raw.isdigit():
        return None
    if not signature_raw.startswith(_SIG_PREFIX):
        return None
    signature_hex = signature_raw[len(_SIG_PREFIX):]
    if len(signature_hex) != _SIG_HEX_LEN or not _is_hex(signature_hex):
        return None
    timestamp = int(timestamp_raw)
    if abs(int(time.time()) - timestamp) > skew_seconds:
        return None
    canonical = canonical_string(method, path, body, nonce, timestamp)
    for key in keys:
        if not key:
            continue
        if hmac.compare_digest(_signature_hex(key, canonical), signature_hex):
            if not nonce_cache.check_and_record(nonce):
                return None
            return key
    return None


def verify(
    keys: Iterable[str],
    method: str,
    path: str,
    body: bytes | None,
    headers: Mapping[str, str],
    *,
    skew_seconds: int = DEFAULT_SKEW_SECONDS,
    nonce_cache: NonceCache,
) -> bool:
    return (
        match_signature(
            keys, method, path, body, headers,
            skew_seconds=skew_seconds, nonce_cache=nonce_cache,
        )
        is not None
    )
