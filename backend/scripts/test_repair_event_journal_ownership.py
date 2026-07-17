from __future__ import annotations

import os
import shutil
import subprocess
import sys

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-repair-ownership-lock-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from backend_instance_lock import (  # noqa: E402
    acquire_backend_instance_lock,
    release_backend_instance_lock,
)


def _run_repair(session_id: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["BETTER_CLAUDE_HOME"] = _TMP_HOME
    return subprocess.run(
        [sys.executable, os.path.join(_HERE, "repair_event_journal_ownership.py"), session_id],
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )


def main() -> int:
    try:
        # A live backend (this process holding the instance lock) must
        # block the repair script from ever touching the same home's
        # journal, instead of relying on the docstring warning alone.
        acquire_backend_instance_lock()
        blocked = _run_repair("nonexistent-session-blocked")
        assert blocked.returncode != 0, blocked
        assert "already using" in blocked.stderr, blocked.stderr
        release_backend_instance_lock()

        # With no backend running, the repair script must still acquire
        # and then release the lock around its own work so a real
        # backend can start again immediately afterward.
        clean = _run_repair("nonexistent-session-clean")
        assert clean.returncode == 0, clean
        assert "resolved=0" in clean.stdout, clean.stdout

        acquire_backend_instance_lock()  # would raise if repair leaked the lock
        release_backend_instance_lock()

        print("PASS repair_event_journal_ownership takes the backend instance lock")
        return 0
    finally:
        release_backend_instance_lock()
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
