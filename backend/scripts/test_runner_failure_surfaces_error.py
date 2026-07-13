"""Regression test: a native runner that dies at startup must surface
its error to the assistant message AND over the WS — never leave a
permanently blank streaming bubble.

Reproduces the incident where a Native session's `claude --resume`
hit a conversation that didn't exist for the spawned cwd. The runner
exited 1 with "No conversation found", wrote
`complete.json {"success": false, "error": "ProcessError ..."}`,
emitted NO jsonl output and NO "error" stream event — only a
"complete" event. `_drive_cli_run` read the error solely from
"error"-type events, so `primary_result["error"]` was None, the
`set_assistant_error` guard never fired, and the error-gated
`messages_delta` broadcast was skipped. The bubble stayed
content:"" isStreaming:false with no error, forever.

We force the exact failure by pinning `agent_session_id` to a
bogus UUID with no transcript on disk: `claude --resume <bogus>`
fails fast and deterministically (no API turn, no cost) via the same
complete-only path.

Asserts (hardened per review — NOT "any messages_delta", since the
eager pre-spawn frame at orchestrator.py:1646 satisfies that even on
the broken code):
  1. the finalized assistant message has error=True + non-empty
     errorText (pre-fix: no `error` key → FAIL).
  2. a `messages_delta` frame whose messages[0].error is truthy was
     broadcast — the gated frame, distinct from the eager error-less
     one (pre-fix: none → FAIL).

Run with:
    cd backend && .venv/bin/python scripts/test_runner_failure_surfaces_error.py
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

# Per CLAUDE.md: isolate ~/.better-claude state BEFORE importing any
# backend module.
import _test_home
_TMP_HOME = _test_home.isolate("bc-test-runfail-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import itsdangerous  # noqa: E402
import uvicorn  # noqa: E402
import websockets  # noqa: E402

import auth_secrets  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402


def _mint_session_cookie() -> str:
    """Sign a session cookie matching Starlette's SessionMiddleware so
    /ws/chat's policy gate accepts the WS handshake. Read-only on the
    keychain — never writes credentials."""
    secret = auth_secrets.get_session_secret()
    signer = itsdangerous.TimestampSigner(str(secret))
    payload = base64.b64encode(
        json.dumps({"user": {"username": "integration-test"}}).encode()
    )
    return signer.sign(payload).decode("utf-8")

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"
MODEL = "claude-haiku-4-5-20251001"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _BackgroundUvicorn:
    def __init__(self, app_path: str, port: int) -> None:
        self._cfg = uvicorn.Config(
            app_path, host="127.0.0.1", port=port, log_level="warning",
        )
        self.server = uvicorn.Server(self._cfg)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self) -> None:
        self.thread.start()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(
                    ("127.0.0.1", self._cfg.port), 0.2
                ):
                    return
            except OSError:
                time.sleep(0.2)
        raise RuntimeError("uvicorn did not come up")

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)


async def _drive_turn(
    ws_url: str, app_session_id: str, cwd: str, timeout: float,
    cookie_header: dict,
) -> list[dict]:
    """Subscribe, send one prompt, collect frames until `turn_complete`
    (plus a short drain) or timeout."""
    frames: list[dict] = []
    async with websockets.connect(ws_url, additional_headers=cookie_header) as ws:
        await ws.send(json.dumps({
            "type": "subscribe",
            "subscription_class": "foreground",
            "app_session_id": app_session_id,
            "cwd": cwd,
        }))
        await asyncio.sleep(0.3)
        await ws.send(json.dumps({
            "type": "send_message",
            "prompt": "hi",
            "model": MODEL,
            "cwd": cwd,
            "app_session_id": app_session_id,
            "send_mode": "send",
        }))
        deadline = time.monotonic() + timeout
        saw_complete = False
        drain_until = 0.0
        while time.monotonic() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=0.5)
            except asyncio.TimeoutError:
                if saw_complete and time.monotonic() >= drain_until:
                    break
                continue
            except websockets.ConnectionClosed:
                break
            try:
                f = json.loads(raw)
            except json.JSONDecodeError:
                continue
            frames.append(f)
            if f.get("type") == "turn_complete" and not saw_complete:
                saw_complete = True
                drain_until = time.monotonic() + 1.0
    return frames


async def main() -> int:
    port = _free_port()
    server = _BackgroundUvicorn("main:app", port)
    server.start()
    ws_url = f"ws://127.0.0.1:{port}/ws/chat"
    cwd = tempfile.mkdtemp(prefix="bc-runfail-cwd-")
    cookie = _mint_session_cookie()
    cookie_header = {"Cookie": f"bc_session={cookie}"}
    failures = 0
    try:
        sess = session_manager.create(
            name="t", model=MODEL, cwd=cwd, orchestration_mode="native",
        )
        sid = sess["id"]
        # Pin a bogus resume target with no transcript anywhere →
        # `claude --resume <bogus>` exits 1 fast, complete-only path.
        session_manager.set_agent_sid(sid, "native", str(uuid.uuid4()))

        frames = await _drive_turn(ws_url, sid, cwd, timeout=120.0, cookie_header=cookie_header)

        if not any(f.get("type") == "turn_complete" for f in frames):
            print(f"{FAIL}  never received turn_complete (frames="
                  f"{[f.get('type') for f in frames]})")
            return 1

        # (1) finalized assistant message carries the error on disk.
        msgs = (session_manager.get(sid) or {}).get("messages", [])
        asst = next(
            (m for m in reversed(msgs) if m.get("role") == "assistant"),
            None,
        )
        if asst is None:
            print(f"{FAIL}  no assistant message persisted")
            failures += 1
        elif asst.get("error") is True and (asst.get("errorText") or "").strip():
            print(f"{PASS}  assistant message surfaced error: "
                  f"{asst['errorText'][:80]!r}")
        else:
            print(f"{FAIL}  assistant message did NOT surface the runner "
                  f"failure: error={asst.get('error')!r} "
                  f"errorText={asst.get('errorText')!r} "
                  f"content={asst.get('content')!r}")
            failures += 1

        # (2) an *errored* messages_delta was broadcast (not the eager
        # error-less pre-spawn frame).
        errored = [
            f for f in frames
            if f.get("type") == "messages_delta"
            and any(
                m.get("error")
                for m in (f.get("data") or {}).get("messages", [])
            )
        ]
        if errored:
            print(f"{PASS}  errored messages_delta broadcast "
                  f"({len(errored)} frame(s))")
        else:
            n_delta = sum(
                1 for f in frames if f.get("type") == "messages_delta"
            )
            print(f"{FAIL}  no errored messages_delta broadcast "
                  f"({n_delta} messages_delta frame(s), none with error) "
                  f"— UI would stay blank/stuck")
            failures += 1

        return 1 if failures else 0
    finally:
        server.stop()
        shutil.rmtree(cwd, ignore_errors=True)
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
