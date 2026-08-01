from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest  # noqa: E402

import execution_environment as ee  # noqa: E402


# --- happy paths -------------------------------------------------------------

@pytest.mark.parametrize("value", [None, {}, {"MY_VAR": "value"}])
def test_validate_extra_env_accepts_valid(value):
    # Returns None and must not raise for None, empty, or well-formed maps.
    assert ee.validate_extra_env(value) is None


def test_validate_extra_env_accepts_boundary_key_length():
    # Regex allows a 128-char key (1 lead + 127 trailing).
    key = "A" + "b" * 127
    assert len(key) == 128
    assert ee.validate_extra_env({key: "x"}) is None


def test_validate_extra_env_accepts_boundary_value_length():
    # Values up to 32768 chars are allowed.
    ee.validate_extra_env({"MY_VAR": "x" * 32768})


def test_validate_extra_env_value_case_insensitive_protection():
    # Lowercase "path" normalizes to protected "PATH" and is rejected, proving
    # the key is uppercased before the protected-set lookup.
    with pytest.raises(ValueError, match="protected"):
        ee.validate_extra_env({"path": "x"})


# --- type / shape rejections -------------------------------------------------

@pytest.mark.parametrize("bad", ["str", 1, 1.5, ("a", "b"), ["a"], {1, 2}])
def test_validate_extra_env_rejects_non_dict(bad):
    with pytest.raises(ValueError, match="string map or null"):
        ee.validate_extra_env(bad)


def test_validate_extra_env_rejects_non_string_key():
    with pytest.raises(ValueError, match="string map or null"):
        ee.validate_extra_env({1: "x"})


def test_validate_extra_env_rejects_non_string_value():
    with pytest.raises(ValueError, match="string map or null"):
        ee.validate_extra_env({"MY_VAR": 1})


# --- key-name / protected rejections ----------------------------------------

def test_validate_extra_env_rejects_bad_key_name():
    # Leading digit fails the env-key regex.
    with pytest.raises(ValueError, match="protected"):
        ee.validate_extra_env({"1VAR": "x"})


def test_validate_extra_env_rejects_protected_exact_key():
    with pytest.raises(ValueError, match="protected: PATH"):
        ee.validate_extra_env({"PATH": "/bin"})


def test_validate_extra_env_rejects_protected_prefix():
    with pytest.raises(ValueError, match="protected: AGY_TOKEN"):
        ee.validate_extra_env({"AGY_TOKEN": "secret"})


def test_validate_extra_env_rejects_overlong_key():
    # 129-char key exceeds the 128-char regex bound.
    key = "A" * 129
    with pytest.raises(ValueError, match="protected"):
        ee.validate_extra_env({key: "x"})


# --- value rejections --------------------------------------------------------

def test_validate_extra_env_rejects_null_byte_value():
    with pytest.raises(ValueError, match="value is invalid"):
        ee.validate_extra_env({"MY_VAR": "a\x00b"})


def test_validate_extra_env_rejects_oversized_value():
    with pytest.raises(ValueError, match="value is invalid"):
        ee.validate_extra_env({"MY_VAR": "x" * 32769})
