"""Test desktop/app_main.py — frozen-app role dispatch.

Run with:
    backend/.venv/bin/python desktop/test_app_main.py
"""

from __future__ import annotations

import io
import sys
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_BACKEND = _HERE.parent / "backend"
for _p in (_HERE, _BACKEND):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import app_main
from app_main import _role
from deep_link import redact_argv

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def test_role_dispatch() -> None:
    cases = {
        (): "shell",
        ("--serve",): "backend",
        ("--serve-node",): "backend",
        ("--run-dir", "/runs/x"): "backend",
        ("--run-dir", "/runs/x", "--runner-kind", "agy"): "backend",
        ("--frozen-artifact-smoke", "--output", "/tmp/result"): "backend",
    }
    for argv, expected in cases.items():
        got = _role(list(argv))
        assert got == expected, f"{argv}: expected {expected!r}, got {got!r}"


def test_diagnostic_argv_redacts_pair_intent() -> None:
    argv = [
        "Better Agent",
        "betteragent://marketplace/pair?v=1&intent=secret-pair-token",
    ]
    redacted = redact_argv(argv)
    assert "secret-pair-token" not in " ".join(redacted)


def test_diagnostic_watchdog_has_an_exit_owner() -> None:
    import inspect
    source = inspect.getsource(app_main._run_main)
    assert "finally:" in source
    assert "_stop_diagnostics()" in source


def test_run_main_reports_unexpected_failure_before_diagnostics_close() -> None:
    events: list[str] = []

    class OrderedLog(io.StringIO):
        def write(self, value: str) -> int:
            events.append("write")
            return super().write(value)

    log = OrderedLog()
    with (
        mock.patch.object(
            app_main,
            "main",
            side_effect=RuntimeError("frozen backend failure"),
        ),
        mock.patch.object(app_main, "_fh", log),
        mock.patch.object(
            app_main,
            "_stop_diagnostics",
            side_effect=lambda: events.append("stop"),
        ),
    ):
        result = app_main._run_main()
        recorded = log.getvalue()
    assert result == 1
    assert "RuntimeError: frozen backend failure" in recorded
    assert events[-1] == "stop"


def test_run_main_does_not_consume_process_control_exceptions() -> None:
    stopped = False

    def stop() -> None:
        nonlocal stopped
        stopped = True

    with (
        mock.patch.object(
            app_main,
            "main",
            side_effect=KeyboardInterrupt(),
        ),
        mock.patch.object(app_main, "_stop_diagnostics", side_effect=stop),
    ):
        try:
            app_main._run_main()
        except KeyboardInterrupt:
            pass
        else:
            raise AssertionError("KeyboardInterrupt was consumed")
    assert stopped


TESTS = [
    ("app_main._role classifies shell vs backend invocations",
     test_role_dispatch),
    ("app_main diagnostics redact marketplace pair intents",
     test_diagnostic_argv_redacts_pair_intent),
    ("app_main always stops its diagnostic watchdog",
     test_diagnostic_watchdog_has_an_exit_owner),
    ("app_main records unexpected failures before closing diagnostics",
     test_run_main_reports_unexpected_failure_before_diagnostics_close),
    ("app_main preserves process-control exceptions",
     test_run_main_does_not_consume_process_control_exceptions),
]


def main_run() -> int:
    failed = 0
    for name, fn in TESTS:
        try:
            fn()
            ok = True
        except Exception as e:
            ok = False
            import traceback
            traceback.print_exc()
            print(f"  exception: {e}")
        print(f"{PASS if ok else FAIL}  {name}")
        if not ok:
            failed += 1
    print()
    print(f"{failed} of {len(TESTS)} test(s) FAILED" if failed
          else f"all {len(TESTS)} tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main_run())
