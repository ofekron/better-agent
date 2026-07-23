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
import installation_profile  # noqa: E402
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


def test_local_https_candidates_include_configured_and_verified_shapes() -> None:
    import os

    old = os.environ.get("BETTER_AGENT_HTTPS_URLS")
    try:
        os.environ["BETTER_AGENT_HTTPS_URLS"] = (
            "https://ba.example.test/path,"
            "http://not-https.test,"
            "https://user:pass@bad.test,"
            "https://ba.example.test"
        )
        assert tailscale_https.local_https_candidates(
            "http://192.168.1.20:18765",
        ) == [
            "https://ba.example.test",
            "https://192.168.1.20",
            "https://192.168.1.20:18765",
        ]
        assert tailscale_https.local_https_candidates(
            "http://127.0.0.1:18765",
            allow_loopback=True,
        ) == [
            "https://ba.example.test",
            "https://127.0.0.1",
            "https://127.0.0.1:18765",
            "https://localhost",
            "https://localhost:18765",
        ]
        assert tailscale_https.local_https_candidates(
            "http://127.0.0.1:18765",
        ) == ["https://ba.example.test"]
    finally:
        if old is None:
            os.environ.pop("BETTER_AGENT_HTTPS_URLS", None)
        else:
            os.environ["BETTER_AGENT_HTTPS_URLS"] = old


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


def _foreground_semantics_run(calls: list[list[str]]):
    """Fake tailscale CLI with real 1.98.x serve semantics: a serve apply
    without --bg blocks in the foreground (the caller's timeout kills it and
    the config dies with the process); with --bg it returns and persists."""

    def run(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args == ["tailscale", "serve", "status", "--json"]:
            return subprocess.CompletedProcess(args, 0, json.dumps({"Services": {}}), "")
        if args[:2] == ["tailscale", "serve"] and "--bg" not in args:
            raise subprocess.TimeoutExpired(cmd=args, timeout=5)
        return subprocess.CompletedProcess(args, 0, "", "")

    return run


def test_ensure_tailscale_serve_https_applies_persistent_background_config() -> None:
    tailscale_https._SERVE_LAST_ATTEMPT.clear()
    calls: list[list[str]] = []

    assert tailscale_https.ensure_tailscale_serve_https(
        "https://mac.tailnet.ts.net",
        "http://192.168.1.20:18765",
        run=_foreground_semantics_run(calls),
    ) is True
    assert calls == [
        ["tailscale", "serve", "status", "--json"],
        ["tailscale", "serve", "--bg", "--https=443", "http://127.0.0.1:18765"],
    ]
    assert tailscale_https.last_serve_failure_reason() == ""


def test_ensure_tailscale_serve_https_reports_serve_not_enabled() -> None:
    tailscale_https._SERVE_LAST_ATTEMPT.clear()

    def run(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if args == ["tailscale", "serve", "status", "--json"]:
            return subprocess.CompletedProcess(args, 0, json.dumps({"Services": {}}), "")
        # The CLI blocks waiting for browser approval; the timeout kills it
        # with the reason already printed to stderr.
        raise subprocess.TimeoutExpired(
            cmd=args,
            timeout=5,
            stderr=b"Serve is not enabled on your tailnet.\nTo enable, visit: https://login.tailscale.com/f/serve",
        )

    assert tailscale_https.ensure_tailscale_serve_https(
        "https://mac.tailnet.ts.net",
        "http://192.168.1.20:18765",
        run=run,
    ) is False
    assert tailscale_https.last_serve_failure_reason() == "serve_not_enabled"


def test_ensure_tailscale_serve_https_retries_after_backoff() -> None:
    tailscale_https._SERVE_LAST_ATTEMPT.clear()
    apply_attempts: list[list[str]] = []
    clock = {"now": 1000.0}

    def run(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if args == ["tailscale", "serve", "status", "--json"]:
            return subprocess.CompletedProcess(args, 0, json.dumps({"Services": {}}), "")
        apply_attempts.append(args)
        return subprocess.CompletedProcess(args, 1, "", "boom")

    def ensure() -> bool:
        return tailscale_https.ensure_tailscale_serve_https(
            "https://mac.tailnet.ts.net",
            "http://192.168.1.20:18765",
            run=run,
            now=lambda: clock["now"],
        )

    assert ensure() is False
    assert len(apply_attempts) == 1
    assert tailscale_https.last_serve_failure_reason() == "serve_apply_failed"

    # Within the backoff window: no second apply attempt.
    clock["now"] += tailscale_https._SERVE_RETRY_BACKOFF_SECONDS / 2
    assert ensure() is False
    assert len(apply_attempts) == 1

    # Past the backoff window: the heal retries instead of staying poisoned.
    clock["now"] += tailscale_https._SERVE_RETRY_BACKOFF_SECONDS
    assert ensure() is False
    assert len(apply_attempts) == 2


def test_ensure_tailscale_serve_https_does_not_overwrite_conflict() -> None:
    tailscale_https._SERVE_LAST_ATTEMPT.clear()
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
    assert tailscale_https.last_serve_failure_reason() == "serve_conflict"


def test_serve_reconcile_tick_reasserts_lost_config() -> None:
    original_url = tailscale_https.current_tailscale_https_url
    original_reachable = tailscale_https.better_agent_is_reachable
    original_ensure = tailscale_https.ensure_tailscale_serve_https
    ensured: list[tuple[str, str]] = []
    try:
        tailscale_https.current_tailscale_https_url = lambda: "https://mac.tailnet.ts.net"  # type: ignore[assignment]
        tailscale_https.better_agent_is_reachable = lambda url: False  # type: ignore[assignment]
        tailscale_https.ensure_tailscale_serve_https = (  # type: ignore[assignment]
            lambda tailscale_url, local_url: ensured.append((tailscale_url, local_url)) is None
        )
        assert tailscale_https.serve_reconcile_tick("http://127.0.0.1:18765") is True
        assert ensured == [("https://mac.tailnet.ts.net", "http://127.0.0.1:18765")]

        tailscale_https.current_tailscale_https_url = lambda: None  # type: ignore[assignment]
        assert tailscale_https.serve_reconcile_tick("http://127.0.0.1:18765") is False
        assert len(ensured) == 1
    finally:
        tailscale_https.current_tailscale_https_url = original_url  # type: ignore[assignment]
        tailscale_https.better_agent_is_reachable = original_reachable  # type: ignore[assignment]
        tailscale_https.ensure_tailscale_serve_https = original_ensure  # type: ignore[assignment]


def test_serve_reconciler_local_url_requires_port_env() -> None:
    import os

    saved = {
        key: os.environ.pop(key, None)
        for key in ("BETTER_AGENT_BACKEND_PORT", "BETTER_CLAUDE_BACKEND_PORT")
    }
    try:
        assert main._tailscale_serve_reconciler_local_url() is None
        os.environ["BETTER_CLAUDE_BACKEND_PORT"] = "18765"
        assert main._tailscale_serve_reconciler_local_url() == "http://127.0.0.1:18765"
        os.environ["BETTER_AGENT_BACKEND_PORT"] = "not-a-port"
        assert main._tailscale_serve_reconciler_local_url() is None
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


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


def test_preferred_external_url_uses_local_https_after_tailscale() -> None:
    original_url = tailscale_https.current_tailscale_https_url
    original_reachable = tailscale_https.better_agent_is_reachable
    original_ensure = tailscale_https.ensure_tailscale_serve_https
    original_candidates = tailscale_https.local_https_candidates
    try:
        tailscale_https.current_tailscale_https_url = lambda: "https://mac.tailnet.ts.net"  # type: ignore[assignment]
        tailscale_https.ensure_tailscale_serve_https = lambda tailscale_url, local_url: False  # type: ignore[assignment]
        tailscale_https.local_https_candidates = lambda local_url, allow_loopback=False: ["https://192.168.1.20"]  # type: ignore[assignment]
        tailscale_https.better_agent_is_reachable = lambda url: url == "https://192.168.1.20"  # type: ignore[assignment]

        preference = tailscale_https.preferred_external_url_details("http://192.168.1.20:18765")
        assert preference.url == "https://192.168.1.20"
        assert preference.source == "local_https"
        assert preference.https_available is True
        assert preference.https_unavailable_reason == ""
    finally:
        tailscale_https.current_tailscale_https_url = original_url  # type: ignore[assignment]
        tailscale_https.better_agent_is_reachable = original_reachable  # type: ignore[assignment]
        tailscale_https.ensure_tailscale_serve_https = original_ensure  # type: ignore[assignment]
        tailscale_https.local_https_candidates = original_candidates  # type: ignore[assignment]


def test_preferred_external_url_reports_http_fallback() -> None:
    original_url = tailscale_https.current_tailscale_https_url
    original_reachable = tailscale_https.better_agent_is_reachable
    original_candidates = tailscale_https.local_https_candidates
    try:
        tailscale_https.current_tailscale_https_url = lambda: None  # type: ignore[assignment]
        tailscale_https.local_https_candidates = lambda local_url, allow_loopback=False: ["https://192.168.1.20"]  # type: ignore[assignment]
        tailscale_https.better_agent_is_reachable = lambda url: False  # type: ignore[assignment]

        preference = tailscale_https.preferred_external_url_details("http://192.168.1.20:18765")
        assert preference.url == "http://192.168.1.20:18765"
        assert preference.source == "http_fallback"
        assert preference.https_available is False
        assert preference.https_unavailable_reason == "no_tailscale"
    finally:
        tailscale_https.current_tailscale_https_url = original_url  # type: ignore[assignment]
        tailscale_https.better_agent_is_reachable = original_reachable  # type: ignore[assignment]
        tailscale_https.local_https_candidates = original_candidates  # type: ignore[assignment]


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
        assert mobile["server_url_source"] == "tailscale"
        assert mobile["https_available"] is True
        assert desktop["server_url"] == "https://mac.tailnet.ts.net"
        assert desktop["server_url_source"] == "tailscale"
        assert desktop["https_available"] is True
        assert desktop["update_url"] == "https://mac.tailnet.ts.net/api/desktop/updates"
    finally:
        main._lan_ip = original_lan_ip  # type: ignore[assignment]
        tailscale_https.current_tailscale_https_url = original_url  # type: ignore[assignment]
        tailscale_https.better_agent_is_reachable = original_reachable  # type: ignore[assignment]


def test_desktop_only_rejects_native_mobile_endpoints() -> None:
    client = _client()
    installation_profile.save(mode=installation_profile.DESKTOP_UI_ONLY, provider="codex")
    assert client.get("/api/installation-profile").json()["mobile_enabled"] is False
    assert client.get("/api/mobile/status").status_code == 404

    installation_profile.save(mode=installation_profile.MOBILE_DESKTOP_UI_ONLY, provider="codex")
    assert client.get("/api/installation-profile").json()["mobile_enabled"] is True
    assert client.get("/api/mobile/status").status_code == 200


def test_status_endpoints_fall_back_to_local_url() -> None:
    original_lan_ip = main._lan_ip
    original_url = tailscale_https.current_tailscale_https_url
    original_candidates = tailscale_https.local_https_candidates
    try:
        main._lan_ip = lambda: "192.168.1.20"  # type: ignore[assignment]
        tailscale_https.current_tailscale_https_url = lambda: None  # type: ignore[assignment]
        tailscale_https.local_https_candidates = lambda local_url, allow_loopback=False: []  # type: ignore[assignment]

        client = _client()
        mobile = client.get("/api/mobile/status").json()
        desktop = client.get("/api/desktop/status").json()

        assert mobile["server_url"] == "http://192.168.1.20:18765"
        assert mobile["server_url_source"] == "http_fallback"
        assert mobile["https_available"] is False
        assert mobile["https_unavailable_reason"] == "no_tailscale"
        assert desktop["server_url"] == "http://192.168.1.20:18765"
        assert desktop["server_url_source"] == "http_fallback"
        assert desktop["https_available"] is False
        assert desktop["update_url"] == "http://192.168.1.20:18765/api/desktop/updates"
    finally:
        main._lan_ip = original_lan_ip  # type: ignore[assignment]
        tailscale_https.current_tailscale_https_url = original_url  # type: ignore[assignment]
        tailscale_https.local_https_candidates = original_candidates  # type: ignore[assignment]


def test_desktop_status_can_use_loopback_https() -> None:
    original_lan_ip = main._lan_ip
    original_url = tailscale_https.current_tailscale_https_url
    original_candidates = tailscale_https.local_https_candidates
    original_reachable = tailscale_https.better_agent_is_reachable
    try:
        main._lan_ip = lambda: "192.168.1.20"  # type: ignore[assignment]
        tailscale_https.current_tailscale_https_url = lambda: None  # type: ignore[assignment]

        def candidates(local_url: str, allow_loopback: bool = False) -> list[str]:
            return ["https://localhost"] if allow_loopback else []

        tailscale_https.local_https_candidates = candidates  # type: ignore[assignment]
        tailscale_https.better_agent_is_reachable = lambda url: url == "https://localhost"  # type: ignore[assignment]

        desktop = _client().get("/api/desktop/status").json()
        assert desktop["server_url"] == "https://localhost"
        assert desktop["server_url_source"] == "local_https"
        assert desktop["https_available"] is True
        assert desktop["update_url"] == "https://localhost/api/desktop/updates"
    finally:
        main._lan_ip = original_lan_ip  # type: ignore[assignment]
        tailscale_https.current_tailscale_https_url = original_url  # type: ignore[assignment]
        tailscale_https.local_https_candidates = original_candidates  # type: ignore[assignment]
        tailscale_https.better_agent_is_reachable = original_reachable  # type: ignore[assignment]


def main_run() -> int:
    for test in (
        test_status_to_https_url_requires_active_tailscale_dns,
        test_current_tailscale_https_url_uses_fixed_status_command,
        test_local_serve_target_uses_loopback_port_only,
        test_local_https_candidates_include_configured_and_verified_shapes,
        test_serve_https_state_detects_configured_empty_and_conflict,
        test_ensure_tailscale_serve_https_applies_persistent_background_config,
        test_ensure_tailscale_serve_https_reports_serve_not_enabled,
        test_ensure_tailscale_serve_https_retries_after_backoff,
        test_ensure_tailscale_serve_https_does_not_overwrite_conflict,
        test_serve_reconcile_tick_reasserts_lost_config,
        test_serve_reconciler_local_url_requires_port_env,
        test_preferred_external_url_falls_back_when_unreachable,
        test_preferred_external_url_uses_local_https_after_tailscale,
        test_preferred_external_url_reports_http_fallback,
        test_preferred_external_url_uses_serve_when_it_makes_tailscale_reachable,
        test_preferred_external_url_falls_back_when_serve_conflicts,
        test_status_endpoints_prefer_reachable_tailscale_https,
        test_desktop_only_rejects_native_mobile_endpoints,
        test_status_endpoints_fall_back_to_local_url,
        test_desktop_status_can_use_loopback_https,
    ):
        test()
    print("tailscale_https pure checks passed")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main_run())
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
