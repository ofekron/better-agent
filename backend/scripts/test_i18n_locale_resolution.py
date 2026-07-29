"""Regression: backend i18n must render in the user's configured language.

Before the fix, `t()` hard-coded English whenever a caller omitted `locale`
(every caller does), so `backend/i18n/he.py` was unreachable despite a real
user language preference. This test isolates BETTER_AGENT_HOME, sets the
preference to Hebrew, and asserts `t()` honours it.
"""

import os
import sys
import tempfile

# Isolate state BEFORE importing any backend module so user_prefs never
# touches the developer's real home. See CLAUDE.md "State directory isolation".
_TEST_HOME = tempfile.mkdtemp(prefix="ba-i18n-test-")
os.environ["BETTER_AGENT_HOME"] = _TEST_HOME
os.environ["BETTER_CLAUDE_HOME"] = _TEST_HOME
os.environ["BETTER_AGENT_TEST_MODE"] = "1"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from i18n import t  # noqa: E402
from i18n.en import TRANSLATIONS as EN  # noqa: E402
from i18n.he import TRANSLATIONS as HE  # noqa: E402
import user_prefs  # noqa: E402

# A key whose Hebrew rendering differs from English.
KEY = "error.session_not_found"


def test_t_uses_user_language_preference() -> None:
    user_prefs.set_language("he")
    assert user_prefs.get_language() == "he"
    rendered = t(KEY)
    assert rendered == HE[KEY], (
        f"expected Hebrew '{HE[KEY]}' for user pref 'he', got '{rendered}'"
    )
    assert rendered != EN[KEY], "Hebrew preference still renders English"


def test_explicit_locale_overrides_preference() -> None:
    user_prefs.set_language("he")
    assert t(KEY, locale="en") == EN[KEY], "explicit locale must override pref"


def test_untranslated_language_falls_back_to_english() -> None:
    # "es" is supported but has no translation file, so it floors to English.
    user_prefs.set_language("es")
    assert t(KEY) == EN[KEY]


def test_missing_key_returns_key() -> None:
    user_prefs.set_language("he")
    assert t("error.does_not_exist_zzz") == "error.does_not_exist_zzz"


if __name__ == "__main__":
    test_t_uses_user_language_preference()
    test_explicit_locale_overrides_preference()
    test_untranslated_language_falls_back_to_english()
    test_missing_key_returns_key()
    print("OK")
