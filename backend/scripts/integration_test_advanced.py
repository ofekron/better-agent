"""Adversarial-review-driven integration tests for the worker redesign.

Covers the paths the basic integration_test.py did NOT exercise:

  A. ask-fork fresh-worker rejection — worker_session_id is required;
     a null-worker call must 400 up-front and MUST NOT create a
     pending-approval record. (Fresh targets are delegate-task's job.)

  B. Deny path resolves the gate — for an approval-gated
     delegate-task under always_new_approve:
        1. Issue delegate-task; wait for the pending_approvals record
           + in-memory waiter Future.
        2. Deny via the internal REST surface.
        3. Verify the blocked call returns non-success ("denied"), the
           waiter Future is cleaned up, and the record disappears from
           the internal approvals listing.
        4. A fresh delegate-task + auto-approve then resolves normally.

  C. Subscribe re-emit dismissed-card-resurrection guard — simulates
     the "user already approved, then WS reconnects" race:
        1. Issue an approval-gated delegate-task, capture its
           delegation_id from the internal approvals listing.
        2. Approve via REST.
        3. WS subscribe AFTER the approve has resolved.
        4. Verify NO worker_creation_requested event lands on the
           subscriber (the gated re-emit drops resolved records).

  D. Manager-mode worker delegation does not crash the runner —
     create a manager-orch worker and ask-fork to it.

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
from auth_test_helpers import authenticate_async_client, internal_post
from _extension_test_helpers import install_extension_fixture

BFF_SERVICE_TOKEN_HEADER = "X-Better-Agent-BFF-Token"


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
                time.sleep(0.05)
        raise RuntimeError("uvicorn failed to start in 30s")

    def stop(self):
        if self.server:
            self.server.should_exit = True
        if self.thread:
            self.thread.join(timeout=10)


def _ok(label: str): print(f"\033[92mPASS\033[0m  {label}")
def _fail(label: str, why: str): print(f"\033[91mFAIL\033[0m  {label}: {why}")


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


async def main() -> int:
    # Isolated state dir — never touches the developer's real
    # ~/.better-claude/. See integration_test.py for rationale.
    ba_home = tempfile.mkdtemp(prefix="bc-int-adv-home-")
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
    # Resolve symlinks (/var → /private/var on macOS) so the cwd recorded on
    # session records matches the cwd the test filters by.
    cwd = str(Path(tempfile.mkdtemp(prefix="bc-int-adv-")).resolve())
    failures = 0
    token = (Path(ba_home) / "internal_token").read_text().strip()
    bff_token = (Path(ba_home) / "runtime" / "bff-service.token").read_text().strip()

    try:
        # Worker init turns run a real provision prompt and can take several
        # minutes — size the client timeout for that.
        async with httpx.AsyncClient(base_url=base, timeout=600) as client:
            user_token = await authenticate_async_client(client)
            ws_url = f"{ws_url}?token={user_token}"
            # Caller BC for tests B-D
            r = await client.post(
                "/api/bff-runtime/sessions",
                json={
                    "name": "AdvCaller", "model": "claude-haiku-4-5-20251001",
                    "cwd": cwd, "orchestration_mode": "manager",
                },
                headers={BFF_SERVICE_TOKEN_HEADER: bff_token},
            )
            if r.status_code != 200:
                _fail("create caller session", f"HTTP {r.status_code}: {r.text}")
                return 1
            caller_bc = r.json()["id"]

            # ============================================================
            # Test A — ask-fork rejects fresh-worker (null target) requests
            # ============================================================
            print("\n[A] ask-fork rejects worker_session_id=null up-front")
            import main as _main
            coord = _main.coordinator

            r = await internal_post(
                client,
                "/api/internal/ask-fork",
                {
                    "app_session_id": caller_bc,
                    "instructions": "noop",
                    "worker_session_id": None,
                    "worker_description": "Rejected",
                    "justification": "should be rejected",
                    "proposed_orchestration_mode": "native",
                    "model": "claude-haiku-4-5-20251001",
                    "cwd": cwd,
                },
                token,
            )
            if r.status_code == 400 and "worker_session_id" in (
                r.json().get("detail") or ""
            ):
                _ok("null-worker ask-fork rejected with HTTP 400")
            else:
                _fail("null-worker rejection",
                      f"HTTP {r.status_code}: {r.text[:200]}")
                failures += 1
            # Verify NO disk record was created.
            pending = await internal_post(
                client, "/api/internal/pending-approvals/list",
                {"cwd": cwd}, token,
            )
            if not pending.json()["approvals"]:
                _ok("no pending_approvals record created during rejection")
            else:
                _fail("rejection side-effect", f"orphan approval: {pending.json()}")
                failures += 1

            # ============================================================
            # Test B — deny resolves the approval gate
            # ============================================================
            print("\n[B] denied delegate-task approval resolves and cleans up")
            r = await internal_post(
                client, "/api/internal/delegate-task-policy/set",
                {"policy": "always_new_approve"}, token,
            )
            if r.json().get("policy") != "always_new_approve":
                _fail("delegate-task policy set", f"got {r.json()}")
                failures += 1
            delegated_targets: list[str] = []

            async def pending_dt_ids() -> list[str]:
                rr = await internal_post(
                    client, "/api/internal/pending-approvals/list",
                    {"cwd": cwd}, token,
                )
                return [a["delegation_id"] for a in rr.json()["approvals"]]

            # Subscribe to capture WS events.
            ws_events_b: list[dict] = []
            stop_b = asyncio.Event()
            ws_b_done = asyncio.Event()

            async def ws_b():
                try:
                    async with websockets.connect(ws_url) as ws:
                        await ws.send(json.dumps({
                            "type": "subscribe", "subscription_class": "foreground", "app_session_id": caller_bc,
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
            # First collected frame proves the subscription is live.
            if not await wait_for(
                lambda: ws_events_b and coord.ws_callbacks.get(caller_bc),
                15,
            ):
                _fail("B subscribe", "subscription never registered on server")
                failures += 1

            # First call — deny it once the waiter is registered.
            async def first_call_b():
                try:
                    return await internal_post(
                        client,
                        "/api/internal/delegate-task",
                        {
                            "sender_session_id": caller_bc,
                            "task": "Reply: OK_B (denied)",
                            "cwd": cwd,
                        },
                        token,
                    )
                except Exception:
                    return None

            t1 = asyncio.create_task(first_call_b())
            # Wait until the pending record + waiter Future exist.
            denied_did: str | None = None

            async def _pending_visible() -> bool:
                nonlocal denied_did
                for did in await pending_dt_ids():
                    if did in coord.approval_waiters:
                        denied_did = did
                        return True
                return False

            if await wait_for(_pending_visible, 15):
                _ok(f"delegate-task created pending approval {denied_did}")
            else:
                _fail("pending approval record", "never appeared for delegate-task")
                failures += 1

            dr = await internal_post(
                client,
                "/api/internal/pending-approvals/deny",
                {"delegation_id": denied_did},
                token,
            )
            if dr.json().get("status") != "denied":
                _fail("deny", f"unexpected deny response: {dr.json()}")
                failures += 1
            resp1 = await t1
            d1 = resp1.json() if resp1 is not None else {}
            if d1.get("success") is False and "denied" in (d1.get("error") or ""):
                _ok("blocked delegate-task returned denied")
            else:
                _fail("denied resolution", f"unexpected response: {d1}")
                failures += 1

            # The waiter Future must be cleaned up on resolution...
            if denied_did and await wait_for(
                lambda: denied_did not in coord.approval_waiters, 15,
            ):
                _ok("denied approval waiter cleaned up")
            else:
                _fail("waiter cleanup", "approval waiter survived deny")
                failures += 1
            # ...and the resolved record must leave the approvals listing.
            if denied_did and denied_did not in await pending_dt_ids():
                _ok("denied approval excluded from internal listing")
            else:
                _fail("denied listing", "denied approval still listed")
                failures += 1

            # A fresh delegate-task + auto-approve must then resolve.
            async def auto_approve_b():
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    for did in await pending_dt_ids():
                        if did != denied_did and did in coord.approval_waiters:
                            r = await internal_post(
                                client,
                                "/api/internal/pending-approvals/approve",
                                {"delegation_id": did},
                                token,
                            )
                            return r.json()
                    await asyncio.sleep(0.05)
                return None

            approve_task = asyncio.create_task(auto_approve_b())
            r2 = await internal_post(
                client,
                "/api/internal/delegate-task",
                {
                    "sender_session_id": caller_bc,
                    "task": "Reply: OK_B (retried)",
                    "cwd": cwd,
                },
                token,
            )
            await approve_task
            d2 = r2.json()
            if d2.get("success") and d2.get("target_session_id"):
                delegated_targets.append(d2["target_session_id"])
                _ok("retried delegate-task resolved successfully")
            else:
                _fail("retried delegate-task", f"failed: {d2}")
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
            resolved_did_c: str | None = None

            async def auto_approve_c():
                nonlocal resolved_did_c
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    for did in await pending_dt_ids():
                        if did not in coord.approval_waiters:
                            continue
                        resolved_did_c = did
                        r = await internal_post(
                            client,
                            "/api/internal/pending-approvals/approve",
                            {"delegation_id": did},
                            token,
                        )
                        return r.json()
                    await asyncio.sleep(0.05)
                return None

            ap_c = asyncio.create_task(auto_approve_c())
            r3 = await internal_post(
                client,
                "/api/internal/delegate-task",
                {
                    "sender_session_id": caller_bc,
                    "task": "Reply: OK_C",
                    "cwd": cwd,
                },
                token,
            )
            await ap_c
            d3 = r3.json()
            if d3.get("success") and d3.get("target_session_id"):
                delegated_targets.append(d3["target_session_id"])
            else:
                _fail("C delegate-task", f"failed: {d3}")
                failures += 1
            # The subscribe re-emit is gated on `approval_waiters` — wait
            # until the resolved delegation's waiter is gone so the gate
            # under test is actually in its post-resolution state.
            await wait_for(
                lambda: resolved_did_c is not None
                and resolved_did_c not in coord.approval_waiters,
                15,
            )

            # Now subscribe and verify NO worker_creation_requested
            ws_events_c: list[dict] = []
            stop_c = asyncio.Event()
            done_c = asyncio.Event()

            async def ws_c():
                try:
                    async with websockets.connect(ws_url) as ws:
                        await ws.send(json.dumps({
                            "type": "subscribe", "subscription_class": "foreground", "app_session_id": caller_bc,
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

            # The journal catch-up replay legitimately re-delivers the
            # ORIGINAL worker_creation_requested fact (its data carries the
            # delegate_task fields, no "status"). The rehydration re-emit
            # under test sends the pending_approvals RECORD (has "status") —
            # only that shape resurrects a card.
            stale_emits = [
                e for e in ws_events_c
                if e.get("type") == "worker_creation_requested"
                and e.get("data", {}).get("delegation_id") == resolved_did_c
                and "status" in (e.get("data") or {})
            ]
            if not stale_emits:
                _ok("no stale worker_creation_requested re-emitted for resolved approval")
            else:
                _fail("stale re-emit", f"got {len(stale_emits)} re-emits for resolved approval")
                failures += 1

            # And the REST listing should also exclude the resolved approval
            pending = await internal_post(
                client, "/api/internal/pending-approvals/list",
                {"cwd": cwd}, token,
            )
            stale_in_rest = [
                a for a in pending.json()["approvals"]
                if a.get("delegation_id") == resolved_did_c
            ]
            if not stale_in_rest:
                _ok("approvals listing also excludes resolved approval")
            else:
                _fail("approvals listing", f"resolved approval still listed: {stale_in_rest}")
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
            ws_d_ready = asyncio.Event()
            async def ws_d_keep():
                try:
                    async with websockets.connect(ws_url) as ws:
                        await ws.send(json.dumps({
                            "type": "subscribe", "subscription_class": "foreground", "app_session_id": caller_bc,
                        }))
                        while not stop_d.is_set():
                            try:
                                await asyncio.wait_for(ws.recv(), timeout=0.5)
                            except asyncio.TimeoutError:
                                continue
                            except websockets.ConnectionClosed:
                                return
                            # First frame proves the subscription is live.
                            ws_d_ready.set()
                finally:
                    done_d.set()
            ws_d_task = asyncio.create_task(ws_d_keep())
            if not await wait_for(
                lambda: ws_d_ready.is_set()
                and coord.ws_callbacks.get(caller_bc),
                15,
            ):
                _fail("D subscribe", "subscription never registered on server")
                failures += 1

            r = await internal_post(
                client,
                "/api/internal/workers/create",
                {
                    "cwd": cwd,
                    "description": "ManagerWorker",
                    "orchestration_mode": "manager",
                    "model": "claude-haiku-4-5-20251001",
                },
                token,
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
                r = await internal_post(
                    client,
                    "/api/internal/ask-fork",
                    {
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
                    token,
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

            # Stop the detached delegate-task runs so no runner outlives
            # the test process.
            for tid in delegated_targets:
                try:
                    await client.post(f"/api/sessions/{tid}/stop")
                except Exception:
                    pass

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
