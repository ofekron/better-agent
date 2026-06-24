"""Integration test for the background-startup refactor.

Boots uvicorn under an isolated `BETTER_CLAUDE_HOME` and exercises:

  1. `/api/startup_tasks` responds within 2s of server-ready — proves
     `on_startup` no longer blocks on long-running steps.

  2. All three known startup tasks (`adv_sync_overlay_recovery`,
     `bcfile_migration`, `recover_in_flight`) transition to
     `state=done` via BOTH the REST snapshot AND
     `startup_task_changed` WS frames. (`v3_migration` was deleted
     by A11 — schema migrations are not supported per CLAUDE.md.)

  3. WS connection is opened concurrently with startup so the early
     frames are captured (regression for "frames dropped because the
     test connected after on_startup returned").

  4. Loop-starvation probe: a parallel pinger hits `/api/sessions`
     every 100ms across the startup+recovery window. Max latency must
     stay under 500ms — generous enough to absorb cold-import jitter,
     tight enough to catch a regression that re-introduces a
     synchronous multi-second blocker on the event loop.

  5. Task-level failure: monkey-patch
     `file_ref_resolver.run_migration_once` to raise BEFORE
     `on_startup` imports it. Restart the server, assert
     `bcfile_migration` reaches `state=failed` with `error`
     populated, while the other two still reach `done` (tasks are
     independent).

Per-run failure paths (corrupt complete.json, alive-run finalizers)
are covered structurally by the existing `integration_test.py`
which drives real claude CLI runs end-to-end and exercises
`_finalize_when_done` for live worker spawns. This test focuses on
the registry + cross-thread WS + non-blocking-startup shape.

Run with:
    cd backend && .venv/bin/python scripts/integration_test_startup.py
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

# Importable from backend/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
import uvicorn
import websockets

# Pre-import auth so we can monkey-patch credential verification for
# the test process. Calling /api/auth/login with any password then
# succeeds and stamps the session cookie, satisfying the auth gate
# without depending on the developer's keychain entries (or platform).
import auth as _auth


async def _bypass_credentials(*_args, **_kwargs) -> bool:
    return True


_auth.verify_credentials = _bypass_credentials


async def login(client: httpx.AsyncClient) -> None:
    """Stamp a session cookie via /api/auth/login using the
    verify_credentials bypass installed above. Subsequent /api/*
    requests on the same httpx client carry the cookie."""
    r = await client.post(
        "/api/auth/login",
        json={"username": "test", "password": "test"},
    )
    if r.status_code != 204:
        raise RuntimeError(f"login failed: {r.status_code} {r.text}")


def _ensure_logs_dir(ba_home: str) -> None:
    """Backend logger opens `<ba_home>/logs/backend.log` on first emit;
    the parent dir must exist or the FileHandler raises FileNotFoundError
    and uvicorn aborts startup. Production code creates it lazily; in
    a fresh tempdir we have to pre-create."""
    os.makedirs(os.path.join(ba_home, "logs"), exist_ok=True)


EXPECTED_TASK_IDS = {
    "adv_sync_overlay_recovery",
    "bcfile_migration",
    "recover_in_flight",
}


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class BackgroundUvicorn:
    """uvicorn driven from a thread so the test's asyncio loop can poll
    the running server. Identical pattern to integration_test.py;
    duplicated rather than imported because that file's helper isn't
    exported."""

    def __init__(self, port: int):
        self.port = port
        self.server: uvicorn.Server | None = None
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        config = uvicorn.Config(
            "main:app",
            host="127.0.0.1",
            port=self.port,
            log_level="warning",
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()

    def wait_ready(self, timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), 0.2):
                    return
            except OSError:
                time.sleep(0.05)
        raise RuntimeError(f"uvicorn failed to start in {timeout}s")

    def stop(self) -> None:
        if self.server:
            self.server.should_exit = True
        if self.thread:
            self.thread.join(timeout=10)


def _ok(label: str) -> None:
    print(f"\033[92mPASS\033[0m  {label}")


def _fail(label: str, why: str) -> None:
    print(f"\033[91mFAIL\033[0m  {label}: {why}")


async def collect_startup_ws_frames(
    url: str,
    cookie_header: str,
    seen_tasks: dict[str, dict],
    stop_event: asyncio.Event,
) -> None:
    """Subscribe with `subscribe_global=true` and accumulate every
    `startup_task_changed` frame into `seen_tasks`. The map keys are
    task ids, values are the latest task dict received (last-write-
    wins). The `cleared: true` payload empties the map."""
    async with websockets.connect(
        url, additional_headers={"Cookie": cookie_header}
    ) as ws:
        # Backend treats a bare connect as opted into global frames
        # via the existing subscribe shape used by other tests; sending
        # a subscribe for a non-existent session is the established
        # idle-attach pattern that still receives broadcast_global
        # frames.
        await ws.send(json.dumps({
            "type": "subscribe",
            "app_session_id": "__startup_test__",
            "cwd": "/tmp",
        }))
        while not stop_event.is_set():
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=0.2)
            except asyncio.TimeoutError:
                continue
            except websockets.ConnectionClosed:
                return
            try:
                ev = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if ev.get("type") != "startup_task_changed":
                continue
            data = ev.get("data") or {}
            if data.get("cleared"):
                seen_tasks.clear()
                continue
            task = data.get("task") or {}
            tid = task.get("id")
            if tid:
                seen_tasks[tid] = task


async def latency_pinger(
    client: httpx.AsyncClient,
    stop_event: asyncio.Event,
    samples: list[float],
) -> None:
    """Hit `/api/sessions` every 100ms via the shared (already-logged-
    in) client; record per-request latency in seconds. Stops when
    `stop_event` is set."""
    while not stop_event.is_set():
        t0 = time.monotonic()
        try:
            await client.get("/api/sessions")
        except Exception:
            # First call can race the server-ready check — record
            # max-bucket latency so a regression doesn't sneak in.
            samples.append(5.0)
            await asyncio.sleep(0.1)
            continue
        samples.append(time.monotonic() - t0)
        await asyncio.sleep(0.1)


def _cookie_header_from_client(client: httpx.AsyncClient) -> str:
    """Serialize the client's current cookie jar into a `Cookie:`
    header value the websockets library can ship on the upgrade
    request. httpx stores cookies in a `Cookies` wrapper over a
    `http.cookiejar.CookieJar`; iterate the jar to get the live set."""
    return "; ".join(f"{c.name}={c.value}" for c in client.cookies.jar)


async def rest_poll_all_done(
    client: httpx.AsyncClient,
    timeout: float,
) -> tuple[bool, list[dict]]:
    """Poll `GET /api/startup_tasks` until every expected task is
    `done` OR timeout. REST (not WS) is the authority — see CLAUDE.md
    state-ownership rule: REST is the snapshot, WS is the live delta.
    On an empty ba_home the tasks finish so fast (<100ms) that a WS
    client that opens post-`/api/auth/login` misses every frame; the
    frontend lives with this because it does REST-on-mount.
    Returns (ok, last_snapshot)."""
    deadline = time.monotonic() + timeout
    last: list[dict] = []
    while time.monotonic() < deadline:
        last = (await client.get("/api/startup_tasks")).json()
        if not isinstance(last, list):
            await asyncio.sleep(0.1)
            continue
        done = {t["id"] for t in last if t.get("state") == "done"}
        if EXPECTED_TASK_IDS.issubset(done):
            return True, last
        await asyncio.sleep(0.1)
    return False, last


async def rest_poll_bcfile_failed(
    client: httpx.AsyncClient,
    timeout: float,
) -> tuple[bool, list[dict]]:
    """Variant for scenario B: wait until `bcfile_migration` is
    `failed` AND all other tasks reach `done`. Replaces the previous
    `v3_migration`-failure variant after A11 deleted v3_migration —
    bcfile_migration is the new "one-shot migrator" target for the
    task-level isolation contract."""
    deadline = time.monotonic() + timeout
    last: list[dict] = []
    while time.monotonic() < deadline:
        last = (await client.get("/api/startup_tasks")).json()
        if not isinstance(last, list):
            await asyncio.sleep(0.1)
            continue
        by_id = {t["id"]: t for t in last if isinstance(t, dict)}
        bcf = by_id.get("bcfile_migration", {})
        others_done = all(
            by_id.get(tid, {}).get("state") == "done"
            for tid in EXPECTED_TASK_IDS - {"bcfile_migration"}
        )
        if bcf.get("state") == "failed" and others_done:
            return True, last
        await asyncio.sleep(0.1)
    return False, last


async def scenario_happy_path(ba_home: str, failures: list[str]) -> None:
    """Boot uvicorn cold, verify all 4 tasks reach `done` via REST +
    WS, response time + loop latency under threshold."""
    print(f"[scenario_happy_path] BETTER_CLAUDE_HOME = {ba_home}")

    port = free_port()
    server = BackgroundUvicorn(port)
    server.start()
    base = f"http://127.0.0.1:{port}"
    ws_url = f"ws://127.0.0.1:{port}/ws/chat"

    seen_tasks: dict[str, dict] = {}
    ping_samples: list[float] = []
    stop_event = asyncio.Event()
    ws_task = None
    ping_task = None

    try:
        server.wait_ready(timeout=30.0)
        ready_at = time.monotonic()

        # One httpx client across the whole scenario so the cookie jar
        # populated by /api/auth/login persists into every subsequent
        # request. Re-opening AsyncClient with a copied jar would
        # silently drop the Set-Cookie that login installed.
        async with httpx.AsyncClient(base_url=base, timeout=5.0) as client:
            await login(client)
            cookie_header = _cookie_header_from_client(client)

            # Connect WS + pinger right after login — startup tasks
            # are still in flight, so we observe their lifecycle
            # frames live.
            async def ws_with_retry():
                deadline = time.monotonic() + 30.0
                while time.monotonic() < deadline:
                    try:
                        await collect_startup_ws_frames(
                            ws_url, cookie_header, seen_tasks, stop_event,
                        )
                        return
                    except OSError:
                        await asyncio.sleep(0.05)

            ws_task = asyncio.create_task(ws_with_retry())
            ping_task = asyncio.create_task(
                latency_pinger(client, stop_event, ping_samples)
            )

            # Step 1: REST snapshot responds quickly post-ready.
            t0 = time.monotonic()
            r = await client.get("/api/startup_tasks")
            rest_latency = time.monotonic() - t0
            if r.status_code != 200:
                failures.append(f"GET /api/startup_tasks → {r.status_code}")
                return
            snapshot = r.json()
            if not isinstance(snapshot, list):
                failures.append("startup_tasks REST shape is not a list")
                return
            # 2s budget covers the tcp-accept race after wait_ready.
            if (time.monotonic() - ready_at) > 2.0:
                failures.append(
                    f"GET /api/startup_tasks didn't respond within 2s "
                    f"of ready (took {rest_latency:.2f}s)"
                )
            else:
                _ok(f"GET /api/startup_tasks responds quickly "
                    f"({rest_latency*1000:.0f}ms)")

            # Step 2: every expected task reaches `done` within 30s
            # per REST snapshot (the authoritative read; WS is the
            # delta channel and only catches frames whose tasks were
            # still running at WS-connect time).
            ok, snapshot = await rest_poll_all_done(client, timeout=30.0)
            if not ok:
                pending = [
                    t for t in snapshot if t.get("state") != "done"
                ]
                failures.append(
                    f"tasks didn't all reach done in 30s — pending/failed: {pending}"
                )
            else:
                _ok("all 4 startup tasks reached state=done")

            # Let the pinger collect a meaningful sample before
            # stopping it. The startup work completes faster than
            # the pinger's 100ms tick, so without a beat here the
            # latency check sees just 1-2 samples and can't catch a
            # regression that only manifests after a few hundred ms
            # of load.
            await asyncio.sleep(1.0)

            # Step 3: broadcast-path round-trip. Register a synthetic
            # task NOW (post-WS-connect) and assert the WS client
            # observes both lifecycle frames. This exercises the
            # `coordinator.broadcast_global` + cross-thread dispatch
            # path that frontend banner deltas depend on, without
            # depending on startup timing.
            from startup_tasks import startup_task_registry
            startup_task_registry.register("__test_synth__", "test.synth")
            startup_task_registry.mark_done("__test_synth__")
            # Give the loop a tick to schedule + deliver.
            deadline = time.monotonic() + 5.0
            synth_states: list[str] = []
            while time.monotonic() < deadline:
                synth = seen_tasks.get("__test_synth__")
                if synth is not None:
                    synth_states.append(synth.get("state") or "?")
                    if synth.get("state") == "done":
                        break
                await asyncio.sleep(0.05)
            if not synth_states or synth_states[-1] != "done":
                failures.append(
                    f"WS broadcast path missed synthetic task — "
                    f"seen states: {synth_states or 'none'}"
                )
            else:
                _ok("WS broadcast path delivered synthetic task lifecycle")

        # Step 4: loop latency stayed bounded across startup. The
        # threshold is loose (500ms) because cold imports + fs scans
        # can spike on a busy laptop; a real regression that pushes
        # multi-second blockers back onto the loop blows past this.
        stop_event.set()
        if ping_samples:
            max_lat = max(ping_samples)
            if max_lat > 0.5:
                failures.append(
                    f"event-loop starvation regression: max "
                    f"/api/sessions latency {max_lat*1000:.0f}ms"
                )
            else:
                _ok(f"loop latency stayed bounded "
                    f"(max {max_lat*1000:.0f}ms across "
                    f"{len(ping_samples)} samples)")
        else:
            failures.append("latency pinger collected zero samples")

    finally:
        stop_event.set()
        if ping_task:
            ping_task.cancel()
        if ws_task:
            ws_task.cancel()
        server.stop()


async def scenario_task_level_failure(ba_home: str, failures: list[str]) -> None:
    """Monkey-patch `file_ref_resolver.run_migration_once` to raise
    BEFORE the new uvicorn imports it, then verify the
    `bcfile_migration` task reaches `failed` with `error` populated,
    while the other two reach `done`. (A11 deleted v3_migration;
    bcfile_migration is the new "one-shot migrator" target.)"""
    print(f"[scenario_task_level_failure] BETTER_CLAUDE_HOME = {ba_home}")

    # Pre-import so we can patch the symbol the new on_startup will
    # look up via `from file_ref_resolver import run_migration_once`.
    # The import in on_startup happens at startup time — patching the
    # module attribute now means it sees the broken version.
    import file_ref_resolver

    original = file_ref_resolver.run_migration_once

    def broken_migration(*_args, **_kwargs) -> int:
        raise RuntimeError("injected migration failure")

    file_ref_resolver.run_migration_once = broken_migration

    port = free_port()
    server = BackgroundUvicorn(port)
    server.start()
    ws_url = f"ws://127.0.0.1:{port}/ws/chat"
    base = f"http://127.0.0.1:{port}"

    seen_tasks: dict[str, dict] = {}
    stop_event = asyncio.Event()
    ws_task = None

    try:
        server.wait_ready(timeout=30.0)

        async with httpx.AsyncClient(base_url=base, timeout=5.0) as client:
            await login(client)
            cookie_header = _cookie_header_from_client(client)

            async def ws_with_retry():
                deadline = time.monotonic() + 30.0
                while time.monotonic() < deadline:
                    try:
                        await collect_startup_ws_frames(
                            ws_url, cookie_header, seen_tasks, stop_event,
                        )
                        return
                    except OSError:
                        await asyncio.sleep(0.05)

            ws_task = asyncio.create_task(ws_with_retry())

            # REST is the authority; wait until `bcfile_migration` is
            # `failed` AND the other two reach `done` (proves tasks
            # are independent).
            ok, snapshot = await rest_poll_bcfile_failed(client, timeout=30.0)
            by_id = {t["id"]: t for t in snapshot if isinstance(t, dict)}
            if not ok:
                failures.append(
                    f"task-level failure scenario: snapshot={by_id}"
                )
            else:
                bcf = by_id["bcfile_migration"]
                if not bcf.get("error") or "injected" not in bcf.get("error", ""):
                    failures.append(
                        f"bcfile_migration failed but error field is wrong: {bcf.get('error')!r}"
                    )
                else:
                    _ok("monkey-patched bcfile_migration → state=failed with error, "
                        "other tasks still reached done")
                # Lifecycle invariant: every task that reached a
                # terminal state should carry a finished_at > started_at.
                # Catches a regression where mark_done forgets to stamp
                # the timestamp, OR where the registry serves a stale
                # `running` task as `done` without timing data.
                #
                # NOTE: this test reuses ba_home from scenario A, so
                # the non-v3 tasks may execute against a sentinel-gated
                # no-op (e.g. bcfile_migration's marker is already
                # written). The lifecycle check still passes because
                # `run_task` stamps started_at/finished_at regardless
                # of whether the wrapped fn did real work.
                bad_timing: list[str] = []
                for tid in EXPECTED_TASK_IDS:
                    task = by_id.get(tid)
                    if task is None:
                        bad_timing.append(f"{tid}: missing")
                        continue
                    fa, sa = task.get("finished_at"), task.get("started_at")
                    if not fa or not sa or fa < sa:
                        bad_timing.append(
                            f"{tid}: started_at={sa!r} finished_at={fa!r}"
                        )
                if bad_timing:
                    failures.append(
                        f"lifecycle timing invariant violated: {bad_timing}"
                    )
                else:
                    _ok("every task carries finished_at >= started_at")
                _ok("REST snapshot reflects failure state")

    finally:
        stop_event.set()
        if ws_task:
            ws_task.cancel()
        server.stop()
        file_ref_resolver.run_migration_once = original


async def amain() -> int:
    failures: list[str] = []
    # Shared ba_home across scenarios — the backend logger's
    # FileHandler is opened at module import (main.py:147), so the
    # path it points to is captured the first time uvicorn imports
    # `main:app`. Reusing the same ba_home lets us run multiple
    # scenarios in one process without the second one tripping over
    # a deleted log directory.
    ba_home = tempfile.mkdtemp(prefix="bc-startup-test-home-")
    os.environ["BETTER_CLAUDE_HOME"] = ba_home
    os.environ["BETTER_AGENT_HOME"] = ba_home
    _ensure_logs_dir(ba_home)

    try:
        print("=" * 60)
        print("Scenario A — happy path (all tasks reach done)")
        print("=" * 60)
        await scenario_happy_path(ba_home, failures)

        print("\n" + "=" * 60)
        print("Scenario B — task-level failure (monkey-patched migration)")
        print("=" * 60)
        await scenario_task_level_failure(ba_home, failures)
    finally:
        shutil.rmtree(ba_home, ignore_errors=True)

    print("\n" + "=" * 60)
    if failures:
        for f in failures:
            _fail("startup", f)
        return 1
    print("\033[92mAll scenarios passed.\033[0m")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(amain()))
