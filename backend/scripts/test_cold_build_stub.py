"""Cold-clone serve-then-build.

With no built frontend dist/, mount_frontend must not crash uvicorn at import.
It serves a 'Building frontend…' placeholder while run.sh builds in the
background, and arms a one-shot supervisor restart that swaps in the real dist
when it lands. An explicitly requested dist_dir that does not exist still fails
loudly (callers that name a specific dist rely on that).

Also locks daemonhost.pointer.is-switching: run.sh uses it to tell a cold clone
(serve-then-build) from a line switch (synchronous build, for revert safety).
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import paths

_home = tempfile.mkdtemp(prefix="ba-cold-build-test-")
paths.engage_test_home(_home)
os.environ["BETTER_CLAUDE_API_ONLY"] = "1"

import main  # noqa: E402
import daemonhost.pointer as pointer  # noqa: E402
from daemonhost.jsonio import write_json  # noqa: E402
from daemonhost.paths import pointer_path  # noqa: E402

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def run() -> None:
    # An explicitly requested dist that is absent keeps the strict contract —
    # callers naming a specific dist must hear about its absence, not get a stub.
    missing = Path(tempfile.mkdtemp()) / "missing"
    try:
        main.mount_frontend(FastAPI(), dist_dir=missing)
        raise AssertionError("explicit missing dist_dir must raise RuntimeError")
    except RuntimeError:
        pass

    # Stub path: a non-API GET serves the placeholder, never crashes.
    app = FastAPI()
    main._mount_cold_build_stub(app)
    client = TestClient(app)
    r = client.get("/")
    assert r.status_code == 200, r.status_code
    assert "Building frontend" in r.text
    assert "no-store" in r.headers.get("cache-control", ""), "placeholder must be no-cache"
    r = client.get("/s/any-session-id")
    assert r.status_code == 200 and "Building frontend" in r.text

    # Without the run.sh supervisor env, arming the restart is a no-op: no
    # handler is registered and nothing throws. (Under the supervisor the
    # watcher would SIGTERM this process, so it is not exercised live here.)
    os.environ.pop("BETTER_CLAUDE_RUN_SH_SUPERVISOR", None)
    main._arm_cold_build_restart(app, Path(tempfile.mkdtemp()) / "dist" / "index.html")

    # is-switching reflects the pointer status and its CLI mirrors that.
    write_json(pointer_path(), {"status": "switching", "active": "/x"})
    assert pointer.is_switching() is True
    assert _cli_is_switching() == 0
    write_json(pointer_path(), {"status": "active", "active": "/x"})
    assert pointer.is_switching() is False
    assert _cli_is_switching() == 1

    print("test_cold_build_stub: OK")


def _cli_is_switching() -> int:
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parent.parent.parent)}
    return subprocess.run(
        [sys.executable, "-m", "daemonhost.pointer", "is-switching"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode


if __name__ == "__main__":
    run()
