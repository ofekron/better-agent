"""Lock the pytest_ignore_collect hook that keeps standalone scripts out of
the pytest unit suite.

`backend/scripts` holds ~340 standalone assertion scripts named `test_*.py`
that define no `def test_*`. pytest would otherwise IMPORT them during
collection to run their top-level code; the unguarded ones crash the whole
session ("mainloop: caught unexpected SystemExit"). The conftest hook skips
any `test_*.py` that defines no collectable test function or `Test*` class.

This test pins both directions: standalone-only files are ignored, real
test modules always pass through.
"""
import importlib.util
from pathlib import Path

import pytest


def _load_conftest_module():
    """Load scripts/conftest.py without triggering its module-body side effects."""
    path = Path(__file__).resolve().parent / "conftest.py"
    spec = importlib.util.spec_from_file_location("_conftest_under_test", path)
    module = importlib.util.module_from_spec(spec)
    # Execute it: it engages _test_home, which is fine (isolated home).
    spec.loader.exec_module(module)
    return module


class _Path:
    """Minimal stand-in matching the attributes pytest_ignore_collect reads."""

    def __init__(self, name: str, body: str):
        self.name = name
        self._body = body

    def read_text(self, encoding="utf-8"):
        return self._body


def _hook(path):
    return _load_conftest_module().pytest_ignore_collect(path, config=None)


def test_standalone_only_script_is_ignored():
    # No def test_* / Test class → standalone script → must be ignored.
    body = "import sys\nprint('side effect')\nsys.exit(0)\n"
    assert _hook(_Path("test_standalone_thing.py", body)) is True


def test_real_test_module_is_not_ignored():
    body = "def test_real():\n    assert True\n"
    assert _hook(_Path("test_real_thing.py", body)) is None


def test_class_based_test_module_is_not_ignored():
    body = "class TestThing:\n    def helper(self):\n        return 1\n"
    assert _hook(_Path("test_class_thing.py", body)) is None


def test_non_test_filename_is_not_ignored():
    body = "x = 1\n"
    assert _hook(_Path("helper.py", body)) is None


def test_unparseable_file_is_not_silently_ignored():
    # A syntax-broken file must surface as a real pytest error, not be hidden.
    assert _hook(_Path("test_broken.py", "def (\n")) is None
