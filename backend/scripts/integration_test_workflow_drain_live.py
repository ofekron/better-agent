"""Live end-to-end proof: a Better Agent claude session survives a real
Workflow tool run.

Boots a REAL isolated backend (fresh home, real installation profile,
primary-launcher lease, uvicorn — the same sequence the fullstack
Playwright harness uses), creates a session over REST, sends a prompt
over the real /ws/chat WebSocket that makes the model launch a real
Workflow (two sequential subagents, so the workflow is still running
when the launching turn's ResultMessage lands), and waits for
`turn_complete`.

Pre-fix the per-turn runner exited at that first ResultMessage, killing
the CLI and the workflow mid-flight — the turn ended with 'WAITING' and
the workflow outcome was lost. Post-fix the runner drains until the
workflow's auto notification turn completes, and the turn's durable
completion carries the workflow-produced marker.

LLM-gated: spends real model turns (cheapest model, haiku). Run with the
resolved backend venv python (needs websockets + backend deps):
    RUN_LLM_TESTS=1 <venv-python> scripts/integration_test_workflow_drain_live.py
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

from live_llm_test_guard import require_live_llm_tests  # noqa: E402

if not require_live_llm_tests("live Workflow drain end-to-end proof"):
    sys.exit(0)

MODEL = "claude-haiku-4-5-20251001"
MARKER = f"WF_OK_{uuid.uuid4().hex[:8]}"
WF_SCRIPT = f"""export const meta = {{name:'e2e-probe', description:'e2e drain probe', phases:[{{title:'P'}}]}}
phase('P')
await agent('Write a 250-word story about a lighthouse. No tools.')
const m = await agent('Reply with exactly {MARKER} and nothing else. No tools.')
return {{marker: m.trim()}}
"""
PROMPT = (
    "Call the Workflow tool exactly once with the script below as the "
    "`script` parameter, verbatim. After calling it, say WAITING and end "
    "your turn. When the task notification later arrives, reply with "
    "exactly the returned marker value and nothing else. Do not call any "
    "other tool.\n\n" + WF_SCRIPT
)


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


HOME = Path(tempfile.mkdtemp(prefix="ba-wf-drain-live-"))
WORK = HOME / "work"
WORK.mkdir()
AUTH_DIR = HOME / "_auth"
AUTH_DIR.mkdir()
PORT = _free_port()
BASE = f"http://127.0.0.1:{PORT}"

username = f"ba-wf-e2e-{secrets.token_hex(4)}"
password = secrets.token_urlsafe(18)
plain = AUTH_DIR / "password.plain"
hash_file = AUTH_DIR / "password.hash"
secret_file = AUTH_DIR / "session.secret"
plain.write_text(password)
subprocess.run(
    [sys.executable, os.path.join(_REPO, "scripts", "hash-password.py"),
     "--password-file", str(plain), "--out", str(hash_file)],
    check=True, cwd=_REPO, capture_output=True,
)
plain.unlink()
secret_file.write_text(secrets.token_hex(32))

_SDK_DIR = os.path.join(_REPO, "sdk")
env = {
    **os.environ,
    "PYTHONPATH": (
        f"{_SDK_DIR}:{os.environ['PYTHONPATH']}"
        if os.environ.get("PYTHONPATH") else _SDK_DIR
    ),
    "BETTER_AGENT_HOME": str(HOME),
    "BETTER_CLAUDE_HOME": str(HOME),
    "BETTER_AGENT_TEST_MODE": "1",
    "BETTER_AGENT_HEADLESS_AUTH": "1",
    "BETTER_AGENT_USERNAME": username,
    "BETTER_AGENT_PASSWORD_HASH_FILE": str(hash_file),
    "BETTER_AGENT_SESSION_SECRET_FILE": str(secret_file),
    "BETTER_AGENT_BACKEND_PORT": str(PORT),
    "BETTER_AGENT_BACKEND_URL": BASE,
}

print(f"=== install profile (home={HOME})")
install = subprocess.run(
    [sys.executable, os.path.join(_REPO, "scripts", "install.py"),
     "--mode", "default", "--provider", "claude", "--yes", "--adopt"],
    cwd=_REPO, env=env, capture_output=True, text=True,
)
if install.returncode != 0:
    raise SystemExit(f"install.py failed:\n{install.stdout}\n{install.stderr}")

print(f"=== booting backend on :{PORT}")
backend_log = (HOME / "backend.log").open("ab")
backend = subprocess.Popen(
    [sys.executable,
     os.path.join(_REPO, "frontend", "tests", "fullstack", "harness",
                  "reserve_and_launch_backend.py"),
     "--host", "127.0.0.1", "--port", str(PORT)],
    cwd=_BACKEND, env=env, stdin=subprocess.DEVNULL,
    stdout=backend_log, stderr=backend_log, start_new_session=True,
)


def _cleanup() -> None:
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
    backend_log.close()
    # Frozen runtime bundles under runs/ carry read-only modes; restore
    # write permission or the rmtree leaves residue behind.
    subprocess.run(["chmod", "-R", "u+w", str(HOME)], capture_output=True)
    shutil.rmtree(HOME, ignore_errors=True)


def _fail(msg: str) -> None:
    tail = ""
    try:
        tail = (HOME / "backend.log").read_text(errors="replace")[-4000:]
    except OSError:
        pass
    _cleanup()
    raise SystemExit(f"FAIL: {msg}\n--- backend.log tail ---\n{tail}")


# Wait for the backend to serve.
deadline = time.monotonic() + 120
token = ""
while True:
    if backend.poll() is not None:
        _fail(f"backend exited rc={backend.returncode} before serving")
    try:
        mint = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.path.insert(0, sys.argv[1]); "
             "import auth; print(auth.create_token('wf-e2e'))",
             _BACKEND],
            env=env, capture_output=True, text=True, timeout=30,
        )
        token = mint.stdout.strip()
        if mint.returncode == 0 and token:
            _http("GET", f"{BASE}/api/sessions", token)
            break
    except Exception:
        pass
    if time.monotonic() > deadline:
        _fail("backend did not serve /api/sessions within 120s")
    time.sleep(1.0)
print("backend up, token minted")

session = _http("POST", f"{BASE}/api/sessions", token, {
    "name": "wf-drain-e2e",
    "cwd": str(WORK),
    "orchestration_mode": "native",
    "source": "cli",
})
sid = session["id"]
print(f"session {sid}")


async def _drive_turn() -> tuple[str, list[str]]:
    import websockets

    frames: list[str] = []
    final_text = ""
    async with websockets.connect(
        f"ws://127.0.0.1:{PORT}/ws/chat?token={token}", max_size=None,
    ) as ws:
        await ws.send(json.dumps({"type": "subscribe", "app_session_id": sid}))
        await ws.send(json.dumps({
            "type": "send_message",
            "app_session_id": sid,
            "prompt": PROMPT,
            "cwd": str(WORK),
            "model": MODEL,
            "client_id": str(uuid.uuid4()),
        }))
        deadline = time.monotonic() + 420
        while time.monotonic() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
            except asyncio.TimeoutError:
                continue
            frame = json.loads(raw)
            ftype = frame.get("type")
            frames.append(ftype or "?")
            if ftype in ("turn_complete", "turn_stopped", "error"):
                if ftype != "turn_complete":
                    raise AssertionError(f"turn ended abnormally: {frame}")
                data = frame.get("data") or {}
                final_text = json.dumps(data)
                return final_text, frames
        raise AssertionError("no turn_complete within 420s")


try:
    final_payload, frames = asyncio.run(_drive_turn())
    print(f"turn_complete received ({len(frames)} frames)")
    assert '"success": true' in final_payload or json.loads(final_payload).get(
        "success"
    ), f"turn_complete reports failure: {final_payload[:2000]}"

    # THE proof, part 1: the session's persisted render tree carries the
    # marker that only the workflow's notification turn can produce
    # (turn 1 ended with 'WAITING' — pre-fix the runner died there and
    # the marker never existed).
    tree = _http("GET", f"{BASE}/api/sessions/{sid}", token)
    tree_dump = json.dumps(tree)
    assert MARKER in tree_dump, (
        f"session render tree does not carry the workflow marker {MARKER}"
    )

    # Part 2: the runner actually entered the drain (the launching turn's
    # result landed while the workflow was live) and its durable
    # completion carries the marker.
    runs_root = HOME / "runs"
    run_dirs = sorted(runs_root.iterdir()) if runs_root.is_dir() else []
    assert run_dirs, f"no runs under {runs_root}"
    drain_seen = False
    marker_in_complete = False
    for rd in run_dirs:
        for logname in ("stdout.log", "stderr.log", "runner.log"):
            p = rd / logname
            if p.is_file() and "draining until idle" in p.read_text(errors="replace"):
                drain_seen = True
        c = rd / "complete.json"
        if c.is_file() and MARKER in (
            json.loads(c.read_text()).get("final_assistant_text") or ""
        ):
            marker_in_complete = True
    assert drain_seen, (
        "no runner entered the drain phase — the workflow either failed "
        "to launch or finished implausibly fast"
    )
    assert marker_in_complete, (
        "no run-level complete.json carries the workflow marker"
    )

    evidence = os.environ.get("WF_DRAIN_EVIDENCE_DIR")
    if evidence:
        Path(evidence).mkdir(parents=True, exist_ok=True)
        for rd in run_dirs:
            c = rd / "complete.json"
            if c.is_file():
                shutil.copy(c, Path(evidence) / f"{rd.name}-complete.json")
        (Path(evidence) / "session-tree.json").write_text(tree_dump)

    print("\n=== LIVE E2E PASS ===")
    print("  real backend + real session + real /ws/chat turn")
    print("  workflow launched in background; launching turn ended first")
    print("  runner drained until the notification turn completed")
    print(f"  turn_complete + complete.json carry workflow marker {MARKER}")
finally:
    _cleanup()
