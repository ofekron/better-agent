#!/usr/bin/env python3
from __future__ import annotations

import os
import shutil
import sys
import tempfile

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-browser-trust-")
os.environ.pop("BETTER_CLAUDE_TEST_AUTH_BYPASS", None)

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from starlette.testclient import TestClient, WebSocketDisconnect  # noqa: E402

import auth  # noqa: E402
import main  # noqa: E402
import user_prefs  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _client() -> TestClient:
    client = TestClient(main.app, client=("127.0.0.1", 50000), base_url="http://localhost:8000")
    client.headers.update({"Authorization": f"Bearer {auth.create_token('browser')}"})
    client.cookies.set("better_agent_session", "present")
    return client


def _bearer_client() -> TestClient:
    client = TestClient(main.app, client=("127.0.0.1", 50000), base_url="http://localhost:8000")
    client.headers.update({"Authorization": f"Bearer {auth.create_token('native')}"})
    return client


def test_malicious_origin_rejected_for_cookie_read() -> tuple[bool, str]:
    res = _client().get("/api/sessions", headers={"Origin": "http://evil.test", "Host": "localhost:8000"})
    return res.status_code == 403, f"expected 403, got {res.status_code}: {res.text[:120]}"


def test_loopback_origin_allowed_for_cookie_read() -> tuple[bool, str]:
    res = _client().get("/api/sessions", headers={"Origin": "http://localhost:8000", "Host": "localhost:8000"})
    return res.status_code == 200, f"expected 200, got {res.status_code}: {res.text[:120]}"


def test_native_bearer_origin_exempt_without_cookie() -> tuple[bool, str]:
    res = _bearer_client().get("/api/auth/me", headers={"Origin": "capacitor://localhost", "Host": "lan-host:8000"})
    return res.status_code == 200, f"expected 200, got {res.status_code}: {res.text[:120]}"


def test_websocket_malicious_origin_rejected() -> tuple[bool, str]:
    try:
        with _client().websocket_connect(
            "/ws/chat",
            headers={"Origin": "http://evil.test", "Host": "localhost:8000"},
        ):
            return False, "websocket connected"
    except WebSocketDisconnect:
        return True, ""
    except Exception:
        return True, ""


def test_lan_ip_same_origin_allowed_in_lan_mode() -> tuple[bool, str]:
    user_prefs.set_network_bind_address("0.0.0.0")
    try:
        res = _client().get(
            "/api/sessions",
            headers={"Origin": "http://192.168.1.20:8000", "Host": "192.168.1.20:8000"},
        )
    finally:
        user_prefs.set_network_bind_address("127.0.0.1")
    return res.status_code == 200, f"expected 200, got {res.status_code}: {res.text[:120]}"


def test_tailscale_ip_same_origin_allowed_in_lan_mode() -> tuple[bool, str]:
    user_prefs.set_network_bind_address("0.0.0.0")
    try:
        res = _client().get(
            "/api/sessions",
            headers={"Origin": "http://100.101.102.103:8000", "Host": "100.101.102.103:8000"},
        )
    finally:
        user_prefs.set_network_bind_address("127.0.0.1")
    return res.status_code == 200, f"expected 200, got {res.status_code}: {res.text[:120]}"


def test_tailscale_dev_origin_allowed_for_direct_backend_in_lan_mode() -> tuple[bool, str]:
    user_prefs.set_network_bind_address("0.0.0.0")
    try:
        res = _client().get(
            "/api/sessions",
            headers={"Origin": "http://100.101.102.103:3000", "Host": "100.101.102.103:8000"},
        )
    finally:
        user_prefs.set_network_bind_address("127.0.0.1")
    return res.status_code == 200, f"expected 200, got {res.status_code}: {res.text[:120]}"


def test_tailscale_dev_origin_allowed_through_vite_proxy_in_lan_mode() -> tuple[bool, str]:
    user_prefs.set_network_bind_address("0.0.0.0")
    try:
        res = _client().get(
            "/api/sessions",
            headers={"Origin": "http://100.101.102.103:3000", "Host": "localhost:8000"},
        )
    finally:
        user_prefs.set_network_bind_address("127.0.0.1")
    return res.status_code == 200, f"expected 200, got {res.status_code}: {res.text[:120]}"


def test_tailscale_dns_same_origin_allowed_in_lan_mode() -> tuple[bool, str]:
    user_prefs.set_network_bind_address("0.0.0.0")
    try:
        res = _client().get(
            "/api/sessions",
            headers={"Origin": "http://mac.tailnet.ts.net:8000", "Host": "mac.tailnet.ts.net:8000"},
        )
    finally:
        user_prefs.set_network_bind_address("127.0.0.1")
    return res.status_code == 200, f"expected 200, got {res.status_code}: {res.text[:120]}"


def test_tailscale_dns_https_same_origin_allowed_in_local_mode() -> tuple[bool, str]:
    res = _client().get(
        "/api/sessions",
        headers={"Origin": "https://mac.tailnet.ts.net", "Host": "mac.tailnet.ts.net"},
    )
    return res.status_code == 200, f"expected 200, got {res.status_code}: {res.text[:120]}"


def test_tailscale_dns_http_same_origin_rejected_in_local_mode() -> tuple[bool, str]:
    res = _client().get(
        "/api/sessions",
        headers={"Origin": "http://mac.tailnet.ts.net", "Host": "mac.tailnet.ts.net"},
    )
    return res.status_code == 403, f"expected 403, got {res.status_code}: {res.text[:120]}"


def test_tailscale_dns_dev_origin_allowed_through_vite_proxy_in_lan_mode() -> tuple[bool, str]:
    user_prefs.set_network_bind_address("0.0.0.0")
    try:
        res = _client().get(
            "/api/sessions",
            headers={"Origin": "http://mac.tailnet.ts.net:3000", "Host": "localhost:8000"},
        )
    finally:
        user_prefs.set_network_bind_address("127.0.0.1")
    return res.status_code == 200, f"expected 200, got {res.status_code}: {res.text[:120]}"


def test_tailscale_cross_host_origin_rejected_in_lan_mode() -> tuple[bool, str]:
    user_prefs.set_network_bind_address("0.0.0.0")
    try:
        res = _client().get(
            "/api/sessions",
            headers={"Origin": "http://100.101.102.103:3000", "Host": "100.101.102.104:8000"},
        )
    finally:
        user_prefs.set_network_bind_address("127.0.0.1")
    return res.status_code == 403, f"expected 403, got {res.status_code}: {res.text[:120]}"


def test_tailscale_dns_cross_host_origin_rejected_in_lan_mode() -> tuple[bool, str]:
    user_prefs.set_network_bind_address("0.0.0.0")
    try:
        res = _client().get(
            "/api/sessions",
            headers={"Origin": "http://mac.tailnet.ts.net:3000", "Host": "other.tailnet.ts.net:8000"},
        )
    finally:
        user_prefs.set_network_bind_address("127.0.0.1")
    return res.status_code == 403, f"expected 403, got {res.status_code}: {res.text[:120]}"


def test_tailscale_dns_https_cross_host_rejected_in_local_mode() -> tuple[bool, str]:
    res = _client().get(
        "/api/sessions",
        headers={"Origin": "https://mac.tailnet.ts.net", "Host": "other.tailnet.ts.net"},
    )
    return res.status_code == 403, f"expected 403, got {res.status_code}: {res.text[:120]}"


def test_tailscale_dev_origin_cors_preflight_allowed() -> tuple[bool, str]:
    user_prefs.set_network_bind_address("0.0.0.0")
    try:
        res = _client().options(
            "/api/auth/login",
            headers={
                "Origin": "http://100.101.102.103:3000",
                "Host": "100.101.102.103:8000",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
    finally:
        user_prefs.set_network_bind_address("127.0.0.1")
    allowed_origin = res.headers.get("access-control-allow-origin")
    ok = res.status_code == 200 and allowed_origin == "http://100.101.102.103:3000"
    return ok, f"expected CORS allow, got {res.status_code} origin={allowed_origin!r}"


def test_android_capacitor_origin_cors_preflight_allowed() -> tuple[bool, str]:
    user_prefs.set_network_bind_address("0.0.0.0")
    try:
        res = _client().options(
            "/api/auth/login",
            headers={
                "Origin": "http://localhost",
                "Host": "100.101.102.103:8000",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
    finally:
        user_prefs.set_network_bind_address("127.0.0.1")
    allowed_origin = res.headers.get("access-control-allow-origin")
    ok = res.status_code == 200 and allowed_origin == "http://localhost"
    return ok, f"expected CORS allow, got {res.status_code} origin={allowed_origin!r}"


def test_tailscale_dns_dev_origin_cors_preflight_allowed() -> tuple[bool, str]:
    user_prefs.set_network_bind_address("0.0.0.0")
    try:
        res = _client().options(
            "/api/auth/login",
            headers={
                "Origin": "http://mac.tailnet.ts.net:3000",
                "Host": "mac.tailnet.ts.net:8000",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
    finally:
        user_prefs.set_network_bind_address("127.0.0.1")
    allowed_origin = res.headers.get("access-control-allow-origin")
    ok = res.status_code == 200 and allowed_origin == "http://mac.tailnet.ts.net:3000"
    return ok, f"expected CORS allow, got {res.status_code} origin={allowed_origin!r}"


def test_tailscale_dns_https_origin_cors_preflight_allowed() -> tuple[bool, str]:
    res = _client().options(
        "/api/auth/login",
        headers={
            "Origin": "https://mac.tailnet.ts.net",
            "Host": "mac.tailnet.ts.net",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    allowed_origin = res.headers.get("access-control-allow-origin")
    ok = res.status_code == 200 and allowed_origin == "https://mac.tailnet.ts.net"
    return ok, f"expected CORS allow, got {res.status_code} origin={allowed_origin!r}"


def test_tailscale_dns_https_cross_host_cors_preflight_rejected() -> tuple[bool, str]:
    res = _client().options(
        "/api/auth/login",
        headers={
            "Origin": "https://mac.tailnet.ts.net",
            "Host": "other.tailnet.ts.net",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    allowed_origin = res.headers.get("access-control-allow-origin")
    ok = res.status_code == 400 and allowed_origin is None
    return ok, f"expected CORS reject, got {res.status_code} origin={allowed_origin!r}"


def test_untrusted_hostname_cors_preflight_rejected() -> tuple[bool, str]:
    res = _client().options(
        "/api/auth/login",
        headers={
            "Origin": "http://evil.test:3000",
            "Host": "100.101.102.103:8000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    allowed_origin = res.headers.get("access-control-allow-origin")
    ok = res.status_code == 400 and allowed_origin is None
    return ok, f"expected CORS reject, got {res.status_code} origin={allowed_origin!r}"


def test_lan_mode_still_rejects_untrusted_hostname() -> tuple[bool, str]:
    user_prefs.set_network_bind_address("0.0.0.0")
    try:
        res = _client().get("/api/sessions", headers={"Origin": "http://evil.test:8000", "Host": "evil.test:8000"})
    finally:
        user_prefs.set_network_bind_address("127.0.0.1")
    return res.status_code == 403, f"expected 403, got {res.status_code}: {res.text[:120]}"


TESTS = [
    ("malicious Origin rejected for cookie-auth read", test_malicious_origin_rejected_for_cookie_read),
    ("loopback Origin allowed for cookie-auth read", test_loopback_origin_allowed_for_cookie_read),
    ("native bearer without cookie bypasses browser Origin gate", test_native_bearer_origin_exempt_without_cookie),
    ("malicious websocket Origin rejected", test_websocket_malicious_origin_rejected),
    ("LAN IP same-origin browser allowed in LAN mode", test_lan_ip_same_origin_allowed_in_lan_mode),
    ("Tailscale IP same-origin browser allowed in LAN mode", test_tailscale_ip_same_origin_allowed_in_lan_mode),
    ("Tailscale dev origin direct backend allowed in LAN mode", test_tailscale_dev_origin_allowed_for_direct_backend_in_lan_mode),
    ("Tailscale dev origin through Vite proxy allowed in LAN mode", test_tailscale_dev_origin_allowed_through_vite_proxy_in_lan_mode),
    ("Tailscale DNS same-origin browser allowed in LAN mode", test_tailscale_dns_same_origin_allowed_in_lan_mode),
    ("Tailscale DNS HTTPS same-origin browser allowed in local mode", test_tailscale_dns_https_same_origin_allowed_in_local_mode),
    ("Tailscale DNS HTTP same-origin rejected in local mode", test_tailscale_dns_http_same_origin_rejected_in_local_mode),
    ("Tailscale DNS dev origin through Vite proxy allowed in LAN mode", test_tailscale_dns_dev_origin_allowed_through_vite_proxy_in_lan_mode),
    ("Tailscale cross-host origin rejected in LAN mode", test_tailscale_cross_host_origin_rejected_in_lan_mode),
    ("Tailscale DNS cross-host origin rejected in LAN mode", test_tailscale_dns_cross_host_origin_rejected_in_lan_mode),
    ("Tailscale DNS HTTPS cross-host rejected in local mode", test_tailscale_dns_https_cross_host_rejected_in_local_mode),
    ("Tailscale dev origin CORS preflight allowed", test_tailscale_dev_origin_cors_preflight_allowed),
    ("Android Capacitor origin CORS preflight allowed", test_android_capacitor_origin_cors_preflight_allowed),
    ("Tailscale DNS dev origin CORS preflight allowed", test_tailscale_dns_dev_origin_cors_preflight_allowed),
    ("Tailscale DNS HTTPS origin CORS preflight allowed", test_tailscale_dns_https_origin_cors_preflight_allowed),
    ("Tailscale DNS HTTPS cross-host CORS preflight rejected", test_tailscale_dns_https_cross_host_cors_preflight_rejected),
    ("untrusted hostname CORS preflight rejected", test_untrusted_hostname_cors_preflight_rejected),
    ("LAN mode rejects untrusted hostname", test_lan_mode_still_rejects_untrusted_hostname),
]


def main_run() -> int:
    failed = 0
    for name, fn in TESTS:
        try:
            ok, detail = fn()
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, f"exception: {exc}"
        print(f"  {PASS if ok else FAIL} {name}{'' if ok else ' - ' + detail}")
        if not ok:
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main_run())
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
