"""Monitoring-state decision logic (Phase 3).

`Coordinator.monitoring_state(sid)` is a pure derivation (no stored field,
mirrors is_running). This locks the precedence:

    stopped  <  idle  <  waiting_on_background  <  active  <  blocked_on_user

i.e. user action wins over execution; active wins over background; background
wins over idle; stopped when no live run.
"""

import os
import sys
import tempfile

import _test_home
_test_home.isolate("bc_montest_")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator import Coordinator  # noqa: E402
import containment as containment_mod  # noqa: E402
from stores import pending_approvals  # noqa: E402

SID = "sess-mon-test"
MYPID = os.getpid()
failures = []


class _StubContainment:
    bg = False

    def has_background_work(self, run_id, pid):
        return _StubContainment.bg


def _coord():
    c = Coordinator.__new__(Coordinator)
    c._run_state = {}
    c.active_run_ids = {}
    c.cancel_events = {}
    return c


def _check(got, want):
    ok = got == want
    print(("  PASS" if ok else "  FAIL") + f": want {want!r}, got {got!r}")
    if not ok:
        failures.append((want, got))


def main():
    containment_mod._INSTANCE = _StubContainment()
    _pending = []
    pending_approvals.list_pending = lambda **k: list(_pending)

    c = _coord()

    print("T1 no live run -> stopped")
    _check(c.monitoring_state(SID), "stopped")

    print("T2 live run, no turn/approval/bg -> idle")
    c._run_state = {SID: [{"run_id": "r1", "pid": MYPID}]}
    _StubContainment.bg = False
    _pending.clear()
    _check(c.monitoring_state(SID), "idle")

    print("T3 live run + background work -> waiting_on_background")
    _StubContainment.bg = True
    _check(c.monitoring_state(SID), "waiting_on_background")

    print("T4 live run + pending approval beats background -> blocked_on_user")
    _pending.append({"app_session_id": SID})
    _check(c.monitoring_state(SID), "blocked_on_user")

    print("T5 pending approval remains visible during active work")
    c.cancel_events[SID] = object()
    _check(c.monitoring_state(SID), "blocked_on_user")

    print("T5b active turn beats background when no approval is pending")
    _pending.clear()
    c.active_run_ids[SID] = ["r1"]
    _check(c.monitoring_state(SID), "active")

    print("T6 dead pid -> stopped (no live run)")
    c.cancel_events.clear()
    _pending.clear()
    _StubContainment.bg = False
    c._run_state = {SID: [{"run_id": "r1", "pid": 2147480000}]}  # almost-certainly-dead
    _check(c.monitoring_state(SID), "stopped")

    print(f"\n{'PASS' if not failures else 'FAIL'}: {len(failures)} failed checks")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
