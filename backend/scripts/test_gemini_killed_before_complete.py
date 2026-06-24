"""Regression test: when the Gemini runner is SIGKILLed before it can
write `complete.json`, the orchestrator MUST still surface a non-null
error to the user — never hang the turn waiting for a complete.json
that will never arrive.

Pre-fix, `provider_gemini._watch_complete` looped on
`complete_path.exists() AND popen.poll() is not None` — if the runner
was killed (OOM, manual `kill -9`, OS reaper) it never wrote
complete.json, so the loop spun forever and the orchestrator never
received a `complete` StreamEvent. The turn sat permanently
in-flight; from the user's POV the session stopped with no indication.

Post-fix, the loop breaks on `popen.poll() is not None` alone (with a
short grace window), then `_emit_complete_from_file`'s built-in
fallback synthesizes `{"success": false, "error": "runner exited
without writing complete.json"}` and the orchestrator's error gate
fires.

We force the exact failure with a stub `gemini` on PATH that emits
an `init` line (so bootstrap proceeds) then `sleep`s — and SIGKILL
the runner_gemini.py subprocess before it can finalize.

Asserts:
  1. `turn_complete` arrives within a bounded window (post-fix:
     ~grace + cancel propagation; pre-fix: hangs → test timeout).
  2. The persisted assistant message has error=True + non-empty
     errorText.

Run with:
    cd backend && .venv/bin/python scripts/test_gemini_killed_before_complete.py
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import signal
import socket
import stat
import sys
import tempfile
import threading
import time
from pathlib import Path

# Per CLAUDE.md: isolate ~/.better-claude state BEFORE importing any
# backend module.
import _test_home
_TMP_HOME = _test_home.isolate("bc-test-gemini-kill-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

# Stub gemini on PATH BEFORE backend imports.
_STUB_DIR = tempfile.mkdtemp(prefix="bc-test-gemini-kill-stub-")
_STUB_PATH = Path(_STUB_DIR) / "gemini"
# Stub emits init, then sleeps long enough for us to SIGKILL the
# runner. Drains stdin first so the runner's stdin.drain() returns.
_STUB_PATH.write_text(
    "#!/usr/bin/env python3\n"
    "import json, sys, time, uuid\n"
    "try:\n"
    "    sys.stdin.read()\n"
    "except Exception:\n"
    "    pass\n"
    "sys.stdout.write(json.dumps({\n"
    "    'type': 'init',\n"
    "    'session_id': str(uuid.uuid4()),\n"
    "    'model': 'fake-test-model',\n"
    "}) + '\\n')\n"
    "sys.stdout.flush()\n"
    "time.sleep(120)\n"
)
_STUB_PATH.chmod(_STUB_PATH.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
os.environ["PATH"] = f"{_STUB_DIR}{os.pathsep}{os.environ.get('PATH', '')}"

import itsdangerous  # noqa: E402
import uvicorn  # noqa: E402
import websockets  # noqa: E402

import auth_secrets  # noqa: E402
import config_store  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402
from runs_dir import runs_root as _runs_root  # noqa: E402


def _mint_session_cookie() -> str:
    secret = auth_secrets.get_session_secret()
    signer = itsdangerous.TimestampSigner(str(secret))
    payload = base64.b64encode(
        json.dumps({"user": {"username": "integration-test"}}).encode()
    )
    return signer.sign(payload).decode("utf-8")

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"
MODEL = "gemini-2.5-pro"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _BackgroundUvicorn:
    def __init__(self, app_path: str, port: int) -> None:
        self._cfg = uvicorn.Config(
            app_path, host="127.0.0.1", port=port, log_level="warning",
        )
        self.server = uvicorn.Server(self._cfg)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self) -> None:
        self.thread.start()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(
                    ("127.0.0.1", self._cfg.port), 0.2
                ):
                    return
            except OSError:
                time.sleep(0.2)
        raise RuntimeError("uvicorn did not come up")

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)


async def _kill_runner_when_spawned(app_session_id: str, deadline: float) -> int:
    """Watch the runs dir for a backend_state.json belonging to our
    session, then SIGKILL its runner_pid. Returns the pid that was
    killed, or 0 if none was found in time. The kill targets the
    runner_gemini.py process — its child gemini stub is left to die on
    its own; what matters for this test is the runner can't finalize
    complete.json."""
    while time.monotonic() < deadline:
        await asyncio.sleep(0.1)
        try:
            for d in _runs_root().iterdir():
                bs_path = d / "backend_state.json"
                state_path = d / "state.json"
                if not bs_path.exists() or not state_path.exists():
                    continue
                try:
                    bs = json.loads(bs_path.read_text())
                except Exception:
                    continue
                if bs.get("app_session_id") != app_session_id:
                    continue
                pid = bs.get("runner_pid")
                if not pid:
                    continue
                # state.json present means bootstrap succeeded (session
                # discovered, tailer + watcher running). Now kill the
                # runner — provider must surface the error.
                try:
                    os.kill(int(pid), signal.SIGKILL)
                    return int(pid)
                except (ProcessLookupError, PermissionError):
                    return 0
        except FileNotFoundError:
            continue
    return 0


async def _drive_turn(
    ws_url: str, app_session_id: str, cwd: str, timeout: float,
    cookie_header: dict,
) -> list[dict]:
    frames: list[dict] = []
    async with websockets.connect(ws_url, additional_headers=cookie_header) as ws:
        await ws.send(json.dumps({
            "type": "subscribe",
            "app_session_id": app_session_id,
            "cwd": cwd,
        }))
        await asyncio.sleep(0.3)
        await ws.send(json.dumps({
            "type": "send_message",
            "prompt": "hi",
            "model": MODEL,
            "cwd": cwd,
            "app_session_id": app_session_id,
            "send_mode": "send",
        }))
        deadline = time.monotonic() + timeout
        saw_complete = False
        drain_until = 0.0
        while time.monotonic() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=0.5)
            except asyncio.TimeoutError:
                if saw_complete and time.monotonic() >= drain_until:
                    break
                continue
            except websockets.ConnectionClosed:
                break
            try:
                f = json.loads(raw)
            except json.JSONDecodeError:
                continue
            frames.append(f)
            if f.get("type") == "turn_complete" and not saw_complete:
                saw_complete = True
                drain_until = time.monotonic() + 0.5
    return frames


async def main() -> int:
    port = _free_port()
    server = _BackgroundUvicorn("main:app", port)
    server.start()
    ws_url = f"ws://127.0.0.1:{port}/ws/chat"
    cwd = tempfile.mkdtemp(prefix="bc-gemkill-cwd-")
    cookie = _mint_session_cookie()
    cookie_header = {"Cookie": f"bc_session={cookie}"}
    failures = 0
    killed_pid = 0
    try:
        prov = config_store.add_provider({
            "name": "gemini-kill-test",
            "kind": "gemini",
            "mode": "subscription",
            "default_model": MODEL,
        })
        config_store.set_default_provider(prov["id"])

        sess = session_manager.create(
            name="t", model=MODEL, cwd=cwd, orchestration_mode="native",
            provider_id=prov["id"],
        )
        sid = sess["id"]

        # Run the kill-watcher in parallel with the turn driver. The
        # watcher gives the bootstrap ~20s to write state.json + spawn
        # the watch_complete task, then SIGKILLs the runner.
        kill_task = asyncio.create_task(
            _kill_runner_when_spawned(sid, time.monotonic() + 20.0),
            name="kill-watcher",
        )

        # Bounded total — post-fix should complete in a few seconds;
        # pre-fix hangs forever (watch_complete never breaks).
        frames = await _drive_turn(ws_url, sid, cwd, timeout=30.0, cookie_header=cookie_header)
        killed_pid = await kill_task

        if killed_pid == 0:
            print(f"{FAIL}  could not find runner to kill (test setup issue)")
            return 1

        if not any(f.get("type") == "turn_complete" for f in frames):
            print(f"{FAIL}  never received turn_complete after SIGKILL "
                  f"(frames={[f.get('type') for f in frames]}) — "
                  f"watch_complete is wedged on missing complete.json")
            failures += 1
        else:
            print(f"{PASS}  turn_complete arrived after SIGKILLing runner pid={killed_pid}")

        msgs = (session_manager.get(sid) or {}).get("messages", [])
        asst = next(
            (m for m in reversed(msgs) if m.get("role") == "assistant"),
            None,
        )
        if asst is None:
            print(f"{FAIL}  no assistant message persisted")
            failures += 1
        elif asst.get("error") is True and (asst.get("errorText") or "").strip():
            print(f"{PASS}  assistant message surfaced runner-death error: "
                  f"{asst['errorText'][:80]!r}")
        else:
            print(f"{FAIL}  assistant message did NOT surface the runner "
                  f"death: error={asst.get('error')!r} "
                  f"errorText={asst.get('errorText')!r} "
                  f"content={asst.get('content')!r}")
            failures += 1

        return 1 if failures else 0
    finally:
        server.stop()
        shutil.rmtree(cwd, ignore_errors=True)
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
        shutil.rmtree(_STUB_DIR, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
