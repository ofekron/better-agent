"""Regression test: the session record is the authoritative source of
truth for `cwd` — a stale per-turn cwd riding in on send_message must
be discarded in favour of `session["cwd"]`.

Reproduces the incident trigger: the frontend sent a stale cwd
(`/workspace/better-agent`, the backend's own process dir) on
the second send of a session whose record said `/workspace/nns`.
`handle_prompt` guarded `orchestration_mode` from the record but
trusted `cwd` verbatim, so the runner spawned `claude --resume` with
the wrong cwd → wrong ~/.claude*/projects/<encoded-cwd> dir → "No
conversation found" → the turn died.

We create a native session whose record cwd is DIR_A, then send a
message carrying cwd=DIR_B. The spawned run's `input.json` (written
synchronously at spawn, before any claude result) must show DIR_A,
not DIR_B. A bogus resume sid makes the underlying claude run fail
fast (no API cost) — irrelevant to what we assert (input.json cwd).

Pre-fix: input.json cwd == DIR_B → FAIL. Post-fix: == DIR_A → PASS.

Run with:
    cd backend && .venv/bin/python scripts/test_cwd_authority.py
"""

from __future__ import annotations

import asyncio
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
_TMP_HOME = _test_home.isolate("bc-test-cwdauth-")
os.environ["BETTER_CLAUDE_API_ONLY"] = "1"

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import uvicorn  # noqa: E402
import httpx  # noqa: E402
import websockets  # noqa: E402

from auth_test_helpers import authenticate_async_client  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402
from provider_claude import _runs_root  # noqa: E402

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
                time.sleep(0.02)
        raise RuntimeError("uvicorn did not come up")

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)


async def _send_one(
    ws_url: str, app_session_id: str, frontend_cwd: str, timeout: float,
) -> dict | None:
    async with websockets.connect(ws_url) as ws:
        await ws.send(json.dumps({
            "type": "subscribe",
            "subscription_class": "foreground",
            "app_session_id": app_session_id,
            "cwd": frontend_cwd,
        }))
        # The server answers every subscribe frame; receiving the reply
        # proves the subscribe was consumed before send_message.
        await asyncio.wait_for(ws.recv(), timeout=10.0)
        await ws.send(json.dumps({
            "type": "send_message",
            "prompt": "hi",
            "model": MODEL,
            "cwd": frontend_cwd,
            "app_session_id": app_session_id,
            "send_mode": "send",
        }))
        # Keep the socket open only until the queue processor spawns the
        # run (input.json written). We don't need the turn to finish.
        return await _await_input_json(app_session_id, timeout=timeout)


async def _await_input_json(sid: str, timeout: float) -> dict | None:
    deadline = time.monotonic() + timeout
    root = _runs_root()
    while time.monotonic() < deadline:
        if root.exists():
            for ij in root.glob("*/input.json"):
                try:
                    data = json.loads(ij.read_text())
                except Exception:
                    continue
                if data.get("app_session_id") == sid:
                    return data
        await asyncio.sleep(0.02)
    return None


async def main() -> int:
    port = _free_port()
    server = _BackgroundUvicorn("main:app", port)
    server.start()
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=10) as client:
        token = await authenticate_async_client(client)
    ws_url = f"ws://127.0.0.1:{port}/ws/chat?token={token}"

    dir_a = tempfile.mkdtemp(prefix="bc-cwd-A-")   # authoritative record
    dir_b = tempfile.mkdtemp(prefix="bc-cwd-B-")   # stale frontend value
    failures = 0
    try:
        sess = session_manager.create(
            name="t", model=MODEL, cwd=dir_a, orchestration_mode="native",
        )
        sid = sess["id"]
        # Bogus resume target → underlying claude run fails fast and
        # free; input.json (with cwd) is still written at spawn.
        session_manager.set_agent_sid(sid, "native", str(uuid.uuid4()))

        data = await _send_one(ws_url, sid, frontend_cwd=dir_b, timeout=30.0)
        if data is None:
            print(f"{FAIL}  no run input.json appeared for session {sid[:8]}")
            return 1

        spawned = data.get("cwd")
        if spawned == dir_a:
            print(f"{PASS}  runner used authoritative session cwd "
                  f"({spawned!r}), discarded stale frontend cwd "
                  f"({dir_b!r})")
        else:
            print(f"{FAIL}  runner used cwd={spawned!r}; expected the "
                  f"session record cwd {dir_a!r} (stale frontend value "
                  f"{dir_b!r} should have been discarded)")
            failures += 1

        return 1 if failures else 0
    finally:
        server.stop()
        for d in (dir_a, dir_b):
            shutil.rmtree(d, ignore_errors=True)
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
