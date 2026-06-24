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
    release_backend_instance_lock,
)


def _child_attempt() -> subprocess.CompletedProcess[str]:
    code = """
import os, sys
sys.path.insert(0, os.environ["BA_BACKEND_PATH"])
from backend_instance_lock import acquire_backend_instance_lock, release_backend_instance_lock
try:
    acquire_backend_instance_lock()
except RuntimeError as exc:
    print(str(exc))
    raise SystemExit(7)
else:
    release_backend_instance_lock()
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

        blocked = _child_attempt()
        assert blocked.returncode == 7, blocked
        assert "already using" in blocked.stdout, blocked.stdout

        release_backend_instance_lock()
        acquired = _child_attempt()
        assert acquired.returncode == 0, acquired

        lock_path = Path(_TMP_HOME) / "backend.lock"
        assert lock_path.exists()
        print("PASS backend instance lock excludes same-home second process")
        return 0
    finally:
        release_backend_instance_lock()
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
