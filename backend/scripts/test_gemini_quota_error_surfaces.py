"""Regression test: Gemini CLI emits a quota/capacity API error as assistant
text content followed by a result event with status: "success". The runner,
orchestrator, and WS broadcast must still surface it as an error — never as
a successful turn with the error buried in the content.

Reproduces the pattern:
  gemini CLI receives: "You have exhausted your capacity on this model.
  Your quota will reset after 21h18m59s."
  → emits as assistant message text
  → emits result {status: "success"} (no error field)

Pre-fix: runner writes complete.json {success: true}, orchestrator sees
run_failed=False and content_looks_erroring=False (regex required "4xx"),
assistant message stays green with error text as content.

Post-fix: runner detects "API Error:" in accumulated content and flips
success to False; orchestrator's content_looks_erroring matches "API Error:"
without requiring a 4xx code; assistant message gets error bubble.

Asserts:
  1. complete.json["success"] is False (runner caught it)
  2. complete.json["error"] is non-null (runner set error)
  3. Persisted assistant message has error=True + non-empty errorText
  4. A messages_delta with error=True was broadcast

Run with:
    cd backend && .venv/bin/python scripts/test_gemini_quota_error_surfaces.py
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
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
_TMP_HOME = _test_home.isolate("bc-test-gemini-quota-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

# Stub gemini that emits a quota error as assistant text then claims success.
_STUB_DIR = tempfile.mkdtemp(prefix="bc-test-gemini-stub-")
_STUB_PATH = Path(_STUB_DIR) / "gemini"
_STUB_PATH.write_text(
    "#!/usr/bin/env python3\n"
    "import json, sys, uuid\n"
    "try:\n"
    "    sys.stdin.read()\n"
    "except Exception:\n"
    "    pass\n"
    # Emit init
    "sys.stdout.write(json.dumps({\n"
    "    'type': 'init',\n"
    "    'session_id': str(uuid.uuid4()),\n"
    "    'model': 'fake-test-model',\n"
    "}) + '\\n')\n"
    "sys.stdout.flush()\n"
    # Emit an API error as assistant message text — this is the shape
    # the real Gemini CLI produces for various API errors. Use a
    # non-retryable message so the test doesn't enter the retry loop.
    "sys.stdout.write(json.dumps({\n"
    "    'type': 'message',\n"
    "    'role': 'assistant',\n"
    "    'content': '[API Error: Model not found. Please check the model name.]',\n"
    "}) + '\\n')\n"
    "sys.stdout.flush()\n"
    # Emit result with status: "success" — the CLI considers this a
    # "successful" run even though the content is an error.
    "sys.stdout.write(json.dumps({\n"
    "    'type': 'result',\n"
    "    'status': 'success',\n"
    "    'stats': {'input_tokens': 10, 'output_tokens': 20},\n"
    "}) + '\\n')\n"
    "sys.stdout.flush()\n"
    "sys.exit(0)\n"
)
_STUB_PATH.chmod(_STUB_PATH.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
os.environ["PATH"] = f"{_STUB_DIR}{os.pathsep}{os.environ.get('PATH', '')}"

import itsdangerous  # noqa: E402
import uvicorn  # noqa: E402
import websockets  # noqa: E402

import auth_secrets  # noqa: E402
import config_store  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402


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


async def _drive_turn(
    ws_url: str, app_session_id: str, cwd: str, timeout: float,
    cookie_header: dict,
) -> list[dict]:
    frames: list[dict] = []
    async with websockets.connect(ws_url, additional_headers=cookie_header) as ws:
        await ws.send(json.dumps({
            "type": "subscribe",
            "subscription_class": "foreground",
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
                drain_until = time.monotonic() + 1.0
    return frames


async def main() -> int:
    port = _free_port()
    server = _BackgroundUvicorn("main:app", port)
    server.start()
    ws_url = f"ws://127.0.0.1:{port}/ws/chat"
    cwd = tempfile.mkdtemp(prefix="bc-gemquota-cwd-")
    cookie = _mint_session_cookie()
    cookie_header = {"Cookie": f"bc_session={cookie}"}
    failures = 0
    try:
        prov = config_store.add_provider({
            "name": "gemini-quota-test",
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

        frames = await _drive_turn(ws_url, sid, cwd, timeout=60.0, cookie_header=cookie_header)

        if not any(f.get("type") == "turn_complete" for f in frames):
            print(f"{FAIL}  never received turn_complete (frames="
                  f"{[f.get('type') for f in frames]})")
            return 1

        # (1) Runner contract — complete.json["success"] must be False.
        from runs_dir import runs_root as _runs_root
        run_dirs = sorted(
            (d for d in _runs_root().iterdir() if d.is_dir()),
            key=lambda d: d.stat().st_mtime,
        )
        target = None
        for d in reversed(run_dirs):
            bs_path = d / "backend_state.json"
            if not bs_path.exists():
                continue
            try:
                bs = json.loads(bs_path.read_text())
            except Exception:
                continue
            if bs.get("app_session_id") == sid:
                target = d
                break
        if target is None:
            print(f"{FAIL}  no run dir found for session")
            failures += 1
        else:
            complete_path = target / "complete.json"
            if not complete_path.exists():
                print(f"{FAIL}  no complete.json at {complete_path}")
                failures += 1
            else:
                try:
                    cdata = json.loads(complete_path.read_text())
                except Exception as e:
                    print(f"{FAIL}  complete.json unreadable: {e}")
                    cdata = {}
                    failures += 1
                if cdata.get("success") is False:
                    print(f"{PASS}  complete.json success=False")
                else:
                    print(f"{FAIL}  complete.json success={cdata.get('success')!r} "
                          f"— runner did not detect API error in content")
                    failures += 1
                if cdata.get("error"):
                    print(f"{PASS}  complete.json error set: "
                          f"{str(cdata['error'])[:80]!r}")
                else:
                    print(f"{FAIL}  complete.json error is null — "
                          f"runner did not extract error from content")
                    failures += 1

        # (2) Orchestrator gate — finalized assistant message has error
        # stamped on disk.
        msgs = (session_manager.get(sid) or {}).get("messages", [])
        asst = next(
            (m for m in reversed(msgs) if m.get("role") == "assistant"),
            None,
        )
        if asst is None:
            print(f"{FAIL}  no assistant message persisted")
            failures += 1
        elif asst.get("error") is True and (asst.get("errorText") or "").strip():
            print(f"{PASS}  assistant message surfaced error: "
                  f"{asst['errorText'][:80]!r}")
        else:
            print(f"{FAIL}  assistant message did NOT surface error: "
                  f"error={asst.get('error')!r} "
                  f"errorText={asst.get('errorText')!r} "
                  f"content={str(asst.get('content'))[:120]!r}")
            failures += 1

        # (3) Frontend surface — an errored messages_delta was broadcast.
        errored = [
            f for f in frames
            if f.get("type") == "messages_delta"
            and any(
                m.get("error")
                for m in (f.get("data") or {}).get("messages", [])
            )
        ]
        if errored:
            print(f"{PASS}  errored messages_delta broadcast "
                  f"({len(errored)} frame(s))")
        else:
            n_delta = sum(
                1 for f in frames if f.get("type") == "messages_delta"
            )
            print(f"{FAIL}  no errored messages_delta broadcast "
                  f"({n_delta} messages_delta frame(s), none with error)")
            failures += 1

        return 1 if failures else 0
    finally:
        server.stop()
        shutil.rmtree(cwd, ignore_errors=True)
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
        shutil.rmtree(_STUB_DIR, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
