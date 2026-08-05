#!/usr/bin/env python3
"""Locks that UI-only modes seed an empty bundled harness during the prepared
runtime activation transaction, while default mode leaves user choices intact.

Runs against an isolated state home; never touches real Better Agent data.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "backend"))
sys.path.insert(0, str(REPO / "scripts"))

from _test_home import TestHome as _TestHome  # noqa: E402

_TEST_HOME = _TestHome.acquire("ba-ui-only-harness-")
HOME = Path(_TEST_HOME.path)

import bundled_extensions  # noqa: E402
import config_store  # noqa: E402
import installation_profile  # noqa: E402
import _test_installation  # noqa: E402


def _seed(mode: str) -> None:
    _test_installation.activate(HOME, mode=mode, provider="codex")
    config_store.apply_installation_profile_selection(seed_ui_only_harness=True)


def test_desktop_ui_only_disables_every_bundled_extension() -> None:
    config_store.set_disabled_builtin_extensions([])
    _seed(installation_profile.DESKTOP_UI_ONLY)
    disabled = set(config_store.get_disabled_builtin_extensions())
    expected = set(bundled_extensions.PUBLIC_EXTENSION_PATHS)
    if disabled != expected:
        raise AssertionError(f"expected all bundled extensions disabled, got {disabled!r}")


def test_mobile_desktop_ui_only_disables_every_bundled_extension() -> None:
    config_store.set_disabled_builtin_extensions([])
    _seed(installation_profile.MOBILE_DESKTOP_UI_ONLY)
    disabled = set(config_store.get_disabled_builtin_extensions())
    expected = set(bundled_extensions.PUBLIC_EXTENSION_PATHS)
    if disabled != expected:
        raise AssertionError(f"expected all bundled extensions disabled, got {disabled!r}")


def test_default_mode_leaves_bundled_extensions_untouched() -> None:
    config_store.set_disabled_builtin_extensions([])
    _seed(installation_profile.DEFAULT)
    disabled = config_store.get_disabled_builtin_extensions()
    if disabled != []:
        raise AssertionError(f"default mode must not disable anything, got {disabled!r}")


def test_boot_recommit_preserves_user_extension_choices() -> None:
    _seed(installation_profile.DESKTOP_UI_ONLY)
    user_choice = [sorted(bundled_extensions.PUBLIC_EXTENSION_PATHS)[0]]
    config_store.set_disabled_builtin_extensions(user_choice)
    config_store.apply_installation_profile_selection()
    disabled = config_store.get_disabled_builtin_extensions()
    if disabled != user_choice:
        raise AssertionError(f"boot recommit changed user choices: {disabled!r}")


if __name__ == "__main__":
    test_desktop_ui_only_disables_every_bundled_extension()
    test_mobile_desktop_ui_only_disables_every_bundled_extension()
    test_default_mode_leaves_bundled_extensions_untouched()
    test_boot_recommit_preserves_user_extension_choices()
    print("ui-only-harness-seed tests passed")
