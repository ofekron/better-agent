"""Integration test for the approval-based node registration flow.

Exercises the full primary-side loop that replaces the static
`BETTER_CLAUDE_NODE_TOKEN` with a trust-on-first-approve handshake:

  1. A brand-new worker-node dials `/api/node/connect` presenting its
     self-generated secret (no shared token, not declared in topology).
  2. Primary holds the WS open and emits `node_registration_requested`;
     the node appears through the authenticated machine-nodes `pending`
     capability with a fingerprint (never its secret).
  3. The machine-nodes extension approves through the core capability API.
  4. The held WS receives the reciprocal `handshake`, the node reaches
     `connected` in the `list` capability, and the registry now persists
     its secret so a reconnect auto-authenticates.
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
import logging
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from websockets.exceptions import ConnectionClosed

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _test_home


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
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if not self.thread.is_alive():
                    raise RuntimeError(f"uvicorn {self.app_path} exited before readiness")
                try:
                    with urllib.request.urlopen(
                        f"http://127.0.0.1:{self.port}/readyz",
                        timeout=0.3,
                    ) as response:
                        if response.status == 200:
                            return
                except (OSError, urllib.error.URLError):
                    pass
                time.sleep(0.05)
            raise RuntimeError(f"uvicorn {self.app_path} failed readiness")
        except BaseException:
            self.stop()
            raise

    def stop(self):
        if self.server:
            self.server.should_exit = True
        if self.thread:
            self.thread.join(timeout=10)
            if self.thread.is_alive():
                raise RuntimeError(f"uvicorn {self.app_path} did not stop")


class NodeProtocolPeer:
    def __init__(self, websocket):
        self.websocket = websocket
        self.frames: asyncio.Queue[dict] = asyncio.Queue()
        self.reader = asyncio.create_task(self._read(), name="node-protocol-peer")

    async def _read(self) -> None:
        try:
            async for raw in self.websocket:
                frame = json.loads(raw)
                if frame.get("type") == "rpc_request":
                    await self.websocket.send(json.dumps({
                        "type": "rpc_response",
                        "request_id": frame.get("request_id"),
                        "ok": True,
                        "result": {"ok": True},
                    }))
                    continue
                await self.frames.put(frame)
        except ConnectionClosed as exc:
            if exc.code not in {1000, 1008}:
                raise

    async def receive(self, timeout: float = 5.0) -> dict:
        return await asyncio.wait_for(self.frames.get(), timeout=timeout)

    async def close(self) -> None:
        await self.websocket.close()
        await self.reader


async def _run_registration_tests_in_home(home: Path) -> list[bool]:
    import websockets
    import httpx
    import _test_extension
    import _test_installation

    results: list[bool] = []

    _test_installation.activate(home, provider="codex")
    _test_extension.install_core_role(home, "machine-nodes")

    port = free_port()
    # No topology is intentional: topology.yaml is an allowlist when present.
    # A dynamic-only deployment is the contract where unknown nodes enter the
    # approval flow.
    os.environ.pop("BETTER_CLAUDE_TOPOLOGY_PATH", None)
    os.environ.pop("BETTER_CLAUDE_NODE_TOKEN", None)  # critical: no shared token
    os.environ["BETTER_CLAUDE_API_ONLY"] = "1"
    import topology
    topology._cache = None

    ws_url = f"ws://127.0.0.1:{port}/api/node/connect"
    base_url = f"http://127.0.0.1:{port}"
    nodes_path = "/api/internal/machine-nodes/list"
    pending_path = "/api/internal/machine-nodes/pending"
    approve_path = "/api/internal/machine-nodes/approve"
    deny_path = "/api/internal/machine-nodes/deny"
    revoke_path = "/api/internal/machine-nodes/revoke"

    server = BackgroundUvicorn("main:app", port)
    server.start()
    import main
    internal_token = main.coordinator.internal_token

    async def _post(
        path: str,
        body: dict | None = None,
        *,
        token: str = internal_token,
    ) -> tuple[int, object]:
        async with httpx.AsyncClient(
            base_url=base_url,
            timeout=10,
            headers={"X-Internal-Token": token},
        ) as c:
            r = await c.post(path, json=body or {})
            try:
                return r.status_code, r.json()
            except json.JSONDecodeError as exc:
                raise AssertionError(
                    f"POST {path} returned non-JSON status={r.status_code}: {r.text!r}"
                ) from exc

    async def _post_success(
        path: str,
        expected_type: type,
        body: dict | None = None,
    ):
        code, payload = await _post(path, body)
        if code != 200:
            raise AssertionError(f"POST {path} returned status={code}: {payload!r}")
        if not isinstance(payload, expected_type):
            raise AssertionError(
                f"POST {path} returned {type(payload).__name__}, "
                f"expected {expected_type.__name__}: {payload!r}"
            )
        return payload

    async def _wait_pending(node_id: str, timeout: float = 5.0) -> dict | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            snap = await _post_success(pending_path, dict)
            pending = snap.get("pending_nodes")
            if not isinstance(pending, list) or any(
                not isinstance(record, dict) for record in pending
            ):
                raise AssertionError(f"pending-node response has invalid shape: {snap!r}")
            for rec in pending:
                if rec.get("node_id") == node_id:
                    return rec
            await asyncio.sleep(0.1)
        return None

    async def _wait_node_state(node_id: str, want: str, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            snap = await _post_success(nodes_path, list)
            if any(not isinstance(record, dict) for record in snap):
                raise AssertionError(f"node response has invalid shape: {snap!r}")
            n = next((x for x in snap if x.get("id") == node_id), None)
            if n and n.get("state") == want:
                return True
            await asyncio.sleep(0.1)
        return False

    try:
        label = "machine-node core API rejects the wrong runtime token"
        code, _ = await _post(nodes_path, token="wrong-runtime-token")
        auth_ok = code == 403
        if auth_ok:
            _ok(label)
        else:
            _fail(label, f"status={code}")
        results.append(auth_ok)

        # ============================================================
        # 1) Approval happy path
        # ============================================================
        _section("Approval flow")
        node_id = "ofeks-test-node"
        secret = "s3cr3t-node-secret-aaaa"

        label = "unknown node dialing gets registration_pending (not rejected)"
        ok = False
        try:
            async with websockets.connect(
                ws_url, additional_headers={"Authorization": f"Bearer {secret}"},
            ) as ws:
                peer = NodeProtocolPeer(ws)
                try:
                    await ws.send(json.dumps({
                        "type": "handshake",
                        "protocol_version": 1,
                        "node_id": node_id,
                        "registration": {
                            "address": f"ws://localhost:{port + 1}",
                            "cwd_roots": ["/tmp"],
                        },
                    }))
                    first = await peer.receive()
                    ok = (
                        first.get("type") == "registration_pending"
                        and first.get("node_id") == node_id
                    )
                    if ok:
                        _ok(label)
                    else:
                        _fail(label, f"first frame: {first!r}")
                    results.append(ok)

                    # 2) it shows up in the pending-node capability.
                    label = "pending-node capability returns fingerprint, no secret"
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
                    label = "approve capability resolves the held WS into a handshake + resume_stream"
                    code, body = await _post(approve_path, {"node_id": node_id})
                    if (
                        code != 200
                        or not isinstance(body, dict)
                        or body.get("status") != "approved"
                    ):
                        _fail(label, f"approve returned code={code} body={body!r}")
                        results.append(False)
                    else:
                        approved_handshake = await peer.receive()
                        resume_frame = await peer.receive()
                        hs_ok = (
                            approved_handshake.get("type") == "handshake"
                            and approved_handshake.get("node_id") == "primary"
                            and resume_frame.get("type") == "resume_stream"
                        )
                        if hs_ok:
                            _ok(label)
                        else:
                            _fail(label, f"handshake={approved_handshake!r} resume={resume_frame!r}")
                        results.append(hs_ok)

                        # 4) node reaches connected in the authoritative snapshot.
                        label = "approved node reaches state=connected in node list capability"
                        conn_ok = await _wait_node_state(node_id, "connected")
                        if conn_ok:
                            _ok(label)
                        else:
                            snap = await _post_success(nodes_path, list)
                            _fail(label, f"snapshot: {snap!r}")
                        results.append(conn_ok)
                finally:
                    await peer.close()
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
                peer = NodeProtocolPeer(ws)
                try:
                    await ws.send(json.dumps({
                        "type": "handshake", "protocol_version": 1, "node_id": node_id,
                    }))
                    first = await peer.receive()
                    resume = await peer.receive()
                    ok = (
                        first.get("type") == "handshake"
                        and first.get("node_id") == "primary"
                        and resume.get("type") == "resume_stream"
                    )
                finally:
                    await peer.close()
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
        except websockets.exceptions.ConnectionClosed as exc:
            ok = exc.code == 1008
        except Exception as exc:
            _fail(label, f"unexpected: {exc}")
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
                peer = NodeProtocolPeer(ws)
                try:
                    await ws.send(json.dumps({
                        "type": "handshake", "protocol_version": 1, "node_id": deny_id,
                        "registration": {"address": "", "cwd_roots": []},
                    }))
                    first = await peer.receive()
                    if first.get("type") != "registration_pending":
                        _fail(label, f"expected pending, got {first!r}")
                    else:
                        if await _wait_pending(deny_id) is None:
                            _fail(label, "deny node never appeared pending")
                        else:
                            code, body = await _post(deny_path, {"node_id": deny_id})
                            if (
                                code != 200
                                or not isinstance(body, dict)
                                or body.get("status") != "denied"
                            ):
                                _fail(label, f"deny returned code={code} body={body!r}")
                            else:
                                reject = await peer.receive()
                                ok = reject.get("type") == "handshake_reject"
                finally:
                    await peer.close()
        except Exception as e:
            _fail(label, f"unexpected: {e}")
        if ok:
            _ok(label)
        results.append(ok)

        # ============================================================
        # 8) Revoke an approved node
        # ============================================================
        _section("Revoke")
        label = "revoke capability removes the approved registry entry"
        revoke_code, revoke_body = await _post(revoke_path, {"node_id": node_id})
        revoke_ok = (
            revoke_code == 200
            and isinstance(revoke_body, dict)
            and revoke_body.get("status") == "revoked"
            and node_registry_store.get(node_id) is None
        )
        if revoke_ok:
            _ok(label)
        else:
            _fail(
                label,
                f"code={revoke_code} body={revoke_body!r} "
                f"registry={node_registry_store.get(node_id)!r}",
            )
        results.append(revoke_ok)

        label = "approve of a non-existent pending node returns 404"
        code, body = await _post(approve_path, {"node_id": "ghost-node"})
        ok = code == 404
        if ok:
            _ok(label)
        else:
            _fail(label, f"code={code} body={body!r}")
        results.append(ok)

    finally:
        server.stop()

    return results


async def run_registration_tests() -> list[bool]:
    test_home = _test_home.TestHome.acquire("bc-nodereg-")
    home = Path(test_home.path)
    try:
        return await _run_registration_tests_in_home(home)
    finally:
        logging.shutdown()
        test_home.release()
        if home.exists():
            raise RuntimeError(f"node registration test home survived cleanup: {home}")


async def main() -> int:
    results = await run_registration_tests()
    failed = sum(1 for r in results if not r)
    total = len(results)
    print(f"\n{total - failed}/{total} passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
