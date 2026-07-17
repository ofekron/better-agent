"""Shared SSRF guard — the single source of truth for "is this destination
host/IP safe to connect to" across every backend-owned outbound-fetch path.

Declarative checks (e.g. extension manifest validation) use
``is_disallowed_remote_host`` alone. Runtime outbound requests must use
``resolve_safe_ip``, which resolves and validates in one step so the
validated address is the exact one connected to — closing the DNS-rebinding
window between a check and a later, separate connect.
"""

from __future__ import annotations

import ipaddress
import socket

_DISALLOWED_HOSTNAMES = frozenset({
    "localhost",
    "metadata.google.internal",
    "metadata",
})


class SSRFBlockedError(Exception):
    """Raised when a destination host/IP lands on a disallowed range."""


def _is_disallowed_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def is_disallowed_remote_host(hostname: str) -> bool:
    host = (hostname or "").strip().strip(".").lower()
    if not host:
        return True
    if host in _DISALLOWED_HOSTNAMES:
        return True
    if host.endswith(".localhost") or host.endswith(".local") or host.endswith(".internal"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return _is_disallowed_ip(ip)


def resolve_safe_ip(hostname: str, port: int) -> str:
    """Resolve ``hostname`` to a single vetted IP for pinned connection use.

    Fails closed: rejects if the hostname itself is disallowed, if
    resolution fails, or if ANY resolved address lands on a disallowed
    range (loopback/private/link-local/reserved/multicast/unspecified).
    """
    if is_disallowed_remote_host(hostname):
        raise SSRFBlockedError(f"refused: destination host {hostname!r} is not allowed")
    try:
        infos = socket.getaddrinfo(hostname, port, proto=socket.IPPROTO_TCP)
    except OSError as e:
        raise SSRFBlockedError(f"refused: could not resolve {hostname!r}: {e}") from e
    resolved_ips: list[str] = []
    for _family, _type, _proto, _canon, sockaddr in infos:
        ip_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if _is_disallowed_ip(ip):
            raise SSRFBlockedError(
                f"refused: {hostname!r} resolves to disallowed address {ip_str}"
            )
        resolved_ips.append(ip_str)
    if not resolved_ips:
        raise SSRFBlockedError(f"refused: no usable address for {hostname!r}")
    return resolved_ips[0]
