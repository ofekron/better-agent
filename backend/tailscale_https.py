from __future__ import annotations

import json
import re
import subprocess
import urllib.error
import urllib.request
from typing import Any, Callable


_TAILSCALE_DNS_RE = re.compile(r"^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.ts\.net$")


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


def preferred_external_url(local_url: str) -> str:
    tailscale_url = current_tailscale_https_url()
    if tailscale_url and better_agent_is_reachable(tailscale_url):
        return tailscale_url
    return local_url
