"""Locks the provider-config-sync extension proxy onto the SDK Client.

Regression test for the extension-boundary tightening: routes.py must NOT
hand-roll loopback transport (raw urllib + a manually-attached X-Internal-Token
header). It must route every internal call through ``better_agent_sdk.Client``,
preserving HTTP method / query / raw body via ``Client.request_internal``.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[4]
_EXT_BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "sdk"))
sys.path.insert(0, str(_EXT_BACKEND))

import better_agent_sdk  # noqa: E402
import routes  # noqa: E402

_ROUTES_SRC = (Path(routes.__file__)).read_text(encoding="utf-8")


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)
    print(f"ok - {msg}")


def test_source_has_no_handrolled_transport() -> None:
    check("X-Internal-Token" not in _ROUTES_SRC, "routes.py no longer builds X-Internal-Token header")
    check("urllib.request" not in _ROUTES_SRC, "routes.py no longer uses urllib.request directly")
    check("urllib.error" not in _ROUTES_SRC, "routes.py no longer uses urllib.error directly")
    check("BETTER_CLAUDE_INTERNAL_TOKEN" not in _ROUTES_SRC, "routes.py no longer reads the internal token from env")
    check("better_agent_sdk" in _ROUTES_SRC, "routes.py imports better_agent_sdk")
    check("request_internal" in _ROUTES_SRC, "routes.py routes through Client.request_internal")


class _FakeRequest:
    def __init__(self, method: str, query: str, body: bytes) -> None:
        self.method = method
        self._body = body

        class _URL:
            pass

        self.url = _URL()
        self.url.query = query

    async def body(self) -> bytes:
        return self._body


def test_proxy_routes_through_client_preserving_verb() -> None:
    calls: list[dict] = []

    class _SpyClient:
        def request_internal(self, method, path, *, body=None, query="", timeout=60.0):
            calls.append({"method": method, "path": path, "body": body, "query": query})
            return 200, json.dumps({"ok": True}).encode("utf-8")

    orig = routes.Client
    routes.Client = _SpyClient
    try:
        resp = asyncio.run(
            routes._proxy(_FakeRequest("PATCH", "cwd=/x", b'{"k":1}'), "settings")
        )
    finally:
        routes.Client = orig

    check(len(calls) == 1, "proxy made exactly one SDK call")
    call = calls[0]
    check(call["method"] == "PATCH", "proxy preserves the incoming HTTP method (not forced to POST)")
    check(call["path"] == "/api/internal/provider-config-sync/settings", "proxy targets the core internal sub-path")
    check(call["query"] == "cwd=/x", "proxy preserves the query string")
    check(call["body"] == b'{"k":1}', "proxy passes the raw body through untouched")
    check(resp.status_code == 200, "proxy returns the core status code")


def test_sdk_client_exposes_request_internal() -> None:
    check(hasattr(better_agent_sdk.Client, "request_internal"), "SDK Client exposes request_internal")
    bad = False
    try:
        better_agent_sdk.Client(internal_token="t").request_internal("GET", "/api/other")
    except better_agent_sdk.BetterAgentError:
        bad = True
    check(bad, "request_internal rejects paths outside /api/internal/")


if __name__ == "__main__":
    test_source_has_no_handrolled_transport()
    test_proxy_routes_through_client_preserving_verb()
    test_sdk_client_exposes_request_internal()
    print("\nALL PASS")
