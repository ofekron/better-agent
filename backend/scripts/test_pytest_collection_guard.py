"""Locks `should_ignore_test_module` against silently dropping real tests.

The guard exists to skip standalone runner scripts that are named `test_*.py`
but hold no pytest tests. Its failure mode is invisible: a module it wrongly
ignores reports no failures because it never runs at all.
"""

from __future__ import annotations

from pytest_collection_guard import should_ignore_test_module


def _module(tmp_path, body: str, name: str = "test_sample.py"):
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def test_async_only_pytest_module_is_collected(tmp_path):
    # Regression: async tests parse as ast.AsyncFunctionDef, a sibling of
    # FunctionDef rather than a subclass. Matching only FunctionDef silently
    # ignored every module whose tests are all `async def` — including
    # test_tailer_routes_through_apply_event.py, which CLAUDE.md names as a
    # convergence-invariant lock that must never be disabled.
    path = _module(
        tmp_path,
        "import pytest\n\n"
        "pytestmark = pytest.mark.anyio\n\n"
        "async def test_thing():\n    assert True\n",
    )
    assert should_ignore_test_module(path, None) is None


def test_async_only_standalone_script_is_ignored(tmp_path):
    # No pytest import means no async marker, so pytest could only report
    # every coroutine as an unsupported-async failure. These modules drive
    # themselves under a `__main__` guard instead.
    path = _module(
        tmp_path,
        "import asyncio\nimport sys\n\n"
        "async def test_thing():\n    return True\n\n"
        "async def main_runner():\n    return 0 if await test_thing() else 1\n\n"
        'if __name__ == "__main__":\n    sys.exit(asyncio.run(main_runner()))\n',
    )
    assert should_ignore_test_module(path, None) is True


def test_async_module_using_from_import_of_pytest_is_collected(tmp_path):
    path = _module(
        tmp_path,
        "from pytest import mark\n\n"
        "pytestmark = mark.anyio\n\n"
        "async def test_thing():\n    assert True\n",
    )
    assert should_ignore_test_module(path, None) is None


def test_sync_module_is_collected(tmp_path):
    path = _module(tmp_path, "def test_thing():\n    assert True\n")
    assert should_ignore_test_module(path, None) is None


def test_class_based_module_is_collected(tmp_path):
    path = _module(tmp_path, "class TestThing:\n    def test_x(self):\n        assert True\n")
    assert should_ignore_test_module(path, None) is None


def test_module_without_tests_is_ignored(tmp_path):
    path = _module(tmp_path, "def helper():\n    return 1\n")
    assert should_ignore_test_module(path, None) is True


def test_unguarded_exit_runner_is_ignored(tmp_path):
    # The guard's runner heuristic keys on an exit call outside a
    # `__main__` guard, which is what standalone scripts use to signal a
    # pass/fail result.
    path = _module(
        tmp_path,
        "import sys\n\n"
        "def test_thing():\n    assert True\n\n"
        "sys.exit(0)\n",
    )
    assert should_ignore_test_module(path, None) is True


def test_exit_under_main_guard_is_still_collected(tmp_path):
    path = _module(
        tmp_path,
        "import sys\n\n"
        "def test_thing():\n    assert True\n\n"
        'if __name__ == "__main__":\n    sys.exit(0)\n',
    )
    assert should_ignore_test_module(path, None) is None


def test_non_test_filename_is_not_considered(tmp_path):
    path = _module(tmp_path, "def helper():\n    return 1\n", name="helpers.py")
    assert should_ignore_test_module(path, None) is None
