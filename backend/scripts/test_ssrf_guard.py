"""ssrf_guard: shared hostname/IP validation used by extension manifest
hygiene checks and by the credential broker's HTTP sink executor.

Locks:
  * literal loopback/private/link-local/reserved/multicast/metadata
    addresses and hostnames are rejected by is_disallowed_remote_host.
  * resolve_safe_ip rejects the same ranges and raises SSRFBlockedError
    rather than returning a usable address (fail closed).
  * resolve_safe_ip returns a real IP string for a public destination.

Run:
    cd backend && .venv/bin/python scripts/test_ssrf_guard.py
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from ssrf_guard import SSRFBlockedError, is_disallowed_remote_host, resolve_safe_ip  # noqa: E402


def test_is_disallowed_remote_host():
    blocked = [
        "127.0.0.1",
        "localhost",
        "169.254.169.254",  # cloud metadata
        "10.0.0.5",
        "192.168.1.1",
        "172.16.5.5",
        "::1",
        "metadata.google.internal",
        "foo.internal",
        "svc.local",
        "",
    ]
    for host in blocked:
        assert is_disallowed_remote_host(host), f"should reject: {host}"

    allowed = ["api.github.com", "example.com", "8.8.8.8"]
    for host in allowed:
        assert not is_disallowed_remote_host(host), f"should allow: {host}"
    print("ok  is_disallowed_remote_host blocks internal/private/metadata, allows public")


def test_resolve_safe_ip_rejects_disallowed():
    for host in ("127.0.0.1", "169.254.169.254", "10.1.2.3", "::1", "localhost"):
        try:
            resolve_safe_ip(host, 443)
            raise AssertionError(f"should have rejected: {host}")
        except SSRFBlockedError:
            pass
    print("ok  resolve_safe_ip fails closed on loopback/private/link-local/metadata")


def test_resolve_safe_ip_allows_public_literal():
    ip = resolve_safe_ip("8.8.8.8", 443)
    assert ip == "8.8.8.8", ip
    print("ok  resolve_safe_ip returns the vetted address for a public destination")


def main() -> int:
    test_is_disallowed_remote_host()
    test_resolve_safe_ip_rejects_disallowed()
    test_resolve_safe_ip_allows_public_literal()
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
