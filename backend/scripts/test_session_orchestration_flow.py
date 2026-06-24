"""Integration test: session lifecycle REST + WS surface.

Drives the real FastAPI app (HTTP + WebSocket) via TestClient — exactly
like the frontend does — through:

    * WS subscribe → REST /fork → expect `session_forked` frame
    * PATCH /selectors rejects `orchestration_mode` (frozen post-create)
    * DELETE on unknown session id is a no-op (returns {deleted: False})

No claude CLI subprocess: we're testing the session state machine and
its REST + WS surface, not turn execution.

Run with:
    cd backend && .venv/bin/python scripts/test_session_orchestration_flow.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

# Per CLAUDE.md, isolate ~/.better-claude state to a tempdir BEFORE
# importing any backend module.
import _test_home
_TMP_HOME = _test_home.isolate("bc-test-orchestration-flow-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
import session_store  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402


PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _reset_home() -> None:
    sessions_dir = Path(_TMP_HOME) / "sessions"
    if sessions_dir.exists():
        shutil.rmtree(sessions_dir)
    session_store._fork_index.clear()
    session_store._index_loaded = False
    session_manager._roots.clear()
    session_manager._node_root_id.clear()
    session_manager._root_locks.clear()
    session_manager._batches.clear()


def _list_ids_modes(client: TestClient) -> dict[str, str]:
    """Return {session_id: orchestration_mode} from GET /api/sessions."""
    r = client.get("/api/sessions")
    assert r.status_code == 200, f"GET /api/sessions failed: {r.status_code}"
    return {s["id"]: s["orchestration_mode"] for s in r.json()["sessions"]}


def _create(client: TestClient, name: str, mode: str | None = None) -> str:
    body: dict = {"name": name, "cwd": "/tmp"}
    if mode is not None:
        body["orchestration_mode"] = mode
    r = client.post("/api/sessions", json=body)
    assert r.status_code == 200, f"POST /api/sessions failed: {r.status_code} {r.text}"
    return r.json()["id"]


def _delete(client: TestClient, sid: str) -> int:
    return client.delete(f"/api/sessions/{sid}").status_code


def _recv_until(ws, frame_type: str, max_frames: int = 8) -> list[dict]:
    """Synchronously read frames from `ws` until one with
    `type == frame_type` arrives, or `max_frames` have been read.
    Returns the full list (including the matching frame at the end if
    found). receive_json() blocks; we cap the read count so a
    never-arriving frame can't hang the test."""
    out: list[dict] = []
    for _ in range(max_frames):
        msg = ws.receive_json()
        out.append(msg)
        if msg.get("type") == frame_type:
            return out
    return out


def _find_node(tree: dict, sid: str) -> dict | None:
    """Walk a session tree (root + nested forks) and return the node
    whose id matches `sid`, or None."""
    if tree.get("id") == sid:
        return tree
    for f in (tree.get("forks") or []):
        hit = _find_node(f, sid)
        if hit is not None:
            return hit
    return None


def _node_mode(client: TestClient, sid: str) -> str:
    """orchestration_mode of the node with id `sid`. GET /api/sessions/{sid}
    returns the ROOT tree (any sid resolves up); we walk into it to find
    the requested node so this also works for fork ids."""
    r = client.get(f"/api/sessions/{sid}")
    assert r.status_code == 200
    node = _find_node(r.json(), sid)
    assert node is not None, f"node {sid} not found in tree"
    return node["orchestration_mode"]


# ──────────────────────────────────────────────────────────────────────
# WebSocket integration: subscribe + observe session_forked frame
# ──────────────────────────────────────────────────────────────────────

def test_ws_session_forked_broadcast(client: TestClient) -> bool:
    """Open a WS, subscribe to a session, fork it via REST, and assert
    a `session_forked` frame fans out. This pins the broadcast wiring
    end-to-end (session_manager listener → SessionWSBroadcaster →
    coordinator.broadcast → WS frame)."""
    _reset_home()

    # Pre-create a forkable parent.
    a = _create(client, "ws-parent", "manager")
    session_manager.set_agent_sid(a, "manager", "fake-claude-ws")
    session_manager.append_user_msg(a, {
        "id": "u1", "role": "user", "content": "hi", "events": [],
        "timestamp": "2026-05-01T00:00:00", "isStreaming": False,
    })

    with client.websocket_connect("/ws/chat") as ws:
        ws.send_json({
            "type": "subscribe",
            "app_session_id": a,
            "since_seq": 0,
        })
        # Subscribe triggers an immediate replay (messages_replay +
        # run_state). Read those off so they don't show up in the
        # post-fork drain.
        ws.receive_json()  # messages_replay
        ws.receive_json()  # run_state

        # Trigger the fork via REST.
        r = client.post(f"/api/sessions/{a}/fork")
        if r.status_code != 200:
            print(f"{FAIL} ws_session_forked — POST /fork: {r.status_code}")
            return False
        fork_id = r.json()["id"]

        frames = _recv_until(ws, "session_forked", max_frames=4)

    forked_frames = [f for f in frames if f.get("type") == "session_forked"]
    if len(forked_frames) != 1:
        print(f"{FAIL} ws_session_forked — got {len(forked_frames)} session_forked frames; all frames: {[f.get('type') for f in frames]}")
        return False
    payload = forked_frames[0]["data"]
    ok = (
        payload.get("parent_session_id") == a
        and payload.get("session", {}).get("id") == fork_id
        and payload.get("session", {}).get("orchestration_mode") == "manager"
    )
    print(f"{PASS if ok else FAIL} WS session_forked frame fires after REST /fork")
    return ok


# ──────────────────────────────────────────────────────────────────────
# Edge cases
# ──────────────────────────────────────────────────────────────────────

def test_switch_invalid_mode_rejected(client: TestClient) -> bool:
    """PATCH /selectors rejects ANY orchestration_mode in the body —
    the mode is frozen at session creation time (per main.py:651-655),
    so an attempt to change it (valid or invalid value) returns 409
    and leaves the record unchanged."""
    _reset_home()
    sid = _create(client, "x", "manager")

    r = client.patch(
        f"/api/sessions/{sid}/selectors",
        json={"orchestration_mode": "wat"},
    )
    listed = _list_ids_modes(client)
    ok = r.status_code == 409 and listed == {sid: "manager"}
    print(f"{PASS if ok else FAIL} PATCH selectors with orchestration_mode → 409, record unchanged")
    return ok


def test_delete_unknown_returns_falsey(client: TestClient) -> bool:
    """DELETE /api/sessions/{id} on a missing id returns 200 with
    {deleted: False} — not 404. This matches the existing behavior;
    the test pins it so a future "raise on missing" change is a
    deliberate decision, not an accident."""
    _reset_home()
    r = client.delete("/api/sessions/nonexistent-id")
    ok = r.status_code == 200 and r.json().get("deleted") is False
    print(f"{PASS if ok else FAIL} DELETE unknown session → {{deleted:false}}")
    return ok


def test_ws_send_persists_before_processor(client: TestClient) -> bool:
    _reset_home()
    sid = _create(client, "durable-send", "native")
    called = threading.Event()
    captured: dict = {}
    original_submit = main.coordinator.submit_prompt

    def fake_submit(app_session_id: str, params: dict) -> str:
        captured["app_session_id"] = app_session_id
        captured["params"] = dict(params)
        called.set()
        return params["_queued_id"]

    main.coordinator.submit_prompt = fake_submit
    try:
        with client.websocket_connect("/ws/chat") as ws:
            ws.send_json({
                "type": "send_message",
                "prompt": "survive infra crash",
                "model": "sonnet",
                "cwd": "/tmp",
                "app_session_id": sid,
                "orchestration_mode": "native",
                "send_mode": "queue",
                "client_id": "pending-durable",
            })
            if not called.wait(timeout=2):
                print(f"{FAIL} WS send durability — submit_prompt was not called")
                return False
    finally:
        main.coordinator.submit_prompt = original_submit

    session_manager.flush_pending_persists()
    raw = session_store.get_session(sid)
    queued = (raw or {}).get("queued_prompts") or []
    params = captured.get("params") or {}
    ok = (
        captured.get("app_session_id") == sid
        and len(queued) == 1
        and queued[0].get("id") == params.get("_queued_id")
        and queued[0].get("kind") == "send"
        and queued[0].get("content") == "survive infra crash"
        and queued[0].get("client_id") == "pending-durable"
        and not (raw or {}).get("messages")
    )
    print(f"{PASS if ok else FAIL} WS send persists accepted prompt before processor")
    return ok


def test_ws_send_error_echoes_prompt_correlation(client: TestClient) -> bool:
    _reset_home()
    sid = _create(client, "correlated-error", "native")
    with client.websocket_connect("/ws/chat") as ws:
        ws.send_json({
            "type": "send_message",
            "prompt": "bad file",
            "model": "sonnet",
            "cwd": "/tmp",
            "app_session_id": sid,
            "orchestration_mode": "native",
            "send_mode": "queue",
            "client_id": "pending-error",
            "files": [{"name": "too-large.txt", "data": "x", "size": 11 * 1024 * 1024}],
        })
        frames = _recv_until(ws, "error", max_frames=4)

    error_frame = next((f for f in frames if f.get("type") == "error"), None)
    data = (error_frame or {}).get("data") or {}
    ok = (
        data.get("app_session_id") == sid
        and data.get("session_id") == sid
        and data.get("client_id") == "pending-error"
        and "exceeds 10 MB" in (data.get("error") or "")
    )
    print(f"{PASS if ok else FAIL} WS send validation error echoes session/client ids")
    return ok


def test_dequeued_prompt_removed_when_turn_fails(client: TestClient) -> bool:
    _reset_home()
    sid = _create(client, "failed-dequeue", "native")
    started = threading.Event()
    original_handle_prompt = main.coordinator.handle_prompt

    async def fake_handle_prompt(**_params) -> None:
        started.set()
        raise RuntimeError("forced turn failure")

    main.coordinator.handle_prompt = fake_handle_prompt
    try:
        with client.websocket_connect("/ws/chat") as ws:
            ws.send_json({
                "type": "send_message",
                "prompt": "will fail after dequeue",
                "model": "sonnet",
                "cwd": "/tmp",
                "app_session_id": sid,
                "orchestration_mode": "native",
                "send_mode": "queue",
                "client_id": "pending-fail-after-dequeue",
            })
            if not started.wait(timeout=2):
                print(f"{FAIL} dequeued prompt removal — processor did not start")
                return False
            deadline = time.time() + 2
            raw = session_store.get_session(sid) or {}
            while time.time() < deadline:
                session_manager.flush_pending_persists()
                raw = session_store.get_session(sid) or {}
                if raw.get("queued_prompts") == []:
                    break
                time.sleep(0.05)
    finally:
        main.coordinator.handle_prompt = original_handle_prompt

    ok = (raw.get("queued_prompts") == [])
    print(f"{PASS if ok else FAIL} dequeued prompt removed when turn fails")
    return ok


# ──────────────────────────────────────────────────────────────────────
# Test runner
# ──────────────────────────────────────────────────────────────────────

def main_runner() -> int:
    os.environ["BETTER_CLAUDE_TEST_AUTH_BYPASS"] = "1"
    try:
        client = TestClient(main.app, client=("127.0.0.1", 50000))
        tests = [
            test_ws_session_forked_broadcast,
            test_switch_invalid_mode_rejected,
            test_delete_unknown_returns_falsey,
            test_ws_send_persists_before_processor,
            test_ws_send_error_echoes_prompt_correlation,
            test_dequeued_prompt_removed_when_turn_fails,
        ]
        results = [t(client) for t in tests]
    finally:
        os.environ.pop("BETTER_CLAUDE_TEST_AUTH_BYPASS", None)

    failed = sum(1 for r in results if not r)
    print()
    if failed == 0:
        print(f"{PASS} all {len(results)} tests passed")
        rc = 0
    else:
        print(f"{FAIL} {failed}/{len(results)} tests failed")
        rc = 1

    shutil.rmtree(_TMP_HOME, ignore_errors=True)
    return rc


if __name__ == "__main__":
    sys.exit(main_runner())
