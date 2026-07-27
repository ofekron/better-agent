"""Regression: a prompt sent while a turn is in flight must be kind="queued_behind".

User-visible contract: the frontend only renders a queued banner for
kinds "queued_behind"/"interrupt" (frontend/src/utils/queuedPrompts.ts).
If the backend stamps kind="send" for a prompt that is actually queued
behind a running turn, the user sees no banner and believes the prompt
was dropped.

Scenarios covered:
  1. turn actually running through the per-session processor
     (`_in_flight_prompts` populated) -> queued_behind
  2. externally-registered / recovered run (`active_run_ids` populated,
     e.g. after a backend restart re-hooks a detached runner) -> queued_behind

Run with:
    cd backend && .venv/bin/python scripts/test_queue_kind_while_turn_in_flight.py
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home  # noqa: E402
_TMP_HOME = _test_home.isolate("bc-test-queue-kind-")
os.environ["BETTER_CLAUDE_TEST_AUTH_BYPASS"] = "1"

import _test_installation  # noqa: E402
from pathlib import Path  # noqa: E402
_test_installation.activate(Path(_TMP_HOME))

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
import auth  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


async def _receive_json_or_none(ws, timeout: float = 0.2):
    import anyio

    with anyio.move_on_after(timeout):
        message = await ws._send_rx.receive()
        if message.get("type") == "websocket.send":
            text = message.get("text")
            if text is not None:
                return json.loads(text)
    return None


def _frame_session_id(frame: dict) -> str | None:
    data = frame.get("data") or {}
    return data.get("app_session_id") or data.get("session_id")


def _await_lifecycle(ws, sid: str, client_id: str, attempts: int = 40):
    for _ in range(attempts):
        frame = ws.portal.call(_receive_json_or_none, ws, 0.2)
        if frame is None:
            continue
        if frame.get("type") != "user_message_queued":
            continue
        if _frame_session_id(frame) != sid:
            continue
        if (frame.get("data") or {}).get("client_id") != client_id:
            continue
        return frame
    return None


class _TurnGate:
    """Stub for `coordinator.handle_prompt` that parks inside the turn."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    async def __call__(self, **kwargs):
        self.calls += 1
        self.started.set()
        deadline = time.monotonic() + 15.0
        while not self.release.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(0.02)


def _new_session(name: str) -> str:
    return session_manager.create(
        name=name,
        model="m",
        cwd=_TMP_HOME,
        orchestration_mode="native",
    )["id"]


def _send(ws, sid: str, prompt: str, client_id: str, send_mode: str = "queue") -> None:
    ws.send_json({
        "type": "send_message",
        "prompt": prompt,
        "model": "m",
        "cwd": _TMP_HOME,
        "app_session_id": sid,
        "send_mode": send_mode,
        "client_id": client_id,
    })


def in_flight_turn_test(client: TestClient) -> bool:
    sid = _new_session("queue-kind-in-flight")
    gate = _TurnGate()
    original = main.coordinator.handle_prompt
    main.coordinator.handle_prompt = gate
    first_kind = second_kind = None
    try:
        token = auth.create_token("test")
        with client.websocket_connect(f"/ws/chat?token={token}") as ws:
            _send(ws, sid, "first", "c-inflight-1")
            first = _await_lifecycle(ws, sid, "c-inflight-1")
            first_kind = (first or {}).get("data", {}).get("kind")

            # Wait until the processor has really entered the turn.
            entered = gate.started.wait(10.0)

            _send(ws, sid, "second", "c-inflight-2")
            second = _await_lifecycle(ws, sid, "c-inflight-2")
            second_kind = (second or {}).get("data", {}).get("kind")
    finally:
        gate.release.set()
        time.sleep(0.5)
        main.coordinator.handle_prompt = original

    ok = entered and first_kind == "send" and second_kind == "queued_behind"
    print(
        f"{PASS if ok else FAIL} prompt during live turn -> queued_behind "
        f"-- entered_turn={entered} first_kind={first_kind!r} "
        f"second_kind={second_kind!r}",
    )
    return bool(ok)


def recovered_run_test(client: TestClient) -> bool:
    """A detached/recovered run registered only in `active_run_ids`."""
    sid = _new_session("queue-kind-recovered")
    gate = _TurnGate()
    original = main.coordinator.handle_prompt
    main.coordinator.handle_prompt = gate
    tm = main.coordinator.turn_manager
    tm.active_run_ids.setdefault(sid, []).append("recovered-run-1")
    kind = None
    try:
        token = auth.create_token("test")
        with client.websocket_connect(f"/ws/chat?token={token}") as ws:
            _send(ws, sid, "while recovered run alive", "c-recovered-1")
            frame = _await_lifecycle(ws, sid, "c-recovered-1")
            kind = (frame or {}).get("data", {}).get("kind")
    finally:
        tm.active_run_ids.pop(sid, None)
        gate.release.set()
        time.sleep(0.5)
        main.coordinator.handle_prompt = original

    ok = kind == "queued_behind"
    print(
        f"{PASS if ok else FAIL} prompt during recovered run -> queued_behind "
        f"-- kind={kind!r}",
    )
    return ok


def failed_steer_fallback_test(client: TestClient) -> bool:
    """send_mode="steer" on a provider with supports_steering=False.

    Steering finds no owning provider, falls back to "queue"; the prompt
    is still behind the live turn, so it must stay "queued_behind".
    """
    sid = _new_session("queue-kind-failed-steer")
    gate = _TurnGate()
    original = main.coordinator.handle_prompt
    main.coordinator.handle_prompt = gate
    kind = None
    try:
        token = auth.create_token("test")
        with client.websocket_connect(f"/ws/chat?token={token}") as ws:
            _send(ws, sid, "first", "c-steer-1")
            _await_lifecycle(ws, sid, "c-steer-1")
            entered = gate.started.wait(10.0)
            _send(ws, sid, "second", "c-steer-2", send_mode="steer")
            frame = _await_lifecycle(ws, sid, "c-steer-2")
            kind = (frame or {}).get("data", {}).get("kind")
    finally:
        gate.release.set()
        time.sleep(0.5)
        main.coordinator.handle_prompt = original

    ok = bool(entered) and kind == "queued_behind"
    print(
        f"{PASS if ok else FAIL} failed steer during live turn -> queued_behind "
        f"-- entered_turn={entered} kind={kind!r}",
    )
    return ok


if __name__ == "__main__":
    try:
        with TestClient(main.app, client=("127.0.0.1", 50000)) as client:
            ok = in_flight_turn_test(client)
            ok = recovered_run_test(client) and ok
            ok = failed_steer_fallback_test(client) and ok
        sys.exit(0 if ok else 1)
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
