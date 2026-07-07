from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable


_TAILSCALE_DNS_RE = re.compile(r"^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.ts\.net$")
_SERVE_ATTEMPTED_TARGETS: set[tuple[str, str]] = set()


@dataclass(frozen=True)
class ExternalUrlPreference:
    url: str
    source: str
    https_available: bool
    https_unavailable_reason: str


def tailscale_https_url_from_status(status: dict[str, Any]) -> str | None:
    backend_state = status.get("BackendState")
    if isinstance(backend_state, str) and backend_state.lower() != "running":
        return None

    self_info = status.get("Self")
    if not isinstance(self_info, dict):
        return None

    online = self_info.get("Online")
    if online is False:
        return None

    dns_name = self_info.get("DNSName")
    if not isinstance(dns_name, str):
        return None

    host = dns_name.strip().lower().rstrip(".")
    if not _TAILSCALE_DNS_RE.fullmatch(host):
        return None
    return f"https://{host}"


def current_tailscale_https_url(
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    timeout: float = 1.5,
) -> str | None:
    try:
        proc = run(
            ["tailscale", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return None
    if proc.returncode != 0:
        return None
    try:
        status = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(status, dict):
        return None
    return tailscale_https_url_from_status(status)


def better_agent_is_reachable(url: str, *, timeout: float = 0.8) -> bool:
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/healthz", timeout=timeout) as response:
            return 200 <= response.status < 300
    except (OSError, urllib.error.URLError):
        return False


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _configured_https_urls() -> list[str]:
    raw = os.environ.get("BETTER_AGENT_HTTPS_URLS", "")
    out: list[str] = []
    for item in raw.split(","):
        value = item.strip().rstrip("/")
        if not value:
            continue
        parsed = urllib.parse.urlparse(value)
        if parsed.scheme != "https" or parsed.username or parsed.password or not parsed.hostname:
            continue
        out.append(f"https://{parsed.netloc}")
    return _dedupe(out)


def local_https_candidates(local_url: str, *, allow_loopback: bool = False) -> list[str]:
    parsed = urllib.parse.urlparse(local_url)
    candidates = _configured_https_urls()
    if parsed.scheme not in {"http", "https"}:
        return candidates

    host = parsed.hostname
    if host:
        hosts = [host]
        if allow_loopback:
            hosts.append("localhost")
        for candidate_host in _dedupe(hosts):
            if candidate_host in {"localhost", "127.0.0.1", "::1"} and not allow_loopback:
                continue
            bracketed = f"[{candidate_host}]" if ":" in candidate_host else candidate_host
            candidates.append(f"https://{bracketed}")
            if parsed.port and parsed.port != 443:
                candidates.append(f"https://{bracketed}:{parsed.port}")
    return _dedupe(candidates)


def first_reachable_https_url(local_url: str, *, allow_loopback: bool = False) -> str | None:
    for candidate in local_https_candidates(local_url, allow_loopback=allow_loopback):
        if better_agent_is_reachable(candidate):
            return candidate
    return None


def local_serve_target(local_url: str) -> str | None:
    parsed = urllib.parse.urlparse(local_url)
    if parsed.scheme not in {"http", "https"}:
        return None
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if port < 1 or port > 65535:
        return None
    return f"http://127.0.0.1:{port}"


def serve_status(
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    timeout: float = 1.5,
) -> dict[str, Any] | None:
    try:
        proc = run(
            ["tailscale", "serve", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return None
    if proc.returncode != 0:
        return None
    try:
        status = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    return status if isinstance(status, dict) else None


def serve_https_state(status: dict[str, Any], tailscale_url: str, target: str) -> str:
    services = status.get("Services")
    if not isinstance(services, dict):
        return "empty"

    host = urllib.parse.urlparse(tailscale_url).hostname or ""
    wanted_hosts = {f"{host}:443", host}
    saw_https_443 = False

    for service in services.values():
        if not isinstance(service, dict):
            continue
        tcp = service.get("TCP")
        if isinstance(tcp, dict):
            port_443 = tcp.get("443") or tcp.get(443)
            if isinstance(port_443, dict) and port_443.get("HTTPS") is True:
                saw_https_443 = True
        web = service.get("Web")
        if not isinstance(web, dict):
            continue
        for web_host, web_config in web.items():
            if str(web_host).lower() not in wanted_hosts:
                continue
            if not isinstance(web_config, dict):
                continue
            handlers = web_config.get("Handlers")
            if not isinstance(handlers, dict):
                continue
            root_handler = handlers.get("/")
            if not isinstance(root_handler, dict):
                continue
            proxy = root_handler.get("Proxy")
            if proxy == target:
                return "configured"
            if isinstance(proxy, str) and proxy:
                return "conflict"

    return "conflict" if saw_https_443 else "empty"


def ensure_tailscale_serve_https(
    tailscale_url: str,
    local_url: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> bool:
    target = local_serve_target(local_url)
    if target is None:
        return False

    key = (tailscale_url, target)
    status = serve_status(run=run)
    if status is not None:
        state = serve_https_state(status, tailscale_url, target)
        if state == "configured":
            return True
        if state == "conflict":
            return False

    if key in _SERVE_ATTEMPTED_TARGETS:
        return False
    _SERVE_ATTEMPTED_TARGETS.add(key)

    try:
        proc = run(
            ["tailscale", "serve", "--https=443", target],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return False
    return proc.returncode == 0


def preferred_external_url_details(local_url: str, *, allow_loopback_https: bool = False) -> ExternalUrlPreference:
    tailscale_url = current_tailscale_https_url()
    if tailscale_url:
        if better_agent_is_reachable(tailscale_url):
            return ExternalUrlPreference(tailscale_url, "tailscale", True, "")
        if ensure_tailscale_serve_https(tailscale_url, local_url) and better_agent_is_reachable(tailscale_url):
            return ExternalUrlPreference(tailscale_url, "tailscale", True, "")

    local_https_url = first_reachable_https_url(local_url, allow_loopback=allow_loopback_https)
    if local_https_url:
        return ExternalUrlPreference(local_https_url, "local_https", True, "")

    reason = "tailscale_unreachable" if tailscale_url else "no_tailscale"
    return ExternalUrlPreference(local_url, "http_fallback", False, reason)


def preferred_external_url(local_url: str, *, allow_loopback_https: bool = False) -> str:
    return preferred_external_url_details(
        local_url,
        allow_loopback_https=allow_loopback_https,
    ).url
