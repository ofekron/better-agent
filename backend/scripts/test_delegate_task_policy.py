"""delegate_task_policy — the global bc setting that controls how the
delegate_task tool routes a delegated task. get/set/normalize/persist."""

from __future__ import annotations

import importlib
import os
import sys
import tempfile
from pathlib import Path

import _test_home
_test_home.isolate("bc-test-dt-policy-")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config_store


def test_default_is_auto():
    assert config_store.get_delegate_task_policy() == "auto"


def test_set_and_read_all_four_policies():
    for p in ("auto", "manual", "always_new", "always_new_approve"):
        assert config_store.set_delegate_task_policy(p) == p
        assert config_store.get_delegate_task_policy() == p


def test_invalid_normalizes_to_auto():
    assert config_store.set_delegate_task_policy("nonsense") == "auto"
    assert config_store.set_delegate_task_policy("") == "auto"


def test_persists_across_reload():
    config_store.set_delegate_task_policy("manual")
    importlib.reload(config_store)
    assert config_store.get_delegate_task_policy() == "manual"
