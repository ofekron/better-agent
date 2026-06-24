"""FIX5 regression: request headers forwarded to an extension backend
subprocess are an allowlist, not a denylist. A secret header that nobody
remembered to denylist must NOT reach extension code.

Fails before the allowlist flip (denylist forwarded unknown headers),
passes after.
"""
from __future__ import annotations

import os
import sys
import tempfile
from types import SimpleNamespace

import _test_home
_test_home.isolate("ba-hdr-")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import extension_backend_loader  # noqa: E402


def _fake_request(raw_pairs):
    return SimpleNamespace(headers=SimpleNamespace(raw=raw_pairs))


def main() -> int:
    raw = [
        (b"content-type", b"application/json"),
        (b"accept", b"*/*"),
        (b"x-request-id", b"abc"),
        # Secrets / unknown headers that must be stripped:
        (b"authorization", b"Bearer SECRET"),
        (b"cookie", b"session=SECRET"),
        (b"x-internal-token", b"INTERNAL"),
        (b"x-entitlement-token", b"FUTURE-SECRET"),  # never denylisted, must still drop
        (b"x-some-new-secret", b"LEAK"),
    ]
    forwarded = dict(extension_backend_loader._safe_request_headers(_fake_request(raw)))
    keys = {k.lower() for k in forwarded}

    assert keys == {"content-type", "accept", "x-request-id"}, f"unexpected forwarded set: {keys}"
    for secret in ("authorization", "cookie", "x-internal-token",
                   "x-entitlement-token", "x-some-new-secret"):
        assert secret not in keys, f"secret header leaked to extension: {secret}"

    print("OK: header allowlist drops all non-allowlisted (incl. never-denylisted) headers")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
