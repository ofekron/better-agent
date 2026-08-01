from __future__ import annotations

import os
import sys

import pytest
from fastapi import HTTPException

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import browser_trust as bt  # noqa: E402
import user_prefs  # noqa: E402

# Every env var browser_trust reads. Cleared per-test by the ``clean_env`` fixture.
_ENV_KEYS = (
    "BETTER_AGENT_TRUSTED_BROWSER_ORIGINS",
    "BETTER_AGENT_DEV_ORIGINS",
    "VITE_DEV_SERVER",
    "BETTER_AGENT_TRUSTED_BROWSER_HOSTS",
    "BETTER_AGENT_TRUSTED_LAN_HOSTS",
)


@pytest.fixture
def clean_env(monkeypatch):
    """Reset all browser_trust env knobs and bind address to loopback defaults."""
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(user_prefs, "get_network_bind_address", lambda: "127.0.0.1")
    return monkeypatch


class _FakeReq:
    """Stand-in for fastapi Request/WebSocket: only ``headers.items()`` is used."""

    def __init__(self, headers):
        self.headers = headers


# --------------------------------------------------------------------------- #
# _split_host_port
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("", None),
        ("   ", None),
        (None, None),
        ("a@b", None),
        ("user@host:8000", None),
        ("localhost", ("localhost", None)),
        ("LocalHost", ("localhost", None)),
        ("localhost:8000", ("localhost", 8000)),
        ("localhost:abc", None),
        (":8000", None),
        ("::1", ("::1", None)),            # unbracketed IPv6 → multiple colons
        ("localhost:8000:extra", ("localhost:8000:extra", None)),
        ("[::1]", ("::1", None)),
        ("[::1]:8000", ("::1", 8000)),
        ("[::1", None),                    # no closing bracket
        ("[::1]x", None),                  # junk after bracket
        ("[::1]:abc", None),               # non-digit port
        ("[::1]:", None),                  # empty port
        ("[Upper]:9000", ("upper", 9000)),  # host lowercased
    ],
)
def test_split_host_port(raw, expected):
    assert bt._split_host_port(raw) == expected


# --------------------------------------------------------------------------- #
# _parse_origin
# --------------------------------------------------------------------------- #


def test_parse_origin_capacitor():
    assert bt._parse_origin("capacitor://localhost") == bt.ParsedOrigin("capacitor", "localhost", None)


def test_parse_origin_basic():
    assert bt._parse_origin("https://example.com") == bt.ParsedOrigin("https", "example.com", None)
    assert bt._parse_origin("http://example.com:8080") == bt.ParsedOrigin("http", "example.com", 8080)


def test_parse_origin_lowercases_host():
    assert bt._parse_origin("http://EXAMPLE.com").host == "example.com"


@pytest.mark.parametrize(
    "raw",
    [
        "",
        None,
        "ftp://example.com",          # bad scheme
        "http://u@host",              # username present
        "http://u:p@host",            # password present
        "http://",                    # no host
    ],
)
def test_parse_origin_rejects(raw):
    assert bt._parse_origin(raw) is None


# --------------------------------------------------------------------------- #
# host classifiers
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("host,expected", [
    ("localhost", True),
    ("127.0.0.1", True),
    ("::1", True),
    ("10.0.0.1", False),
    ("example.com", False),
])
def test_is_loopback_host(host, expected):
    assert bt._is_loopback_host(host) is expected


@pytest.mark.parametrize("host,expected", [
    ("192.168.1.1", True),
    ("10.0.0.1", True),
    ("172.16.0.1", True),
    ("169.254.1.1", True),     # link-local
    ("100.64.0.1", True),      # tailscale cgnet
    ("8.8.8.8", False),
    ("example.com", False),
])
def test_is_lan_ip_host(host, expected):
    assert bt._is_lan_ip_host(host) is expected


@pytest.mark.parametrize("host,expected", [
    ("foo.ts.net", True),
    ("ts.net", False),         # exactly suffix[1:]
    ("", False),
    ("example.com", False),
])
def test_is_tailscale_dns_host(host, expected):
    assert bt._is_tailscale_dns_host(host) is expected


def test_is_lan_browser_host():
    assert bt._is_lan_browser_host("192.168.1.1") is True
    assert bt._is_lan_browser_host("foo.ts.net") is True
    assert bt._is_lan_browser_host("8.8.8.8") is False


# --------------------------------------------------------------------------- #
# origin set builders + _origin_string
# --------------------------------------------------------------------------- #


def test_configured_origins(clean_env):
    assert bt._configured_origins() == set()
    clean_env.setenv("BETTER_AGENT_TRUSTED_BROWSER_ORIGINS", "http://a.com/, http://b.com,")
    assert bt._configured_origins() == {"http://a.com", "http://b.com"}


def test_dev_origins_disabled(clean_env):
    assert bt._dev_origins() == set()


def test_dev_origins_enabled(clean_env):
    clean_env.setenv("VITE_DEV_SERVER", "1")
    origins = bt._dev_origins()
    assert f"http://localhost:{bt._DEFAULT_DEV_PORT}" in origins
    assert f"http://127.0.0.1:{bt._DEFAULT_DEV_PORT}" in origins
    assert f"http://[::1]:{bt._DEFAULT_DEV_PORT}" in origins


def test_dev_origins_via_dev_flag(clean_env):
    clean_env.setenv("BETTER_AGENT_DEV_ORIGINS", "1")
    assert bt._dev_origins()  # non-empty


def test_origin_string_variants():
    assert bt._origin_string(bt.ParsedOrigin("capacitor", "localhost", None)) == bt._CAPACITOR_ORIGIN
    assert bt._origin_string(bt.ParsedOrigin("http", "example.com", None)) == "http://example.com"
    assert bt._origin_string(bt.ParsedOrigin("http", "example.com", 8000)) == "http://example.com:8000"
    assert bt._origin_string(bt.ParsedOrigin("http", "::1", 8000)) == "http://[::1]:8000"


# --------------------------------------------------------------------------- #
# _allowed_origin
# --------------------------------------------------------------------------- #


def _origin(scheme, host, port=None):
    return bt.ParsedOrigin(scheme, host, port)


def test_allowed_origin_configured(clean_env):
    clean_env.setenv("BETTER_AGENT_TRUSTED_BROWSER_ORIGINS", "http://example.com")
    assert bt._allowed_origin(_origin("http", "example.com"), "localhost", 8000) is True


def test_allowed_origin_dev(clean_env):
    clean_env.setenv("VITE_DEV_SERVER", "1")
    assert bt._allowed_origin(_origin("http", "localhost", bt._DEFAULT_DEV_PORT), "localhost", 8000) is True


def test_allowed_origin_capacitor(clean_env):
    assert bt._allowed_origin(_origin("capacitor", "localhost"), "localhost", 8000) is True


def test_allowed_origin_loopback_ports(clean_env):
    # port None / request_port / backend / dev → all allowed for loopback
    for port in (None, 8000, bt._DEFAULT_DEV_PORT):
        assert bt._allowed_origin(_origin("http", "localhost", port), "localhost", 8000) is True


def test_allowed_origin_loopback_wrong_port_falls_through(clean_env):
    # loopback host but port 9999: not in loopback set, host==request_host,
    # final fallback requires port in {None, request_port=8000} → False.
    assert bt._allowed_origin(_origin("http", "localhost", 9999), "localhost", 8000) is False


def test_allowed_origin_fallback_same_host(clean_env):
    # Non-loopback, non-lan, non-tailscale host: allowed only if host matches
    # request host and port is None or the request port.
    assert bt._allowed_origin(_origin("http", "myhost"), "myhost", 8000) is True
    assert bt._allowed_origin(_origin("http", "myhost", 8000), "myhost", 8000) is True
    assert bt._allowed_origin(_origin("http", "myhost", 9000), "myhost", 8000) is False
    assert bt._allowed_origin(_origin("http", "other"), "myhost", 8000) is False


def test_allowed_origin_tailscale(clean_env):
    host = "foo.ts.net"
    assert bt._allowed_origin(_origin("https", host, None), host, None) is True
    assert bt._allowed_origin(_origin("https", host, 443), host, None) is True
    assert bt._allowed_origin(_origin("http", host, None), host, None) is False  # must be https
    assert bt._allowed_origin(_origin("https", "bar.ts.net", None), host, None) is False  # host mismatch


def test_allowed_origin_tailscale_via_request_host(clean_env):
    # origin host not tailscale but request host is → tailscale branch still taken
    assert bt._allowed_origin(_origin("https", "foo.ts.net", None), "foo.ts.net", 8000) is True


# ---- bind 0.0.0.0 branch ----


def test_allowed_origin_lan_trusted_host(clean_env, monkeypatch):
    monkeypatch.setattr(user_prefs, "get_network_bind_address", lambda: "0.0.0.0")
    monkeypatch.setenv("BETTER_AGENT_TRUSTED_LAN_HOSTS", "192.168.1.50")
    assert bt._allowed_origin(_origin("http", "192.168.1.50", 8000), "localhost", 8000) is True
    # untrusted lan host
    assert bt._allowed_origin(_origin("http", "192.168.1.99", 8000), "localhost", 8000) is False


def test_allowed_origin_lan_browser_same_host(clean_env, monkeypatch):
    monkeypatch.setattr(user_prefs, "get_network_bind_address", lambda: "0.0.0.0")
    # 192.168.1.60 is private (lan browser), matches request host
    assert bt._allowed_origin(_origin("http", "192.168.1.60", 8000), "192.168.1.60", 8000) is True
    # wrong port
    assert bt._allowed_origin(_origin("http", "192.168.1.60", 9999), "192.168.1.60", 8000) is False


def test_allowed_origin_lan_browser_from_loopback_dev_port(clean_env, monkeypatch):
    monkeypatch.setattr(user_prefs, "get_network_bind_address", lambda: "0.0.0.0")
    # lan browser origin, request from loopback, dev port → allowed
    assert (
        bt._allowed_origin(_origin("http", "192.168.1.60", bt._DEFAULT_DEV_PORT), "127.0.0.1", 8000)
        is True
    )


def test_allowed_origin_lan_browser_no_match(clean_env, monkeypatch):
    monkeypatch.setattr(user_prefs, "get_network_bind_address", lambda: "0.0.0.0")
    # lan browser origin, request from a different lan host, non-dev port
    assert bt._allowed_origin(_origin("http", "192.168.1.60", 8000), "192.168.1.70", 8000) is False


def test_allowed_origin_lan_bind_public_host_skips_browser_branch(clean_env, monkeypatch):
    monkeypatch.setattr(user_prefs, "get_network_bind_address", lambda: "0.0.0.0")
    # Under 0.0.0.0 bind: a public (non-lan, non-trusted) origin host is not a
    # lan-browser host, so control skips the browser sub-block and falls through
    # to the tailscale/final checks, which reject it.
    assert bt._allowed_origin(_origin("http", "8.8.8.8", 8000), "localhost", 8000) is False


# --------------------------------------------------------------------------- #
# _allowed_host
# --------------------------------------------------------------------------- #


def test_allowed_host_loopback(clean_env):
    assert bt._allowed_host("localhost", 8000) is True
    assert bt._allowed_host("127.0.0.1", 8000) is True


def test_allowed_host_configured(clean_env):
    clean_env.setenv("BETTER_AGENT_TRUSTED_BROWSER_HOSTS", "myhost, otherhost")
    assert bt._allowed_host("myhost", 8000) is True
    assert bt._allowed_host("unknown", 8000) is False


def test_allowed_host_tailscale(clean_env):
    assert bt._allowed_host("foo.ts.net", 8000) is True


def test_allowed_host_loopback_bind_rejects_public(clean_env):
    assert bt._allowed_host("example.com", 8000) is False


def test_allowed_host_lan_bind_trusted(clean_env, monkeypatch):
    monkeypatch.setattr(user_prefs, "get_network_bind_address", lambda: "0.0.0.0")
    monkeypatch.setenv("BETTER_AGENT_TRUSTED_LAN_HOSTS", "192.168.1.50")
    assert bt._allowed_host("192.168.1.50", 8000) is True
    assert bt._allowed_host("192.168.1.99", 8000) is True  # lan browser ip
    assert bt._allowed_host("8.8.8.8", 8000) is False


# --------------------------------------------------------------------------- #
# _request_host, header helpers
# --------------------------------------------------------------------------- #


def test_request_host_happy(clean_env):
    assert bt._request_host({"host": "localhost:8000"}) == ("localhost", 8000)


def test_request_host_no_header(clean_env):
    assert bt._request_host({}) is None


def test_request_host_unparseable(clean_env):
    assert bt._request_host({"host": "a@b"}) is None


def test_request_host_disallowed(clean_env):
    assert bt._request_host({"host": "example.com"}) is None


def test_header_helpers():
    assert bt._has_cookie({"cookie": "sid=x"}) is True
    assert bt._has_cookie({"cookie": "  "}) is False
    assert bt._has_cookie({}) is False
    assert bt._has_bearer({"authorization": "Bearer xyz"}) is True
    assert bt._has_bearer({"authorization": "basic xyz"}) is False
    assert bt._has_bearer({}) is False
    assert bt._browser_source({"origin": "http://a"}) == "http://a"
    assert bt._browser_source({"referer": "http://b"}) == "http://b"
    assert bt._browser_source({}) == ""


# --------------------------------------------------------------------------- #
# is_cors_origin_allowed (public)
# --------------------------------------------------------------------------- #


def test_cors_allowed_loopback(clean_env):
    assert bt.is_cors_origin_allowed("http://localhost:8000", "localhost:8000") is True


def test_cors_rejected_bad_origin(clean_env):
    assert bt.is_cors_origin_allowed("ftp://localhost", "localhost:8000") is False


def test_cors_rejected_bad_host(clean_env):
    assert bt.is_cors_origin_allowed("http://localhost:8000", "a@b") is False


def test_cors_rejected_disallowed_host(clean_env):
    # origin loopback but request host is a public hostname not trusted
    assert bt.is_cors_origin_allowed("http://example.com", "example.com:8000") is False


def test_cors_configured_origin(clean_env):
    clean_env.setenv("BETTER_AGENT_TRUSTED_BROWSER_ORIGINS", "http://example.com")
    assert bt.is_cors_origin_allowed("http://example.com", "localhost:8000") is True


# --------------------------------------------------------------------------- #
# validate_http_request
# --------------------------------------------------------------------------- #


def test_validate_http_no_source_allows(clean_env):
    bt.validate_http_request(_FakeReq({"host": "localhost:8000"}))  # no raise


def test_validate_http_bearer_without_cookie_allows(clean_env):
    # A source must be present so we reach the bearer short-circuit (not the
    # earlier no-source return).
    req = _FakeReq({
        "host": "localhost:8000",
        "origin": "http://evil.com",
        "authorization": "Bearer t",
    })
    bt.validate_http_request(req)  # no raise — bearer trusted over origin


def test_validate_http_allowed_origin_allows(clean_env):
    req = _FakeReq({
        "host": "localhost:8000",
        "origin": "http://localhost:8000",
        "cookie": "sid=x",
    })
    bt.validate_http_request(req)  # no raise


def test_validate_http_untrusted_origin_rejects(clean_env):
    req = _FakeReq({
        "host": "localhost:8000",
        "origin": "http://evil.com",
        "cookie": "sid=x",
    })
    with pytest.raises(HTTPException) as exc:
        bt.validate_http_request(req)
    assert exc.value.status_code == 403


def test_validate_http_referer_used_when_no_origin(clean_env):
    req = _FakeReq({
        "host": "localhost:8000",
        "referer": "http://localhost:8000/page",
        "cookie": "sid=x",
    })
    bt.validate_http_request(req)  # no raise


def test_validate_http_untrusted_host_rejects(clean_env):
    # origin fine but request host itself untrusted → host None → 403
    req = _FakeReq({
        "host": "evil.com:8000",
        "origin": "http://localhost:8000",
        "cookie": "sid=x",
    })
    with pytest.raises(HTTPException) as exc:
        bt.validate_http_request(req)
    assert exc.value.status_code == 403


# --------------------------------------------------------------------------- #
# validate_websocket
# --------------------------------------------------------------------------- #


def test_validate_websocket_no_source(clean_env):
    assert bt.validate_websocket(_FakeReq({"host": "localhost:8000"})) is True


def test_validate_websocket_bearer_without_cookie(clean_env):
    ws = _FakeReq({
        "host": "localhost:8000",
        "origin": "http://evil.com",
        "authorization": "Bearer t",
    })
    assert bt.validate_websocket(ws) is True


def test_validate_websocket_allowed(clean_env):
    ws = _FakeReq({
        "host": "localhost:8000",
        "origin": "http://localhost:8000",
    })
    assert bt.validate_websocket(ws) is True


def test_validate_websocket_untrusted(clean_env):
    ws = _FakeReq({
        "host": "localhost:8000",
        "origin": "http://evil.com",
    })
    assert bt.validate_websocket(ws) is False


def test_validate_websocket_untrusted_host(clean_env):
    ws = _FakeReq({
        "host": "evil.com:8000",
        "origin": "http://localhost:8000",
    })
    assert bt.validate_websocket(ws) is False
