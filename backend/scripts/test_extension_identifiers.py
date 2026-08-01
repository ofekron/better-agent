from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import extension_identifiers as ei  # noqa: E402


# --- EXTENSION_ID_RE: ^[a-z0-9][a-z0-9_.-]{2,79}$  (3-80 chars) ---


def test_extension_id_accepts_minimal_three_chars():
    assert ei.EXTENSION_ID_RE.fullmatch("abc")


def test_extension_id_accepts_full_charset_and_length():
    assert ei.EXTENSION_ID_RE.fullmatch("a-b.c_d-1")
    assert ei.EXTENSION_ID_RE.fullmatch("0" + "x" * 79)  # 80 chars total


def test_extension_id_rejects_too_short():
    assert ei.EXTENSION_ID_RE.fullmatch("ab") is None
    assert ei.EXTENSION_ID_RE.fullmatch("a") is None


def test_extension_id_rejects_too_long():
    assert ei.EXTENSION_ID_RE.fullmatch("a" * 81) is None


def test_extension_id_rejects_bad_first_char():
    # first char must be lowercase alphanumeric
    assert ei.EXTENSION_ID_RE.fullmatch("_abc") is None
    assert ei.EXTENSION_ID_RE.fullmatch("-abc") is None
    assert ei.EXTENSION_ID_RE.fullmatch(".abc") is None


def test_extension_id_rejects_uppercase_and_empty():
    assert ei.EXTENSION_ID_RE.fullmatch("Abc") is None
    assert ei.EXTENSION_ID_RE.fullmatch("") is None


# --- EXTENSION_SETTING_KEY_RE: ^[a-z][a-z0-9_]{0,62}$  (1-63 chars) ---


def test_setting_key_accepts_single_letter():
    assert ei.EXTENSION_SETTING_KEY_RE.fullmatch("a")


def test_setting_key_accepts_full_charset_and_max_length():
    assert ei.EXTENSION_SETTING_KEY_RE.fullmatch("a_b1")
    assert ei.EXTENSION_SETTING_KEY_RE.fullmatch("a" * 63)


def test_setting_key_rejects_empty():
    assert ei.EXTENSION_SETTING_KEY_RE.fullmatch("") is None


def test_setting_key_rejects_non_letter_first_char():
    assert ei.EXTENSION_SETTING_KEY_RE.fullmatch("1abc") is None
    assert ei.EXTENSION_SETTING_KEY_RE.fullmatch("_abc") is None


def test_setting_key_rejects_uppercase_and_dashes_and_too_long():
    assert ei.EXTENSION_SETTING_KEY_RE.fullmatch("Abc") is None
    assert ei.EXTENSION_SETTING_KEY_RE.fullmatch("a-b") is None  # dash not allowed
    assert ei.EXTENSION_SETTING_KEY_RE.fullmatch("a" * 64) is None
