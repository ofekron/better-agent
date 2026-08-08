"""Outbound URL policy for A2A remote-agent base URLs.

https is required. Plain http is allowed ONLY when the host resolves to
localhost/127.0.0.1 (loopback) — for local dev agents. Everything else
is rejected fail-closed. Never accept credentials/query/fragment in the
base URL: secrets must be sent as a header, never embedded in the URL.
"""
from __future__ import annotations

import ipaddress
from urllib.parse import urlparse


class A2AUrlPolicyError(ValueError):
    pass


def _is_loopback_host(hostname: str) -> bool:
    host = (hostname or "").strip().strip(".").lower()
    if not host:
        return False
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_base_url(raw_url: str) -> str:
    """Validate and normalize an A2A agent base URL. Returns the
    normalized URL (scheme+netloc+path, no trailing slash). Raises
    A2AUrlPolicyError on any violation."""
    url = (raw_url or "").strip()
    if not url:
        raise A2AUrlPolicyError("base_url is required")
    parsed = urlparse(url)
    if parsed.scheme not in ("https", "http"):
        raise A2AUrlPolicyError("base_url must use https or http")
    if not parsed.netloc:
        raise A2AUrlPolicyError("base_url must include a host")
    if parsed.username or parsed.password:
        raise A2AUrlPolicyError("base_url must not embed credentials")
    if parsed.query or parsed.fragment:
        raise A2AUrlPolicyError("base_url must not include query or fragment")
    if parsed.scheme == "http" and not _is_loopback_host(parsed.hostname or ""):
        raise A2AUrlPolicyError(
            "http is only allowed for localhost/127.0.0.1; use https for remote hosts"
        )
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"
