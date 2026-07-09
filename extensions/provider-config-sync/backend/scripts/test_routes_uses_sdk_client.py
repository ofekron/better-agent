"""Locks the provider-config-sync extension proxy onto the SDK Client.

Regression test for the extension-boundary tightening: routes.py must route
through exact capability actions, never raw core method/path/query transport.
"""
from __future__ import annotations

import asyncio
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
    check("invoke_capability" in _ROUTES_SRC, "routes.py routes through Client.invoke_capability")
    check("request_internal" not in _ROUTES_SRC, "routes.py has no raw internal request escape hatch")


class _FakeRequest:
    def __init__(self, method: str, query: str, body: bytes) -> None:
        self.method = method
        self._body = body

        class _URL:
            pass

        self.url = _URL()
        self.url.query = query
        self.query_params = {}

    async def body(self) -> bytes:
        return self._body

    async def json(self):
        import json
        return json.loads(self._body)


def test_proxy_routes_through_client_preserving_verb() -> None:
    calls: list[dict] = []

    class _SpyClient:
        def invoke_capability(self, capability, action, payload=None, *, timeout=60.0):
            calls.append({"capability": capability, "action": action, "payload": payload})
            return {"ok": True}

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
    check(call["capability"] == "provider-config-sync", "proxy targets the provider capability")
    check(call["action"] == "settings.patch", "proxy selects the exact action")
    check(call["payload"] == {"k": 1}, "proxy passes a decoded object payload")
    check(resp.status_code == 200, "proxy returns the capability result")


def test_sdk_client_exposes_capability_invocation_only() -> None:
    check(hasattr(better_agent_sdk.Client, "invoke_capability"), "SDK Client exposes invoke_capability")
    check(not hasattr(better_agent_sdk.Client, "request_internal"), "SDK Client hides raw internal transport")


if __name__ == "__main__":
    test_source_has_no_handrolled_transport()
    test_proxy_routes_through_client_preserving_verb()
    test_sdk_client_exposes_capability_invocation_only()
    print("\nALL PASS")
