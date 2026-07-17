"""Regression: BFF websocket proxy must not cap upstream frame size at 1 MiB.

`proxy_ws` opens the upstream connection with `websockets.connect`/
`unix_connect`. Neither call passed `max_size`, so it inherited the
`websockets` library's default of 1 MiB. Runtime chat/event frames can
exceed that (observed: 1,867,656 bytes), which made the *client* side
enforce its own limit and tear the upstream connection down with close
code 1009 (MESSAGE_TOO_BIG) — dropping the frame and killing the pump
instead of forwarding to the browser.

This test patches `bff_server.websockets.connect` and `.unix_connect`,
drives `proxy_ws` through both the "uds" and "tcp" descriptor branches,
and asserts each call received `max_size=None` (unbounded, matching
`node_client.py`'s upstream client).

Run with:
    cd backend && .venv/bin/python scripts/test_bff_proxy_ws_upstream_max_size.py
"""

from __future__ import annotations

import asyncio
import os
import sys

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-bff-ws-max-size-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import bff_server  # noqa: E402


PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


class _Url:
    def __init__(self) -> None:
        self.path = "/ws/chat"
        self.query = ""
        self.scheme = "ws"


class _FakeBrowserWS:
    def __init__(self) -> None:
        self.headers = {"host": "127.0.0.1:18765"}
        self.url = _Url()
        self.client = None
        self.close_code = None
        self._block = asyncio.Event()

    async def accept(self) -> None:
        return None

    async def receive(self):
        await self._block.wait()
        return {"type": "websocket.disconnect"}

    async def send_text(self, _data: str) -> None:
        return None

    async def send_bytes(self, _data: bytes) -> None:
        return None

    async def close(self, code: int = 1000) -> None:
        self.close_code = code


class _FakeUpstream:
    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration

    async def close(self) -> None:
        return None


class _FakeLease:
    def __init__(self, descriptor: dict) -> None:
        self.descriptor = descriptor

    async def release(self) -> None:
        return None


async def _run_proxy_ws_and_capture(descriptor: dict) -> dict:
    """Drives proxy_ws once and returns the kwargs the upstream connect got."""
    captured: dict = {}

    async def _fake_acquire():
        return _FakeLease(descriptor)

    async def _fake_unix_connect(*args, **kwargs):
        captured["fn"] = "unix_connect"
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _FakeUpstream()

    async def _fake_connect(*args, **kwargs):
        captured["fn"] = "connect"
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _FakeUpstream()

    orig_acquire = bff_server.runtime_upstream.acquire
    orig_unix_connect = bff_server.websockets.unix_connect
    orig_connect = bff_server.websockets.connect
    bff_server.runtime_upstream.acquire = _fake_acquire  # type: ignore[assignment]
    bff_server.websockets.unix_connect = _fake_unix_connect  # type: ignore[assignment]
    bff_server.websockets.connect = _fake_connect  # type: ignore[assignment]
    try:
        ws = _FakeBrowserWS()
        await bff_server.proxy_ws(ws, "chat")
    finally:
        bff_server.runtime_upstream.acquire = orig_acquire  # type: ignore[assignment]
        bff_server.websockets.unix_connect = orig_unix_connect  # type: ignore[assignment]
        bff_server.websockets.connect = orig_connect  # type: ignore[assignment]

    return captured


async def test_uds_upstream_has_no_max_size_cap() -> bool:
    captured = await _run_proxy_ws_and_capture(
        {"kind": "uds", "path": "/tmp/ba-test-bff-ws.sock"}
    )
    if captured.get("fn") != "unix_connect":
        print(f"  expected unix_connect, got {captured.get('fn')!r}")
        return False
    if captured["kwargs"].get("max_size") is not None:
        print(f"  max_size={captured['kwargs'].get('max_size')!r}, expected None (unbounded)")
        return False
    return True


async def test_tcp_upstream_has_no_max_size_cap() -> bool:
    captured = await _run_proxy_ws_and_capture(
        {"kind": "tcp", "host": "127.0.0.1", "port": 18765}
    )
    if captured.get("fn") != "connect":
        print(f"  expected connect, got {captured.get('fn')!r}")
        return False
    if captured["kwargs"].get("max_size") is not None:
        print(f"  max_size={captured['kwargs'].get('max_size')!r}, expected None (unbounded)")
        return False
    return True


TESTS = [
    ("uds upstream connect has no max_size cap", test_uds_upstream_has_no_max_size_cap),
    ("tcp upstream connect has no max_size cap", test_tcp_upstream_has_no_max_size_cap),
]


def main_run() -> int:
    failed = 0
    for name, fn in TESTS:
        try:
            ok = asyncio.run(fn())
        except Exception as e:
            import traceback
            traceback.print_exc()
            ok = False
            print(f"  exception: {e}")
        print(f"{PASS if ok else FAIL}  {name}")
        if not ok:
            failed += 1
    print()
    if failed:
        print(f"{failed} of {len(TESTS)} test(s) FAILED")
    else:
        print(f"all {len(TESTS)} tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main_run())
