"""Wire-contract capture for the chat panel REST + WS data flow.

Boots the REAL backend (uvicorn, in a background thread, isolated
`BETTER_AGENT_HOME`) and drives REAL REST + WS traffic against it — no
Claude/Codex/AGY CLI subprocess, no provider network call. Captures:

  1. The real `GET /api/sessions/{id}` JSON for a freshly seeded session
     (seeded via `session_manager`/`orchs.apply_event` directly, the same
     helper `test_apply_event_unified.py` uses — no live turn).
  2. The real `messages_replay` WS frame sent on `/ws/chat` subscribe.
  3. A live WS push frame, produced by `event_ingester.ingest(...)` (the
     same direct journal-write primitive `apply_event`'s live path and
     worker-fanout call sites use) while the WS client is connected — no
     orchestrator/provider turn involved. `BetterAgentJsonlTailer` tails
     events.jsonl and fans this out to our open socket exactly as it
     would for a real live turn.
  4. The REST snapshot again after that live push, showing the projected
     result (GET /api/sessions/{id} reconciles from events.jsonl on read).

Writes all four to frontend/tests/__fixtures__/chat_contract_wire.json so
`frontend/tests/chatContractWire.test.ts` can feed them into the existing
`renderApp()` / `mockBackend` / `mockWebSocket` harness and assert the
derived render state — proving the real wire shape and the harness's
hand-built fixtures agree, without booting a browser.

Run with:
    cd backend && .venv/bin/python scripts/test_chat_contract_wire_capture.py

Re-run (and re-commit the fixture) whenever the REST session shape or the
WS envelope format changes.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import httpx
import uvicorn
import websockets

import _test_home

# Pre-import so we can monkey-patch credential verification for this
# process. `auth_routes.login` looks up `auth.verify_credentials` via
# module attribute at call time, so patching the attribute here (before
# the real request lands) is enough regardless of import order — same
# rationale/pattern as `integration_test_startup.py`.
import auth as _auth


async def _bypass_credentials(*_args, **_kwargs) -> bool:
    return True


_auth.verify_credentials = _bypass_credentials

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"

FIXTURE_PATH = (
    Path(_BACKEND).parent
    / "frontend"
    / "tests"
    / "__fixtures__"
    / "chat_contract_wire.json"
)

SEED_UUID = "contract-seed-uuid"
SEED_TEXT = "seeded assistant reply"
LIVE_UUID = "contract-live-uuid"
LIVE_TEXT = "live pushed assistant reply"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class BackgroundUvicorn:
    """uvicorn driven from a thread. Duplicated from
    `integration_test_startup.py`'s `BackgroundUvicorn` (not imported —
    that file's helper isn't exported)."""

    def __init__(self, port: int):
        self.port = port
        self.server: uvicorn.Server | None = None
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        config = uvicorn.Config(
            "main:app", host="127.0.0.1", port=self.port, log_level="warning",
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()

    def wait_ready(self, timeout: float = 60.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.thread is not None and not self.thread.is_alive():
                # The daemon thread died — almost certainly `main:app`
                # raised during import (e.g. a concurrent session's git
                # worktree under backend/.claude/worktrees/ was mid-churn
                # when operation_catalog's startup scan walked it — see
                # spawned follow-up task). Fail fast instead of waiting
                # out the full timeout on a thread that can never listen.
                raise RuntimeError("uvicorn thread died during startup (see stderr)")
            try:
                with socket.create_connection(("127.0.0.1", self.port), 0.2):
                    return
            except OSError:
                time.sleep(0.05)
        raise RuntimeError(f"uvicorn failed to start in {timeout}s")

    def stop(self) -> None:
        if self.server:
            self.server.should_exit = True
        if self.thread:
            self.thread.join(timeout=10)


def _ok(label: str) -> None:
    print(f"{PASS}  {label}")


def _fail(label: str, why: str, failures: list[str]) -> None:
    failures.append(f"{label}: {why}")
    print(f"{FAIL}  {label}: {why}")


async def login(client: httpx.AsyncClient) -> None:
    r = await client.post(
        "/api/auth/login", json={"username": "test", "password": "test"},
    )
    if r.status_code >= 300:
        raise RuntimeError(f"login failed: {r.status_code} {r.text}")
    if not client.cookies.jar:
        raise RuntimeError("login succeeded but set no session cookie")


def _cookie_header_from_client(client: httpx.AsyncClient) -> str:
    return "; ".join(f"{c.name}={c.value}" for c in client.cookies.jar)


def _seed_session() -> tuple[str, str]:
    """Create a session with one user + one completed assistant message,
    via the SAME `apply_event` primitive a live turn uses — no provider
    CLI, no network call. Returns (session_id, assistant_msg_id)."""
    from event_journal import event_journal_writer
    from orchs import ApplyEventCtx, get_strategy
    from session_manager import manager as session_manager

    sess = session_manager.create(
        name="chat-contract-capture", model="sonnet", cwd="/tmp",
        orchestration_mode="native", source="cli",
    )
    sid = sess["id"]
    strategy = get_strategy("native")

    user_msg = {"id": "u1", "role": "user", "content": "hello from contract test", "events": []}
    session_manager.append_user_msg(sid, user_msg)

    scaffold = strategy.build_assistant_scaffold()
    msg_id = scaffold["id"]
    session_manager.append_assistant_msg(sid, scaffold)

    ctx = ApplyEventCtx(manager_sid_holder=None, user_msg=None, root_id=sid)
    ev = {
        "type": "agent_message",
        "data": {
            "uuid": SEED_UUID, "type": "assistant",
            "message": {"content": [{"type": "text", "text": SEED_TEXT}]},
        },
    }
    strategy.apply_event(
        app_session_id=sid, msg=scaffold, event=ev, ctx=ctx,
        source_is_provider_stream=True,
    )
    event_journal_writer.barrier_sync(sid)
    session_manager.set_streaming(sid, msg_id, False)
    return sid, msg_id


def _push_live_event(sid: str, msg_id: str) -> None:
    """Append a new event directly to events.jsonl via the same direct
    ingest primitive `apply_event`'s live path and worker-fanout call
    sites use. `BetterAgentJsonlTailer` (already tailing this root
    because our WS client subscribed) fans it out over the open socket —
    no orchestrator/provider turn involved."""
    from event_ingester import event_ingester

    event_ingester.ingest(
        sid, sid=sid, event_type="agent_message",
        data={
            "uuid": LIVE_UUID, "type": "assistant",
            "message": {"content": [{"type": "text", "text": LIVE_TEXT}]},
        },
        source="contract_test", msg_id=msg_id,
    )


async def _recv_until(
    ws, predicate, timeout: float,
) -> dict | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        except asyncio.TimeoutError:
            break
        try:
            frame = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if predicate(frame):
            return frame
    return None


def _boot(timeout: float = 180.0) -> tuple[BackgroundUvicorn, int, "_test_home.TestHome"]:
    """Boot uvicorn once, with a generous timeout.

    `main.py`'s import triggers `operation_catalog`'s startup integrity
    scan, which currently walks `backend/.claude/worktrees/` (a separate,
    already-flagged bug) — so a CONCURRENT session's git worktree churn
    can slow our own cold boot well past a normal timeout. A single
    generous wait absorbs that external flakiness.

    Deliberately NOT a retry loop: `main.py` has process-lifetime
    module-level singletons (e.g. `hot_path_executor`'s thread pool)
    that get torn down on `on_shutdown`. A timed-out attempt that is
    killed but was still mid-startup can finish importing and run its
    shutdown handlers AFTER we've already moved on, poisoning those
    shared singletons for any later attempt in the SAME process —
    observed as `RuntimeError: cannot schedule new futures after
    shutdown` on the very first request of a "successful" retry. One
    attempt per process avoids that hazard entirely."""
    home = _test_home.TestHome.acquire_installed("bc-chat-contract-")
    port = free_port()
    server = BackgroundUvicorn(port)
    server.start()
    try:
        server.wait_ready(timeout=timeout)
    except RuntimeError:
        server.stop()
        home.release()
        raise
    return server, port, home


async def amain() -> int:
    failures: list[str] = []
    server, port, home = _boot()

    try:
        sid, msg_id = _seed_session()

        base = f"http://127.0.0.1:{port}"
        ws_url = f"ws://127.0.0.1:{port}/ws/chat"

        async with httpx.AsyncClient(base_url=base, timeout=5.0) as client:
            await login(client)
            cookie_header = _cookie_header_from_client(client)

            # 1. Real REST snapshot.
            r = await client.get(f"/api/sessions/{sid}")
            if r.status_code != 200:
                _fail("REST GET /api/sessions/{id}", f"status={r.status_code} body={r.text[:300]}", failures)
                return 1
            rest_initial = r.json()
            if not any(m.get("id") == "u1" for m in rest_initial.get("messages", [])):
                _fail("REST initial snapshot", "seeded user message missing", failures)
            elif not any(
                m.get("id") == msg_id and SEED_TEXT in (m.get("content") or "")
                for m in rest_initial.get("messages", [])
            ):
                _fail("REST initial snapshot", "seeded assistant content missing", failures)
            else:
                _ok("REST GET /api/sessions/{id} returns seeded messages")

            # 2 & 3. Real WS replay frame, then a real live push frame.
            async with websockets.connect(
                ws_url, additional_headers={"Cookie": cookie_header},
            ) as ws:
                await ws.send(json.dumps({
                    "type": "subscribe", "app_session_id": sid, "since_seq": 0,
                }))
                replay_frame = await _recv_until(
                    ws, lambda f: f.get("type") == "messages_replay", timeout=10.0,
                )
                if replay_frame is None:
                    _fail("WS messages_replay", "no messages_replay frame received", failures)
                elif not any(
                    m.get("id") == msg_id
                    for m in (replay_frame.get("data") or {}).get("messages", [])
                ):
                    _fail("WS messages_replay", "seeded assistant message missing from replay", failures)
                else:
                    _ok("WS messages_replay carries seeded messages")

                _push_live_event(sid, msg_id)
                live_frame = await _recv_until(
                    ws,
                    lambda f: (f.get("data") or {}).get("uuid") == LIVE_UUID,
                    timeout=10.0,
                )
                if live_frame is None:
                    _fail("WS live push", "no frame with the live-pushed uuid arrived", failures)
                else:
                    _ok(f"WS live push delivered a {live_frame.get('type')!r} frame")

            # 4. REST snapshot after the live push (reconciled from events.jsonl).
            rest_after_live: dict | None = None
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                r2 = await client.get(f"/api/sessions/{sid}")
                candidate = r2.json()
                asst = next(
                    (m for m in candidate.get("messages", []) if m.get("id") == msg_id),
                    None,
                )
                if asst and LIVE_TEXT in (asst.get("content") or ""):
                    rest_after_live = candidate
                    break
                await asyncio.sleep(0.1)
            if rest_after_live is None:
                _fail("REST after live push", "live-pushed content never appeared in REST snapshot", failures)
            else:
                _ok("REST GET /api/sessions/{id} reflects the live-pushed content")

            # 5. Real lazy full-events fetch (the endpoint the frontend calls
            # to hydrate a stubbed message — see MessageBubble.tsx's
            # `needsFetch`/`messageWithHydratedRenderPayload`).
            r3 = await client.get(f"/api/sessions/{sid}/messages/{msg_id}/events")
            if r3.status_code != 200:
                _fail(
                    "REST lazy message-events fetch",
                    f"status={r3.status_code} body={r3.text[:300]}",
                    failures,
                )
                message_events_full = None
            else:
                message_events_full = r3.json()
                full_events = message_events_full.get("events") or []
                if not any(
                    (e.get("data") or {}).get("uuid") == LIVE_UUID for e in full_events
                ):
                    _fail(
                        "REST lazy message-events fetch",
                        "live-pushed event missing from the full events list",
                        failures,
                    )
                else:
                    _ok("REST lazy message-events fetch returns the full (unstubbed) events")

        if not failures:
            FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
            FIXTURE_PATH.write_text(
                json.dumps(
                    {
                        "sessionId": sid,
                        "assistantMsgId": msg_id,
                        "restInitial": rest_initial,
                        "wsMessagesReplay": replay_frame,
                        "wsLivePush": live_frame,
                        "restAfterLivePush": rest_after_live,
                        "restMessageEventsFull": message_events_full,
                    },
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            _ok(f"wrote fixture to {FIXTURE_PATH}")
    finally:
        server.stop()
        home.release()

    print()
    if failures:
        for f in failures:
            print(f"{FAIL}  {f}")
        print(f"{len(failures)} check(s) FAILED")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(amain()))
