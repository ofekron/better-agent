from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import _test_home

TMP_HOME = _test_home.isolate("bc-test-user-prefs-cache-")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import user_prefs  # noqa: E402


def test_repeated_getters_share_cached_file_read() -> None:
    user_prefs.set_session_sort("last_opened_at")
    original_read_json = user_prefs.read_json
    calls = 0

    def counted_read_json(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original_read_json(*args, **kwargs)

    user_prefs._PREFS_CACHE = None  # type: ignore[attr-defined]
    user_prefs.read_json = counted_read_json  # type: ignore[assignment]
    try:
        assert user_prefs.get_session_sort() == "last_opened_at"
        assert user_prefs.get_session_status_sort() is False
        assert user_prefs.get_folder_view_enabled() is True
    finally:
        user_prefs.read_json = original_read_json  # type: ignore[assignment]
    assert calls == 1, f"expected one prefs file read, got {calls}"


def test_external_file_change_invalidates_cache() -> None:
    user_prefs.set_session_sort("updated_at")
    assert user_prefs.get_session_sort() == "updated_at"
    path = Path(TMP_HOME) / "user_prefs.json"
    path.write_text(json.dumps({"session_sort": "last_user_prompt_at"}), encoding="utf-8")
    os.utime(path, None)
    assert user_prefs.get_session_sort() == "last_user_prompt_at"


def test_get_all_loads_preferences_once() -> None:
    user_prefs.set_session_sort("last_opened_at")
    user_prefs.set_font_size(16)
    original_read_json = user_prefs.read_json
    calls = 0

    def counted_read_json(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original_read_json(*args, **kwargs)

    user_prefs._PREFS_CACHE = None  # type: ignore[attr-defined]
    user_prefs.read_json = counted_read_json  # type: ignore[assignment]
    try:
        prefs = user_prefs.get_all()
    finally:
        user_prefs.read_json = original_read_json  # type: ignore[assignment]
    assert calls == 1, f"expected one prefs file read, got {calls}"
    assert prefs["session_sort"] == "last_opened_at"
    assert prefs["font_size"] == 16
    assert prefs["folder_view_enabled"] is True


if __name__ == "__main__":
    try:
        test_repeated_getters_share_cached_file_read()
        test_external_file_change_invalidates_cache()
        test_get_all_loads_preferences_once()
        print("PASS test_user_prefs_cache")
    finally:
        import shutil

        shutil.rmtree(TMP_HOME, ignore_errors=True)
