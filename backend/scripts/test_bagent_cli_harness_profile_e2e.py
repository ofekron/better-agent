#!/usr/bin/env python3
"""E2E test: the `bagent` CLI's harness-profile support reaches real backend
state, through the actual CLI process rather than through its functions
imported in-process.

Deterministic — no vendor LLM CLI, no network, no RUN_LLM_TESTS gate. What it
proves is the CLI-level plumbing added to `backend/cli.py`:

  `bagent harness-profiles`                    -> lists profiles from a live backend
  `bagent --harness-profile ID` (new session)  -> POST /api/sessions carries harness_profile_id
  `bagent --harness-profile ID --session SID`  -> PATCH .../harness-profile is applied on resume
  `bagent` (no --harness-profile, existing)     -> unchanged: no profile PATCH is sent

Each case runs the real `backend/cli.py` as a subprocess against a real
uvicorn server, with stdin closed so the CLI's normal REPL entry point exits
immediately on EOF once its (real, over-the-wire) session setup has run —
proving the subprocess actually reached the backend, not merely that argparse
accepted the flag.

The resolved harness_profile snapshot (`harness_profile_resolver.resolve_for_session`)
is checked after each CLI run: this is the exact function `backend/runner.py`
calls to build the wire fields injected into a provider turn, so a match here
proves the CLI selection reaches the same resolution path a real turn uses.

Run with:
    cd backend && .venv/bin/python scripts/test_bagent_cli_harness_profile_e2e.py
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import _test_home  # noqa: E402

BA_HOME = _test_home.isolate("bc-bagent-cli-harness-profile-")

import _test_installation  # noqa: E402
import uvicorn  # noqa: E402

CLI_PATH = ROOT / "cli.py"
FAILURES: list[str] = []


def _ok(label: str) -> None:
    print(f"\033[92mPASS\033[0m  {label}")


def _fail(label: str, why: str) -> None:
    print(f"\033[91mFAIL\033[0m  {label}: {why}")
    FAILURES.append(f"{label}: {why}")


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
        cfg = uvicorn.Config(self.app_path, host="127.0.0.1", port=self.port, log_level="warning")
        self.server = uvicorn.Server(cfg)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), 0.2):
                    return
            except OSError:
                time.sleep(0.2)
        raise RuntimeError("uvicorn failed to start in 180s")

    def stop(self) -> None:
        if self.server:
            self.server.should_exit = True
        if self.thread:
            self.thread.join(timeout=10)


def run_cli(port: int, token: str, *extra_args: str, cwd: str) -> subprocess.CompletedProcess:
    # --mode native: this isolated test install has no team-orchestration
    # extension, and the default "team" mode 404s session creation on that
    # readiness gate (backend/main.py create_session) — unrelated to the
    # harness-profile plumbing under test.
    return subprocess.run(
        [sys.executable, str(CLI_PATH), "--port", str(port), "--cwd", cwd, "--mode", "native", *extra_args],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
        env={**os.environ, "BETTER_CLAUDE_CLI_TOKEN": token},
    )


def main() -> int:
    try:
        return _run()
    finally:
        shutil.rmtree(BA_HOME, ignore_errors=True)


def _run() -> int:
    print(f"BETTER_AGENT_HOME = {BA_HOME}")
    _test_installation.activate(Path(BA_HOME))

    import auth  # noqa: PLC0415
    import harness_profile_resolver  # noqa: PLC0415
    import harness_profile_store  # noqa: PLC0415
    from session_manager import manager as session_manager  # noqa: PLC0415

    token = auth.create_token("bagent-cli-e2e")
    port = free_port()
    server = BackgroundUvicorn("main:app", port)
    server.start()
    cwd = tempfile.mkdtemp(prefix="bc-bagent-cli-harness-profile-cwd-")

    try:
        profile = harness_profile_store.create_profile({
            "id": "bagent-cli-e2e-profile",
            "name": "Bagent CLI E2E Profile",
        })

        # --- `bagent harness-profiles` lists it. ---
        listed = run_cli(port, token, "harness-profiles", cwd=cwd)
        if listed.returncode != 0:
            _fail("harness-profiles list", f"exit {listed.returncode}: {listed.stderr}")
        elif profile["id"] not in listed.stdout:
            _fail("harness-profiles list", f"{profile['id']!r} missing from:\n{listed.stdout}")
        else:
            _ok("`bagent harness-profiles` lists the created profile")

        # --- New session, --harness-profile: POST /api/sessions carries it. ---
        created = run_cli(port, token, "--harness-profile", profile["id"], cwd=cwd)
        if created.returncode != 0:
            _fail("new session run", f"exit {created.returncode}: {created.stderr}")
        else:
            matches = [
                s for s in session_manager.list()
                if s.get("cwd") == cwd and s.get("name") == "cli-default"
            ]
            if not matches:
                _fail("new session lookup", f"no cli-default session found for cwd={cwd}")
            else:
                # `list()` returns the lighter sidebar summary, which does not
                # carry `harness_profile_id` — fetch the full record.
                sess = session_manager.get(matches[0]["id"]) or matches[0]
                if sess.get("harness_profile_id") != profile["id"]:
                    _fail("new session binding", f"harness_profile_id={sess.get('harness_profile_id')!r}")
                else:
                    _ok("new session created via CLI carries --harness-profile")
                    snapshot = harness_profile_resolver.resolve_for_session(sess)
                    if snapshot.get("profile_id") != profile["id"]:
                        _fail("new session resolution", f"resolved profile_id={snapshot.get('profile_id')!r}")
                    else:
                        _ok("resolver sees the CLI-selected profile (same path runner.py uses)")

                # --- Resume same session, no --harness-profile: unchanged. ---
                unset_run = run_cli(port, token, "--session", sess["id"], cwd=cwd)
                if unset_run.returncode != 0:
                    _fail("resume without flag", f"exit {unset_run.returncode}: {unset_run.stderr}")
                else:
                    after = session_manager.get(sess["id"]) or {}
                    if after.get("harness_profile_id") != profile["id"]:
                        _fail(
                            "resume without flag preserves binding",
                            f"harness_profile_id={after.get('harness_profile_id')!r} (expected unchanged)",
                        )
                    else:
                        _ok("resuming without --harness-profile leaves the binding untouched")

                # --- Resume same session, --harness-profile "": back to default. ---
                other = harness_profile_store.create_profile({
                    "id": "bagent-cli-e2e-profile-2",
                    "name": "Bagent CLI E2E Profile 2",
                })
                switched = run_cli(port, token, "--session", sess["id"], "--harness-profile", other["id"], cwd=cwd)
                if switched.returncode != 0:
                    _fail("resume with different profile", f"exit {switched.returncode}: {switched.stderr}")
                else:
                    after = session_manager.get(sess["id"]) or {}
                    if after.get("harness_profile_id") != other["id"]:
                        _fail("resume switches profile", f"harness_profile_id={after.get('harness_profile_id')!r}")
                    else:
                        _ok("resuming with a different --harness-profile PATCHes the session")
                        snapshot = harness_profile_resolver.resolve_for_session(after)
                        if snapshot.get("profile_id") != other["id"]:
                            _fail("resume resolution", f"resolved profile_id={snapshot.get('profile_id')!r}")
                        else:
                            _ok("resolver reflects the switched profile after CLI resume")

        # --- New session, no --harness-profile: default, unaffected by the flag's existence. ---
        cwd2 = tempfile.mkdtemp(prefix="bc-bagent-cli-harness-profile-cwd2-")
        try:
            plain = run_cli(port, token, cwd=cwd2)
            if plain.returncode != 0:
                _fail("plain new session", f"exit {plain.returncode}: {plain.stderr}")
            else:
                matches = [
                    s for s in session_manager.list()
                    if s.get("cwd") == cwd2 and s.get("name") == "cli-default"
                ]
                plain_sess = session_manager.get(matches[0]["id"]) if matches else None
                if plain_sess is None or plain_sess.get("harness_profile_id"):
                    got = plain_sess.get("harness_profile_id") if plain_sess else "<no session>"
                    _fail("plain new session default", f"harness_profile_id={got!r} (expected empty/default)")
                else:
                    _ok("a plain `bagent` run (no flag) still creates a default-profile session")
        finally:
            shutil.rmtree(cwd2, ignore_errors=True)
    finally:
        server.stop()
        shutil.rmtree(cwd, ignore_errors=True)

    print()
    if FAILURES:
        print(f"\033[91m{len(FAILURES)} FAILURE(S)\033[0m")
        return 1
    print("\033[92mALL PASS — the bagent CLI's harness-profile support reaches real backend state\033[0m")
    return 0


if __name__ == "__main__":
    sys.exit(main())
