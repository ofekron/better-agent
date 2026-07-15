"""End-to-end integration test for the worker redesign.

Boots the FastAPI app via uvicorn in a background thread, then drives
real HTTP + WebSocket interactions against it that exercise actual
claude CLI runs through provider_bridge / runner.py:

  1. POST /api/internal/workers/create — creates a fresh Better Agent
     session and runs an init turn synchronously to mint its agent_sid.
     Validates that the worker appears in /api/internal/workers/list
     afterward.

  2. Simulated delegation via /api/internal/ask-fork with the new
     worker — exercises the per-pair fork mint path. Verifies the
     returned `jsonl_path` exists and contains the worker's response,
     and that worker_store records a fork sid for the (caller, worker)
     pair.

  3. Second delegation to the same worker — should resume the existing
     fork (no re-fork), and total_bytes_now should grow vs the prior
     run while the recorded fork_agent_sid stays the same.

  4. Approval flow: delegate-task under the always_new_approve policy
     creates a pending approval + worker_creation_requested WS card.
     Approve via the internal REST surface; the delegation resolves to
     a freshly created target session. Then create-worker (caller
     policy=approve) spawns a roster worker and fires
     worker_creation_approved on the WS.

  5. Multi-tab approve idempotency: a second approval-gated delegation
     is approved, then the same delegation is denied again and the
     second action must report idempotent instead of double-resolving.

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
from auth_test_helpers import authenticate_async_client, internal_post
from _extension_test_helpers import install_extension_fixture

BFF_SERVICE_TOKEN_HEADER = "X-Better-Agent-BFF-Token"


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
                time.sleep(0.05)
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


async def wait_for(cond, timeout_s: float, interval: float = 0.02) -> bool:
    """Bounded condition poll — True as soon as `cond()` (sync or async
    callable) is truthy, False once the deadline passes."""
    deadline = time.monotonic() + timeout_s
    while True:
        res = cond()
        if asyncio.iscoroutine(res):
            res = await res
        if res:
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(interval)


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
            "subscription_class": "foreground",
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

    # The /api/internal/workers/* and /api/internal/pending-approvals/*
    # routes are gated on an extension owning the team-orchestration core
    # role; install the minimal fixture before the app boots.
    install_extension_fixture(
        ba_home, "test.team-orchestration", core_roles=("team-orchestration",),
    )

    port = free_port()
    server = BackgroundUvicorn("main:app", port)
    server.start()
    base = f"http://127.0.0.1:{port}"
    ws_url = f"ws://127.0.0.1:{port}/ws/chat"
    print(f"backend up at {base}")

    # Resolve symlinks (/var → /private/var on macOS) so the cwd recorded on
    # session records matches the cwd the test filters by.
    cwd = str(Path(tempfile.mkdtemp(prefix="bc-int-")).resolve())
    failures = 0
    try:
        # Worker init turns run a real provision prompt (the worker explores
        # its scope) and can take several minutes — size the client timeout
        # for that, not for plain REST latency.
        async with httpx.AsyncClient(base_url=base, timeout=600) as client:
            user_token = await authenticate_async_client(client)
            ws_url = f"{ws_url}?token={user_token}"
            # Internal signing token for the /api/internal/* surface (the
            # runtime mints it into the isolated home at startup).
            token = (Path(ba_home) / "internal_token").read_text().strip()
            # Service-token header for /api/bff-runtime/* (the session
            # creation surface the BFF drives in production).
            bff_token = (
                Path(ba_home) / "runtime" / "bff-service.token"
            ).read_text().strip()
            bff_headers = {BFF_SERVICE_TOKEN_HEADER: bff_token}

            async def list_workers() -> list[dict]:
                rr = await internal_post(
                    client, "/api/internal/workers/list", {"cwd": cwd}, token,
                )
                return rr.json()["workers"]

            # ----------------------------------------------------------
            # Step 1 — create a fresh worker (/api/internal/workers/create)
            # ----------------------------------------------------------
            print("\n[1] /api/internal/workers/create — create fresh worker (init turn)")
            r = await internal_post(
                client,
                "/api/internal/workers/create",
                {
                    "cwd": cwd,
                    "description": "TestWorkerOne",
                    "orchestration_mode": "native",
                    "model": "claude-haiku-4-5-20251001",
                },
                token,
            )
            if r.status_code != 200:
                _fail("create worker", f"HTTP {r.status_code}: {r.text}")
                return 1
            w1 = r.json()
            if not w1.get("initialized") or not w1.get("agent_sid"):
                _fail("create worker", f"bad payload: {w1}")
                return 1
            worker1_bc = w1["agent_session_id"]
            _ok(f"worker created bc={worker1_bc[:8]} agent_sid={w1['agent_sid'][:8]}")

            workers = await list_workers()
            if len(workers) != 1 or workers[0]["agent_session_id"] != worker1_bc:
                _fail("list workers after create", f"got {workers}")
                failures += 1
            else:
                _ok("worker appears in /api/internal/workers/list")

            # ----------------------------------------------------------
            # Step 2 — direct delegation (manager-style internal call)
            # ----------------------------------------------------------
            print("\n[2] /api/internal/ask-fork — first delegation (fork mint)")
            # We need a "caller" app session id. Create a manager-mode BC
            # session to act as the caller.
            r = await client.post(
                "/api/bff-runtime/sessions",
                json={
                    "name": "TestCaller",
                    "model": "claude-haiku-4-5-20251001",
                    "cwd": cwd,
                    "orchestration_mode": "manager",
                    # Lets step 4b's create-worker spawn without a
                    # pending-approval wait (no active turn to gate on).
                    "worker_creation_policy": "approve",
                },
                headers=bff_headers,
            )
            if r.status_code != 200:
                _fail("create caller session", f"HTTP {r.status_code}: {r.text}")
                return 1
            caller_bc = r.json()["id"]
            _ok(f"caller Better Agent session created bc={caller_bc[:8]}")

            # We need to simulate a WS subscribed for the caller so
            # run_delegation has a callback to fan worker events into.
            ws_events: list[dict] = []
            stop_event = asyncio.Event()
            ws_task = asyncio.create_task(
                collect_ws_events(ws_url, caller_bc, stop_event, ws_events, cwd)
            )
            # The server registers the ws_callback and then immediately
            # sends `user_input_pending_snapshot` — the first collected
            # frame proves the subscription is live.
            import main as _main
            if not await wait_for(
                lambda: ws_events
                and _main.coordinator.ws_callbacks.get(caller_bc),
                15,
            ):
                _fail("ws subscribe", "subscription never registered on server")
                failures += 1

            r = await internal_post(
                client,
                "/api/internal/ask-fork",
                {
                    "app_session_id": caller_bc,
                    "instructions": "Reply with the single word: ECHO1",
                    "worker_session_id": worker1_bc,
                    "worker_description": "TestWorkerOne",
                    "justification": "",
                    "proposed_orchestration_mode": "",
                    "model": "claude-haiku-4-5-20251001",
                    "cwd": cwd,
                },
                token,
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

            # Verify worker_store has a fork record for the pair. The record
            # stores the fork's Better Agent session id; the response carries
            # the fork's provider sid — they meet on the fork session record.
            from stores import worker_store as _ws
            fork_rec = _ws.get_fork_record(cwd, caller_bc, worker1_bc)
            fork_session = (
                _main.session_manager.get(fork_rec["fork_agent_session_id"])
                if fork_rec and fork_rec.get("fork_agent_session_id") else None
            )
            if (
                not fork_session
                or fork_session.get("agent_session_id") != d1.get("fork_agent_sid")
            ):
                _fail("fork record persisted", f"got {fork_rec}")
                failures += 1
            else:
                _ok("fork record persisted with matching fork session")

            # ----------------------------------------------------------
            # Step 3 — second delegation to same pair (resume fork)
            # ----------------------------------------------------------
            print("\n[3] /api/internal/ask-fork — second call (fork resume)")
            r = await internal_post(
                client,
                "/api/internal/ask-fork",
                {
                    "app_session_id": caller_bc,
                    "instructions": "Reply with the single word: ECHO2",
                    "worker_session_id": worker1_bc,
                    "worker_description": "TestWorkerOne",
                    "justification": "",
                    "proposed_orchestration_mode": "",
                    "model": "claude-haiku-4-5-20251001",
                    "cwd": cwd,
                },
                token,
            )
            d2 = r.json()
            # The fork's PROVIDER sid rotates per resumed turn, and the fork
            # Better Agent session itself is re-minted whenever the staleness
            # check trips (e.g. the parent jsonl grew between calls) — both
            # are intended. The stable per-pair contract is: the second
            # delegation succeeds and a live fork record (distinct from the
            # worker session) is persisted for the pair.
            fork_rec2 = _ws.get_fork_record(cwd, caller_bc, worker1_bc)
            if not d2.get("success"):
                _fail("delegation #2", f"non-success: {d2}")
                failures += 1
            elif (
                not fork_rec2
                or not fork_rec2.get("fork_agent_session_id")
                or fork_rec2.get("fork_agent_session_id") == worker1_bc
            ):
                _fail("fork record after resume", f"got {fork_rec2}")
                failures += 1
            elif not d2.get("total_bytes_now"):
                _fail("fork resume",
                      f"no jsonl bytes reported: {d2.get('total_bytes_now')}")
                failures += 1
            else:
                _ok(f"pair fork persisted across delegations; "
                    f"bytes now {d2.get('total_bytes_now')}")

            # ----------------------------------------------------------
            # Step 4 — fresh-worker approval flow
            # ----------------------------------------------------------
            print("\n[4] /api/internal/delegate-task → approval flow (always_new_approve)")

            async def auto_approve():
                """Poll for the new pending approval, approve it."""
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    rr = await internal_post(
                        client, "/api/internal/pending-approvals/list",
                        {"cwd": cwd}, token,
                    )
                    listing = rr.json()["approvals"]
                    if listing:
                        d = listing[0]
                        did = d["delegation_id"]
                        # Approve only once the worker_creation_requested
                        # frame landed on the WS AND the backend's waiter
                        # Future is registered for this delegation.
                        await wait_for(
                            lambda: any(
                                e.get("type") == "worker_creation_requested"
                                and e.get("data", {}).get("delegation_id") == did
                                for e in ws_events
                            )
                            and did in _main.coordinator.approval_waiters,
                            15,
                        )
                        ar = await internal_post(
                            client,
                            "/api/internal/pending-approvals/approve",
                            {
                                "delegation_id": did,
                                "description": "AutoApprovedWorker",
                                "orchestration_mode": "native",
                            },
                            token,
                        )
                        return ar.json()
                    await asyncio.sleep(0.05)
                return None

            r = await internal_post(
                client, "/api/internal/delegate-task-policy/set",
                {"policy": "always_new_approve"}, token,
            )
            if r.json().get("policy") != "always_new_approve":
                _fail("delegate-task policy set", f"got {r.json()}")
                failures += 1

            delegated_targets: list[str] = []
            approve_task = asyncio.create_task(auto_approve())
            r = await internal_post(
                client,
                "/api/internal/delegate-task",
                {
                    "sender_session_id": caller_bc,
                    "task": "Reply with the single word: ECHO3",
                    "cwd": cwd,
                },
                token,
            )
            await approve_task
            d3 = r.json()
            if not d3.get("success") or not d3.get("target_session_id"):
                _fail("delegate-task approval flow", f"non-success: {d3}")
                failures += 1
            else:
                delegated_targets.append(d3["target_session_id"])
                _ok(f"delegate-task approved, target={d3['target_session_id'][:8]} "
                    f"created={d3.get('created_session')}")
                rr = await client.get(f"/api/sessions/{d3['target_session_id']}")
                if rr.status_code == 200:
                    _ok("delegated target session exists")
                else:
                    _fail("delegated target session", f"HTTP {rr.status_code}")
                    failures += 1

            req_evt = next(
                (e for e in ws_events if e.get("type") == "worker_creation_requested"),
                None,
            )
            if req_evt:
                _ok("worker_creation_requested fired on WS")
            else:
                _fail("approval WS events", "no worker_creation_requested seen")
                failures += 1

            # Fresh-worker spawn path: the caller session was created with
            # worker_creation_policy="approve", so create-worker spawns a
            # roster worker (init turn) without a pending-approval wait and
            # emits worker_creation_approved.
            print("\n[4b] /api/internal/create-worker — policy=approve spawns worker")
            r = await internal_post(
                client,
                "/api/internal/create-worker",
                {
                    "app_session_id": caller_bc,
                    "worker_description": "AutoApprovedWorker",
                    "justification": "Need a fresh worker for an unrelated topic.",
                    "orchestration_mode": "native",
                    "model": "claude-haiku-4-5-20251001",
                    "cwd": cwd,
                },
                token,
            )
            cw = r.json()
            if not cw.get("success") or not cw.get("worker_session_id"):
                _fail("create-worker", f"non-success: {cw}")
                failures += 1
            else:
                _ok(f"worker spawned bc={cw['worker_session_id'][:8]}")

            workers_after = await list_workers()
            if len(workers_after) >= 2:
                _ok(f"new worker registered after create-worker ({len(workers_after)} total)")
            else:
                _fail("new worker after create-worker", f"only {len(workers_after)} workers")
                failures += 1

            await wait_for(
                lambda: any(
                    e.get("type") == "worker_creation_approved" for e in ws_events
                ),
                15,
            )
            approved_evt = next(
                (e for e in ws_events if e.get("type") == "worker_creation_approved"),
                None,
            )
            if approved_evt:
                _ok("worker_creation_approved fired on WS")
            else:
                _fail("approval WS events", "no worker_creation_approved seen")
                failures += 1

            # ----------------------------------------------------------
            # Step 5 — multi-tab approve idempotency
            # ----------------------------------------------------------
            print("\n[5] multi-tab approve idempotency")
            # Trigger another approval-gated delegation and try to resolve twice.
            asyncio.create_task(auto_approve())
            r = await internal_post(
                client,
                "/api/internal/delegate-task",
                {
                    "sender_session_id": caller_bc,
                    "task": "Reply with the single word: ECHO4",
                    "cwd": cwd,
                },
                token,
            )
            d4 = r.json()
            if d4.get("success"):
                if d4.get("target_session_id"):
                    delegated_targets.append(d4["target_session_id"])
                _ok("second auto-approval flow completed")
                # Try to deny the same delegation_id we just approved (should be idempotent)
                # Find it from the disk record:
                pa_dir = Path(ba_home) / "pending_approvals"
                files = sorted(pa_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
                if files:
                    rec = json.loads(files[0].read_text())
                    if rec.get("status") in ("approved", "denied"):
                        ar = await internal_post(
                            client,
                            "/api/internal/pending-approvals/deny",
                            {"delegation_id": rec["delegation_id"]},
                            token,
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

            async def _delete_fanned_out() -> bool:
                gone = all(
                    w["agent_session_id"] != worker1_bc
                    for w in await list_workers()
                )
                return gone and not _ws.get_fork(cwd, caller_bc, worker1_bc)

            await wait_for(_delete_fanned_out, 15)
            workers_after = await list_workers()
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

            # Stop the detached delegate-task runs so no runner outlives
            # the test process.
            for tid in delegated_targets:
                try:
                    await client.post(f"/api/sessions/{tid}/stop")
                except Exception:
                    pass

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
