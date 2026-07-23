"""Locks the /ws/chat application-level heartbeat contract.

Neither the ASGI server nor any reverse proxy in this stack configures a WS
idle timeout, so a connection killed silently by a mobile network
transition (OS-suspended background sockets, WiFi<->cellular handoff,
carrier NAT idle-drop) sits open on both ends forever -- `readyState` stays
OPEN, nothing arrives. The client (useWebSocket.ts) detects this by sending
periodic `{"type": "ping"}` frames and watching for `{"type": "pong"}`
replies; this test locks the server side of that contract.

Run with:
    cd backend && .venv/bin/python scripts/test_ws_heartbeat.py
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


def _bootstrap_session_secret() -> tuple[str, str]:
    """Writes a session_secret keychain entry scoped to this test's
    isolated BETTER_AGENT_HOME (home_suffix() derives a unique per-home
    service name), mirroring what run.sh does on first launch. Returns
    (service, account) so the caller can delete it afterward -- this is a
    real OS keychain write, not a temp file, so it must be cleaned up."""
    import keychain_names
    import oskeychain
    import secrets

    service = keychain_names.auth_services()[0]
    account = "session_secret"
    oskeychain.store(service, account, secrets.token_hex(32))
    return service, account


async def _bootstrap_installation_profile(ba_home: str) -> None:
    """`/ws/chat` is gated by InstallationAdmissionMiddleware behind the
    PROVIDER_CONVERSATIONS capability, which requires a fully "active"
    installation profile with a matching activation receipt (see
    installation_profile.py). Constructs one for this test's isolated
    ba_home() WITHOUT touching shared backend state: reads (never writes)
    the repo's already-active backend/.active-venv + its dependency-plan
    marker (belongs to this checkout, not any one BETTER_AGENT_HOME) and
    verifies the already-installed `claude` binary read-only (runs its
    --version-style check, does not install/modify anything)."""
    import installation_profile
    import provider_setup

    identity = await provider_setup.verified_provider_identity("claude")
    if identity is None:
        raise RuntimeError("`claude` CLI not verifiable on PATH")
    profile = installation_profile.new_active_profile(
        mode=installation_profile.DEFAULT,
        provider="claude",
        provider_identity=identity,
    )
    installation_profile.stage_activation(profile)
    config_path = os.path.join(ba_home, "config.json")
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump({
            "default_provider_id": "test-claude",
            "providers": [{"id": "test-claude", "kind": "claude", "suspended": False}],
        }, f)
    installation_profile.mark_selection_applied()


def _mint_bearer_token() -> str:
    # /ws/chat's cookie-session path replicates Starlette's internal
    # SessionMiddleware signing scheme, which is an implementation detail
    # not worth coupling a test to. The bearer-token query-param fallback
    # (the same path native/mobile clients use, per bearerAuth.ts) is a
    # small, stable, public contract: auth.create_token + auth.verify_token.
    import auth
    return auth.create_token("integration-test")


async def main() -> int:
    ba_home = tempfile.mkdtemp(prefix="bc-int-wsheartbeat-home-")
    os.makedirs(os.path.join(ba_home, "logs"), exist_ok=True)
    os.environ["BETTER_CLAUDE_HOME"] = ba_home
    os.environ["BETTER_AGENT_HOME"] = ba_home

    kc_service, kc_account = _bootstrap_session_secret()
    await _bootstrap_installation_profile(ba_home)

    port = free_port()
    server = BackgroundUvicorn("main:app", port)
    server.start()
    token = _mint_bearer_token()
    ws_url = f"ws://127.0.0.1:{port}/ws/chat?token={token}"

    try:
        async with websockets.connect(ws_url) as ws:
            await ws.send(json.dumps({"type": "ping"}))
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                raw = await asyncio.wait_for(ws.recv(), timeout=10)
                evt = json.loads(raw)
                if evt.get("type") == "pong":
                    print("PASS  ping -> pong")
                    return 0
            print("FAIL  no pong received within 10s")
            return 1
    finally:
        server.stop()
        import oskeychain
        oskeychain.delete(kc_service, kc_account)
        shutil.rmtree(ba_home, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
