"""Regression test for the "No output" success-path bug.

The bug: on a successful native turn, the backend stores the
assistant's final text via `session_manager.update_running_content`
(session_manager.py:1994-2004). That helper dispatches change kind
`running_content_updated`, but the kind is NOT in
`SessionWSBroadcaster.on_change`'s allowlist
(session_ws_broadcaster.py:56-273) → silently dropped. Before the
fix, `_dispatch_messages_delta` only fired on lazy-create / error /
stopped paths (orchestrator.py:2277, 2501, 2552) — never on success.
The frontend kept the lazy-create empty-content snapshot for the
whole turn and rendered "No output" via `MessageBubble.tsx:1185`'s
`contentFallback`-falsy branch, even though the persisted
`session.json` had the full assistant reply.

The fix: drop the `if finalized_msg.get("error")` guard at
orchestrator.py:2500 — always dispatch the finalized message,
mirroring the stopped path at lines 2545-2552.

This test boots a real backend, drives a real native claude turn
over the WebSocket, captures every frame, and asserts three
invariants:

  (a) The success path emits at least one `messages_delta` frame
      after `_finalize_turn_messages` (which only matters because
      the broadcaster's silent drop means we MUST get a delta from
      the orchestrator directly).
  (b) The `messages_delta` payload's `messages[0].content` is
      non-empty and matches the persisted disk content (i.e. the
      content the user would see if they refetched). Catches a
      regression where the dispatch is in the wrong place (e.g.
      inside the batch) and the snapshot is stale.
  (c) The `messages_delta` strictly precedes the orchestrator's
      outer `turn_complete` (line 2503). Frame ordering matters
      because a `turn_complete` arriving first could trigger the
      frontend's teardown path and drop a late delta.

Manager-mode success path shares `_finalize_turn_messages`
(orchestrator.py:3121) and the same dispatch block (2486-2507), so
this test transitively covers it; a separate manager-mode driver
would need full delegation setup and is not in scope here.

Run with:
    cd backend && .venv/bin/python scripts/integration_test_messages_delta_on_success.py

Skips cleanly (exit 0) if `claude` is not on PATH (CI containers
without the CLI).
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
    """Sign a session cookie the way Starlette's SessionMiddleware does:
    TimestampSigner(session_secret).sign(b64(json(session))). Read-only —
    never writes keychain credentials."""
    import auth_secrets
    secret = auth_secrets.get_session_secret()
    signer = itsdangerous.TimestampSigner(str(secret))
    payload = base64.b64encode(
        json.dumps({"user": {"username": "integration-test"}}).encode()
    )
    return signer.sign(payload).decode("utf-8")


def _ensure_logs_dir(ba_home: str) -> None:
    os.makedirs(os.path.join(ba_home, "logs"), exist_ok=True)


async def main() -> int:
    ba_home = tempfile.mkdtemp(prefix="bc-int-msgdelta-home-")
    _ensure_logs_dir(ba_home)
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
    cwd = tempfile.mkdtemp(prefix="bc-int-msgdelta-cwd-")

    cookie = _mint_session_cookie()
    cookie_header = {"Cookie": f"bc_session={cookie}"}

    try:
        async with httpx.AsyncClient(
            base_url=base, timeout=180, cookies={"better_agent_session": cookie},
        ) as client:
            r = await client.get("/api/auth/me")
            if r.status_code != 200:
                _fail("auth probe", f"/api/auth/me HTTP {r.status_code}: {r.text[:200]}")
                return 1
            _ok("auth cookie accepted")

            # Create a native claude session. Default provider is fine;
            # the bug is provider-agnostic and lives in orchestrator's
            # success path.
            r = await client.post("/api/sessions", json={
                "name": "MsgDeltaIT",
                "model": "claude-haiku-4-5-20251001",
                "cwd": cwd,
                "orchestration_mode": "native",
            })
            if r.status_code != 200:
                _fail("create session", f"HTTP {r.status_code}: {r.text[:200]}")
                return 1
            sid = r.json()["id"]
            _ok(f"native claude session created id={sid[:8]}")

            # Capture every WS frame in arrival order so we can assert
            # (a) at least one messages_delta exists and (b) it
            # precedes the orchestrator's outer turn_complete.
            frames: list[dict] = []
            turn_done = asyncio.Event()
            ws_error: list[str] = []

            def _is_outer_turn_complete(evt: dict) -> bool:
                """The orchestrator's outer turn_complete (line 2503)
                carries `trace_id`. The inner runner-step turn_complete
                (line 2474) carries `session_id` + `token_usage` but
                NOT `trace_id`. Distinguishing is essential because
                the delta must precede the OUTER one — the inner one
                fires BEFORE _finalize_turn_messages and is not a
                teardown signal."""
                if evt.get("type") != "turn_complete":
                    return False
                d = evt.get("data") or {}
                return "trace_id" in d

            async def run_turn() -> None:
                try:
                    async with websockets.connect(
                        ws_url, additional_headers=cookie_header,
                    ) as ws:
                        await ws.send(json.dumps({
                            "type": "subscribe", "subscription_class": "foreground", "app_session_id": sid,
                        }))
                        await asyncio.sleep(0.3)
                        await ws.send(json.dumps({
                            "type": "send_message",
                            "prompt": "Reply with exactly the word: pong",
                            "app_session_id": sid,
                            "model": "claude-haiku-4-5-20251001",
                            "cwd": cwd,
                        }))
                        deadline = time.monotonic() + 150
                        while time.monotonic() < deadline:
                            try:
                                raw = await asyncio.wait_for(ws.recv(), timeout=5)
                            except asyncio.TimeoutError:
                                continue
                            evt = json.loads(raw)
                            frames.append(evt)
                            if _is_outer_turn_complete(evt):
                                # Drain a brief tail in case more
                                # frames are in flight (unlikely after
                                # the outer complete, but cheap).
                                try:
                                    while True:
                                        raw = await asyncio.wait_for(
                                            ws.recv(), timeout=0.2,
                                        )
                                        frames.append(json.loads(raw))
                                except (asyncio.TimeoutError,
                                        websockets.ConnectionClosed):
                                    pass
                                turn_done.set()
                                return
                            if evt.get("type") == "error":
                                ws_error.append(str(evt.get("data")))
                except Exception as e:
                    ws_error.append(f"{type(e).__name__}: {e}")
                finally:
                    turn_done.set()

            task = asyncio.create_task(run_turn())
            try:
                await asyncio.wait_for(turn_done.wait(), timeout=160)
            except asyncio.TimeoutError:
                _fail("turn", "no outer turn_complete within 160s (deadlock?)")
                return 1
            await task

            # Pull the disk-side ground truth FIRST so we can decide
            # whether this run is a real regression candidate or a
            # provider/auth failure to skip past. Invariant assertions
            # below all depend on the post-finalize disk state.
            r = await client.get(f"/api/sessions/{sid}")
            tree = r.json() if r.status_code == 200 else {}
            msgs = tree.get("messages", [])
            assistant = [m for m in msgs if m.get("role") == "assistant"]
            if not assistant:
                _fail("disk assistant message", f"none found; messages={msgs}")
                return 1
            final_asst = assistant[-1]
            disk_content = (final_asst.get("content") or "").strip()

            # Skip cleanly on auth/provider failures rather than
            # masquerading as a regression. Two conditions are taken
            # as "the test couldn't even drive a real turn":
            #   - ws_error captured a backend `error` frame (e.g.
            #     401 from the model, runner spawn failure)
            #   - the finalized assistant message itself is flagged
            #     `error: True` (set by `set_assistant_error` in
            #     `_finalize_turn_messages` when the run failed)
            # Without these skips, an unauthenticated CI environment
            # would PASS the test for the wrong reason: pre-fix and
            # post-fix both emit an empty lazy-create delta whose
            # content `""` matches an empty `disk_content`.
            if ws_error or final_asst.get("error"):
                print(
                    f"SKIP — provider/auth failure (ws_error={ws_error}, "
                    f"asst.error={final_asst.get('error')}); the test "
                    f"cannot distinguish the regression from the failure"
                )
                await client.delete(f"/api/sessions/{sid}")
                return 0

            # Regression guard: disk_content MUST be non-empty for the
            # invariants below to mean anything. An empty disk_content
            # would let the lazy-create empty-content delta match
            # (`"" == ""`) and produce a false PASS — masking a
            # regression in `_finalize_turn_messages` that nukes the
            # `update_running_content` call entirely.
            if not disk_content:
                _fail(
                    "regression guard",
                    "disk_content is empty after a successful turn — "
                    "`_finalize_turn_messages` failed to populate "
                    "`msg.content` via `update_running_content` (or the "
                    "extracted text is empty). Without non-empty content "
                    "the messages_delta invariants below would falsely "
                    "pass on a lazy-create empty delta.",
                )
                return 1

            # Invariant (a): the success path emitted at least one
            # messages_delta. PRE-FIX this list is empty for a
            # successful turn — every messages_delta call site
            # (orchestrator.py:2277/2501/2552) is gated on either
            # lazy-create / error / stopped.
            delta_indices = [
                i for i, f in enumerate(frames)
                if f.get("type") == "messages_delta"
            ]
            if not delta_indices:
                _fail(
                    "invariant (a)",
                    "no messages_delta frames received on success path; "
                    "frame types: "
                    + str([f.get("type") for f in frames]),
                )
                return 1
            _ok(f"invariant (a): {len(delta_indices)} messages_delta frame(s)")

            # Index every delta by (idx, content) so we can find
            # the success-path one — the FIRST delta whose
            # `messages[0].content` matches the post-finalize disk
            # state. Lazy-create's delta carries empty content;
            # late post-completion deltas from other paths (tailer
            # replays, finalize broadcasts) may carry the same
            # content but arrive AFTER the outer turn_complete.
            # The fix's invariant: AT LEAST ONE delta with the
            # finalized content lands BEFORE the outer
            # turn_complete.
            outer_complete_indices = [
                i for i, f in enumerate(frames) if _is_outer_turn_complete(f)
            ]
            if not outer_complete_indices:
                _fail(
                    "outer turn_complete",
                    "no outer turn_complete (with trace_id) seen; "
                    "frame types: "
                    + str([f.get("type") for f in frames]),
                )
                return 1
            outer_idx = outer_complete_indices[0]

            success_delta_idx = None
            for i in delta_indices:
                payload = (frames[i].get("data") or {}).get("messages") or []
                if not payload:
                    continue
                content = (payload[0].get("content") or "").strip()
                if content == disk_content and i < outer_idx:
                    success_delta_idx = i
                    break

            # Invariant (b): a delta with the finalized content
            # exists. Empty content here means the fix dispatched
            # too early (pre-finalize) or from a stale snapshot.
            if success_delta_idx is None:
                # Surface a useful diagnostic — show every delta's
                # content shape vs the outer turn_complete position.
                trail = []
                for i in delta_indices:
                    payload = (frames[i].get("data") or {}).get(
                        "messages",
                    ) or []
                    c = (payload[0].get("content") or "") if payload else ""
                    trail.append(f"idx={i} len={len(c)}")
                _fail(
                    "invariant (b)+(c)",
                    f"no messages_delta with the finalized content "
                    f"({len(disk_content)} chars: {disk_content[:60]!r}) "
                    f"landed before outer turn_complete (idx {outer_idx}). "
                    f"Deltas seen: {trail}",
                )
                return 1

            _ok(
                f"invariant (b): delta at idx {success_delta_idx} carries "
                f"the finalized content matching disk "
                f"({len(disk_content)} chars: {disk_content[:60]!r})"
            )
            _ok(
                f"invariant (c): success delta (idx {success_delta_idx}) "
                f"strictly precedes outer turn_complete (idx {outer_idx})"
            )

            # Delete the test session so its run dir doesn't linger
            # in the isolated home (cosmetic; tempdir is rm'd on exit).
            await client.delete(f"/api/sessions/{sid}")

    finally:
        server.stop()
        shutil.rmtree(cwd, ignore_errors=True)
        shutil.rmtree(ba_home, ignore_errors=True)

    print("\nall invariants held")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
