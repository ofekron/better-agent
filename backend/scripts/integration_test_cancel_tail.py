"""Real-CLI interrupt-then-resend interleaving test.

Drives the full backend with a real claude CLI:

  1. Start a long turn A (foreground bash sleeps).
  2. After A starts streaming, send an interrupt message B
     (send_mode="interrupt") on the same session.
  3. Wait for B's outer turn_complete.
  4. Assert NO event that streamed during turn A (any uuid seen on the
     WS before B's turn_start) is persisted inside turn B's assistant
     message. Pre-fix, the interrupted CLI's wind-down tail was
     orphan-ingested and seq-bracketed onto B — old-turn events
     rendered inside the new turn.

Run with:
    cd backend && .venv/bin/python scripts/integration_test_cancel_tail.py
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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
import itsdangerous
import uvicorn
import websockets


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class BackgroundUvicorn:
    def __init__(self, app_path: str, port: int):
        self.port = port
        self.app_path = app_path
        self.server: uvicorn.Server | None = None
        self.thread: threading.Thread | None = None

    def start(self):
        cfg = uvicorn.Config(
            self.app_path, host="127.0.0.1", port=self.port, log_level="warning",
        )
        self.server = uvicorn.Server(cfg)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), 0.2):
                    return
            except OSError:
                time.sleep(0.2)
        raise RuntimeError("uvicorn failed to start in 30s")

    def stop(self):
        if self.server:
            self.server.should_exit = True
        if self.thread:
            self.thread.join(timeout=10)


def _ok(label: str) -> None:
    print(f"\033[92mPASS\033[0m  {label}")


def _fail(label: str, why: str) -> None:
    print(f"\033[91mFAIL\033[0m  {label}: {why}")


def _mint_session_cookie() -> str:
    import auth_secrets
    secret = auth_secrets.get_session_secret()
    signer = itsdangerous.TimestampSigner(str(secret))
    payload = base64.b64encode(
        json.dumps({"user": {"username": "integration-test"}}).encode()
    )
    return signer.sign(payload).decode("utf-8")


def _collect_uuids(obj) -> set[str]:
    out: set[str] = set()
    if isinstance(obj, dict):
        u = obj.get("uuid")
        if isinstance(u, str):
            out.add(u)
        for v in obj.values():
            out |= _collect_uuids(v)
    elif isinstance(obj, list):
        for v in obj:
            out |= _collect_uuids(v)
    return out


MODEL = "claude-haiku-4-5-20251001"
PROMPT_A = (
    "Run this exact bash command in the foreground and wait for it, then "
    "report done: for i in $(seq 1 12); do echo tick$i; sleep 2; done"
)
PROMPT_B = "Stop everything. Reply with exactly the word: pong"


async def main() -> int:
    ba_home = tempfile.mkdtemp(prefix="bc-int-canceltail-home-")
    os.makedirs(os.path.join(ba_home, "logs"), exist_ok=True)
    os.environ["BETTER_CLAUDE_HOME"] = ba_home
    os.environ["BETTER_AGENT_HOME"] = ba_home
    print(f"BETTER_CLAUDE_HOME = {ba_home}")

    if shutil.which("claude") is None:
        print("SKIP — `claude` CLI not on PATH")
        return 0

    port = free_port()
    server = BackgroundUvicorn("main:app", port)
    server.start()
    base = f"http://127.0.0.1:{port}"
    ws_url = f"ws://127.0.0.1:{port}/ws/chat"
    cwd = tempfile.mkdtemp(prefix="bc-int-canceltail-cwd-")
    cookie = _mint_session_cookie()
    cookie_header = {"Cookie": f"bc_session={cookie}"}
    failures = 0

    try:
        async with httpx.AsyncClient(
            base_url=base, timeout=240, cookies={"better_agent_session": cookie},
        ) as client:
            r = await client.post("/api/sessions", json={
                "name": "CancelTailIT",
                "model": MODEL,
                "cwd": cwd,
                "orchestration_mode": "native",
            })
            if r.status_code != 200:
                _fail("create session", f"HTTP {r.status_code}: {r.text[:200]}")
                return 1
            sid = r.json()["id"]
            _ok(f"session created id={sid[:8]}")

            pre_b_uuids: set[str] = set()
            saw_b_turn_start = asyncio.Event()
            done = asyncio.Event()
            errors: list[str] = []

            async def drive() -> None:
                try:
                    async with websockets.connect(
                        ws_url, additional_headers=cookie_header,
                    ) as ws:
                        await ws.send(json.dumps({
                            "type": "subscribe", "app_session_id": sid,
                        }))
                        await asyncio.sleep(0.3)
                        await ws.send(json.dumps({
                            "type": "send_message",
                            "prompt": PROMPT_A,
                            "app_session_id": sid,
                            "model": MODEL,
                            "cwd": cwd,
                        }))
                        interrupted = False
                        a_streaming = False
                        deadline = time.monotonic() + 200
                        while time.monotonic() < deadline:
                            try:
                                raw = await asyncio.wait_for(ws.recv(), timeout=5)
                            except asyncio.TimeoutError:
                                continue
                            evt = json.loads(raw)
                            etype = evt.get("type")
                            if not saw_b_turn_start.is_set():
                                pre_b_uuids.update(_collect_uuids(evt))
                            if etype == "agent_message" and not a_streaming:
                                a_streaming = True
                                # A is streaming — give the CLI time to
                                # enter the long bash, then interrupt.
                                await asyncio.sleep(6)
                                await ws.send(json.dumps({
                                    "type": "send_message",
                                    "prompt": PROMPT_B,
                                    "app_session_id": sid,
                                    "model": MODEL,
                                    "cwd": cwd,
                                    "send_mode": "interrupt",
                                }))
                                interrupted = True
                                continue
                            if (
                                interrupted
                                and etype == "turn_start"
                                and not saw_b_turn_start.is_set()
                            ):
                                # First turn_start after the interrupt
                                # send is B's.
                                saw_b_turn_start.set()
                            if (
                                etype == "turn_complete"
                                and "trace_id" in (evt.get("data") or {})
                                and saw_b_turn_start.is_set()
                            ):
                                done.set()
                                return
                            if etype == "error":
                                errors.append(str(evt.get("data")))
                except Exception as e:
                    errors.append(f"{type(e).__name__}: {e}")
                finally:
                    done.set()

            task = asyncio.create_task(drive())
            try:
                await asyncio.wait_for(done.wait(), timeout=220)
            except asyncio.TimeoutError:
                _fail("turns", "did not finish within 220s")
                return 1
            await task
            if errors:
                _fail("ws", f"errors during run: {errors[:3]}")
                return 1
            _ok("turn A interrupted, turn B completed")

            # Disk-side ground truth.
            r = await client.get(f"/api/sessions/{sid}")
            if r.status_code != 200:
                _fail("fetch session", f"HTTP {r.status_code}")
                return 1
            msgs = (r.json() or {}).get("messages", [])
            assistants = [m for m in msgs if m.get("role") == "assistant"]
            if len(assistants) < 2:
                _fail("messages", f"expected 2 assistant msgs, got {len(assistants)}")
                return 1
            msg_b = assistants[-1]
            b_uuids = {
                e.get("uuid") for e in (msg_b.get("events") or [])
                if isinstance(e, dict) and e.get("uuid")
            }
            # Drop B's own streamed uuids: anything seen only AFTER B's
            # turn_start is B's. pre_b_uuids stopped accumulating at
            # B's turn_start, so it is exactly turn A's stream.
            leaked = b_uuids & pre_b_uuids
            if leaked:
                _fail(
                    "interleaving",
                    f"{len(leaked)} turn-A event(s) inside turn B's message: "
                    f"{sorted(leaked)[:5]}",
                )
                failures += 1
            else:
                _ok("no turn-A events inside turn B's message")

            await client.delete(f"/api/sessions/{sid}")
    finally:
        server.stop()
        shutil.rmtree(ba_home, ignore_errors=True)
        shutil.rmtree(cwd, ignore_errors=True)

    print()
    print("ALL PASS" if failures == 0 else f"{failures} FAILURE(S)")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
