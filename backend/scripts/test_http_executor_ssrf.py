"""HttpExecutor SSRF hardening.

Before the fix, HttpExecutor only checked the URL scheme (https-only) and
then handed the request straight to urllib, which resolves DNS and connects
without any IP-range check — a descriptor whose url_template points at
loopback/private/link-local/cloud-metadata would reach a real socket
connect() attempt. This locks that a disallowed destination is rejected by
ssrf_guard.resolve_safe_ip() BEFORE any socket connection is attempted, and
that the executor surfaces it as a normal ok=False ExecResult (not a raw
unhandled exception).

Run:
    cd backend && .venv/bin/python scripts/test_http_executor_ssrf.py
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import credential_broker.executors.http as http_mod  # noqa: E402
from credential_broker.executors.http import HttpExecutor  # noqa: E402


def _descriptor(url: str) -> dict:
    return {
        "provider_id": "prov-test",
        "label": "test",
        "sink_kind": "http",
        "sink": {
            "method": "GET",
            "url_template": url,
            "headers": {"Authorization": "Bearer {{secret}}"},
        },
    }


def test_rejects_disallowed_destination_before_connecting():
    connect_attempts = []
    real_create_connection = http_mod.socket.create_connection

    def _guard(*args, **kwargs):
        connect_attempts.append(args)
        raise AssertionError("socket.create_connection must not be reached for a disallowed host")

    http_mod.socket.create_connection = _guard
    try:
        for url in (
            "https://169.254.169.254/latest/meta-data",  # cloud metadata
            "https://127.0.0.1/steal",
            "https://10.1.2.3/steal",
            "https://localhost/steal",
        ):
            result = HttpExecutor().execute(_descriptor(url), "sk-secret-value")
            assert result.ok is False, f"should refuse: {url}"
            assert "refused" in result.error.lower(), result.error
    finally:
        http_mod.socket.create_connection = real_create_connection

    assert connect_attempts == [], f"connection was attempted: {connect_attempts}"
    print("ok  disallowed destinations are refused before any socket connect")


def test_non_https_still_rejected():
    result = HttpExecutor().execute(_descriptor("http://api.github.com/x"), "sk-secret")
    assert result.ok is False
    assert "non-https" in result.error
    print("ok  non-https destinations still rejected")


def main() -> int:
    test_rejects_disallowed_destination_before_connecting()
    test_non_https_still_rejected()
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
