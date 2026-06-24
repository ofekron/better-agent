from __future__ import annotations

import asyncio
import os
import sys
import tempfile

import _test_home
_test_home.isolate("bc-test-global-ws-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from orchestrator import Coordinator  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


async def test_global_broadcast_reaches_unsubscribed_ws() -> bool:
    coordinator = Coordinator()
    received: list[dict] = []

    async def callback(event: dict) -> None:
        received.append(event)

    coordinator.register_global_ws(callback)
    await coordinator.broadcast_global("projects_changed", {})

    ok = received == [{"type": "projects_changed", "data": {}}]
    print(f"{PASS if ok else FAIL} global broadcast reaches unsubscribed WS")
    return ok


async def test_global_broadcast_dedupes_session_subscribed_ws() -> bool:
    coordinator = Coordinator()
    received: list[dict] = []

    async def callback(event: dict) -> None:
        received.append(event)

    coordinator.register_global_ws(callback)
    coordinator.register_ws("sid-1", callback)
    await coordinator.broadcast_global("projects_changed", {})

    ok = len(received) == 1 and received[0]["type"] == "projects_changed"
    print(f"{PASS if ok else FAIL} global broadcast dedupes subscribed WS")
    return ok


async def main_runner() -> int:
    tests = [
        test_global_broadcast_reaches_unsubscribed_ws,
        test_global_broadcast_dedupes_session_subscribed_ws,
    ]
    results = [await test() for test in tests]
    failed = sum(1 for result in results if not result)
    print()
    if failed:
        print(f"{FAIL} {failed}/{len(results)} tests failed")
        return 1
    print(f"{PASS} all {len(results)} tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main_runner()))
