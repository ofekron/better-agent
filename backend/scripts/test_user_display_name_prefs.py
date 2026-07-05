from __future__ import annotations

import shutil
import sys
from pathlib import Path

import _test_home

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TMP_HOME = _test_home.isolate("bc-test-user-display-name-")

import user_prefs  # noqa: E402


def test_login_username_is_default_display_name() -> None:
    assert user_prefs.get_all("ofek")["user_display_name"] == "ofek"


def test_custom_display_name_overrides_login_username() -> None:
    assert user_prefs.set_user_display_name("  Ofek   Ron  ") == "Ofek Ron"
    assert user_prefs.get_all("ofek")["user_display_name"] == "Ofek Ron"


def test_blank_display_name_returns_to_login_username() -> None:
    user_prefs.set_user_display_name("Ofek Ron")
    assert user_prefs.set_user_display_name("   ") is None
    assert user_prefs.get_all("ofek")["user_display_name"] == "ofek"


def test_invalid_display_name_rejected() -> None:
    for value in (True, 7, "x" * (user_prefs.MAX_USER_DISPLAY_NAME_LENGTH + 1)):
        try:
            user_prefs.set_user_display_name(value)
        except ValueError:
            continue
        raise AssertionError(f"invalid display name accepted: {value!r}")


if __name__ == "__main__":
    try:
        test_login_username_is_default_display_name()
        test_custom_display_name_overrides_login_username()
        test_blank_display_name_returns_to_login_username()
        test_invalid_display_name_rejected()
        print("PASS test_user_display_name_prefs")
    finally:
        shutil.rmtree(TMP_HOME, ignore_errors=True)
