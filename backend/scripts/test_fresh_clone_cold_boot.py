"""Regression: a fresh clone boots through the REAL ASGI lifespan.

Issue #10: on a fresh clone (no `frontend/dist`, run.sh supervisor present)
`_arm_cold_build_restart` registered its watcher through a legacy Starlette
router event hook. That API no longer exists, so importing `main` aborted the
process and the backend never became serviceable. `test_cold_build_watcher_
lifespan.py` locks the registry SHAPE; this test locks that the app actually
BOOTS: it drives a real `lifespan.startup` / `lifespan.shutdown` cycle over the
ASGI protocol (Starlette's `TestClient` context manager is the real lifespan
runner uvicorn uses) and then asserts the app serves traffic — the cold-build
placeholder HTML on a non-API path and a real API route underneath it.

The only mocked thing is `_trigger_supervisor_restart`, so the watcher's
success path cannot SIGTERM the test process.

Run with:
    cd backend && .venv/bin/python scripts/test_fresh_clone_cold_boot.py
"""
import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import paths  # noqa: E402

_home = tempfile.mkdtemp(prefix="ba-fresh-clone-cold-boot-")
paths.engage_test_home(_home)
os.environ["BETTER_CLAUDE_API_ONLY"] = "1"

import _test_installation  # noqa: E402

_test_installation.activate(Path(_home), provider="claude")

import main  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

_RESTART_WAIT_SECONDS = 30.0
_scratch_dirs: list[str] = []


def _engage_supervisor_env() -> None:
    # get_env resolves BETTER_AGENT_ before BETTER_CLAUDE_, so set both names to
    # pin the gate regardless of the ambient shell env.
    for name in ("BETTER_AGENT_RUN_SH_SUPERVISOR", "BETTER_CLAUDE_RUN_SH_SUPERVISOR"):
        os.environ[name] = "1"


def run() -> None:
    _engage_supervisor_env()

    # A fresh clone: frontend/dist does not exist yet. Point the resolver at an
    # absent dist so `mount_frontend(app)` takes the exact same cold branch the
    # module-level import call takes on a real fresh clone.
    dist_root = tempfile.mkdtemp(prefix="ba-fresh-clone-dist-")
    _scratch_dirs.append(dist_root)
    dist_dir = Path(dist_root) / "dist"
    dist_index = dist_dir / "index.html"
    assert not dist_dir.exists()
    main.frontend_dist_dir = lambda: dist_dir

    # The watcher's restart request is the one real side effect we replace: the
    # real call SIGTERMs this process. A threading.Event lets the sync test body
    # wait on the actual completion instead of guessing a sleep.
    restart_requested = threading.Event()
    restart_calls: list[str] = []

    async def _fake_restart(request_id: str) -> None:
        restart_calls.append(request_id)
        restart_requested.set()

    main._trigger_supervisor_restart = _fake_restart

    main._deferred_startup_tasks.clear()
    main._cold_build_watcher_armed = False

    main.mount_frontend(main.app)
    assert len(main._deferred_startup_tasks) == 1, "cold clone must arm the watcher"

    # Real ASGI lifespan: entering the context sends `lifespan.startup` and
    # waits for `lifespan.startup.complete`; a boot failure raises here.
    with TestClient(main.app) as client:
        # Serviceable: an API route answers, and is NOT shadowed by the
        # catch-all placeholder registered after it.
        health = client.get("/api/auth/needs_setup")
        assert health.status_code == 200, (health.status_code, health.text)
        assert "needs_setup" in health.json(), health.text

        # Serviceable: the cold-build placeholder is served for app paths.
        root = client.get("/")
        assert root.status_code == 200, root.status_code
        assert "Building frontend" in root.text, root.text[:200]
        assert "no-store" in root.headers.get("cache-control", "")
        deep = client.get("/s/some-session-id")
        assert deep.status_code == 200 and "Building frontend" in deep.text

        # The armed watcher really ran inside the lifespan: landing the dist
        # makes it request exactly one supervisor restart.
        assert not restart_requested.is_set(), "no restart before the build lands"
        dist_dir.mkdir(parents=True, exist_ok=True)
        dist_index.write_text("<!doctype html>built", encoding="utf-8")
        assert restart_requested.wait(_RESTART_WAIT_SECONDS), (
            "cold-build watcher never requested a supervisor restart"
        )

    # Exiting the context sent `lifespan.shutdown` and waited for its ack.
    assert restart_calls == [""], (
        "watcher must request one restart with an empty request id "
        f"(no rebuild on the way back up), got {restart_calls!r}"
    )

    print("test_fresh_clone_cold_boot: OK")


if __name__ == "__main__":
    try:
        run()
    finally:
        shutil.rmtree(_home, ignore_errors=True)
        for _scratch in _scratch_dirs:
            shutil.rmtree(_scratch, ignore_errors=True)
