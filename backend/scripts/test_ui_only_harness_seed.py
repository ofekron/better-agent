#!/usr/bin/env python3
"""Locks that `desktop-ui-only` / `mobile-desktop-ui-only` ship an empty
default harness (no bundled extensions/skills/MCPs), while `default` leaves
the bundled set untouched — see scripts/install.py's `seed_ui_only_harness`.

Runs against an isolated state home; never touches real Better Agent data.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "backend"))
sys.path.insert(0, str(REPO / "scripts"))

import paths  # noqa: E402

HOME = paths.engage_test_home(tempfile.mkdtemp(prefix="ba-ui-only-harness-"))

import bundled_extensions  # noqa: E402
import config_store  # noqa: E402
import installation_profile  # noqa: E402
from install import seed_ui_only_harness  # noqa: E402


def test_desktop_ui_only_disables_every_bundled_extension() -> None:
    config_store.set_disabled_builtin_extensions([])
    seed_ui_only_harness(installation_profile.DESKTOP_UI_ONLY)
    disabled = set(config_store.get_disabled_builtin_extensions())
    expected = set(bundled_extensions.PUBLIC_EXTENSION_PATHS)
    if disabled != expected:
        raise AssertionError(f"expected all bundled extensions disabled, got {disabled!r}")


def test_mobile_desktop_ui_only_disables_every_bundled_extension() -> None:
    config_store.set_disabled_builtin_extensions([])
    seed_ui_only_harness(installation_profile.MOBILE_DESKTOP_UI_ONLY)
    disabled = set(config_store.get_disabled_builtin_extensions())
    expected = set(bundled_extensions.PUBLIC_EXTENSION_PATHS)
    if disabled != expected:
        raise AssertionError(f"expected all bundled extensions disabled, got {disabled!r}")


def test_default_mode_leaves_bundled_extensions_untouched() -> None:
    config_store.set_disabled_builtin_extensions([])
    seed_ui_only_harness(installation_profile.DEFAULT)
    disabled = config_store.get_disabled_builtin_extensions()
    if disabled != []:
        raise AssertionError(f"default mode must not disable anything, got {disabled!r}")


if __name__ == "__main__":
    test_desktop_ui_only_disables_every_bundled_extension()
    test_mobile_desktop_ui_only_disables_every_bundled_extension()
    test_default_mode_leaves_bundled_extensions_untouched()
    print("ui-only-harness-seed tests passed")
