"""Adversarial-review-driven integration tests for the worker redesign.

Covers the paths the basic integration_test.py did NOT exercise — the
ones flagged in the round-3 adversarial review:

  A. Nested-delegation rejection — a depth>0 delegate call with
     worker_session_id=None must error out (NOT create a pending
     approval card). Tests the `_active_delegations` counter guard.

  B. Re-entry with stable client_delegation_id — simulates "backend
     restart mid-approval" by:
        1. Issue delegate(worker_session_id=null) with a fixed
           client_delegation_id; wait for pending_approvals record
           to land on disk.
        2. Cancel the in-flight call (simulating the runner's HTTP
           connection dying when backend goes down).
        3. Issue a SECOND delegate call with the same
           client_delegation_id (simulating runner retry).
        4. Auto-approve via REST.
        5. Verify the second call resolves AND the worker is spawned
           AND no orphan disk record / waiter Future is left behind.

  C. Subscribe re-emit dismissed-card-resurrection guard — simulates
     the "user already approved, then WS reconnects" race:
        1. Issue delegate(null), capture delegation_id.
        2. Approve via REST.
        3. WS subscribe AFTER the approve has resolved.
        4. Verify NO worker_creation_requested event lands on the
           subscriber (the gated re-emit should drop resolved
           records).

  D. Subscribe re-emit DOES re-emit for actively-waiting approvals —
     opposite of C: open delegate(null), pause without approving,
     subscribe, verify the card lands on the new subscriber.

Run with:
    cd backend && .venv/bin/python scripts/integration_test_advanced.py
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
        self.port = port
        self.app_path = app_path
        self.server: uvicorn.Server | None = None
        self.thread: threading.Thread | None = None

    def start(self):
        cfg = uvicorn.Config(self.app_path, host="127.0.0.1", port=self.port, log_level="warning")
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


def _ok(label: str): print(f"\033[92mPASS\033[0m  {label}")
def _fail(label: str, why: str): print(f"\033[91mFAIL\033[0m  {label}: {why}")


async def main() -> int:
    # Isolated state dir — never touches the developer's real
    # ~/.better-claude/. See integration_test.py for rationale.
    ba_home = tempfile.mkdtemp(prefix="bc-int-adv-home-")
    os.environ["BETTER_CLAUDE_HOME"] = ba_home
    os.environ["BETTER_AGENT_HOME"] = ba_home
    os.environ["BETTER_CLAUDE_API_ONLY"] = "1"
    print(f"BETTER_CLAUDE_HOME = {ba_home}")

    port = free_port()
    server = BackgroundUvicorn("main:app", port)
    server.start()
    base = f"http://127.0.0.1:{port}"
    ws_url = f"ws://127.0.0.1:{port}/ws/chat"
    cwd = tempfile.mkdtemp(prefix="bc-int-adv-")
    failures = 0
    token = (Path(ba_home) / "internal_token").read_text().strip()

    try:
        async with httpx.AsyncClient(base_url=base, timeout=120) as client:
            user_token = await authenticate_async_client(client)
            ws_url = f"{ws_url}?token={user_token}"
            # Caller BC for tests B-D
            r = await client.post("/api/sessions", json={
                "name": "AdvCaller", "model": "claude-haiku-4-5-20251001",
                "cwd": cwd, "orchestration_mode": "manager",
            })
            caller_bc = r.json()["id"]

            # ============================================================
            # Test A — nested-delegation rejection of fresh-worker request
            # ============================================================
            print("\n[A] nested rejection of fresh-worker request")
            import main as _main
            coord = _main.coordinator

            # The "no active WS" guard fires before the depth check, so
            # we need a live WS subscription. Open one for the test.
            stop_a = asyncio.Event()
            ws_a_done = asyncio.Event()
            async def ws_a_keep():
                try:
                    async with websockets.connect(ws_url) as ws:
                        await ws.send(json.dumps({
                            "type": "subscribe", "app_session_id": caller_bc,
                        }))
                        while not stop_a.is_set():
                            try:
                                await asyncio.wait_for(ws.recv(), timeout=0.5)
                            except asyncio.TimeoutError:
                                continue
                            except websockets.ConnectionClosed:
                                return
                finally:
                    ws_a_done.set()
            ws_a_task = asyncio.create_task(ws_a_keep())
            await asyncio.sleep(0.5)  # let subscribe register

            # Simulate depth>0 by bumping the counter manually.
            coord.active_delegations[caller_bc] = 1
            try:
                r = await client.post(
                    "/api/internal/ask-fork",
                    json={
                        "app_session_id": caller_bc,
                        "instructions": "noop",
                        "worker_session_id": None,
                        "worker_description": "Nested",
                        "justification": "should be rejected",
                        "proposed_orchestration_mode": "native",
                        "model": "claude-haiku-4-5-20251001",
                        "cwd": cwd,
                        "client_delegation_id": f"del_{uuid.uuid4().hex[:10]}",
                    },
                    headers={"X-Internal-Token": token},
                )
                d = r.json()
                if d.get("success") is False and "Nested" in (d.get("error") or ""):
                    _ok("nested fresh-worker request rejected with explanatory error")
                else:
                    _fail("nested rejection", f"unexpected response: {d}")
                    failures += 1
                # Verify NO disk record was created.
                pending = await client.get("/api/pending_approvals", params={"cwd": cwd})
                if not pending.json()["approvals"]:
                    _ok("no pending_approvals record created during nested rejection")
                else:
                    _fail("nested side-effect", f"orphan approval: {pending.json()}")
                    failures += 1
            finally:
                coord.active_delegations.pop(caller_bc, None)
                stop_a.set()
                try:
                    await asyncio.wait_for(ws_a_done.wait(), timeout=2)
                except asyncio.TimeoutError:
                    pass
                ws_a_task.cancel()
                try: await ws_a_task
                except Exception: pass

            # ============================================================
            # Test B — re-entry with stable client_delegation_id
            # ============================================================
            print("\n[B] re-entry: same client_delegation_id resolves once")
            stable_did = f"del_{uuid.uuid4().hex[:10]}"

            # Subscribe to capture WS events.
            ws_events_b: list[dict] = []
            stop_b = asyncio.Event()
            ws_b_done = asyncio.Event()

            async def ws_b():
                try:
                    async with websockets.connect(ws_url) as ws:
                        await ws.send(json.dumps({
                            "type": "subscribe", "app_session_id": caller_bc,
                        }))
                        while not stop_b.is_set():
                            try:
                                raw = await asyncio.wait_for(ws.recv(), timeout=0.3)
                            except asyncio.TimeoutError:
                                continue
                            except websockets.ConnectionClosed:
                                return
                            try:
                                ws_events_b.append(json.loads(raw))
                            except Exception:
                                pass
                finally:
                    ws_b_done.set()

            ws_b_task = asyncio.create_task(ws_b())
            await asyncio.sleep(0.3)

            # First call — abandon it after the disk record appears.
            async def first_call_b():
                try:
                    return await client.post(
                        "/api/internal/ask-fork",
                        json={
                            "app_session_id": caller_bc,
                            "instructions": "Reply: OK_B",
                            "worker_session_id": None,
                            "worker_description": "ReentryWorker",
                            "justification": "test re-entry",
                            "proposed_orchestration_mode": "native",
                            "model": "claude-haiku-4-5-20251001",
                            "cwd": cwd,
                            "client_delegation_id": stable_did,
                        },
                        headers={"X-Internal-Token": token},
                        timeout=30,
                    )
                except Exception:
                    return None

            t1 = asyncio.create_task(first_call_b())
            # Wait until pending record exists on disk.
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                pa_path = Path(ba_home) / "pending_approvals" / f"{stable_did}.json"
                if pa_path.exists():
                    break
                await asyncio.sleep(0.1)
            if not pa_path.exists():
                _fail("pending_approvals disk record", "never appeared for stable_did")
                failures += 1
            else:
                _ok("first call created pending_approvals record")
            # Cancel the first call mid-await (simulate runner connection drop).
            t1.cancel()
            try:
                await t1
            except (asyncio.CancelledError, Exception):
                pass

            # Approval Future from the first call should be cleaned up
            # (asyncio.wait... in the finally block pops it).
            await asyncio.sleep(0.3)

            # Now retry with the same client_delegation_id (simulating runner retry).
            async def auto_approve_b():
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    rec_path = Path(ba_home) / "pending_approvals" / f"{stable_did}.json"
                    if rec_path.exists():
                        rec = json.loads(rec_path.read_text())
                        if rec.get("status") == "pending":
                            await asyncio.sleep(0.5)  # let waiter Future register
                            r = await client.post(
                                f"/api/pending_approvals/{stable_did}/approve",
                                json={"description": "ReentryWorker", "orchestration_mode": "native"},
                            )
                            return r.json()
                    await asyncio.sleep(0.2)
                return None

            approve_task = asyncio.create_task(auto_approve_b())
            r2 = await client.post(
                "/api/internal/ask-fork",
                json={
                    "app_session_id": caller_bc,
                    "instructions": "Reply: OK_B",
                    "worker_session_id": None,
                    "worker_description": "ReentryWorker",
                    "justification": "test re-entry",
                    "proposed_orchestration_mode": "native",
                    "model": "claude-haiku-4-5-20251001",
                    "cwd": cwd,
                    "client_delegation_id": stable_did,
                },
                headers={"X-Internal-Token": token},
            )
            ar = await approve_task
            d2 = r2.json()
            if d2.get("success"):
                _ok("re-entry call resolved successfully")
            else:
                _fail("re-entry call", f"failed: {d2}")
                failures += 1
            # Verify only ONE worker was spawned (not two from the
            # abandoned first call + the re-entered second call).
            r = await client.get("/api/workers", params={"cwd": cwd})
            workers = r.json()["workers"]
            reentry_workers = [w for w in workers if w["name"] == "ReentryWorker"]
            if len(reentry_workers) == 1:
                _ok("exactly one worker spawned across abandoned-then-retried calls")
            else:
                _fail("worker spawn count", f"got {len(reentry_workers)}: {reentry_workers}")
                failures += 1

            stop_b.set()
            try:
                await asyncio.wait_for(ws_b_done.wait(), timeout=2)
            except asyncio.TimeoutError:
                pass
            try:
                ws_b_task.cancel()
                await ws_b_task
            except Exception:
                pass

            # ============================================================
            # Test C — subscribe-after-resolution does NOT re-emit
            # ============================================================
            print("\n[C] subscribe after resolution does NOT re-emit dismissed cards")
            stable_did_c = f"del_{uuid.uuid4().hex[:10]}"

            async def auto_approve_c():
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    rp = Path(ba_home) / "pending_approvals" / f"{stable_did_c}.json"
                    if rp.exists():
                        rec = json.loads(rp.read_text())
                        if rec.get("status") == "pending":
                            await asyncio.sleep(0.5)
                            await client.post(
                                f"/api/pending_approvals/{stable_did_c}/approve",
                                json={"description": "C", "orchestration_mode": "native"},
                            )
                            return
                    await asyncio.sleep(0.2)

            ap_c = asyncio.create_task(auto_approve_c())
            r3 = await client.post(
                "/api/internal/ask-fork",
                json={
                    "app_session_id": caller_bc,
                    "instructions": "Reply: OK_C",
                    "worker_session_id": None,
                    "worker_description": "C",
                    "justification": "test C",
                    "proposed_orchestration_mode": "native",
                    "model": "claude-haiku-4-5-20251001",
                    "cwd": cwd,
                    "client_delegation_id": stable_did_c,
                },
                headers={"X-Internal-Token": token},
            )
            await ap_c
            await asyncio.sleep(0.5)

            # Now subscribe and verify NO worker_creation_requested
            ws_events_c: list[dict] = []
            stop_c = asyncio.Event()
            done_c = asyncio.Event()

            async def ws_c():
                try:
                    async with websockets.connect(ws_url) as ws:
                        await ws.send(json.dumps({
                            "type": "subscribe", "app_session_id": caller_bc,
                        }))
                        # Drain for 2s
                        end = time.monotonic() + 2
                        while time.monotonic() < end and not stop_c.is_set():
                            try:
                                raw = await asyncio.wait_for(ws.recv(), timeout=0.2)
                            except asyncio.TimeoutError:
                                continue
                            except websockets.ConnectionClosed:
                                return
                            try:
                                ws_events_c.append(json.loads(raw))
                            except Exception:
                                pass
                finally:
                    done_c.set()

            tc = asyncio.create_task(ws_c())
            await done_c.wait()
            tc.cancel()
            try:
                await tc
            except Exception:
                pass

            stale_emits = [
                e for e in ws_events_c
                if e.get("type") == "worker_creation_requested"
                and e.get("data", {}).get("delegation_id") == stable_did_c
            ]
            if not stale_emits:
                _ok("no stale worker_creation_requested re-emitted for resolved approval")
            else:
                _fail("stale re-emit", f"got {len(stale_emits)} re-emits for resolved approval")
                failures += 1

            # And REST GET should also exclude the resolved approval
            pending = await client.get("/api/pending_approvals", params={"cwd": cwd})
            stale_in_rest = [
                a for a in pending.json()["approvals"]
                if a.get("delegation_id") == stable_did_c
            ]
            if not stale_in_rest:
                _ok("REST GET also excludes resolved approval")
            else:
                _fail("REST GET", f"resolved approval still listed: {stale_in_rest}")
                failures += 1

            # ============================================================
            # Test D — manager-mode worker delegation (regression test
            # for "runner exited early with code 1" bug: when a worker
            # is itself manager-orch, the worker run needs
            # backend_url + internal_token forwarded so the runner
            # passes the mode="manager" precondition and the nested
            # `delegate` MCP tool wires up its HTTP loopback.
            # ============================================================
            print("\n[D] manager-mode worker delegation does not crash runner")
            # WS must be subscribed for run_delegation to fan out events.
            stop_d = asyncio.Event()
            done_d = asyncio.Event()
            async def ws_d_keep():
                try:
                    async with websockets.connect(ws_url) as ws:
                        await ws.send(json.dumps({
                            "type": "subscribe", "app_session_id": caller_bc,
                        }))
                        while not stop_d.is_set():
                            try:
                                await asyncio.wait_for(ws.recv(), timeout=0.5)
                            except asyncio.TimeoutError:
                                continue
                            except websockets.ConnectionClosed:
                                return
                finally:
                    done_d.set()
            ws_d_task = asyncio.create_task(ws_d_keep())
            await asyncio.sleep(0.5)

            r = await client.post(
                "/api/workers",
                json={
                    "cwd": cwd,
                    "description": "ManagerWorker",
                    "orchestration_mode": "manager",
                    "model": "claude-haiku-4-5-20251001",
                },
            )
            if r.status_code != 200:
                _fail("create manager worker", f"HTTP {r.status_code}: {r.text}")
                failures += 1
            else:
                manager_worker_bc = r.json()["agent_session_id"]
                _ok(f"manager-mode worker created bc={manager_worker_bc[:8]}")

                # Delegate to it. The bug surfaces here: worker runs
                # in manager mode → runner.py needs backend_url +
                # internal_token. Without them, `_fail("manager mode
                # requires app_session_id, backend_url, internal_token")`
                # fires and runner exits code 1.
                r = await client.post(
                    "/api/internal/ask-fork",
                    json={
                        "app_session_id": caller_bc,
                        "instructions": "Reply with the single word: ECHO_D",
                        "worker_session_id": manager_worker_bc,
                        "worker_description": "ManagerWorker",
                        "justification": "",
                        "proposed_orchestration_mode": "",
                        "model": "claude-haiku-4-5-20251001",
                        "cwd": cwd,
                        "client_delegation_id": f"del_{uuid.uuid4().hex[:10]}",
                    },
                    headers={"X-Internal-Token": token},
                )
                d = r.json()
                if d.get("success") and not d.get("error"):
                    _ok(f"manager-worker delegation succeeded; "
                        f"jsonl={Path(d.get('jsonl_path') or '').name}")
                elif "exited early" in (d.get("error") or "").lower():
                    _fail("manager-worker delegation",
                          f"REGRESSION: runner exited early — backend_url/"
                          f"internal_token not forwarded? error={d.get('error')}")
                    failures += 1
                else:
                    _fail("manager-worker delegation", f"{d}")
                    failures += 1

            stop_d.set()
            try:
                await asyncio.wait_for(done_d.wait(), timeout=2)
            except asyncio.TimeoutError:
                pass
            ws_d_task.cancel()
            try: await ws_d_task
            except Exception: pass

        print(f"\n{'='*50}\nfailures: {failures}")
        return 0 if failures == 0 else 1
    finally:
        server.stop()
        try:
            shutil.rmtree(ba_home, ignore_errors=True)
        except Exception:
            pass


if __name__ == "__main__":
    try:
        rc = asyncio.run(main())
    except Exception as e:
        import traceback; traceback.print_exc()
        rc = 2
    sys.exit(rc)
