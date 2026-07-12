from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from urllib.parse import urlparse

from fastapi import HTTPException, Request, WebSocket

import user_prefs


@dataclass(frozen=True)
class ParsedOrigin:
    scheme: str
    host: str
    port: int | None


_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
_DEFAULT_BACKEND_PORT = 8000
_DEFAULT_DEV_PORT = 3000
_CAPACITOR_ORIGIN = "capacitor://localhost"
_TAILSCALE_IPV4_NETWORK = ipaddress.ip_network("100.64.0.0/10")
_TAILSCALE_DNS_SUFFIX = ".ts.net"


def _split_host_port(raw: str) -> tuple[str, int | None] | None:
    value = (raw or "").strip()
    if not value or "@" in value:
        return None
    if value.startswith("["):
        end = value.find("]")
        if end < 0:
            return None
        host = value[1:end]
        rest = value[end + 1:]
        if not rest:
            return host.lower(), None
        if not rest.startswith(":"):
            return None
        port_raw = rest[1:]
        if not port_raw.isdigit():
            return None
        return host.lower(), int(port_raw)
    if value.count(":") > 1:
        return value.lower(), None
    if ":" not in value:
        return value.lower(), None
    host, port_raw = value.rsplit(":", 1)
    if not host or not port_raw.isdigit():
        return None
    return host.lower(), int(port_raw)


def _parse_origin(raw: str) -> ParsedOrigin | None:
    value = (raw or "").strip()
    if value == _CAPACITOR_ORIGIN:
        return ParsedOrigin("capacitor", "localhost", None)
    parsed = urlparse(value)
    if parsed.username or parsed.password:
        return None
    if parsed.scheme not in {"http", "https"}:
        return None
    host = (parsed.hostname or "").lower()
    if not host:
        return None
    return ParsedOrigin(parsed.scheme, host, parsed.port)


def _is_loopback_host(host: str) -> bool:
    if host in _LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _is_lan_ip_host(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private or ip.is_link_local or ip in _TAILSCALE_IPV4_NETWORK


def _is_tailscale_dns_host(host: str) -> bool:
    return bool(host and host.endswith(_TAILSCALE_DNS_SUFFIX) and host != _TAILSCALE_DNS_SUFFIX[1:])


def _is_lan_browser_host(host: str) -> bool:
    return _is_lan_ip_host(host) or _is_tailscale_dns_host(host)


def _configured_origins() -> set[str]:
    raw = os.environ.get("BETTER_AGENT_TRUSTED_BROWSER_ORIGINS", "")
    return {item.strip().rstrip("/") for item in raw.split(",") if item.strip()}


def _dev_origins() -> set[str]:
    if os.environ.get("BETTER_AGENT_DEV_ORIGINS") == "1" or os.environ.get("VITE_DEV_SERVER") == "1":
        return {
            f"http://localhost:{_DEFAULT_DEV_PORT}",
            f"http://127.0.0.1:{_DEFAULT_DEV_PORT}",
            f"http://[::1]:{_DEFAULT_DEV_PORT}",
        }
    return set()


def _origin_string(origin: ParsedOrigin) -> str:
    if origin.scheme == "capacitor":
        return _CAPACITOR_ORIGIN
    host = f"[{origin.host}]" if ":" in origin.host else origin.host
    port = f":{origin.port}" if origin.port is not None else ""
    return f"{origin.scheme}://{host}{port}"


def _allowed_origin(origin: ParsedOrigin, request_host: str, request_port: int | None) -> bool:
    rendered = _origin_string(origin)
    if rendered in _configured_origins() or rendered in _dev_origins():
        return True
    if rendered == _CAPACITOR_ORIGIN:
        return True
    if _is_loopback_host(origin.host):
        if origin.port in {None, request_port, _DEFAULT_BACKEND_PORT, _DEFAULT_DEV_PORT}:
            return True
    if user_prefs.get_network_bind_address() == "0.0.0.0":
        trusted_lan_hosts = {
            item.strip().lower()
            for item in os.environ.get("BETTER_AGENT_TRUSTED_LAN_HOSTS", "").split(",")
            if item.strip()
        }
        lan_dev_ports = {None, request_port, _DEFAULT_BACKEND_PORT, _DEFAULT_DEV_PORT}
        if origin.host in trusted_lan_hosts and origin.port in lan_dev_ports:
            return True
        if _is_lan_browser_host(origin.host):
            if origin.host == request_host:
                return origin.port in lan_dev_ports
            if _is_loopback_host(request_host) and origin.port == _DEFAULT_DEV_PORT:
                return True
    if _is_tailscale_dns_host(origin.host) or _is_tailscale_dns_host(request_host):
        return (
            origin.scheme == "https"
            and origin.host == request_host
            and origin.port in {None, request_port, 443}
        )
    return origin.host == request_host and origin.port in {None, request_port}


def is_cors_origin_allowed(origin_raw: str, host_raw: str) -> bool:
    host = _split_host_port(host_raw)
    origin = _parse_origin(origin_raw)
    if host is None or origin is None:
        return False
    request_host, request_port = host
    if not _allowed_host(request_host, request_port):
        return False
    return _allowed_origin(origin, request_host, request_port)


def _allowed_host(host: str, port: int | None) -> bool:
    if _is_loopback_host(host):
        return True
    configured_hosts = {
        item.strip().lower()
        for item in os.environ.get("BETTER_AGENT_TRUSTED_BROWSER_HOSTS", "").split(",")
        if item.strip()
    }
    if host in configured_hosts:
        return True
    if _is_tailscale_dns_host(host):
        return True
    if user_prefs.get_network_bind_address() == "0.0.0.0":
        trusted_lan_hosts = {
            item.strip().lower()
            for item in os.environ.get("BETTER_AGENT_TRUSTED_LAN_HOSTS", "").split(",")
            if item.strip()
        }
        return host in trusted_lan_hosts or _is_lan_browser_host(host)
    return False


def _request_host(headers: dict[str, str]) -> tuple[str, int | None] | None:
    parsed = _split_host_port(headers.get("host", ""))
    if parsed is None:
        return None
    host, port = parsed
    if not _allowed_host(host, port):
        return None
    return host, port


def _has_cookie(headers: dict[str, str]) -> bool:
    return bool(headers.get("cookie", "").strip())


def _has_bearer(headers: dict[str, str]) -> bool:
    return headers.get("authorization", "").lower().startswith("bearer ")


def _browser_source(headers: dict[str, str]) -> str:
    return headers.get("origin") or headers.get("referer") or ""


def validate_http_request(request: Request) -> None:
    headers = {k.lower(): v for k, v in request.headers.items()}
    source = _browser_source(headers)
    if not source:
        return
    if _has_bearer(headers) and not _has_cookie(headers):
        return
    host = _request_host(headers)
    origin = _parse_origin(source)
    if host is None or origin is None or not _allowed_origin(origin, host[0], host[1]):
        raise HTTPException(status_code=403, detail="untrusted browser origin")


def validate_websocket(websocket: WebSocket) -> bool:
    headers = {k.lower(): v for k, v in websocket.headers.items()}
    source = _browser_source(headers)
    if not source:
        return True
    if _has_bearer(headers) and not _has_cookie(headers):
        return True
    host = _request_host(headers)
    origin = _parse_origin(source)
    return host is not None and origin is not None and _allowed_origin(origin, host[0], host[1])
