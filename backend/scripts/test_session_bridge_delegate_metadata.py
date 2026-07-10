"""Session-bridge delegated prompt metadata.

Run:
    cd backend && .venv/bin/python scripts/test_session_bridge_delegate_metadata.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import types

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-session-bridge-metadata-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _fake_runtime  # noqa: E402
import session_bridge  # noqa: E402


FAILURES: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(f"  {'✓' if cond else '✗'} {msg}")
    if not cond:
        FAILURES.append(msg)


async def test_run_turn_submits_root_display_metadata() -> None:
    captured: dict = {}

    class FakeCoordinator:
        def register_ws(self, sid, callback, *, from_seq=0):
            captured["registered"] = (sid, from_seq)

        def unregister_ws(self, sid, callback):
            captured["unregistered"] = sid

        def submit_prompt(self, sid, params):
            captured["sid"] = sid
            captured["params"] = params
            callback = params["ws_callback"]
            asyncio.create_task(callback({
                "type": "user_message_done",
                "data": {
                    "lifecycle_msg_id": params["lifecycle_msg_id"],
                    "success": True,
                },
            }))

    class FakeIngester:
        def max_seq_by_sid(self, sid):
            return {}

    class FakeBus:
        def subscribe(self, *args, **kwargs):
            captured["subscribed"] = kwargs.get("name")

        def unsubscribe(self, name):
            captured["unsubscribed"] = name

    class FakeSessionManager:
        def get(self, sid):
            return {
                "model": "m",
                "cwd": "/repo",
                "orchestration_mode": "native",
                "messages": [
                    {"role": "assistant", "id": "assistant-1", "content": "done"},
                ],
            }

    original_event_ingester = sys.modules.get("event_ingester")
    original_event_bus = sys.modules.get("event_bus")
    original_session_manager = session_bridge.session_manager
    sys.modules["event_ingester"] = types.SimpleNamespace(event_ingester=FakeIngester())
    sys.modules["event_bus"] = types.SimpleNamespace(bus=FakeBus())
    session_bridge.session_manager = FakeSessionManager()
    try:
        with _fake_runtime.bind_coordinator(FakeCoordinator()):
            result = await session_bridge._run_turn(
                "target-sid",
                "model-facing task",
                display_prompt=(
                    '<delegated-task source="tool" role="worker">'
                    "<user_prompt>model-facing task</user_prompt>"
                    "</delegated-task>"
                ),
                source="tool:worker",
                client_id="tool-worker-r1",
            )
    finally:
        if original_event_ingester is None:
            sys.modules.pop("event_ingester", None)
        else:
            sys.modules["event_ingester"] = original_event_ingester
        if original_event_bus is None:
            sys.modules.pop("event_bus", None)
        else:
            sys.modules["event_bus"] = original_event_bus
        session_bridge.session_manager = original_session_manager

    params = captured["params"]
    check(result["text"] == "done", "delegated turn returns final assistant text")
    check(params["prompt"].startswith("<delegated-task"), "display prompt is persisted as root user message")
    check(params["cli_prompt"] == "model-facing task", "model-facing prompt stays unwrapped")
    check(params["source"] == "tool:worker", "source metadata is forwarded")
    check(params["client_id"] == "tool-worker-r1", "client id is forwarded")
    check(captured["unregistered"] == "target-sid", "temporary websocket registration is cleaned up")


async def main() -> int:
    try:
        await test_run_turn_submits_root_display_metadata()
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} assertion(s)")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
