#!/usr/bin/env python3
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import _test_home

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

_TMP_HOME = _test_home.isolate("bc-test-tailscale-https-")

from fastapi.testclient import TestClient  # noqa: E402

import auth  # noqa: E402
import main  # noqa: E402
import tailscale_https  # noqa: E402


def _status(dns_name: str = "mac.tailnet.ts.net.") -> dict:
    return {
        "BackendState": "Running",
        "Self": {
            "DNSName": dns_name,
            "Online": True,
        },
    }


def _client() -> TestClient:
    client = TestClient(main.app, base_url="http://127.0.0.1:18765")
    client.headers.update({"Authorization": f"Bearer {auth.create_token('tailscale-test')}"})
    client.cookies.set("better_agent_session", "present")
    return client


def test_status_to_https_url_requires_active_tailscale_dns() -> None:
    assert tailscale_https.tailscale_https_url_from_status(_status()) == "https://mac.tailnet.ts.net"
    assert tailscale_https.tailscale_https_url_from_status({**_status(), "BackendState": "Stopped"}) is None
    assert tailscale_https.tailscale_https_url_from_status({"Self": {"DNSName": "mac.local."}}) is None
    assert tailscale_https.tailscale_https_url_from_status({"Self": {"DNSName": "mac.tailnet.ts.net:443"}}) is None
    assert tailscale_https.tailscale_https_url_from_status({"Self": {"DNSName": "mac.tailnet.ts.net.", "Online": False}}) is None


def test_current_tailscale_https_url_uses_fixed_status_command() -> None:
    calls: list[list[str]] = []

    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True
        assert kwargs["check"] is False
        return subprocess.CompletedProcess(args, 0, json.dumps(_status()), "")

    assert tailscale_https.current_tailscale_https_url(run=run) == "https://mac.tailnet.ts.net"
    assert calls == [["tailscale", "status", "--json"]]


def _serve_status(proxy: str | None = "http://127.0.0.1:18765") -> dict:
    handlers = {}
    if proxy is not None:
        handlers["/"] = {"Proxy": proxy}
    return {
        "Services": {
            "https": {
                "TCP": {"443": {"HTTPS": True}},
                "Web": {
                    "mac.tailnet.ts.net:443": {
                        "Handlers": handlers,
                    },
                },
            },
        },
    }


def test_local_serve_target_uses_loopback_port_only() -> None:
    assert tailscale_https.local_serve_target("http://192.168.1.20:18765") == "http://127.0.0.1:18765"
    assert tailscale_https.local_serve_target("https://mac.tailnet.ts.net") == "http://127.0.0.1:443"
    assert tailscale_https.local_serve_target("file:///tmp/nope") is None


def test_serve_https_state_detects_configured_empty_and_conflict() -> None:
    assert tailscale_https.serve_https_state(
        _serve_status(),
        "https://mac.tailnet.ts.net",
        "http://127.0.0.1:18765",
    ) == "configured"
    assert tailscale_https.serve_https_state(
        {"Services": {}},
        "https://mac.tailnet.ts.net",
        "http://127.0.0.1:18765",
    ) == "empty"
    assert tailscale_https.serve_https_state(
        _serve_status("http://127.0.0.1:9000"),
        "https://mac.tailnet.ts.net",
        "http://127.0.0.1:18765",
    ) == "conflict"


def test_ensure_tailscale_serve_https_runs_only_when_443_is_free() -> None:
    tailscale_https._SERVE_ATTEMPTED_TARGETS.clear()
    calls: list[list[str]] = []

    def run(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args == ["tailscale", "serve", "status", "--json"]:
            return subprocess.CompletedProcess(args, 0, json.dumps({"Services": {}}), "")
        return subprocess.CompletedProcess(args, 0, "", "")

    assert tailscale_https.ensure_tailscale_serve_https(
        "https://mac.tailnet.ts.net",
        "http://192.168.1.20:18765",
        run=run,
    ) is True
    assert calls == [
        ["tailscale", "serve", "status", "--json"],
        ["tailscale", "serve", "--https=443", "http://127.0.0.1:18765"],
    ]


def test_ensure_tailscale_serve_https_does_not_overwrite_conflict() -> None:
    tailscale_https._SERVE_ATTEMPTED_TARGETS.clear()
    calls: list[list[str]] = []

    def run(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, json.dumps(_serve_status("http://127.0.0.1:9000")), "")

    assert tailscale_https.ensure_tailscale_serve_https(
        "https://mac.tailnet.ts.net",
        "http://192.168.1.20:18765",
        run=run,
    ) is False
    assert calls == [["tailscale", "serve", "status", "--json"]]


def test_preferred_external_url_falls_back_when_unreachable() -> None:
    original_url = tailscale_https.current_tailscale_https_url
    original_reachable = tailscale_https.better_agent_is_reachable
    original_ensure = tailscale_https.ensure_tailscale_serve_https
    try:
        tailscale_https.current_tailscale_https_url = lambda: "https://mac.tailnet.ts.net"  # type: ignore[assignment]
        tailscale_https.better_agent_is_reachable = lambda url: False  # type: ignore[assignment]
        tailscale_https.ensure_tailscale_serve_https = lambda tailscale_url, local_url: False  # type: ignore[assignment]
        assert tailscale_https.preferred_external_url("http://192.168.1.20:18765") == "http://192.168.1.20:18765"
    finally:
        tailscale_https.current_tailscale_https_url = original_url  # type: ignore[assignment]
        tailscale_https.better_agent_is_reachable = original_reachable  # type: ignore[assignment]
        tailscale_https.ensure_tailscale_serve_https = original_ensure  # type: ignore[assignment]


def test_preferred_external_url_uses_serve_when_it_makes_tailscale_reachable() -> None:
    original_url = tailscale_https.current_tailscale_https_url
    original_reachable = tailscale_https.better_agent_is_reachable
    original_ensure = tailscale_https.ensure_tailscale_serve_https
    checks: list[str] = []
    try:
        tailscale_https.current_tailscale_https_url = lambda: "https://mac.tailnet.ts.net"  # type: ignore[assignment]
        tailscale_https.better_agent_is_reachable = lambda url: checks.append(url) is None and len(checks) > 1  # type: ignore[assignment]
        tailscale_https.ensure_tailscale_serve_https = lambda tailscale_url, local_url: True  # type: ignore[assignment]
        assert tailscale_https.preferred_external_url("http://192.168.1.20:18765") == "https://mac.tailnet.ts.net"
        assert checks == ["https://mac.tailnet.ts.net", "https://mac.tailnet.ts.net"]
    finally:
        tailscale_https.current_tailscale_https_url = original_url  # type: ignore[assignment]
        tailscale_https.better_agent_is_reachable = original_reachable  # type: ignore[assignment]
        tailscale_https.ensure_tailscale_serve_https = original_ensure  # type: ignore[assignment]


def test_preferred_external_url_falls_back_when_serve_conflicts() -> None:
    original_url = tailscale_https.current_tailscale_https_url
    original_reachable = tailscale_https.better_agent_is_reachable
    original_ensure = tailscale_https.ensure_tailscale_serve_https
    try:
        tailscale_https.current_tailscale_https_url = lambda: "https://mac.tailnet.ts.net"  # type: ignore[assignment]
        tailscale_https.better_agent_is_reachable = lambda url: False  # type: ignore[assignment]
        tailscale_https.ensure_tailscale_serve_https = lambda tailscale_url, local_url: False  # type: ignore[assignment]
        assert tailscale_https.preferred_external_url("http://192.168.1.20:18765") == "http://192.168.1.20:18765"
    finally:
        tailscale_https.current_tailscale_https_url = original_url  # type: ignore[assignment]
        tailscale_https.better_agent_is_reachable = original_reachable  # type: ignore[assignment]
        tailscale_https.ensure_tailscale_serve_https = original_ensure  # type: ignore[assignment]


def test_status_endpoints_prefer_reachable_tailscale_https() -> None:
    original_lan_ip = main._lan_ip
    original_url = tailscale_https.current_tailscale_https_url
    original_reachable = tailscale_https.better_agent_is_reachable
    try:
        main._lan_ip = lambda: "192.168.1.20"  # type: ignore[assignment]
        tailscale_https.current_tailscale_https_url = lambda: "https://mac.tailnet.ts.net"  # type: ignore[assignment]
        tailscale_https.better_agent_is_reachable = lambda url: True  # type: ignore[assignment]

        client = _client()
        mobile = client.get("/api/mobile/status").json()
        desktop = client.get("/api/desktop/status").json()

        assert mobile["server_url"] == "https://mac.tailnet.ts.net"
        assert desktop["server_url"] == "https://mac.tailnet.ts.net"
        assert desktop["update_url"] == "https://mac.tailnet.ts.net/api/desktop/updates"
    finally:
        main._lan_ip = original_lan_ip  # type: ignore[assignment]
        tailscale_https.current_tailscale_https_url = original_url  # type: ignore[assignment]
        tailscale_https.better_agent_is_reachable = original_reachable  # type: ignore[assignment]


def test_status_endpoints_fall_back_to_local_url() -> None:
    original_lan_ip = main._lan_ip
    original_url = tailscale_https.current_tailscale_https_url
    try:
        main._lan_ip = lambda: "192.168.1.20"  # type: ignore[assignment]
        tailscale_https.current_tailscale_https_url = lambda: None  # type: ignore[assignment]

        client = _client()
        mobile = client.get("/api/mobile/status").json()
        desktop = client.get("/api/desktop/status").json()

        assert mobile["server_url"] == "http://192.168.1.20:18765"
        assert desktop["server_url"] == "http://192.168.1.20:18765"
        assert desktop["update_url"] == "http://192.168.1.20:18765/api/desktop/updates"
    finally:
        main._lan_ip = original_lan_ip  # type: ignore[assignment]
        tailscale_https.current_tailscale_https_url = original_url  # type: ignore[assignment]


def main_run() -> int:
    for test in (
        test_status_to_https_url_requires_active_tailscale_dns,
        test_current_tailscale_https_url_uses_fixed_status_command,
        test_local_serve_target_uses_loopback_port_only,
        test_serve_https_state_detects_configured_empty_and_conflict,
        test_ensure_tailscale_serve_https_runs_only_when_443_is_free,
        test_ensure_tailscale_serve_https_does_not_overwrite_conflict,
        test_preferred_external_url_falls_back_when_unreachable,
        test_preferred_external_url_uses_serve_when_it_makes_tailscale_reachable,
        test_preferred_external_url_falls_back_when_serve_conflicts,
        test_status_endpoints_prefer_reachable_tailscale_https,
        test_status_endpoints_fall_back_to_local_url,
    ):
        test()
    print("tailscale_https pure checks passed")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main_run())
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
