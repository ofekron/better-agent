from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-shortcuts-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


class _ProviderHandler(BaseHTTPRequestHandler):
    calls = 0
    delay = 0.0
    lock = threading.Lock()

    def do_POST(self):
        length = int(self.headers.get("content-length") or "0")
        if length:
            self.rfile.read(length)
        with self.lock:
            type(self).calls += 1
        if type(self).delay:
            time.sleep(type(self).delay)
        body = json.dumps({"content": [{"text": "[0]"}]}).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


async def _run() -> bool:
    import shortcut_picker

    await asyncio.to_thread(shortcut_picker.prewarm_http_stack)

    server = ThreadingHTTPServer(("127.0.0.1", 0), _ProviderHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    shortcut_picker._cache.clear()
    shortcut_picker._inflight.clear()
    shortcut_picker._PICK_WAIT_TIMEOUT_SECS = 0.2
    shortcut_picker.user_prefs.get_shortcut_responses = lambda: ["TLDR", "/Adv"]
    shortcut_picker.config_store.get_default_provider = lambda: {
        "id": "test-provider",
        "base_url": f"http://127.0.0.1:{server.server_port}",
        "api_key": "test-key",
        "custom_models": ["claude-3-5-haiku-latest"],
    }

    try:
        first = await asyncio.gather(
            *[shortcut_picker.pick_shortcuts("assistant output") for _ in range(5)]
        )
        if first != [["TLDR"]] * 5:
            print(f"{FAIL} unexpected concurrent result: {first!r}")
            return False
        if _ProviderHandler.calls != 1:
            print(f"{FAIL} concurrent identical calls hit provider {_ProviderHandler.calls} times")
            return False

        second = await shortcut_picker.pick_shortcuts("assistant output")
        if second != ["TLDR"] or _ProviderHandler.calls != 1:
            print(f"{FAIL} cached call result={second!r} calls={_ProviderHandler.calls}")
            return False

        third = await shortcut_picker.pick_shortcuts("different output")
        if third != ["TLDR"] or _ProviderHandler.calls != 2:
            print(f"{FAIL} distinct input result={third!r} calls={_ProviderHandler.calls}")
            return False

        _ProviderHandler.delay = 0.4
        slow_start = time.monotonic()
        slow = await shortcut_picker.pick_shortcuts("slow output")
        elapsed = time.monotonic() - slow_start
        if slow != ["TLDR", "/Adv"] or elapsed > 0.35:
            print(f"{FAIL} slow picker did not fall back quickly result={slow!r} elapsed={elapsed:.3f}")
            return False
        await asyncio.sleep(0.35)
        _ProviderHandler.delay = 0.0
        cached_slow = await shortcut_picker.pick_shortcuts("slow output")
        if cached_slow != ["TLDR"]:
            print(f"{FAIL} timed-out picker did not populate cache: {cached_slow!r}")
            return False

        print(f"{PASS} shortcut picker coalesces and caches exact duplicate requests")
        return True
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    try:
        ok = asyncio.run(_run())
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
    raise SystemExit(0 if ok else 1)
