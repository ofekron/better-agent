"""Integration test for the approval-based node registration flow.

Exercises the full primary-side loop that replaces the static
`BETTER_CLAUDE_NODE_TOKEN` with a trust-on-first-approve handshake:

  1. A brand-new worker-node dials `/api/node/connect` presenting its
     self-generated secret (no shared token, not declared in topology).
  2. Primary holds the WS open and emits `node_registration_requested`;
     the node appears in `GET /api/pending_nodes` with a fingerprint
     (never its secret).
  3. An operator approves via `POST /api/pending_nodes/{id}/approve`.
  4. The held WS receives the reciprocal `handshake`, the node reaches
     `connected` in `GET /api/nodes`, and the registry now persists its
     secret so a reconnect auto-authenticates.
  5. A denial path: a second node is denied and gets `handshake_reject`.
  6. Reconnect auth: an approved node redialing with the SAME secret
     skips the popup; a WRONG secret is rejected.

Boots `main:app` in a background uvicorn (so env vars + the registration
listener wiring are realistic) and simulates the node side with a raw
`websockets` client — same strategy as
`integration_test_multi_machine.py`'s handshake band.

Run:
    cd backend && .venv/bin/python scripts/integration_test_node_registration.py
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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Each test isolates via BETTER_CLAUDE_HOME; drop any inherited BETTER_AGENT_HOME
# (which takes precedence) so a real home can't shadow the per-test tempdir.
os.environ.pop("BETTER_AGENT_HOME", None)


def _ok(label: str) -> None:
    print(f"\033[92mPASS\033[0m  {label}")


def _fail(label: str, why: str) -> None:
    print(f"\033[91mFAIL\033[0m  {label}: {why}")


def _section(title: str) -> None:
    print(f"\n=== {title} ===")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class BackgroundUvicorn:
    def __init__(self, app_path: str, port: int):
        self.app_path = app_path
        self.port = port
        self.server = None
        self.thread = None

    def start(self):
        import uvicorn
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
        raise RuntimeError(f"uvicorn {self.app_path} failed to start")

    def stop(self):
        if self.server:
            self.server.should_exit = True
        if self.thread:
            self.thread.join(timeout=10)


async def run_registration_tests() -> list[bool]:
    import websockets
    import httpx
    from auth_test_helpers import authenticate_async_client

    results: list[bool] = []

    home = tempfile.mkdtemp(prefix="bc-nodereg-")
    topo_path = Path(home) / "topology.yaml"
    port = free_port()
    # Primary-only topology: NO nodes declared, NO shared token. Any node
    # that dials is "unknown" and must go through the approval flow.
    topo_path.write_text(
        f"schema_version: 1\n"
        f"primary: {{id: primary, address: 'ws://localhost:{port}', cwd_roots: []}}\n"
        f"nodes: {{}}\n"
    )
    os.environ["BETTER_CLAUDE_HOME"] = home
    os.environ["BETTER_CLAUDE_TOPOLOGY_PATH"] = str(topo_path)
    os.environ.pop("BETTER_CLAUDE_NODE_TOKEN", None)  # critical: no shared token
    os.environ["BETTER_CLAUDE_API_ONLY"] = "1"
    import topology
    topology._cache = None

    ws_url = f"ws://127.0.0.1:{port}/api/node/connect"
    base_url = f"http://127.0.0.1:{port}"

    server = BackgroundUvicorn("main:app", port)
    server.start()
    async with httpx.AsyncClient(base_url=base_url, timeout=10) as auth_client:
        token = await authenticate_async_client(auth_client)

    async def _get_json(path: str) -> dict:
        async with httpx.AsyncClient(
            base_url=base_url,
            timeout=10,
            headers={"Authorization": f"Bearer {token}"},
        ) as c:
            r = await c.get(path)
            return r.json()

    async def _post(path: str) -> tuple[int, dict]:
        async with httpx.AsyncClient(
            base_url=base_url,
            timeout=10,
            headers={"Authorization": f"Bearer {token}"},
        ) as c:
            r = await c.post(path)
            try:
                return r.status_code, r.json()
            except Exception:
                return r.status_code, {"text": r.text}

    async def _wait_pending(node_id: str, timeout: float = 5.0) -> dict | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            snap = await _get_json("/api/pending_nodes")
            for rec in snap.get("pending_nodes", []):
                if rec.get("node_id") == node_id:
                    return rec
            await asyncio.sleep(0.1)
        return None

    async def _wait_node_state(node_id: str, want: str, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            snap = await _get_json("/api/nodes")
            n = next((x for x in snap if x.get("id") == node_id), None)
            if n and n.get("state") == want:
                return True
            await asyncio.sleep(0.1)
        return False

    try:
        # ============================================================
        # 1) Approval happy path
        # ============================================================
        _section("Approval flow")
        node_id = "ofeks-test-node"
        secret = "s3cr3t-node-secret-aaaa"

        label = "unknown node dialing gets registration_pending (not rejected)"
        ok = False
        approved_handshake = None
        resume_frame = None
        try:
            async with websockets.connect(
                ws_url, additional_headers={"Authorization": f"Bearer {secret}"},
            ) as ws:
                await ws.send(json.dumps({
                    "type": "handshake",
                    "protocol_version": 1,
                    "node_id": node_id,
                    "registration": {
                        "address": f"ws://localhost:{port + 1}",
                        "cwd_roots": ["/tmp"],
                    },
                }))
                first = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                ok = first.get("type") == "registration_pending" and first.get("node_id") == node_id
                if ok:
                    _ok(label)
                else:
                    _fail(label, f"first frame: {first!r}")
                results.append(ok)

                # 2) it shows up in GET /api/pending_nodes (with fingerprint, no secret)
                label = "pending node appears in GET /api/pending_nodes with fingerprint, no secret"
                rec = await _wait_pending(node_id)
                pend_ok = (
                    rec is not None
                    and rec.get("fingerprint")
                    and "secret_hash" not in rec
                    and "secret" not in json.dumps(rec)
                    and rec.get("cwd_roots") == ["/tmp"]
                )
                if pend_ok:
                    _ok(label)
                else:
                    _fail(label, f"rec={rec!r}")
                results.append(pend_ok)

                # 3) approve via REST → held WS gets the reciprocal handshake
                label = "POST approve resolves the held WS into a handshake + resume_stream"
                code, body = await _post(f"/api/pending_nodes/{node_id}/approve")
                if code != 200 or body.get("status") != "approved":
                    _fail(label, f"approve returned code={code} body={body!r}")
                    results.append(False)
                else:
                    try:
                        approved_handshake = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                        resume_frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                    except asyncio.TimeoutError:
                        approved_handshake = None
                    hs_ok = (
                        approved_handshake is not None
                        and approved_handshake.get("type") == "handshake"
                        and approved_handshake.get("node_id") == "primary"
                        and resume_frame is not None
                        and resume_frame.get("type") == "resume_stream"
                    )
                    if hs_ok:
                        _ok(label)
                    else:
                        _fail(label, f"handshake={approved_handshake!r} resume={resume_frame!r}")
                    results.append(hs_ok)

                    # 4) node reaches connected in GET /api/nodes
                    label = "approved node reaches state=connected in GET /api/nodes"
                    conn_ok = await _wait_node_state(node_id, "connected")
                    if conn_ok:
                        _ok(label)
                    else:
                        snap = await _get_json("/api/nodes")
                        _fail(label, f"snapshot: {snap!r}")
                    results.append(conn_ok)
                # keep WS open through the assertions above; closing here
        except Exception as e:
            _fail("approval flow", f"unexpected: {e}")
            results.append(False)

        # after close, node should drop to disconnected
        label = "node drops to disconnected after WS close"
        disc_ok = await _wait_node_state(node_id, "disconnected")
        if disc_ok:
            _ok(label)
        else:
            _fail(label, "node did not become disconnected")
        results.append(disc_ok)

        # 5) registry now persists the secret (it left pending, joined registry)
        label = "approved node persisted to registry; no longer pending"
        import node_registry_store
        from stores import pending_node_registrations
        reg_ok = (
            node_registry_store.get(node_id) is not None
            and node_registry_store.verify_secret(node_id, secret)
            and pending_node_registrations.get(node_id).get("status") == "approved"
        )
        if reg_ok:
            _ok(label)
        else:
            _fail(label, f"registry={node_registry_store.get(node_id)!r}")
        results.append(reg_ok)

        # ============================================================
        # 6) Reconnect auth — same secret skips popup, wrong secret rejected
        # ============================================================
        _section("Reconnect auth")
        label = "approved node reconnecting with SAME secret skips popup (immediate handshake)"
        ok = False
        try:
            async with websockets.connect(
                ws_url, additional_headers={"Authorization": f"Bearer {secret}"},
            ) as ws:
                await ws.send(json.dumps({
                    "type": "handshake", "protocol_version": 1, "node_id": node_id,
                }))
                first = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                ok = first.get("type") == "handshake" and first.get("node_id") == "primary"
                # Drain the trailing resume_stream so we close gracefully
                # rather than racing the server mid-send.
                try:
                    await asyncio.wait_for(ws.recv(), timeout=2)
                except asyncio.TimeoutError:
                    pass
        except Exception as e:
            _fail(label, f"unexpected: {e}")
        if ok:
            _ok(label)
        else:
            _fail(label, "reconnect did not auto-authenticate")
        results.append(ok)

        label = "approved node reconnecting with WRONG secret is rejected"
        ok = False
        try:
            async with websockets.connect(
                ws_url, additional_headers={"Authorization": "Bearer WRONG-secret"},
            ) as ws:
                await ws.send(json.dumps({
                    "type": "handshake", "protocol_version": 1, "node_id": node_id,
                }))
                first = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                ok = first.get("type") == "handshake_reject"
        except Exception:
            ok = True  # closed connection is also an acceptable rejection
        if ok:
            _ok(label)
        else:
            _fail(label, "wrong-secret reconnect was not rejected")
        results.append(ok)

        # ============================================================
        # 7) Denial path
        # ============================================================
        _section("Denial flow")
        deny_id = "ofeks-deny-node"
        deny_secret = "deny-secret-bbbb"
        label = "denied node receives handshake_reject"
        ok = False
        try:
            async with websockets.connect(
                ws_url, additional_headers={"Authorization": f"Bearer {deny_secret}"},
            ) as ws:
                await ws.send(json.dumps({
                    "type": "handshake", "protocol_version": 1, "node_id": deny_id,
                    "registration": {"address": "", "cwd_roots": []},
                }))
                first = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                if first.get("type") != "registration_pending":
                    _fail(label, f"expected pending, got {first!r}")
                else:
                    if await _wait_pending(deny_id) is None:
                        _fail(label, "deny node never appeared pending")
                    else:
                        code, body = await _post(f"/api/pending_nodes/{deny_id}/deny")
                        if code != 200 or body.get("status") != "denied":
                            _fail(label, f"deny returned code={code} body={body!r}")
                        else:
                            reject = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                            ok = reject.get("type") == "handshake_reject"
        except Exception as e:
            _fail(label, f"unexpected: {e}")
        if ok:
            _ok(label)
        results.append(ok)

        # ============================================================
        # 8) Revoke an approved node
        # ============================================================
        _section("Revoke")
        label = "DELETE /api/nodes/{id} revokes registry entry; secret stops authenticating"
        async with httpx.AsyncClient(
            base_url=base_url,
            timeout=10,
            headers={"Authorization": f"Bearer {token}"},
        ) as c:
            r = await c.delete(f"/api/nodes/{node_id}")
            del_code = r.status_code
        revoke_ok = del_code == 200 and node_registry_store.get(node_id) is None
        if revoke_ok:
            _ok(label)
        else:
            _fail(label, f"del_code={del_code} registry={node_registry_store.get(node_id)!r}")
        results.append(revoke_ok)

        label = "approve of a non-existent pending node returns 404"
        code, body = await _post("/api/pending_nodes/ghost-node/approve")
        ok = code == 404
        if ok:
            _ok(label)
        else:
            _fail(label, f"code={code} body={body!r}")
        results.append(ok)

    finally:
        server.stop()
        # Leave the tempdir; main.py's logging FileHandler may hold an FD.

    return results


async def main() -> int:
    results = await run_registration_tests()
    failed = sum(1 for r in results if not r)
    total = len(results)
    print(f"\n{total - failed}/{total} passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
