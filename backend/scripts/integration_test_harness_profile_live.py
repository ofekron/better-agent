#!/usr/bin/env python3
"""Live end-to-end test: a harness profile actually shapes a real run.

Isolation: its own `BETTER_AGENT_HOME`, its own activated installation, its
own uvicorn on a free port. Nothing touches the developer's real state.

Model: the Antigravity (`agy`) CLI on its cheapest tier, so proving the
profile reaches a real turn never spends subscription usage on a capable
model.

The deterministic suites already lock resolution and projection in memory.
What only a live run can prove: a session bound to a named profile spawns,
completes a turn, and carries that profile's overrides into the run inputs
the provider was launched with.

Run with:
    cd backend && RUN_LLM_TESTS=1 .venv/bin/python \
        scripts/integration_test_harness_profile_live.py
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import secrets
import shutil
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import _test_home  # noqa: E402

BA_HOME = _test_home.isolate("bc-int-harness-profile-")


def _engage_headless_auth(home: Path) -> None:
    """Throwaway credentials for this run only.

    The keychain entries are home-scoped, so an isolated home has none and
    the backend would refuse to start. Headless auth reads the secrets from
    files instead — written here into the temp home and destroyed with it.
    No login is performed: the test signs its own cookie with this secret.
    """
    import bcrypt

    secret_file = home / "session-secret"
    secret_file.write_text(secrets.token_hex(32), encoding="utf-8")
    hash_file = home / "password-hash"
    hash_file.write_text(
        bcrypt.hashpw(secrets.token_hex(16).encode(), bcrypt.gensalt()).decode(),
        encoding="utf-8",
    )
    os.environ["BETTER_AGENT_HEADLESS_AUTH"] = "1"
    os.environ["BETTER_AGENT_USERNAME"] = "integration-test"
    os.environ["BETTER_AGENT_SESSION_SECRET_FILE"] = str(secret_file)
    os.environ["BETTER_AGENT_PASSWORD_HASH_FILE"] = str(hash_file)


_engage_headless_auth(Path(BA_HOME))

import _test_installation  # noqa: E402
import httpx  # noqa: E402
import itsdangerous  # noqa: E402
import uvicorn  # noqa: E402
import websockets  # noqa: E402

from live_llm_test_guard import require_live_llm_tests  # noqa: E402

# Cheapest Antigravity tier — see provider_agy.AGY_MODELS.
CHEAP_MODEL = "Gemini 3.5 Flash (Low)"
# A builtin tool the profile turns off. Disabling it is inert for a one-shot
# prompt, so the assertion is about the override travelling, not behavior.
DISABLED_TOOL = "ask"


def _ok(label: str) -> None:
    print(f"\033[92mPASS\033[0m  {label}")


def _fail(label: str, why: str) -> None:
    print(f"\033[91mFAIL\033[0m  {label}: {why}")


def _assistant_text(message: dict) -> str:
    """Assistant text as the UI reads it: from the message's `agent_message`
    events. The flat `content` field carries user prompts, not agent output."""
    chunks: list[str] = []
    for event in message.get("events") or []:
        data = event.get("data") or {}
        # Some events wrap a second `agent_message` envelope; unwrap to the
        # payload that actually holds `message.content`.
        while isinstance(data.get("data"), dict) and "message" not in data:
            data = data["data"]
        for block in (data.get("message") or {}).get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text":
                chunks.append(str(block.get("text") or ""))
    return "".join(chunks).strip()


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class BackgroundUvicorn:
    def __init__(self, app_path: str, port: int) -> None:
        self.port = port
        self.app_path = app_path
        self.server: uvicorn.Server | None = None
        self.thread: threading.Thread | None = None

    def start(self) -> None:
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

    def stop(self) -> None:
        if self.server:
            self.server.should_exit = True
        if self.thread:
            self.thread.join(timeout=10)


def _mint_session_cookie() -> str:
    """Sign a session cookie the way Starlette's SessionMiddleware does.
    Read-only against the keychain — never writes credentials."""
    import auth_secrets

    signer = itsdangerous.TimestampSigner(str(auth_secrets.get_session_secret()))
    payload = base64.b64encode(
        json.dumps({"user": {"username": "integration-test"}}).encode()
    )
    return signer.sign(payload).decode("utf-8")


async def _drive_turn(ws_url: str, cookie_header: dict, sid: str, cwd: str) -> list[str]:
    """One real turn. Returns the errors seen on the wire (empty == clean)."""
    errors: list[str] = []
    try:
        async with websockets.connect(ws_url, additional_headers=cookie_header) as ws:
            await ws.send(json.dumps({"type": "subscribe", "app_session_id": sid}))
            await asyncio.sleep(0.3)
            await ws.send(json.dumps({
                "type": "send_message",
                "prompt": "Reply with exactly the word: pong",
                "app_session_id": sid,
                "model": CHEAP_MODEL,
                "cwd": cwd,
            }))
            deadline = time.monotonic() + 240
            while time.monotonic() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=5)
                except asyncio.TimeoutError:
                    continue
                evt = json.loads(raw)
                if evt.get("type") == "turn_complete":
                    return errors
                if evt.get("type") == "error":
                    errors.append(str(evt.get("data")))
            errors.append("no turn_complete within 240s")
    except Exception as exc:  # noqa: BLE001 - reported, not swallowed
        errors.append(f"{type(exc).__name__}: {exc}")
    return errors


async def main() -> int:
    if not require_live_llm_tests("live harness-profile integration"):
        return 0
    if shutil.which("agy") is None:
        print("SKIP — `agy` CLI not on PATH")
        return 0

    print(f"BETTER_AGENT_HOME = {BA_HOME}")
    _test_installation.activate(Path(BA_HOME))

    import harness_profile_resolver  # noqa: PLC0415
    import harness_run_projection  # noqa: PLC0415
    from session_manager import manager as session_manager  # noqa: PLC0415

    port = free_port()
    server = BackgroundUvicorn("main:app", port)
    server.start()
    base = f"http://127.0.0.1:{port}"
    ws_url = f"ws://127.0.0.1:{port}/ws/chat"
    cwd = tempfile.mkdtemp(prefix="bc-int-harness-profile-cwd-")
    cookie = _mint_session_cookie()
    failures = 0

    try:
        async with httpx.AsyncClient(
            base_url=base, timeout=300, cookies={"better_agent_session": cookie},
        ) as client:
            r = await client.get("/api/auth/me")
            if r.status_code != 200:
                _fail("auth probe", f"HTTP {r.status_code}: {r.text[:200]}")
                return 1
            _ok("auth cookie accepted")

            # 1) A real Antigravity provider, pinned to the cheapest tier.
            r = await client.post("/api/providers", json={
                "name": "Antigravity-IT", "kind": "agy", "mode": "subscription",
                "default_model": CHEAP_MODEL,
            })
            if r.status_code != 200:
                _fail("create provider", f"HTTP {r.status_code}: {r.text[:200]}")
                return 1
            provider_id = r.json()["id"]
            r = await client.post(f"/api/providers/{provider_id}/set-default")
            if r.status_code != 200:
                _fail("activate provider", f"HTTP {r.status_code}: {r.text[:200]}")
                return 1
            _ok(f"agy provider active on {CHEAP_MODEL!r}")

            # 2) A named profile that turns one builtin tool off.
            r = await client.post("/api/harness-profiles", json={"name": "Live Profile"})
            if r.status_code != 200:
                _fail("create profile", f"HTTP {r.status_code}: {r.text[:200]}")
                return 1
            profile = r.json()
            profile_id = profile["id"]

            r = await client.patch(
                f"/api/harness-profiles/{profile_id}/fields",
                json={"revision": profile["revision"], "writes": [
                    {"path": ["disabled_builtin_tools", DISABLED_TOOL], "value": False},
                ]},
            )
            if r.status_code != 200:
                _fail("write profile field", f"HTTP {r.status_code}: {r.text[:200]}")
                return 1
            profile = r.json()
            _ok(f"profile {profile_id!r} disables {DISABLED_TOOL!r}")

            # The write must stay scoped to the profile — Default keeps the
            # tool available, which is what makes the run assertion meaningful.
            r = await client.get("/api/harness/default/disabled-builtin-tools")
            if DISABLED_TOOL in (r.json().get("disabled_builtin_tools") or []):
                _fail("profile isolation", "the named-profile write leaked into Default")
                failures += 1
            else:
                _ok("Default still leaves the tool available")

            # 3) A session bound to that profile.
            r = await client.post("/api/sessions", json={
                "name": "HarnessProfileIT", "model": CHEAP_MODEL, "cwd": cwd,
                "orchestration_mode": "native", "provider_id": provider_id,
                "harness_profile_id": profile_id,
                "harness_profile_revision": profile["revision"],
            })
            if r.status_code != 200:
                _fail("create session", f"HTTP {r.status_code}: {r.text[:200]}")
                return 1
            sid = r.json()["id"]

            stored = session_manager.get(sid) or {}
            if stored.get("harness_profile_id") != profile_id:
                _fail("session binding", f"persisted profile={stored.get('harness_profile_id')!r}")
                return 1
            _ok(f"session {sid[:8]} bound to the profile")

            # 4) The snapshot the run is launched with must carry the override.
            snapshot = harness_profile_resolver.resolve_for_session(stored)
            if not harness_run_projection.is_active(snapshot):
                _fail("run snapshot", f"inactive snapshot: {snapshot}")
                return 1
            projected = harness_run_projection.apply_to_inputs(
                {"resolved_harness_run_config": snapshot}
            )
            if DISABLED_TOOL not in (projected.get("disabled_builtin_tools") or []):
                _fail(
                    "run projection",
                    f"{DISABLED_TOOL!r} missing from {projected.get('disabled_builtin_tools')}",
                )
                failures += 1
            else:
                _ok(f"run inputs carry disabled_builtin_tools={DISABLED_TOOL!r}")

            # 5) The real turn — the part no in-memory test can prove.
            errors = await _drive_turn(
                ws_url, {"Cookie": f"better_agent_session={cookie}"}, sid, cwd,
            )
            if errors:
                _fail("live turn", f"errors: {errors}")
                return 1
            _ok("turn_complete on the profile-bound session")

            r = await client.get(f"/api/sessions/{sid}")
            messages = (r.json() if r.status_code == 200 else {}).get("messages", [])
            assistant = [m for m in messages if m.get("role") == "assistant"]
            if not assistant:
                _fail("assistant message", "none produced")
                return 1
            last = assistant[-1]
            content = _assistant_text(last)
            if not content:
                _fail("assistant content", f"no agent_message text; msg={last}")
                failures += 1
            elif last.get("error"):
                _fail("assistant error", str(last.get("error")))
                failures += 1
            else:
                _ok(f"assistant replied: {content[:60]!r}")

            # 6) The binding survives the turn — a completed run must not
            # silently drop the session back onto Default.
            after = session_manager.get(sid) or {}
            if after.get("harness_profile_id") != profile_id:
                _fail("binding after turn", f"profile={after.get('harness_profile_id')!r}")
                failures += 1
            else:
                _ok("profile binding intact after the turn")
    finally:
        server.stop()
        shutil.rmtree(cwd, ignore_errors=True)
        shutil.rmtree(BA_HOME, ignore_errors=True)

    print()
    if failures:
        print(f"\033[91m{failures} FAILURE(S)\033[0m")
        return 1
    print("\033[92mALL PASS — harness profiles shape a real run\033[0m")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
