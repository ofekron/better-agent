"""Comprehensive unit owner for `user_prefs`.

Exercises every preference accessor/mutator, the private coercion helpers, the
load/save cache fingerprinting (including the OSError fallbacks), and `get_all`
projection. Every test runs against an isolated BETTER_AGENT_HOME; an autouse
fixture rebinds the module-level `_PREFS_PATH` to the active home so the suite
is hermetic regardless of import order when run alongside other files that also
import `user_prefs`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import _test_home

TMP_HOME = _test_home.isolate("bc-test-user-prefs-")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import paths  # noqa: E402
import user_prefs  # noqa: E402


@pytest.fixture(autouse=True)
def _hermetic_prefs():
    """Pin the prefs file to the active isolated home and start each test with
    a cold cache and no on-disk file. `_PREFS_PATH` is bound once at module
    import; rebinding it here keeps this file correct when a multi-file pytest
    session has already imported `user_prefs` against a different home. The
    original binding is restored on teardown so the rebound path never leaks
    into subsequently-run files."""
    _test_home.engage(TMP_HOME)
    original_path = user_prefs._PREFS_PATH
    user_prefs._PREFS_PATH = paths.bc_home() / "user_prefs.json"
    user_prefs._PREFS_CACHE = None
    prefs_file = user_prefs._prefs_path()
    if prefs_file.exists():
        prefs_file.unlink()
    yield
    user_prefs._PREFS_CACHE = None
    user_prefs._PREFS_PATH = original_path


def _write_raw_prefs(payload: dict) -> None:
    """Bypass the cache: hand-write a prefs file and bump mtime so the next
    `_load` sees a fresh fingerprint."""
    path = user_prefs._prefs_path()
    path.write_text(json.dumps(payload), encoding="utf-8")
    import os

    os.utime(path, None)


# --------------------------------------------------------------------------- #
# Private coercion helpers
# --------------------------------------------------------------------------- #
def test_bool_pref_returns_value_for_bool() -> None:
    assert user_prefs._bool_pref({"k": True}, "k", False) is True


def test_bool_pref_returns_default_for_non_bool() -> None:
    assert user_prefs._bool_pref({"k": "yes"}, "k", False) is False
    assert user_prefs._bool_pref({"k": 1}, "k", False) is False


def test_bool_pref_returns_default_when_missing() -> None:
    assert user_prefs._bool_pref({}, "k", True) is True


def test_choice_pref_returns_value_in_choices() -> None:
    assert user_prefs._choice_pref({"k": "b"}, "k", "a", ("a", "b")) == "b"


def test_choice_pref_returns_default_when_not_in_choices() -> None:
    assert user_prefs._choice_pref({"k": "z"}, "k", "a", ("a", "b")) == "a"


def test_choice_pref_returns_default_when_missing() -> None:
    assert user_prefs._choice_pref({}, "k", "a", ("a", "b")) == "a"


def test_bounded_int_pref_returns_valid_value() -> None:
    assert user_prefs._bounded_int_pref({"k": 5}, "k", 1, 1, 10) == 5


def test_bounded_int_pref_rejects_bool() -> None:
    assert user_prefs._bounded_int_pref({"k": True}, "k", 1, 1, 10) == 1


def test_bounded_int_pref_rejects_non_int() -> None:
    assert user_prefs._bounded_int_pref({"k": "5"}, "k", 1, 1, 10) == 1


def test_bounded_int_pref_clamps_below_minimum_to_default() -> None:
    assert user_prefs._bounded_int_pref({"k": 0}, "k", 5, 1, 10) == 5


def test_bounded_int_pref_clamps_above_maximum_to_default() -> None:
    assert user_prefs._bounded_int_pref({"k": 11}, "k", 5, 1, 10) == 5


def test_bounded_int_pref_returns_default_when_missing() -> None:
    assert user_prefs._bounded_int_pref({}, "k", 7, 1, 10) == 7


def test_optional_positive_int_pref_none_is_honored() -> None:
    assert user_prefs._optional_positive_int_pref({"k": None}, "k", None) is None


def test_optional_positive_int_pref_default_none_when_missing() -> None:
    assert user_prefs._optional_positive_int_pref({}, "k", None) is None


def test_optional_positive_int_pref_returns_valid_value() -> None:
    assert user_prefs._optional_positive_int_pref({"k": 3}, "k", None) == 3


def test_optional_positive_int_pref_rejects_bool() -> None:
    assert user_prefs._optional_positive_int_pref({"k": True}, "k", None) is None


def test_optional_positive_int_pref_rejects_non_int() -> None:
    assert user_prefs._optional_positive_int_pref({"k": "3"}, "k", None) is None


def test_optional_positive_int_pref_rejects_non_positive() -> None:
    assert user_prefs._optional_positive_int_pref({"k": 0}, "k", None) is None


def test_clean_user_display_name_none_passes_through() -> None:
    assert user_prefs._clean_user_display_name(None) is None


def test_clean_user_display_name_normalizes_whitespace() -> None:
    assert user_prefs._clean_user_display_name("  Ofek   Ron  ") == "Ofek Ron"


def test_clean_user_display_name_empty_becomes_none() -> None:
    assert user_prefs._clean_user_display_name("   ") is None
    assert user_prefs._clean_user_display_name("") is None


def test_clean_user_display_name_rejects_non_string() -> None:
    for bad in (True, 7, 1.5, ["a"]):
        with pytest.raises(ValueError):
            user_prefs._clean_user_display_name(bad)


def test_clean_user_display_name_rejects_too_long() -> None:
    with pytest.raises(ValueError):
        user_prefs._clean_user_display_name("x" * (user_prefs.MAX_USER_DISPLAY_NAME_LENGTH + 1))


def test_display_name_pref_prefers_stored() -> None:
    assert user_prefs._display_name_pref({"user_display_name": "Stored"}, "login") == "Stored"


def test_display_name_pref_falls_back_to_login_username() -> None:
    assert user_prefs._display_name_pref({}, "  ofek  ") == "ofek"
    assert user_prefs._display_name_pref({}, "ofek") == "ofek"


def test_display_name_pref_defaults_to_none_without_login() -> None:
    assert user_prefs._display_name_pref({}) is None
    assert user_prefs._display_name_pref({}, "   ") is None


# --------------------------------------------------------------------------- #
# Load / save cache fingerprinting
# --------------------------------------------------------------------------- #
def test_load_on_missing_file_returns_empty_and_caches() -> None:
    # path.stat() raises OSError on a missing file -> fingerprint (0, 0) branch.
    assert user_prefs.get_send_mode() == user_prefs.DEFAULT_SEND_MODE
    cached = user_prefs._PREFS_CACHE
    assert cached is not None and cached[0] == (0, 0)


def test_repeated_getters_share_cached_file_read() -> None:
    user_prefs.set_session_sort("last_opened_at")
    original_read_json = user_prefs.read_json
    calls = 0

    def counted_read_json(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original_read_json(*args, **kwargs)

    user_prefs._PREFS_CACHE = None
    user_prefs.read_json = counted_read_json
    try:
        assert user_prefs.get_session_sort() == "last_opened_at"
        assert user_prefs.get_session_status_sort() is False
        assert user_prefs.get_folder_view_enabled() is True
    finally:
        user_prefs.read_json = original_read_json
    assert calls == 1


def test_external_file_change_invalidates_cache() -> None:
    user_prefs.set_session_sort("updated_at")
    assert user_prefs.get_session_sort() == "updated_at"
    _write_raw_prefs({"session_sort": "last_user_prompt_at"})
    assert user_prefs.get_session_sort() == "last_user_prompt_at"


def test_save_records_fingerprint_in_cache() -> None:
    user_prefs.set_send_mode("interrupt")
    cached = user_prefs._PREFS_CACHE
    assert cached is not None
    fingerprint, data = cached
    assert fingerprint != (0, 0)
    assert data["send_mode"] == "interrupt"


def test_save_handles_stat_oserror(monkeypatch) -> None:
    real_path = user_prefs._prefs_path()
    real_path.parent.mkdir(parents=True, exist_ok=True)

    # A Path whose .stat() raises only for the prefs file itself; the parent
    # dir (returned by .parent) still stats normally so write_json's mkdir and
    # the atomic replace succeed, and only the post-write stat in _save trips
    # the OSError fallback.
    base = type(real_path)
    file_posix = real_path.as_posix()

    class _StatFailPath(base):
        __slots__ = ()

        def stat(self, *args, **kwargs):
            if self.as_posix() == file_posix:
                raise OSError("simulated post-write stat failure")
            return base.stat(self, *args, **kwargs)

    stat_fail_path = _StatFailPath(real_path.as_posix())
    monkeypatch.setattr(user_prefs, "_load", lambda: {})
    monkeypatch.setattr(user_prefs, "_prefs_path", lambda: stat_fail_path)
    user_prefs._PREFS_CACHE = None

    assert user_prefs.set_send_mode("interrupt") == "interrupt"
    # The OSError branch must fall back to fingerprint (0, 0) rather than raise.
    assert user_prefs._PREFS_CACHE is not None
    assert user_prefs._PREFS_CACHE[0] == (0, 0)
    # The write itself still landed on disk despite the stat failure.
    raw = json.loads(real_path.read_text(encoding="utf-8"))
    assert raw["send_mode"] == "interrupt"


def test_removed_auto_restart_preference_is_not_projected() -> None:
    _write_raw_prefs({"auto_restart_on_idle": True})
    assert "auto_restart_on_idle" not in user_prefs.get_all()


# --------------------------------------------------------------------------- #
# send_mode
# --------------------------------------------------------------------------- #
def test_send_mode_default() -> None:
    assert user_prefs.get_send_mode() == "queue"


def test_send_mode_rejects_unknown_persisted_value() -> None:
    _write_raw_prefs({"send_mode": "telepathy"})
    assert user_prefs.get_send_mode() == "queue"


def test_set_send_mode_round_trips() -> None:
    assert user_prefs.set_send_mode("interrupt") == "interrupt"
    user_prefs._PREFS_CACHE = None
    assert user_prefs.get_send_mode() == "interrupt"


def test_set_send_mode_rejects_invalid() -> None:
    with pytest.raises(ValueError):
        user_prefs.set_send_mode("telepathy")  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# language
# --------------------------------------------------------------------------- #
def test_language_default() -> None:
    assert user_prefs.get_language() == "en"


def test_language_rejects_unknown_persisted_value() -> None:
    _write_raw_prefs({"language": "klingon"})
    assert user_prefs.get_language() == "en"


def test_set_language_round_trips() -> None:
    assert user_prefs.set_language("he") == "he"
    user_prefs._PREFS_CACHE = None
    assert user_prefs.get_language() == "he"


def test_set_language_rejects_invalid() -> None:
    with pytest.raises(ValueError):
        user_prefs.set_language("klingon")


# --------------------------------------------------------------------------- #
# shortcut_responses
# --------------------------------------------------------------------------- #
def test_shortcut_responses_default_contents() -> None:
    assert user_prefs.get_shortcut_responses() == list(user_prefs.DEFAULT_SHORTCUT_RESPONSES)


def test_shortcut_responses_rejects_non_list_persisted() -> None:
    _write_raw_prefs({"shortcut_responses": "TLDR"})
    assert user_prefs.get_shortcut_responses() == list(user_prefs.DEFAULT_SHORTCUT_RESPONSES)


def test_shortcut_responses_rejects_list_with_non_string() -> None:
    _write_raw_prefs({"shortcut_responses": ["ok", 7]})
    assert user_prefs.get_shortcut_responses() == list(user_prefs.DEFAULT_SHORTCUT_RESPONSES)


def test_set_shortcut_responses_round_trips() -> None:
    custom = ["a", "b"]
    assert user_prefs.set_shortcut_responses(custom) == ["a", "b"]
    user_prefs._PREFS_CACHE = None
    assert user_prefs.get_shortcut_responses() == ["a", "b"]


def test_set_shortcut_responses_rejects_non_list() -> None:
    with pytest.raises(ValueError):
        user_prefs.set_shortcut_responses("TLDR")  # type: ignore[arg-type]


def test_set_shortcut_responses_rejects_non_string_element() -> None:
    with pytest.raises(ValueError):
        user_prefs.set_shortcut_responses(["ok", 7])  # type: ignore[list-item]


# --------------------------------------------------------------------------- #
# cross_session_delegate_auto
# --------------------------------------------------------------------------- #
def test_cross_session_delegate_auto_defaults_off() -> None:
    assert user_prefs.get_cross_session_delegate_auto() is False


def test_cross_session_delegate_auto_rejects_non_bool_persisted() -> None:
    _write_raw_prefs({"cross_session_delegate_auto": "yes"})
    assert user_prefs.get_cross_session_delegate_auto() is False


def test_set_cross_session_delegate_auto_round_trips() -> None:
    assert user_prefs.set_cross_session_delegate_auto(True) is True
    user_prefs._PREFS_CACHE = None
    assert user_prefs.get_cross_session_delegate_auto() is True


def test_set_cross_session_delegate_auto_rejects_non_bool() -> None:
    with pytest.raises(ValueError):
        user_prefs.set_cross_session_delegate_auto("yes")  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# session_auto_delete_days
# --------------------------------------------------------------------------- #
def test_session_auto_delete_days_default_none() -> None:
    assert user_prefs.get_session_auto_delete_days() is None


def test_session_auto_delete_days_rejects_invalid_persisted() -> None:
    _write_raw_prefs({"session_auto_delete_days": True})
    assert user_prefs.get_session_auto_delete_days() is None
    _write_raw_prefs({"session_auto_delete_days": 0})
    assert user_prefs.get_session_auto_delete_days() is None


def test_set_session_auto_delete_days_none_and_value_round_trips() -> None:
    assert user_prefs.set_session_auto_delete_days(30) == 30
    user_prefs._PREFS_CACHE = None
    assert user_prefs.get_session_auto_delete_days() == 30
    assert user_prefs.set_session_auto_delete_days(None) is None
    user_prefs._PREFS_CACHE = None
    assert user_prefs.get_session_auto_delete_days() is None


def test_set_session_auto_delete_days_rejects_invalid() -> None:
    for bad in (True, 0, -3, "5"):
        with pytest.raises(ValueError):
            user_prefs.set_session_auto_delete_days(bad)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# font_family / font_size / appearance_theme
# --------------------------------------------------------------------------- #
def test_set_font_family_round_trips_each_choice() -> None:
    for fam in ("system", "serif", "mono", "inter"):
        assert user_prefs.set_font_family(fam) == fam
        user_prefs._PREFS_CACHE = None
        assert user_prefs.get_all()["font_family"] == fam


def test_set_font_family_rejects_invalid() -> None:
    with pytest.raises(ValueError):
        user_prefs.set_font_family("comic-sans")  # type: ignore[arg-type]


def test_font_family_rejects_unknown_persisted_value() -> None:
    _write_raw_prefs({"font_family": "comic-sans"})
    assert user_prefs.get_all()["font_family"] == "system"


def test_set_font_size_round_trips_bounds() -> None:
    for size in (user_prefs.MIN_FONT_SIZE, 14, user_prefs.MAX_FONT_SIZE):
        assert user_prefs.set_font_size(size) == size
        user_prefs._PREFS_CACHE = None
        assert user_prefs.get_all()["font_size"] == size


def test_set_font_size_rejects_invalid() -> None:
    for bad in (True, user_prefs.MIN_FONT_SIZE - 1, user_prefs.MAX_FONT_SIZE + 1, "14"):
        with pytest.raises(ValueError):
            user_prefs.set_font_size(bad)  # type: ignore[arg-type]


def test_font_size_clamps_out_of_range_persisted_to_default() -> None:
    _write_raw_prefs({"font_size": 1})
    assert user_prefs.get_all()["font_size"] == user_prefs.DEFAULT_FONT_SIZE


def test_set_appearance_theme_round_trips_each_choice() -> None:
    for theme in user_prefs.APPEARANCE_THEME_VALUES:
        assert user_prefs.set_appearance_theme(theme) == theme
        user_prefs._PREFS_CACHE = None
        assert user_prefs.get_all()["appearance_theme"] == theme


def test_set_appearance_theme_rejects_invalid() -> None:
    with pytest.raises(ValueError):
        user_prefs.set_appearance_theme("unknown")  # type: ignore[arg-type]


def test_appearance_theme_rejects_unknown_persisted_value() -> None:
    _write_raw_prefs({"appearance_theme": "unknown"})
    assert user_prefs.get_all()["appearance_theme"] == "default"


# --------------------------------------------------------------------------- #
# first_run_wizard_done
# --------------------------------------------------------------------------- #
def test_first_run_wizard_done_default_false() -> None:
    assert user_prefs.get_first_run_wizard_done() is False


def test_set_first_run_wizard_done_round_trips() -> None:
    assert user_prefs.set_first_run_wizard_done(True) is True
    user_prefs._PREFS_CACHE = None
    assert user_prefs.get_first_run_wizard_done() is True


def test_set_first_run_wizard_done_rejects_non_bool() -> None:
    with pytest.raises(ValueError):
        user_prefs.set_first_run_wizard_done("yes")  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# network_bind_address
# --------------------------------------------------------------------------- #
def test_network_bind_address_default() -> None:
    assert user_prefs.get_network_bind_address() == "127.0.0.1"


def test_network_bind_address_rejects_unknown_persisted() -> None:
    _write_raw_prefs({"network_bind_address": "8.8.8.8"})
    assert user_prefs.get_network_bind_address() == "127.0.0.1"


def test_set_network_bind_address_round_trips() -> None:
    assert user_prefs.set_network_bind_address("0.0.0.0") == "0.0.0.0"
    user_prefs._PREFS_CACHE = None
    assert user_prefs.get_network_bind_address() == "0.0.0.0"


def test_set_network_bind_address_rejects_invalid() -> None:
    with pytest.raises(ValueError):
        user_prefs.set_network_bind_address("8.8.8.8")  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# folder_view_enabled
# --------------------------------------------------------------------------- #
def test_folder_view_enabled_default_true() -> None:
    assert user_prefs.get_folder_view_enabled() is True


def test_folder_view_enabled_rejects_non_bool_persisted() -> None:
    _write_raw_prefs({"folder_view_enabled": 1})
    assert user_prefs.get_folder_view_enabled() is True


def test_set_folder_view_enabled_round_trips() -> None:
    assert user_prefs.set_folder_view_enabled(False) is False
    user_prefs._PREFS_CACHE = None
    assert user_prefs.get_folder_view_enabled() is False


def test_set_folder_view_enabled_rejects_non_bool() -> None:
    with pytest.raises(ValueError):
        user_prefs.set_folder_view_enabled(1)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# session_sort
# --------------------------------------------------------------------------- #
def test_session_sort_default() -> None:
    assert user_prefs.get_session_sort() == "updated_at"


def test_set_session_sort_round_trips_each_choice() -> None:
    for val in user_prefs.SESSION_SORT_VALUES:
        assert user_prefs.set_session_sort(val) == val
        user_prefs._PREFS_CACHE = None
        assert user_prefs.get_session_sort() == val


def test_set_session_sort_rejects_invalid() -> None:
    with pytest.raises(ValueError):
        user_prefs.set_session_sort("name")


# --------------------------------------------------------------------------- #
# session_status_sort
# --------------------------------------------------------------------------- #
def test_session_status_sort_default_false() -> None:
    assert user_prefs.get_session_status_sort() is False


def test_set_session_status_sort_round_trips() -> None:
    assert user_prefs.set_session_status_sort(True) is True
    user_prefs._PREFS_CACHE = None
    assert user_prefs.get_session_status_sort() is True


def test_set_session_status_sort_rejects_non_bool() -> None:
    with pytest.raises(ValueError):
        user_prefs.set_session_status_sort(1)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# session_tabs_sort / session_tabs_visible / background_work_visible
# / voice_close_on_background
# --------------------------------------------------------------------------- #
def test_set_session_tabs_sort_round_trips_each_choice() -> None:
    for val in user_prefs.SESSION_TABS_SORT_VALUES:
        assert user_prefs.set_session_tabs_sort(val) == val
        user_prefs._PREFS_CACHE = None
        assert user_prefs.get_all()["sessions_tabs_sort"] == val


def test_set_session_tabs_sort_rejects_invalid() -> None:
    with pytest.raises(ValueError):
        user_prefs.set_session_tabs_sort("name")


def test_set_session_tabs_visible_round_trips() -> None:
    assert user_prefs.set_session_tabs_visible(False) is False
    user_prefs._PREFS_CACHE = None
    assert user_prefs.get_all()["sessions_tabs_visible"] is False


def test_set_session_tabs_visible_rejects_non_bool() -> None:
    with pytest.raises(ValueError):
        user_prefs.set_session_tabs_visible(0)  # type: ignore[arg-type]


def test_set_background_work_visible_round_trips() -> None:
    assert user_prefs.set_background_work_visible(False) is False
    user_prefs._PREFS_CACHE = None
    assert user_prefs.get_all()["background_work_visible"] is False


def test_set_background_work_visible_rejects_non_bool() -> None:
    with pytest.raises(ValueError):
        user_prefs.set_background_work_visible(0)  # type: ignore[arg-type]


def test_set_voice_close_on_background_round_trips() -> None:
    assert user_prefs.set_voice_close_on_background(False) is False
    user_prefs._PREFS_CACHE = None
    assert user_prefs.get_all()["voice_close_on_background"] is False


def test_set_voice_close_on_background_rejects_non_bool() -> None:
    with pytest.raises(ValueError):
        user_prefs.set_voice_close_on_background(0)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# context_strategy
# --------------------------------------------------------------------------- #
def test_context_strategy_default() -> None:
    assert user_prefs.get_context_strategy() == "native_compact"


def test_context_strategy_rejects_unknown_persisted() -> None:
    _write_raw_prefs({"context_strategy": "magic"})
    assert user_prefs.get_context_strategy() == "native_compact"


def test_set_context_strategy_round_trips() -> None:
    assert user_prefs.set_context_strategy("continuation") == "continuation"
    user_prefs._PREFS_CACHE = None
    assert user_prefs.get_context_strategy() == "continuation"


def test_set_context_strategy_rejects_invalid() -> None:
    with pytest.raises(ValueError):
        user_prefs.set_context_strategy("magic")  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# last_model_by_runtime_profile
# --------------------------------------------------------------------------- #
def test_last_models_default_empty() -> None:
    assert user_prefs.get_last_models() == {}


def test_last_models_rejects_non_dict_persisted() -> None:
    _write_raw_prefs({"last_model_by_runtime_profile": []})
    assert user_prefs.get_last_models() == {}


def test_last_models_filters_blank_keys_and_non_string_values() -> None:
    _write_raw_prefs({
        "last_model_by_runtime_profile": {
            "prof": "sonnet",
            "": "empty-key",
            "blank": "",
            "  ": "whitespace-key",
            "ok": 7,
        }
    })
    assert user_prefs.get_last_models() == {"prof": "sonnet"}


def test_last_models_filters_non_string_keys(monkeypatch) -> None:
    # JSON keys are always strings on disk; the non-str-key guard defends
    # against programmatic corruption, so exercise it through _load directly.
    monkeypatch.setattr(user_prefs, "_load", lambda: {
        "last_model_by_runtime_profile": {5: "sonnet", "prof": "opus"},
    })
    assert user_prefs.get_last_models() == {"prof": "opus"}


def test_set_last_model_returns_true_on_change_and_records() -> None:
    assert user_prefs.set_last_model("prof", "sonnet") is True
    user_prefs._PREFS_CACHE = None
    assert user_prefs.get_last_models() == {"prof": "sonnet"}


def test_set_last_model_returns_false_when_unchanged() -> None:
    user_prefs.set_last_model("prof", "sonnet")
    assert user_prefs.set_last_model("prof", "sonnet") is False


def test_set_last_model_rejects_invalid_arguments() -> None:
    for bad_id in ("", "   ", 5):
        with pytest.raises(ValueError):
            user_prefs.set_last_model(bad_id, "sonnet")  # type: ignore[arg-type]
    for bad_model in ("", "   ", 5):
        with pytest.raises(ValueError):
            user_prefs.set_last_model("prof", bad_model)  # type: ignore[arg-type]


def test_set_last_model_drops_legacy_provider_key() -> None:
    _write_raw_prefs({"last_model_by_provider": {"claude": "opus"}})
    user_prefs.set_last_model("prof", "sonnet")
    raw = json.loads(user_prefs._prefs_path().read_text(encoding="utf-8"))
    assert "last_model_by_provider" not in raw
    assert raw["last_model_by_runtime_profile"] == {"prof": "sonnet"}


# --------------------------------------------------------------------------- #
# last_reasoning_effort_by_runtime_profile
# --------------------------------------------------------------------------- #
def test_last_reasoning_efforts_default_empty() -> None:
    assert user_prefs.get_last_reasoning_efforts() == {}


def test_last_reasoning_efforts_rejects_non_dict_persisted() -> None:
    _write_raw_prefs({"last_reasoning_effort_by_runtime_profile": []})
    assert user_prefs.get_last_reasoning_efforts() == {}


def test_last_reasoning_efforts_filters_blank_keys_and_non_string_values() -> None:
    _write_raw_prefs({
        "last_reasoning_effort_by_runtime_profile": {
            "prof": "high",
            "": "empty",
            "blank": "",
            "ok": 7,
        }
    })
    assert user_prefs.get_last_reasoning_efforts() == {"prof": "high"}


def test_last_reasoning_efforts_filters_non_string_keys(monkeypatch) -> None:
    monkeypatch.setattr(user_prefs, "_load", lambda: {
        "last_reasoning_effort_by_runtime_profile": {5: "high", "prof": "medium"},
    })
    assert user_prefs.get_last_reasoning_efforts() == {"prof": "medium"}


def test_set_last_reasoning_effort_change_and_no_change() -> None:
    assert user_prefs.set_last_reasoning_effort("prof", "high") is True
    assert user_prefs.set_last_reasoning_effort("prof", "high") is False
    user_prefs._PREFS_CACHE = None
    assert user_prefs.get_last_reasoning_efforts() == {"prof": "high"}


def test_set_last_reasoning_effort_rejects_invalid_arguments() -> None:
    with pytest.raises(ValueError):
        user_prefs.set_last_reasoning_effort("", "high")
    with pytest.raises(ValueError):
        user_prefs.set_last_reasoning_effort("prof", "")


def test_set_last_reasoning_effort_drops_legacy_provider_key() -> None:
    _write_raw_prefs({"last_reasoning_effort_by_provider": {"claude": "high"}})
    user_prefs.set_last_reasoning_effort("prof", "high")
    raw = json.loads(user_prefs._prefs_path().read_text(encoding="utf-8"))
    assert "last_reasoning_effort_by_provider" not in raw


# --------------------------------------------------------------------------- #
# Bounded-int preferences: task_start_silence_seconds + recursion caps
# --------------------------------------------------------------------------- #
def test_task_start_silence_seconds_default() -> None:
    assert user_prefs.get_task_start_silence_seconds() == user_prefs.DEFAULT_TASK_START_SILENCE_SECONDS


def test_task_start_silence_seconds_clamps_out_of_range_persisted() -> None:
    _write_raw_prefs({"task_start_silence_seconds": user_prefs.MIN_TASK_START_SILENCE_SECONDS - 1})
    assert user_prefs.get_task_start_silence_seconds() == user_prefs.DEFAULT_TASK_START_SILENCE_SECONDS
    _write_raw_prefs({"task_start_silence_seconds": user_prefs.MAX_TASK_START_SILENCE_SECONDS + 1})
    assert user_prefs.get_task_start_silence_seconds() == user_prefs.DEFAULT_TASK_START_SILENCE_SECONDS


def test_task_start_silence_seconds_rejects_non_int_persisted() -> None:
    _write_raw_prefs({"task_start_silence_seconds": True})
    assert user_prefs.get_task_start_silence_seconds() == user_prefs.DEFAULT_TASK_START_SILENCE_SECONDS


def test_set_task_start_silence_seconds_round_trips_bounds() -> None:
    lo, hi = user_prefs.MIN_TASK_START_SILENCE_SECONDS, user_prefs.MAX_TASK_START_SILENCE_SECONDS
    for val in (lo, 120, hi):
        assert user_prefs.set_task_start_silence_seconds(val) == val
        user_prefs._PREFS_CACHE = None
        assert user_prefs.get_task_start_silence_seconds() == val


def test_set_task_start_silence_seconds_rejects_invalid() -> None:
    for bad in (True, user_prefs.MIN_TASK_START_SILENCE_SECONDS - 1,
                user_prefs.MAX_TASK_START_SILENCE_SECONDS + 1, "90"):
        with pytest.raises(ValueError):
            user_prefs.set_task_start_silence_seconds(bad)  # type: ignore[arg-type]


def test_sync_wait_depth_cap_default_and_bounds() -> None:
    assert user_prefs.get_sync_wait_depth_cap() == user_prefs.DEFAULT_SYNC_WAIT_DEPTH_CAP
    for val in (user_prefs.MIN_SYNC_WAIT_DEPTH_CAP, user_prefs.MAX_SYNC_WAIT_DEPTH_CAP):
        assert user_prefs.set_sync_wait_depth_cap(val) == val
        user_prefs._PREFS_CACHE = None
        assert user_prefs.get_sync_wait_depth_cap() == val


def test_sync_wait_depth_cap_clamps_and_rejects() -> None:
    _write_raw_prefs({"sync_wait_depth_cap": user_prefs.MAX_SYNC_WAIT_DEPTH_CAP + 1})
    assert user_prefs.get_sync_wait_depth_cap() == user_prefs.DEFAULT_SYNC_WAIT_DEPTH_CAP
    with pytest.raises(ValueError):
        user_prefs.set_sync_wait_depth_cap(user_prefs.MAX_SYNC_WAIT_DEPTH_CAP + 1)


def test_session_creation_depth_cap_default_and_bounds() -> None:
    assert user_prefs.get_session_creation_depth_cap() == user_prefs.DEFAULT_SESSION_CREATION_DEPTH_CAP
    for val in (user_prefs.MIN_SESSION_CREATION_DEPTH_CAP, user_prefs.MAX_SESSION_CREATION_DEPTH_CAP):
        assert user_prefs.set_session_creation_depth_cap(val) == val
        user_prefs._PREFS_CACHE = None
        assert user_prefs.get_session_creation_depth_cap() == val


def test_session_creation_depth_cap_clamps_and_rejects() -> None:
    _write_raw_prefs({"session_creation_depth_cap": -1})
    assert user_prefs.get_session_creation_depth_cap() == user_prefs.DEFAULT_SESSION_CREATION_DEPTH_CAP
    with pytest.raises(ValueError):
        user_prefs.set_session_creation_depth_cap(user_prefs.MAX_SESSION_CREATION_DEPTH_CAP + 1)


def test_session_max_live_descendants_default_and_bounds() -> None:
    assert user_prefs.get_session_max_live_descendants() == user_prefs.DEFAULT_SESSION_MAX_LIVE_DESCENDANTS
    for val in (user_prefs.MIN_SESSION_MAX_LIVE_DESCENDANTS, user_prefs.MAX_SESSION_MAX_LIVE_DESCENDANTS):
        assert user_prefs.set_session_max_live_descendants(val) == val
        user_prefs._PREFS_CACHE = None
        assert user_prefs.get_session_max_live_descendants() == val


def test_session_max_live_descendants_clamps_and_rejects() -> None:
    _write_raw_prefs({"session_max_live_descendants": user_prefs.MAX_SESSION_MAX_LIVE_DESCENDANTS + 1})
    assert user_prefs.get_session_max_live_descendants() == user_prefs.DEFAULT_SESSION_MAX_LIVE_DESCENDANTS
    with pytest.raises(ValueError):
        user_prefs.set_session_max_live_descendants(user_prefs.MAX_SESSION_MAX_LIVE_DESCENDANTS + 1)


# --------------------------------------------------------------------------- #
# user_display_name (public mutator)
# --------------------------------------------------------------------------- #
def test_set_user_display_name_normalizes_and_persists() -> None:
    assert user_prefs.set_user_display_name("  Ofek   Ron  ") == "Ofek Ron"
    user_prefs._PREFS_CACHE = None
    assert user_prefs.get_all("login")["user_display_name"] == "Ofek Ron"


def test_set_user_display_name_none_clears_stored() -> None:
    user_prefs.set_user_display_name("Ofek")
    assert user_prefs.set_user_display_name(None) is None
    user_prefs._PREFS_CACHE = None
    assert user_prefs.get_all("login")["user_display_name"] == "login"


def test_set_user_display_name_rejects_invalid() -> None:
    for bad in (True, 7, "x" * (user_prefs.MAX_USER_DISPLAY_NAME_LENGTH + 1)):
        with pytest.raises(ValueError):
            user_prefs.set_user_display_name(bad)


# --------------------------------------------------------------------------- #
# get_all projection
# --------------------------------------------------------------------------- #
def test_get_all_loads_preferences_once() -> None:
    user_prefs.set_session_sort("last_opened_at")
    user_prefs.set_font_size(16)
    user_prefs.set_appearance_theme("nord")
    original_read_json = user_prefs.read_json
    calls = 0

    def counted_read_json(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original_read_json(*args, **kwargs)

    user_prefs._PREFS_CACHE = None
    user_prefs.read_json = counted_read_json
    try:
        prefs = user_prefs.get_all()
    finally:
        user_prefs.read_json = original_read_json
    assert calls == 1
    assert prefs["session_sort"] == "last_opened_at"
    assert prefs["font_size"] == 16
    assert prefs["appearance_theme"] == "nord"


def test_get_all_defaults_match_module_constants() -> None:
    prefs = user_prefs.get_all()
    assert prefs["send_mode"] == user_prefs.DEFAULT_SEND_MODE
    assert prefs["language"] == user_prefs.DEFAULT_LANGUAGE
    assert prefs["shortcut_responses"] == user_prefs.DEFAULT_SHORTCUT_RESPONSES
    assert prefs["cross_session_delegate_auto"] == user_prefs.DEFAULT_CROSS_SESSION_DELEGATE_AUTO
    assert prefs["context_strategy"] == user_prefs.DEFAULT_CONTEXT_STRATEGY
    assert prefs["session_auto_delete_days"] == user_prefs.DEFAULT_SESSION_AUTO_DELETE_DAYS
    assert prefs["font_family"] == user_prefs.DEFAULT_FONT_FAMILY
    assert prefs["font_size"] == user_prefs.DEFAULT_FONT_SIZE
    assert prefs["appearance_theme"] == user_prefs.DEFAULT_APPEARANCE_THEME
    assert prefs["first_run_wizard_done"] == user_prefs.DEFAULT_FIRST_RUN_WIZARD_DONE
    assert prefs["network_bind_address"] == user_prefs.DEFAULT_NETWORK_BIND_ADDRESS
    assert prefs["folder_view_enabled"] == user_prefs.DEFAULT_FOLDER_VIEW_ENABLED
    assert prefs["session_sort"] == user_prefs.DEFAULT_SESSION_SORT
    assert prefs["session_status_sort"] == user_prefs.DEFAULT_SESSION_STATUS_SORT
    assert prefs["sessions_tabs_sort"] == user_prefs.DEFAULT_SESSION_TABS_SORT
    assert prefs["sessions_tabs_visible"] == user_prefs.DEFAULT_SESSION_TABS_VISIBLE
    assert prefs["background_work_visible"] == user_prefs.DEFAULT_BACKGROUND_WORK_VISIBLE
    assert prefs["voice_close_on_background"] == user_prefs.DEFAULT_VOICE_CLOSE_ON_BACKGROUND
    assert prefs["task_start_silence_seconds"] == user_prefs.DEFAULT_TASK_START_SILENCE_SECONDS
    assert prefs["sync_wait_depth_cap"] == user_prefs.DEFAULT_SYNC_WAIT_DEPTH_CAP
    assert prefs["session_creation_depth_cap"] == user_prefs.DEFAULT_SESSION_CREATION_DEPTH_CAP
    assert prefs["session_max_live_descendants"] == user_prefs.DEFAULT_SESSION_MAX_LIVE_DESCENDANTS
    assert prefs["user_display_name"] == user_prefs.DEFAULT_USER_DISPLAY_NAME


def test_get_all_uses_login_username_when_no_stored_name() -> None:
    assert user_prefs.get_all("ofek")["user_display_name"] == "ofek"
