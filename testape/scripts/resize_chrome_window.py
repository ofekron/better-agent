#!/usr/bin/env python3
"""Resize a TestApe Chrome window via CDP so its viewport is desktop width.

The Better Agent composer sends on Enter only at desktop viewport width
(`enterIsNewline = viewport.mode !== "desktop"` in InputArea.tsx), and the
session sidebar collapses into a drawer below the desktop breakpoint. CDP
`Emulation.setDeviceMetricsOverride` is per-CDP-session and does NOT carry
into a flow run's own session, so the only persistent fix is resizing the
real OS window. Run this once after `testape chrome start`:

    python3 testape/scripts/resize_chrome_window.py --port 9224 --width 1440 --height 900
"""
import argparse
import asyncio
import json
import urllib.request

import websockets


async def resize(port: int, width: int, height: int) -> None:
    targets = json.load(urllib.request.urlopen(f"http://localhost:{port}/json"))
    page = next((t for t in targets if t.get("type") == "page"), None)
    if page is None:
        raise SystemExit(f"no page target on port {port}")
    browser_ws = json.load(
        urllib.request.urlopen(f"http://localhost:{port}/json/version")
    )["webSocketDebuggerUrl"]
    async with websockets.connect(browser_ws, max_size=None) as c:
        gid = 0

        async def call(method: str, params: dict | None = None):
            nonlocal gid
            gid += 1
            await c.send(json.dumps({"id": gid, "method": method, "params": params or {}}))
            while True:
                msg = json.loads(await c.recv())
                if msg.get("id") == gid:
                    if "error" in msg:
                        raise RuntimeError(f"{method} error: {msg['error']}")
                    return msg.get("result", {})

        wid = (await call("Browser.getWindowForTarget", {"targetId": page["id"]}))["windowId"]
        await call(
            "Browser.setWindowBounds",
            {"windowId": wid, "bounds": {"width": width, "height": height, "windowState": "normal"}},
        )
    print(f"resized port={port} windowId={wid} -> {width}x{height}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--width", type=int, default=1440)
    ap.add_argument("--height", type=int, default=900)
    a = ap.parse_args()
    asyncio.run(resize(a.port, a.width, a.height))


if __name__ == "__main__":
    main()
