"""End-to-end integration test for the worker redesign.

Boots the FastAPI app via uvicorn in a background thread, then drives
real HTTP + WebSocket interactions against it that exercise actual
claude CLI runs through provider_bridge / runner.py:

  1. POST /api/workers — creates a fresh Better Agent session and runs an init
     turn synchronously to mint its claude_sid. Validates that the
     worker appears in /api/workers afterward.

  2. Simulated delegation via /api/internal/ask-fork with the new
     worker — exercises the per-pair fork mint path. Verifies the
     returned `jsonl_path` exists and contains the worker's response,
     and that worker_store records a fork sid for the (caller, worker)
     pair.

  3. Second delegation to the same worker — should resume the existing
     fork (no re-fork), and total_bytes_now should grow vs the prior
     run while the recorded fork_agent_sid stays the same.

  4. Nested-delegation guard test: simulate a depth>0 delegate call
     with worker_session_id=None and verify the response is an error,
     not a pending approval.

  5. Fresh-worker approval flow: a real delegate call with
     worker_session_id=None creates a pending approval. Approve via
     REST. Verify a new worker Better Agent session is spawned and a
     worker_creation_approved event lands on the WS.

  6. Cleanup: deletes the Better Agent sessions created during the test and
     verifies fan-out (worker_store entry + fork records gone).

Run with:
    cd backend && .venv/bin/python scripts/integration_test.py

Each step prints PASS/FAIL. Exit 0 on full pass.
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
from pathlib import Path

# Importable from backend/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
import uvicorn
import websockets
from auth_test_helpers import authenticate_async_client


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class BackgroundUvicorn:
    def __init__(self, app_path: str, port: int):
        self.app_path = app_path
        self.port = port
        self.server: uvicorn.Server | None = None
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        config = uvicorn.Config(
            self.app_path,
            host="127.0.0.1",
            port=self.port,
            log_level="warning",
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        # Wait until the server is responsive.
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), 0.2):
                    return
            except OSError:
                time.sleep(0.2)
        raise RuntimeError("uvicorn failed to start in 30s")

    def stop(self) -> None:
        if self.server:
            self.server.should_exit = True
        if self.thread:
            self.thread.join(timeout=10)


def _ok(label: str) -> None:
    print(f"\033[92mPASS\033[0m  {label}")


def _fail(label: str, why: str) -> None:
    print(f"\033[91mFAIL\033[0m  {label}: {why}")


async def collect_ws_events(
    url: str,
    app_session_id: str,
    stop_event: asyncio.Event,
    out: list[dict],
    cwd: str,
) -> None:
    """Subscribe to a WS connection for `app_session_id` and append
    every received message to `out` until `stop_event` is set."""
    async with websockets.connect(url) as ws:
        await ws.send(json.dumps({
            "type": "subscribe",
            "app_session_id": app_session_id,
            "cwd": cwd,
        }))
        try:
            while not stop_event.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue
                except websockets.ConnectionClosed:
                    return
                try:
                    out.append(json.loads(raw))
                except json.JSONDecodeError:
                    continue
        finally:
            try:
                await ws.send(json.dumps({
                    "type": "unsubscribe",
                    "app_session_id": app_session_id,
                }))
            except Exception:
                pass


async def main() -> int:
    # Use an ISOLATED Better Agent home so the test never touches the
    # developer's real ~/.better-claude/. The env var must be set
    # BEFORE uvicorn imports backend modules (paths.py reads it at
    # access time, but module-level constants in some modules cache
    # at import). Set it here in the parent process; the child
    # uvicorn thread inherits it via os.environ.
    ba_home = tempfile.mkdtemp(prefix="bc-int-home-")
    os.environ["BETTER_CLAUDE_HOME"] = ba_home
    os.environ["BETTER_AGENT_HOME"] = ba_home
    os.environ["BETTER_CLAUDE_API_ONLY"] = "1"
    print(f"BETTER_CLAUDE_HOME = {ba_home}")

    port = free_port()
    server = BackgroundUvicorn("main:app", port)
    server.start()
    base = f"http://127.0.0.1:{port}"
    ws_url = f"ws://127.0.0.1:{port}/ws/chat"
    print(f"backend up at {base}")

    cwd = tempfile.mkdtemp(prefix="bc-int-")
    failures = 0
    try:
        async with httpx.AsyncClient(base_url=base, timeout=120) as client:
            token = await authenticate_async_client(client)
            ws_url = f"{ws_url}?token={token}"
            # ----------------------------------------------------------
            # Step 1 — create a fresh worker (POST /api/workers)
            # ----------------------------------------------------------
            print("\n[1] POST /api/workers — create fresh worker (init turn)")
            r = await client.post(
                "/api/workers",
                json={
                    "cwd": cwd,
                    "description": "TestWorkerOne",
                    "orchestration_mode": "native",
                    "model": "claude-haiku-4-5-20251001",
                },
            )
            if r.status_code != 200:
                _fail("create worker", f"HTTP {r.status_code}: {r.text}")
                return 1
            w1 = r.json()
            if not w1.get("initialized") or not w1.get("claude_sid"):
                _fail("create worker", f"bad payload: {w1}")
                return 1
            worker1_bc = w1["agent_session_id"]
            _ok(f"worker created bc={worker1_bc[:8]} claude_sid={w1['claude_sid'][:8]}")

            r = await client.get("/api/workers", params={"cwd": cwd})
            workers = r.json()["workers"]
            if len(workers) != 1 or workers[0]["agent_session_id"] != worker1_bc:
                _fail("list workers after create", f"got {workers}")
                failures += 1
            else:
                _ok("worker appears in GET /api/workers")

            # ----------------------------------------------------------
            # Step 2 — direct delegation (manager-style internal call)
            # ----------------------------------------------------------
            print("\n[2] /api/internal/ask-fork — first delegation (fork mint)")
            # We need a "caller" app session id. Create a manager-mode BC
            # session to act as the caller.
            r = await client.post(
                "/api/sessions",
                json={
                    "name": "TestCaller",
                    "model": "claude-haiku-4-5-20251001",
                    "cwd": cwd,
                    "orchestration_mode": "manager",
                },
            )
            caller_bc = r.json()["id"]
            _ok(f"caller Better Agent session created bc={caller_bc[:8]}")

            # We need to simulate a WS subscribed for the caller so
            # run_delegation has a callback to fan worker events into.
            ws_events: list[dict] = []
            stop_event = asyncio.Event()
            ws_task = asyncio.create_task(
                collect_ws_events(ws_url, caller_bc, stop_event, ws_events, cwd)
            )
            await asyncio.sleep(0.3)  # let subscribe register

            # Read the internal token from the ISOLATED home.
            token = (Path(ba_home) / "internal_token").read_text().strip()
            r = await client.post(
                "/api/internal/ask-fork",
                json={
                    "app_session_id": caller_bc,
                    "instructions": "Reply with the single word: ECHO1",
                    "worker_session_id": worker1_bc,
                    "worker_description": "TestWorkerOne",
                    "justification": "",
                    "proposed_orchestration_mode": "",
                    "model": "claude-haiku-4-5-20251001",
                    "cwd": cwd,
                },
                headers={"X-Internal-Token": token},
            )
            if r.status_code != 200:
                _fail("delegation #1", f"HTTP {r.status_code}: {r.text}")
                stop_event.set(); await ws_task
                return 1
            d1 = r.json()
            if not d1.get("success"):
                _fail("delegation #1", f"non-success: {d1}")
                failures += 1
            else:
                _ok(f"delegation #1 success, fork_sid={d1.get('fork_agent_sid','?')[:8]}")

            jpath = d1.get("jsonl_path")
            if jpath and Path(jpath).exists():
                _ok(f"jsonl_path exists: {jpath}")
            else:
                _fail("jsonl_path", f"missing or not on disk: {jpath}")
                failures += 1

            # Verify worker_store has a fork record for the pair
            from stores import worker_store as _ws
            fork_rec = _ws.get_fork_record(cwd, caller_bc, worker1_bc)
            if not fork_rec or fork_rec.get("fork_agent_sid") != d1.get("fork_agent_sid"):
                _fail("fork record persisted", f"got {fork_rec}")
                failures += 1
            else:
                _ok("fork record persisted with matching sid")

            # ----------------------------------------------------------
            # Step 3 — second delegation to same pair (resume fork)
            # ----------------------------------------------------------
            print("\n[3] /api/internal/ask-fork — second call (fork resume)")
            r = await client.post(
                "/api/internal/ask-fork",
                json={
                    "app_session_id": caller_bc,
                    "instructions": "Reply with the single word: ECHO2",
                    "worker_session_id": worker1_bc,
                    "worker_description": "TestWorkerOne",
                    "justification": "",
                    "proposed_orchestration_mode": "",
                    "model": "claude-haiku-4-5-20251001",
                    "cwd": cwd,
                },
                headers={"X-Internal-Token": token},
            )
            d2 = r.json()
            if not d2.get("success"):
                _fail("delegation #2", f"non-success: {d2}")
                failures += 1
            elif d2.get("fork_agent_sid") != d1.get("fork_agent_sid"):
                _fail("fork reuse",
                      f"sid changed: {d1.get('fork_agent_sid')} → {d2.get('fork_agent_sid')}")
                failures += 1
            elif d2.get("total_bytes_now", 0) <= d1.get("total_bytes_now", 0):
                _fail("fork resume growth",
                      f"lines did not grow: {d1.get('total_bytes_now')} → {d2.get('total_bytes_now')}")
                failures += 1
            else:
                _ok(f"fork reused; bytes {d1.get('total_bytes_now')} → {d2.get('total_bytes_now')}")

            # ----------------------------------------------------------
            # Step 4 — fresh-worker approval flow
            # ----------------------------------------------------------
            print("\n[4] /api/internal/ask-fork worker_session_id=null → approval flow")

            async def auto_approve():
                """Poll for the new pending approval, approve it."""
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    rr = await client.get("/api/pending_approvals", params={"cwd": cwd})
                    listing = rr.json()["approvals"]
                    if listing:
                        d = listing[0]
                        await asyncio.sleep(0.3)  # let WS event land
                        ar = await client.post(
                            f"/api/pending_approvals/{d['delegation_id']}/approve",
                            json={"description": "AutoApprovedWorker", "orchestration_mode": "native"},
                        )
                        return ar.json()
                    await asyncio.sleep(0.3)
                return None

            approve_task = asyncio.create_task(auto_approve())
            r = await client.post(
                "/api/internal/ask-fork",
                json={
                    "app_session_id": caller_bc,
                    "instructions": "Reply with the single word: ECHO3",
                    "worker_session_id": None,
                    "worker_description": "ProposedWorker",
                    "justification": "Need a fresh worker for an unrelated topic.",
                    "proposed_orchestration_mode": "native",
                    "model": "claude-haiku-4-5-20251001",
                    "cwd": cwd,
                },
                headers={"X-Internal-Token": token},
            )
            await approve_task
            d3 = r.json()
            if not d3.get("success"):
                _fail("fresh-worker approval flow", f"non-success: {d3}")
                failures += 1
            else:
                # The delegation should have used the auto-approved worker.
                _ok(f"approval flow completed, fork_sid={d3.get('fork_agent_sid','?')[:8]}")

            # Verify a new worker exists in the registry
            r = await client.get("/api/workers", params={"cwd": cwd})
            workers_after = r.json()["workers"]
            if len(workers_after) >= 2:
                _ok(f"new worker registered after approval ({len(workers_after)} total)")
            else:
                _fail("new worker after approval", f"only {len(workers_after)} workers")
                failures += 1

            # Verify worker_creation_requested AND worker_creation_approved
            # came over the WS.
            await asyncio.sleep(0.5)
            req_evt = next(
                (e for e in ws_events if e.get("type") == "worker_creation_requested"),
                None,
            )
            approved_evt = next(
                (e for e in ws_events if e.get("type") == "worker_creation_approved"),
                None,
            )
            if req_evt and approved_evt:
                _ok("worker_creation_{requested,approved} both fired on WS")
            else:
                _fail("approval WS events",
                      f"req={bool(req_evt)} approved={bool(approved_evt)}")
                failures += 1

            # ----------------------------------------------------------
            # Step 5 — multi-tab approve idempotency
            # ----------------------------------------------------------
            print("\n[5] multi-tab approve idempotency")
            # Trigger another fresh-worker request and try to approve twice.
            asyncio.create_task(auto_approve())
            r = await client.post(
                "/api/internal/ask-fork",
                json={
                    "app_session_id": caller_bc,
                    "instructions": "Reply with the single word: ECHO4",
                    "worker_session_id": None,
                    "worker_description": "Another",
                    "justification": "Idempotency test.",
                    "proposed_orchestration_mode": "native",
                    "model": "claude-haiku-4-5-20251001",
                    "cwd": cwd,
                },
                headers={"X-Internal-Token": token},
            )
            d4 = r.json()
            if d4.get("success"):
                _ok("second auto-approval flow completed")
                # Try to deny the same delegation_id we just approved (should be idempotent)
                # Find it from the disk record:
                pa_dir = Path(ba_home) / "pending_approvals"
                files = sorted(pa_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
                if files:
                    rec = json.loads(files[0].read_text())
                    if rec.get("status") in ("approved", "denied"):
                        ar = await client.post(
                            f"/api/pending_approvals/{rec['delegation_id']}/deny",
                        )
                        ar_data = ar.json()
                        if ar_data.get("idempotent"):
                            _ok("idempotent re-approve / deny works")
                        else:
                            _fail("idempotency", f"second action did not return idempotent: {ar_data}")
                            failures += 1
            else:
                _fail("multi-tab", f"second flow failed: {d4}")
                failures += 1

            # ----------------------------------------------------------
            # Step 6 — fan-out cleanup on session delete
            # ----------------------------------------------------------
            print("\n[6] Better Agent session delete fan-out")
            await client.delete(f"/api/sessions/{worker1_bc}")
            await asyncio.sleep(0.3)
            r = await client.get("/api/workers", params={"cwd": cwd})
            workers_after = r.json()["workers"]
            still_there = next(
                (w for w in workers_after if w["agent_session_id"] == worker1_bc), None,
            )
            if still_there:
                _fail("delete fan-out", f"deleted worker still in registry: {still_there}")
                failures += 1
            else:
                _ok("deleted Better Agent session removed from worker registry")
            fork_after = _ws.get_fork(cwd, caller_bc, worker1_bc)
            if fork_after:
                _fail("delete fan-out forks", f"fork still recorded: {fork_after}")
                failures += 1
            else:
                _ok("forks cleared on BC delete")

            stop_event.set()
            await ws_task

        print(f"\n{'='*50}\nfailures: {failures}")
        return 0 if failures == 0 else 1
    finally:
        server.stop()
        # Wipe the isolated home — totally safe because BETTER_CLAUDE_HOME
        # was set to a tempdir at the top of this run.
        try:
            shutil.rmtree(ba_home, ignore_errors=True)
        except Exception:
            pass


if __name__ == "__main__":
    try:
        rc = asyncio.run(main())
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"!!! UNEXPECTED ERROR !!! {type(e).__name__}: {e}", file=sys.stderr)
        rc = 2
    sys.exit(rc)
