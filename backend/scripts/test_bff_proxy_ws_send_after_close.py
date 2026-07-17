"""Regression: BFF websocket proxy must not leak a send-after-close RuntimeError.

When the browser disconnects mid-stream, Starlette raises
`RuntimeError("Unexpected ASGI message 'websocket.send', ...")` on the next
send. The `upstream_to_browser` pump in bff_server.proxy_ws used to let that
exception escape its task, which asyncio then logged as an un-retrieved
"Task exception was never retrieved" / `ERROR: Exception in ASGI application`
traceback in the BFF run log (noisy and looked like a crash).

This test forces that exact path through the real proxy_ws coroutine with a
fake upstream that emits a frame and a fake browser websocket whose
send_text raises the same RuntimeError, and asserts:

  * proxy_ws completes without raising, and
  * no task exception is left un-retrieved (the guard caught it), and
  * send_text was actually invoked (we exercised the path).

Run with:
    cd backend && .venv/bin/python scripts/test_bff_proxy_ws_send_after_close.py
"""

from __future__ import annotations

import asyncio
import gc
import os
import sys

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-bff-ws-close-")

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
    """Stand-in for a starlette WebSocket already past disconnect."""

    def __init__(self) -> None:
        self.headers = {"host": "127.0.0.1:18765"}
        self.url = _Url()
        self.client = None  # _ws_forward_headers falls back to "127.0.0.1"
        self.send_calls = 0
        self.close_code = None
        self._block = asyncio.Event()

    async def accept(self) -> None:
        return None

    async def receive(self):
        # Browser never sends anything / disconnects; block until the pump
        # teardown cancels us. This guarantees upstream_to_browser is the
        # pump that hits send_text.
        await self._block.wait()
        return {"type": "websocket.disconnect"}

    async def send_text(self, _data: str) -> None:
        self.send_calls += 1
        raise RuntimeError(
            "Unexpected ASGI message 'websocket.send', after sending "
            "'websocket.close' or response already completed."
        )

    async def send_bytes(self, _data: bytes) -> None:
        self.send_calls += 1
        raise RuntimeError(
            "Unexpected ASGI message 'websocket.send', after sending "
            "'websocket.close' or response already completed."
        )

    async def close(self, code: int = 1000) -> None:
        self.close_code = code


class _FakeUpstream:
    def __init__(self) -> None:
        self.close_code = None
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        # Yield a text frame; the pump will try to forward it to the browser
        # and hit the send-after-close RuntimeError.
        await asyncio.sleep(0)  # let the pumps start deterministically
        return '{"type":"noop"}'

    async def close(self) -> None:
        self.closed = True


class _FakeLease:
    def __init__(self) -> None:
        self.descriptor = {"kind": "uds", "path": "/tmp/ba-test-bff-ws.sock"}

    async def release(self) -> None:
        return None


async def test_send_after_close_does_not_leak() -> bool:
    loop = asyncio.get_running_loop()

    unretrieved: list[dict] = []

    def exc_handler(_loop, context):
        msg = context.get("message", "") or ""
        if "never retrieved" in msg:
            unretrieved.append(context)
        _loop.default_exception_handler(context)

    loop.set_exception_handler(exc_handler)

    ws = _FakeBrowserWS()
    upstream = _FakeUpstream()

    async def _fake_acquire():
        return _FakeLease()

    async def _fake_unix_connect(*_args, **_kwargs):
        return upstream

    orig_acquire = bff_server.runtime_upstream.acquire
    orig_unix_connect = bff_server.websockets.unix_connect
    bff_server.runtime_upstream.acquire = _fake_acquire  # type: ignore[assignment]
    bff_server.websockets.unix_connect = _fake_unix_connect  # type: ignore[assignment]
    try:
        # The real proxy_ws coroutine. Must return normally even though the
        # browser socket raises on send.
        await bff_server.proxy_ws(ws, "chat")
    except Exception as exc:
        print(f"  proxy_ws propagated an exception: {exc!r}")
        return False
    finally:
        bff_server.runtime_upstream.acquire = orig_acquire  # type: ignore[assignment]
        bff_server.websockets.unix_connect = orig_unix_connect  # type: ignore[assignment]
        # Let any destroyed tasks report + force collection of the pumps.
        for _ in range(3):
            await asyncio.sleep(0)
        gc.collect()
        for _ in range(3):
            await asyncio.sleep(0)

    if ws.send_calls < 1:
        print("  send_text was never invoked; the race path was not exercised")
        return False
    if not upstream.closed:
        print("  upstream was not closed by proxy_ws teardown")
        return False
    if unretrieved:
        msgs = [c.get("message") for c in unretrieved]
        print(f"  un-retrieved task exception leaked: {msgs!r}")
        return False
    return True


TESTS = [
    ("send-after-close RuntimeError is contained by proxy_ws", test_send_after_close_does_not_leak),
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
