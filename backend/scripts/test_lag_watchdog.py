"""Lag-watchdog dumps the main-thread traceback when the loop heartbeat goes
stale. This is the mechanism that finally makes the recurring multi-second
event-loop lags attributable: the monitor coroutine can only run (and dump)
once the loop is free — i.e. AFTER a synchronous blocker has returned — so a
separate watchdog thread is needed to capture the blocker mid-flight.

Run with:
    cd backend && .venv/bin/python scripts/test_lag_watchdog.py
"""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
import time

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-lag-wd-")
atexit.register(lambda: shutil.rmtree(_TMP_HOME, ignore_errors=True))

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import main  # noqa: E402
import paths  # noqa: E402


def test_watchdog_dumps_when_heartbeat_stale() -> None:
    # Simulate a loop that has been blocked in sync code for 5s: the
    # heartbeat the monitor would normally stamp each cycle is stale.
    main._LAG_HEARTBEAT[0] = time.monotonic() - 5.0
    main._LAG_LAST_DUMP[0] = 0.0

    dump_path = paths.ba_home() / "logs" / "backend-faulthandler.log"
    assert not dump_path.exists()

    main._start_lag_watchdog(threshold=0.2, cooldown=0.0)

    # Watchdog polls every 0.5s; give it a few cycles to notice + dump.
    for _ in range(20):
        if dump_path.exists():
            break
        time.sleep(0.2)

    assert dump_path.exists(), "watchdog did not write a dump for a stale heartbeat"
    content = dump_path.read_text(encoding="utf-8")
    assert "event loop blocked" in content
    # A real traceback follows the header (faulthandler frames), not just
    # the one-line header.
    assert len(content.splitlines()) > 3, content


if __name__ == "__main__":
    test_watchdog_dumps_when_heartbeat_stale()
    print("PASS: lag watchdog dumps on stale heartbeat")
