"""Regression test: restart idle-wait probes must see pending/active cold-batch
recovery integration as busy.

RCA: `recover_all_in_flight` marks `startup_recovery_gate` done as soon as
cold (completed/stale) recovered runs are *enqueued* for the
`_recovered_cold_run_worker` background task — not once that task has
finished integrating them. `_has_restart_blocking_agent_work` had no
signal for that in-flight background integration at all, so a
multi-minute cold batch (observed: 8 runs / 281s) read as idle for its
entire duration, allowing an idle-mode manual restart mid-integration.

Run with:
    cd backend && .venv/bin/python scripts/test_restart_busy_cold_recovery.py
"""

from __future__ import annotations

import atexit
import os
import shutil
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home

_TMP_HOME = _test_home.isolate("ba-test-restart-busy-cold-recovery-")
atexit.register(lambda: shutil.rmtree(_TMP_HOME, ignore_errors=True))

import main  # noqa: E402
import recovery  # noqa: E402
import ops_api  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"

_results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, ok, detail))


def _reset_cold_recovery_state() -> None:
    recovery._RECOVERED_COLD_PENDING.clear()
    recovery._RECOVERED_COLD_ACTIVE.clear()
    recovery._RECOVERED_COLD_FAILED.clear()
    recovery._RECOVERED_COLD_RETRY_REQUESTED.clear()


def main_() -> int:
    _reset_cold_recovery_state()

    # 1. Baseline: no cold-recovery work pending/active -> not blocking.
    check(
        "no cold recovery -> not pending",
        recovery._cold_recovery_integration_pending() is False,
        "expected False with empty pending/active sets",
    )

    # 2. A pending (not yet started) cold batch counts as busy.
    recovery._RECOVERED_COLD_PENDING["sid-1"] = [{"run_id": "r1"}]
    check(
        "pending cold batch -> integration pending",
        recovery._cold_recovery_integration_pending() is True,
        "expected True with a non-empty pending dict",
    )
    check(
        "pending cold batch -> restart-blocking work",
        ops_api._has_restart_blocking_agent_work() is True,
        "expected True: cold recovery must block a restart-cadence idle read",
    )
    _reset_cold_recovery_state()

    # 3. An in-flight (actively integrating) cold batch counts as busy too,
    #    even once it's been popped out of the pending dict.
    recovery._RECOVERED_COLD_ACTIVE.add("sid-2")
    check(
        "active cold batch -> integration pending",
        recovery._cold_recovery_integration_pending() is True,
        "expected True with a non-empty active set",
    )
    check(
        "active cold batch -> restart-blocking work",
        ops_api._has_restart_blocking_agent_work() is True,
        "expected True: an in-flight cold integration must block idle too",
    )
    _reset_cold_recovery_state()

    # 4. A deliberately parked failed batch is retained for demand but
    #    does not keep restart cadence permanently busy.
    recovery._RECOVERED_COLD_PENDING["sid-parked"] = [{"run_id": "r-parked"}]
    recovery._RECOVERED_COLD_FAILED.add("sid-parked")
    check(
        "parked failed batch -> not integration pending",
        recovery._cold_recovery_integration_pending() is False,
        "expected False while retained work is waiting for real demand",
    )
    check(
        "parked failed batch -> not restart-blocking",
        ops_api._has_restart_blocking_agent_work() is False,
        "expected False: a quarantined failure must not prevent restart",
    )
    recovery._RECOVERED_COLD_FAILED.discard("sid-parked")
    check(
        "retry-ready failed batch -> integration pending",
        recovery._cold_recovery_integration_pending() is True,
        "expected True after demand makes the retained batch runnable",
    )
    _reset_cold_recovery_state()

    # 5. Draining back to empty clears the busy signal.
    check(
        "drained cold recovery -> not pending",
        recovery._cold_recovery_integration_pending() is False,
        "expected False after clearing pending/active",
    )

    all_ok = True
    for name, ok, detail in _results:
        status = PASS if ok else FAIL
        suffix = f" ({detail})" if detail and not ok else ""
        print(f"{status}  {name}{suffix}")
        all_ok = all_ok and ok
    return 0 if all_ok else 1


def test_restart_busy_cold_recovery() -> None:
    _results.clear()
    assert main_() == 0


if __name__ == "__main__":
    raise SystemExit(main_())
