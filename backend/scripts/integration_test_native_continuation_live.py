"""Live end-to-end proof: Better Agent continues an externally-created
native claude session.

Phase A writes a REAL native claude session outside Better Agent
(`claude -p` with the host's own CLI config, in a throwaway cwd) that
memorizes a codeword. Phase B imports it into an isolated BA home via
`native_import.import_session` — which stamps `agent_session_id` with the
native sid. Phase C boots a REAL isolated backend and sends a prompt over
the real /ws/chat WebSocket asking for the codeword.

The proof of true continuation (not a fresh amnesiac session):
  1. the reply recalls the codeword only the native session was told;
  2. the session's `agent_session_id` still equals the native sid after
     the turn (the runner resumed, it did not mint a fresh sid);
  3. the ORIGINAL native jsonl under the host's claude projects dir grew
     during the turn — the provider appended to the same transcript.

Everything runs under the __main__ guard: the backend's projection
dispatcher spawns multiprocessing children which re-execute this main
module (macOS spawn), so unguarded top-level work would recurse.

LLM-gated: spends two real haiku turns. Run with the resolved backend
venv python (needs websockets + backend deps):
    RUN_LLM_TESTS=1 <venv-python> scripts/integration_test_native_continuation_live.py
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
_REPO = os.path.dirname(_BACKEND)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

MODEL = "claude-haiku-4-5-20251001"


def _import_phase() -> None:
    """Runs in a subprocess with BETTER_AGENT_HOME already isolated:
    registers the projection drainer (import_session's fold requires it),
    imports the native session passed on argv, prints JSON."""
    import faulthandler

    # A hang here (fold barrier, keyring prompt, …) must self-diagnose:
    # dump every thread's stack and exit instead of pinning the parent
    # against its subprocess timeout with no evidence.
    faulthandler.dump_traceback_later(90, exit=True)

    import config_store
    import native_import
    from _projection_fold import start_projection_fold
    from session_manager import manager as session_manager

    native_sid, jsonl_path, cwd = sys.argv[2], sys.argv[3], sys.argv[4]
    start_projection_fold()

    providers = config_store.list_providers().get("providers", [])
    claude = next(p for p in providers if p.get("kind") == "claude")
    sess = native_import.NativeSession(
        provider_id=claude["id"], provider_kind="claude",
        native_id=native_sid, jsonl_path=jsonl_path, cwd=cwd,
    )
    root_id = native_import.import_session(sess)
    loaded = session_manager.get(root_id) or {}
    print(json.dumps({
        "root_id": root_id,
        "agent_session_id": loaded.get("agent_session_id"),
        "provider_id": claude["id"],
    }))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _http(method: str, url: str, token: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(url, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    data = None
    if body is not None:
        req.add_header("Content-Type", "application/json")
        data = json.dumps(body).encode()
    with urllib.request.urlopen(req, data=data, timeout=30) as resp:
        raw = resp.read()
    return json.loads(raw) if raw else {}


def _main() -> None:
    codeword = f"zebra-lantern-{uuid.uuid4().hex[:6]}"
    root = Path(tempfile.mkdtemp(prefix="ba-native-cont-live-"))
    home = root / "home"
    work = root / "work"
    auth_dir = root / "_auth"
    for d in (home, work, auth_dir):
        d.mkdir()
    port = _free_port()
    base = f"http://127.0.0.1:{port}"

    # ------------------------------------------------------------ Phase A
    # A genuinely external native session: the host's own claude CLI, its
    # own config (real ~/.claude — auth lives there), a throwaway cwd. BA
    # is not involved in any way.
    print(f"=== phase A: external native session in {work}")
    ext = subprocess.run(
        ["claude", "-p",
         f"Remember this: the codeword is {codeword}. Reply with exactly OK.",
         "--model", MODEL, "--output-format", "json"],
        cwd=work, capture_output=True, text=True, timeout=180,
    )
    if ext.returncode != 0:
        shutil.rmtree(root, ignore_errors=True)
        raise SystemExit(f"external claude turn failed:\n{ext.stdout}\n{ext.stderr}")
    native_sid = json.loads(ext.stdout)["session_id"]

    # The CLI wrote its transcript under the REAL claude config dir, in a
    # project dir derived from our unique tmp cwd (cannot collide with
    # real projects). Locate it by sid; remember its size; delete the
    # whole project dir on exit.
    claude_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    hits = list((claude_dir / "projects").glob(f"*/{native_sid}.jsonl"))
    assert len(hits) == 1, f"expected 1 native transcript for {native_sid}, got {hits}"
    native_jsonl = hits[0]
    native_project_dir = native_jsonl.parent
    pre_turn_size = native_jsonl.stat().st_size
    print(f"native sid {native_sid}, transcript {pre_turn_size} bytes")

    username = f"ba-cont-e2e-{secrets.token_hex(4)}"
    password = secrets.token_urlsafe(18)
    plain = auth_dir / "password.plain"
    hash_file = auth_dir / "password.hash"
    secret_file = auth_dir / "session.secret"
    plain.write_text(password)
    subprocess.run(
        [sys.executable, os.path.join(_REPO, "scripts", "hash-password.py"),
         "--password-file", str(plain), "--out", str(hash_file)],
        check=True, cwd=_REPO, capture_output=True,
    )
    plain.unlink()
    secret_file.write_text(secrets.token_hex(32))

    sdk_dir = os.path.join(_REPO, "sdk")
    env = {
        **os.environ,
        "PYTHONPATH": (
            f"{sdk_dir}:{os.environ['PYTHONPATH']}"
            if os.environ.get("PYTHONPATH") else sdk_dir
        ),
        "BETTER_AGENT_HOME": str(home),
        "BETTER_CLAUDE_HOME": str(home),
        "BETTER_AGENT_TEST_MODE": "1",
        "BETTER_AGENT_HEADLESS_AUTH": "1",
        "BETTER_AGENT_USERNAME": username,
        "BETTER_AGENT_PASSWORD_HASH_FILE": str(hash_file),
        "BETTER_AGENT_SESSION_SECRET_FILE": str(secret_file),
        "BETTER_AGENT_BACKEND_PORT": str(port),
        "BETTER_AGENT_BACKEND_URL": base,
    }

    backend = None
    backend_log = None

    def _cleanup() -> None:
        if backend is not None:
            try:
                os.killpg(backend.pid, signal.SIGTERM)
                for _ in range(50):
                    if backend.poll() is not None:
                        break
                    time.sleep(0.2)
                if backend.poll() is None:
                    os.killpg(backend.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        if backend_log is not None:
            backend_log.close()
        # Frozen runtime bundles under runs/ carry read-only modes.
        subprocess.run(["chmod", "-R", "u+w", str(root)], capture_output=True)
        shutil.rmtree(root, ignore_errors=True)
        # The only trace phase A left in the real claude config dir is the
        # project dir derived from our unique tmp cwd — remove exactly that.
        shutil.rmtree(native_project_dir, ignore_errors=True)

    def _fail(msg: str) -> None:
        tail = ""
        try:
            tail = (root / "backend.log").read_text(errors="replace")[-4000:]
        except OSError:
            pass
        _cleanup()
        raise SystemExit(f"FAIL: {msg}\n--- backend.log tail ---\n{tail}")

    try:
        print(f"=== install profile (home={home})")
        install = subprocess.run(
            [sys.executable, os.path.join(_REPO, "scripts", "install.py"),
             "--mode", "default", "--provider", "claude", "--yes", "--adopt"],
            cwd=_REPO, env=env, capture_output=True, text=True,
        )
        if install.returncode != 0:
            _fail(f"install.py failed:\n{install.stdout}\n{install.stderr}")

        # -------------------------------------------------------- Phase B
        print("=== phase B: import into isolated BA home")
        imp = subprocess.run(
            [sys.executable, os.path.abspath(__file__), "--import-phase",
             native_sid, str(native_jsonl), str(work)],
            cwd=_BACKEND, env=env, capture_output=True, text=True, timeout=150,
        )
        if imp.returncode != 0:
            _fail(f"import phase failed:\n{imp.stdout}\n{imp.stderr}")
        imported = json.loads(imp.stdout.strip().splitlines()[-1])
        root_id = imported["root_id"]
        assert imported["agent_session_id"] == native_sid, (
            f"import did not stamp the native sid: {imported}"
        )
        print(f"imported as {root_id}, agent_session_id stamped")

        # -------------------------------------------------------- Phase C
        print(f"=== phase C: booting backend on :{port}")
        backend_log = (root / "backend.log").open("ab")
        backend = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "main:app",
             "--host", "127.0.0.1", "--port", str(port)],
            cwd=_BACKEND, env=env, stdin=subprocess.DEVNULL,
            stdout=backend_log, stderr=backend_log, start_new_session=True,
        )

        deadline = time.monotonic() + 120
        token = ""
        while True:
            if backend.poll() is not None:
                _fail(f"backend exited rc={backend.returncode} before serving")
            try:
                mint = subprocess.run(
                    [sys.executable, "-c",
                     "import sys; sys.path.insert(0, sys.argv[1]); "
                     "import auth; print(auth.create_token('cont-e2e'))",
                     _BACKEND],
                    env=env, capture_output=True, text=True, timeout=30,
                )
                token = mint.stdout.strip()
                if mint.returncode == 0 and token:
                    _http("GET", f"{base}/api/sessions", token)
                    break
            except Exception:
                pass
            if time.monotonic() > deadline:
                _fail("backend did not serve /api/sessions within 120s")
            time.sleep(1.0)
        print("backend up, token minted")

        pre = _http("GET", f"{base}/api/sessions/{root_id}", token)
        if pre.get("agent_session_id") != native_sid:
            _fail(f"backend loaded session without the stamped sid: "
                  f"{pre.get('agent_session_id')!r}")

        async def _drive_turn() -> dict:
            import websockets

            async with websockets.connect(
                f"ws://127.0.0.1:{port}/ws/chat?token={token}", max_size=None,
            ) as ws:
                await ws.send(json.dumps({
                    "type": "subscribe", "app_session_id": root_id,
                }))
                await ws.send(json.dumps({
                    "type": "send_message",
                    "app_session_id": root_id,
                    "prompt": ("What is the codeword I told you earlier? "
                               "Reply with the codeword only."),
                    "cwd": str(work),
                    "model": MODEL,
                    "client_id": str(uuid.uuid4()),
                }))
                turn_deadline = time.monotonic() + 300
                while time.monotonic() < turn_deadline:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
                    except asyncio.TimeoutError:
                        continue
                    frame = json.loads(raw)
                    if frame.get("type") in ("turn_complete", "turn_stopped", "error"):
                        if frame["type"] != "turn_complete":
                            raise AssertionError(f"turn ended abnormally: {frame}")
                        return frame.get("data") or {}
                raise AssertionError("no turn_complete within 300s")

        result = asyncio.run(_drive_turn())
        assert result.get("success"), f"turn_complete reports failure: {result}"
        print("turn_complete received")

        post = _http("GET", f"{base}/api/sessions/{root_id}", token)

        # Proof 1: the reply recalls the codeword only the native session
        # knew.
        assistants = [m for m in post.get("messages") or []
                      if m.get("role") == "assistant"]
        assert assistants, "no assistant messages after the turn"
        last_dump = json.dumps(assistants[-1])
        assert codeword in last_dump, (
            f"resumed turn did not recall the codeword {codeword}; "
            f"last assistant msg: {last_dump[:1500]}"
        )

        # Proof 2: the runner resumed the native sid — it did not mint a
        # new one.
        assert post.get("agent_session_id") == native_sid, (
            f"agent_session_id changed across the turn: "
            f"{post.get('agent_session_id')!r} != {native_sid!r}"
        )

        # Proof 3: the provider appended to the ORIGINAL native transcript.
        post_size = native_jsonl.stat().st_size
        assert post_size > pre_turn_size, (
            f"native transcript did not grow ({pre_turn_size} -> {post_size}); "
            "the turn ran in a fresh provider session"
        )

        print("\n=== LIVE CONTINUATION PASS ===")
        print(f"  external native session {native_sid} (codeword {codeword})")
        print(f"  imported as {root_id} with agent_session_id stamped")
        print("  resumed /ws/chat turn recalled the codeword")
        print(f"  native transcript grew {pre_turn_size} -> {post_size} bytes in place")
    finally:
        _cleanup()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--import-phase":
        _import_phase()
        sys.exit(0)

    from live_llm_test_guard import require_live_llm_tests

    if not require_live_llm_tests("live native-session continuation proof"):
        sys.exit(0)
    _main()
