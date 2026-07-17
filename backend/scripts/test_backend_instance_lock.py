from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-backend-lock-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from backend_instance_lock import (  # noqa: E402
    acquire_backend_instance_lock,
    acquire_bff_instance_lock,
    release_backend_instance_lock,
    release_bff_instance_lock,
)


def _child_attempt(component: str) -> subprocess.CompletedProcess[str]:
    code = f"""
import os, sys
sys.path.insert(0, os.environ["BA_BACKEND_PATH"])
from backend_instance_lock import acquire_{component}_instance_lock, release_{component}_instance_lock
try:
    acquire_{component}_instance_lock()
except RuntimeError as exc:
    print(str(exc))
    raise SystemExit(7)
else:
    release_{component}_instance_lock()
"""
    env = os.environ.copy()
    env["BETTER_CLAUDE_HOME"] = _TMP_HOME
    env["BA_BACKEND_PATH"] = _BACKEND
    return subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def main() -> int:
    try:
        acquire_backend_instance_lock()
        acquire_backend_instance_lock()

        blocked = _child_attempt("backend")
        assert blocked.returncode == 7, blocked
        assert "already using" in blocked.stdout, blocked.stdout

        # The BFF and the runtime legitimately run concurrently against
        # the same home — its lock must be independent, not blocked by
        # the runtime holding "backend.lock".
        bff_while_backend_held = _child_attempt("bff")
        assert bff_while_backend_held.returncode == 0, bff_while_backend_held

        release_backend_instance_lock()
        acquired = _child_attempt("backend")
        assert acquired.returncode == 0, acquired

        lock_path = Path(_TMP_HOME) / "backend.lock"
        assert lock_path.exists()

        acquire_bff_instance_lock()
        acquire_bff_instance_lock()
        bff_blocked = _child_attempt("bff")
        assert bff_blocked.returncode == 7, bff_blocked
        assert "already using" in bff_blocked.stdout, bff_blocked.stdout
        # A second BFF is excluded independently of the runtime lock, which
        # this process never took in this branch.
        backend_while_bff_held = _child_attempt("backend")
        assert backend_while_bff_held.returncode == 0, backend_while_bff_held
        release_bff_instance_lock()

        bff_lock_path = Path(_TMP_HOME) / "bff.lock"
        assert bff_lock_path.exists()

        print("PASS backend/bff instance locks exclude same-home second process, independently of each other")
        return 0
    finally:
        release_backend_instance_lock()
        release_bff_instance_lock()
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
