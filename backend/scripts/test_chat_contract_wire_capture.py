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
  5. The real lazy `GET .../messages/{id}/events` full-events fetch for a
     stubbed message.
  6. A MANAGER-mode session with a completed worker delegation: the real
     `workers` array shape on both REST and `messages_replay`.
  7. The real `GET .../messages?before_seq=N` pagination response for a
     session with several turn pairs.

Writes all of the above to frontend/tests/__fixtures__/chat_contract_wire.json so
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

DELEGATION_ID = "contract-delegation-1"
WORKER_UUID = "contract-worker-uuid"
WORKER_TEXT = "worker reply text"

PAGINATION_TURNS = 4


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


def _seed_manager_worker_session() -> tuple[str, str]:
    """Create a MANAGER-mode session with a completed worker delegation
    panel on the owning assistant message, via the same `apply_event`
    primitive a real delegation uses — no orchestrator/provider CLI.
    Returns (session_id, owner_msg_id)."""
    from event_journal import event_journal_writer
    from orchs import ApplyEventCtx, get_strategy
    from session_manager import manager as session_manager

    sess = session_manager.create(
        name="chat-contract-manager-capture", model="sonnet", cwd="/tmp",
        orchestration_mode="manager", source="cli",
    )
    sid = sess["id"]
    strategy = get_strategy("manager")

    user_msg = {"id": "u1", "role": "user", "content": "delegate this to a worker", "events": []}
    session_manager.append_user_msg(sid, user_msg)

    owner = strategy.build_assistant_scaffold()
    owner_id = owner["id"]
    session_manager.append_assistant_msg(sid, owner)

    ctx = ApplyEventCtx(manager_sid_holder={"id": None}, user_msg=None, root_id=sid)
    strategy.apply_event(
        app_session_id=sid, msg=owner,
        event={
            "type": "worker_start",
            "data": {
                "delegation_id": DELEGATION_ID,
                "worker_session_id": "contract-worker-session",
                "worker_description": "contract test worker",
                "is_new": True,
                "instructions_preview": "do the thing",
            },
        },
        ctx=ctx, source_is_provider_stream=True,
    )
    strategy.apply_event(
        app_session_id=sid, msg=owner,
        event={
            "type": "worker_event",
            "data": {
                "delegation_id": DELEGATION_ID,
                "event": {
                    "type": "agent_message",
                    "data": {
                        "uuid": WORKER_UUID, "type": "assistant",
                        "message": {"content": [{"type": "text", "text": WORKER_TEXT}]},
                    },
                },
            },
        },
        ctx=ctx, source_is_provider_stream=True,
    )
    strategy.apply_event(
        app_session_id=sid, msg=owner,
        event={
            "type": "worker_complete",
            "data": {"delegation_id": DELEGATION_ID, "success": True},
        },
        ctx=ctx, source_is_provider_stream=True,
    )
    event_journal_writer.barrier_sync(sid)
    session_manager.set_streaming(sid, owner_id, False)
    return sid, owner_id


def _seed_paginated_session() -> str:
    """Create a session with several user/assistant turn pairs so the
    `GET /messages?before_seq=N` pagination endpoint has real older
    history to page through. Returns session_id."""
    from event_journal import event_journal_writer
    from orchs import ApplyEventCtx, get_strategy
    from session_manager import manager as session_manager

    sess = session_manager.create(
        name="chat-contract-pagination-capture", model="sonnet", cwd="/tmp",
        orchestration_mode="native", source="cli",
    )
    sid = sess["id"]
    strategy = get_strategy("native")
    ctx = ApplyEventCtx(manager_sid_holder=None, user_msg=None, root_id=sid)

    for i in range(PAGINATION_TURNS):
        user_msg = {"id": f"pu{i}", "role": "user", "content": f"turn {i} question", "events": []}
        session_manager.append_user_msg(sid, user_msg)
        scaffold = strategy.build_assistant_scaffold()
        scaffold_id = scaffold["id"]
        session_manager.append_assistant_msg(sid, scaffold)
        ev = {
            "type": "agent_message",
            "data": {
                "uuid": f"pagination-uuid-{i}", "type": "assistant",
                "message": {"content": [{"type": "text", "text": f"turn {i} answer"}]},
            },
        }
        strategy.apply_event(
            app_session_id=sid, msg=scaffold, event=ev, ctx=ctx,
            source_is_provider_stream=True,
        )
        session_manager.set_streaming(sid, scaffold_id, False)
    event_journal_writer.barrier_sync(sid)
    return sid


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


async def _capture_replay(
    ws_url: str, cookie_header: str, sid: str, timeout: float = 10.0,
) -> dict | None:
    """Open a fresh WS connection, subscribe cold (since_seq=0), and
    return the real `messages_replay` frame."""
    async with websockets.connect(
        ws_url, additional_headers={"Cookie": cookie_header},
    ) as ws:
        await ws.send(json.dumps({
            "type": "subscribe", "app_session_id": sid, "since_seq": 0,
        }))
        return await _recv_until(
            ws, lambda f: f.get("type") == "messages_replay", timeout=timeout,
        )


async def _capture_manager_worker_scenario(
    client: httpx.AsyncClient, ws_url: str, cookie_header: str, failures: list[str],
) -> dict | None:
    """Manager-mode session with a completed worker delegation: capture
    the real REST shape (the `workers` array on the owning message) and
    the real `messages_replay` WS frame carrying the same panel."""
    sid, owner_id = _seed_manager_worker_session()

    r = await client.get(f"/api/sessions/{sid}")
    if r.status_code != 200:
        _fail("manager/worker REST snapshot", f"status={r.status_code} body={r.text[:300]}", failures)
        return None
    rest_snapshot = r.json()
    owner_msg = next(
        (m for m in rest_snapshot.get("messages", []) if m.get("id") == owner_id), None,
    )
    panel = next(
        (w for w in (owner_msg or {}).get("workers", []) if w.get("delegation_id") == DELEGATION_ID),
        None,
    )
    if panel is None:
        _fail("manager/worker REST snapshot", "delegation panel missing from workers array", failures)
    elif panel.get("success") is not True:
        _fail("manager/worker REST snapshot", f"panel.success expected True, got {panel.get('success')!r}", failures)
    elif panel.get("events"):
        # `GET /api/sessions/{id}` never inlines worker-panel events (no
        # stub/preview either, unlike top-level messages) — the panel
        # metadata (delegation_id, success, description) is real, but
        # `events` is always empty here by design. A non-empty list
        # would mean that contract changed.
        _fail(
            "manager/worker REST snapshot",
            f"expected panel.events empty on plain GET, got {len(panel['events'])} entries",
            failures,
        )
    else:
        _ok("REST GET /api/sessions/{id} returns the worker delegation panel (metadata only, events empty by design)")

    # Worker panel events are only available via the SAME lazy
    # full-events fetch used to un-stub the owning message's own events
    # (MessageBubble.tsx's `needsFetch` merges `hydrated.workers` too).
    r_full = await client.get(f"/api/sessions/{sid}/messages/{owner_id}/events")
    owner_full: dict | None = None
    if r_full.status_code != 200:
        _fail("manager/worker lazy events fetch", f"status={r_full.status_code} body={r_full.text[:300]}", failures)
    else:
        owner_full = r_full.json()
        full_panel = next(
            (w for w in (owner_full or {}).get("workers", []) if w.get("delegation_id") == DELEGATION_ID),
            None,
        )
        if full_panel is None:
            _fail("manager/worker lazy events fetch", "delegation panel missing from full-fetch response", failures)
        elif not any(
            (e.get("data") or {}).get("uuid") == WORKER_UUID for e in full_panel.get("events", [])
        ):
            _fail("manager/worker lazy events fetch", "worker reply event missing from full-fetch panel", failures)
        else:
            _ok("REST lazy /messages/{id}/events fetch returns the full worker panel events")

    replay_frame = await _capture_replay(ws_url, cookie_header, sid)
    if replay_frame is None:
        _fail("manager/worker WS messages_replay", "no messages_replay frame received", failures)
    else:
        replay_owner = next(
            (m for m in (replay_frame.get("data") or {}).get("messages", [])
             if m.get("id") == owner_id),
            None,
        )
        replay_panel = next(
            (w for w in (replay_owner or {}).get("workers", [])
             if w.get("delegation_id") == DELEGATION_ID),
            None,
        )
        if replay_panel is None:
            _fail("manager/worker WS messages_replay", "delegation panel missing from replay", failures)
        else:
            _ok("WS messages_replay carries the worker delegation panel")

    return {
        "sessionId": sid,
        "ownerMsgId": owner_id,
        "delegationId": DELEGATION_ID,
        "restSnapshot": rest_snapshot,
        "wsMessagesReplay": replay_frame,
        "restOwnerFullFetch": owner_full,
    }


async def _capture_pagination_scenario(
    client: httpx.AsyncClient, failures: list[str],
) -> dict | None:
    """Session with several turn pairs: capture a small first page (REST
    GET with msg_limit) and the real `GET .../messages?before_seq=N`
    response for the older page."""
    sid = _seed_paginated_session()

    r = await client.get(f"/api/sessions/{sid}", params={"msg_limit": 2})
    if r.status_code != 200:
        _fail("pagination first page", f"status={r.status_code} body={r.text[:300]}", failures)
        return None
    first_page = r.json()
    pagination = first_page.get("pagination") or {}
    if not pagination.get("has_older"):
        _fail("pagination first page", f"expected has_older=True, got pagination={pagination!r}", failures)
        return None
    _ok("REST GET /api/sessions/{id}?msg_limit=2 reports has_older=True")

    oldest = pagination.get("oldest_loaded_seq")
    r2 = await client.get(f"/api/sessions/{sid}/messages", params={"before_seq": oldest})
    if r2.status_code != 200:
        _fail("pagination older page", f"status={r2.status_code} body={r2.text[:300]}", failures)
        return None
    older_page = r2.json()
    first_page_ids = {m.get("id") for m in first_page.get("messages", [])}
    older_ids = {m.get("id") for m in older_page.get("messages", [])}
    if not older_ids:
        _fail("pagination older page", "GET .../messages?before_seq returned no messages", failures)
    elif older_ids & first_page_ids:
        _fail("pagination older page", "older page overlaps with the first page", failures)
    else:
        _ok("REST GET /api/sessions/{id}/messages?before_seq returns distinct older messages")

    return {
        "sessionId": sid,
        "firstPage": first_page,
        "olderPage": older_page,
    }


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

            # 6. Manager-mode session with a completed worker delegation.
            manager_worker_scenario = await _capture_manager_worker_scenario(
                client, ws_url, cookie_header, failures,
            )

            # 7. REST pagination (GET .../messages?before_seq=N).
            pagination_scenario = await _capture_pagination_scenario(client, failures)

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
                        "managerWorkerScenario": manager_worker_scenario,
                        "paginationScenario": pagination_scenario,
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
