"""Locks that ``runner`` is importable before any execution path runs.

A standalone ``import runner`` exercising module top-level side effects
(import-order regressions, missing optional deps) without driving a turn.
"""

import _test_home

_test_home.isolate("bc-test-runner-import-")


def test_runner_imports_before_executing() -> None:
    import runner  # noqa: F401
