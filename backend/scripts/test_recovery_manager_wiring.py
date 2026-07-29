"""Locks how main.py wires the recovery thread and bus pinning.

main.py cannot be imported without the full server dependency set, so
these assertions read the source. They are structural guards, not
behavioral tests — the behavior itself is covered by
test_recovery_manager.py and test_event_bus_cross_thread.py.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_MAIN = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")


def test_subscribers_are_pinned_before_recovery_starts() -> None:
    pin = _MAIN.index("event_bus.bind_unpinned_to_current_loop()")
    start = _MAIN.index("recovery_manager.manager.start()")
    begin = _MAIN.index("startup_recovery_gate.begin_recovery()")
    # Pin first: an unpinned subscriber would run on the recovery loop
    # instead of main once recovery starts publishing.
    assert pin < start, "subscribers must be pinned before the recovery thread starts"
    assert start < begin, "recovery thread must be up before recovery is dispatched"


def test_default_subscribers_registered_before_pinning() -> None:
    register = _MAIN.index("register_default_subscribers()")
    pin = _MAIN.index("event_bus.bind_unpinned_to_current_loop()")
    assert register < pin, "pinning must happen after the default subscribers exist"


def test_scan_phase_goes_through_the_recovery_manager() -> None:
    assert "recovery_manager.manager.run(factory)" in _MAIN
    # Both scan call sites — the live pass and the background pass.
    assert _MAIN.count("await _scan_recovered_runs(") >= 1
    assert _MAIN.count("_scan_recovered_runs(") >= 3  # def + 2 call sites


def test_integration_still_runs_on_the_main_loop() -> None:
    # Integration reaches loop-bound state (per-session prompt queue,
    # turn_manager cancel events, the reattach queue). It must NOT be
    # routed through the recovery manager.
    integrate = _MAIN.index("await integrate_recovered_runs(coordinator, batch)")
    window = _MAIN[integrate - 400:integrate]
    assert "recovery_manager" not in window


def test_recovery_manager_is_stopped_on_shutdown() -> None:
    assert "asyncio.to_thread(recovery_manager.manager.stop)" in _MAIN


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("PASS")
