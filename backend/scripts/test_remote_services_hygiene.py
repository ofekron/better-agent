"""FIX4 (downgraded): remote_services manifest validation rejects private,
loopback, link-local, and cloud-metadata hosts as declarative hygiene.

This is NOT a runtime egress control (extension code is trusted-by-install and
can reach any host); it only stops a published manifest from *declaring* an
internal SSRF target as a legitimate service.
"""
from __future__ import annotations

import os
import sys
import tempfile

import _test_home
_test_home.isolate("ba-rs-")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import extension_store as es  # noqa: E402


def _rejects(url: str) -> bool:
    try:
        es._validate_remote_services([{"name": "svc", "base_url": url, "purpose": "x"}])
        return False
    except es.ExtensionError:
        return True


def main() -> int:
    blocked = [
        "https://127.0.0.1/api",
        "https://localhost/api",
        "https://169.254.169.254/latest/meta-data",   # cloud metadata
        "https://10.0.0.5/x",                          # RFC1918
        "https://192.168.1.1/x",
        "https://172.16.5.5/x",
        "https://[::1]/x",                             # ipv6 loopback
        "https://metadata.google.internal/x",
        "https://foo.internal/x",
        "https://svc.local/x",
    ]
    for url in blocked:
        assert _rejects(url), f"should reject internal host: {url}"

    allowed = ["https://api.github.com/x", "https://example.com/v1", "https://8.8.8.8/x"]
    for url in allowed:
        assert not _rejects(url), f"should allow public host: {url}"

    # And the helper directly:
    assert es._is_disallowed_remote_host("169.254.169.254") is True
    assert es._is_disallowed_remote_host("") is True
    assert es._is_disallowed_remote_host("api.stripe.com") is False

    print("OK: remote_services rejects internal/private/metadata hosts; allows public")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
