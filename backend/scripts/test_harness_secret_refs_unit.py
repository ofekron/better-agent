"""Unit tests for ``harness_secret_refs.normalize_harness_secret_refs``.

Pure-logic validator. Every error branch raises ``HarnessSecretRefError``;
the happy path returns the normalized mapping unchanged.

Pins:
  1. Happy path: empty object, single ref, multiple extensions/refs.
  2. Top-level shape errors: non-object value, non-str / malformed extension id.
  3. Per-extension errors: refs not a list.
  4. Per-reference errors: non-str reference, duplicate reference, and each
     malformed-shape arm of the format ``or`` chain (wrong arity, wrong
     scheme, ext mismatch, invalid setting key).

Run with:
    cd backend && .venv/bin/python -m pytest scripts/test_harness_secret_refs_unit.py -v
"""

from __future__ import annotations

import os
import sys

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-harness-secret-refs-unit-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest  # noqa: E402

from harness_secret_refs import (  # noqa: E402
    HarnessSecretRefError,
    normalize_harness_secret_refs,
)

_VALID_EXT = "abc"
_VALID_KEY = "key"


def _ref(ext, key):
    return f"extension-setting:{ext}:{key}"


def test_empty_object():
    assert normalize_harness_secret_refs({}) == {}


def test_valid_single():
    ref = _ref(_VALID_EXT, _VALID_KEY)
    assert normalize_harness_secret_refs({_VALID_EXT: [ref]}) == {_VALID_EXT: [ref]}


def test_valid_multiple_extensions_and_refs():
    out = normalize_harness_secret_refs(
        {
            "abc": [_ref("abc", "key"), _ref("abc", "other")],
            "xyz": [_ref("xyz", "k2")],
        },
    )
    assert out == {
        "abc": [_ref("abc", "key"), _ref("abc", "other")],
        "xyz": [_ref("xyz", "k2")],
    }


@pytest.mark.parametrize("bad", [[], "x", 1, None])
def test_value_not_dict_raises(bad):
    with pytest.raises(HarnessSecretRefError, match="must be an object"):
        normalize_harness_secret_refs(bad)


def test_extension_id_not_str_raises():
    with pytest.raises(HarnessSecretRefError, match="extension id is invalid"):
        normalize_harness_secret_refs({123: []})


def test_extension_id_invalid_format_raises():
    with pytest.raises(HarnessSecretRefError, match="extension id is invalid"):
        normalize_harness_secret_refs({"AB": []})


def test_refs_not_list_raises():
    with pytest.raises(HarnessSecretRefError, match=r"secret_refs\.abc must be a list"):
        normalize_harness_secret_refs({_VALID_EXT: "nope"})


def test_reference_not_str_raises():
    with pytest.raises(HarnessSecretRefError, match="invalid reference"):
        normalize_harness_secret_refs({_VALID_EXT: [123]})


def test_reference_duplicate_raises():
    ref = _ref(_VALID_EXT, _VALID_KEY)
    with pytest.raises(HarnessSecretRefError, match="invalid reference"):
        normalize_harness_secret_refs({_VALID_EXT: [ref, ref]})


@pytest.mark.parametrize(
    "badref",
    [
        "extension-setting:abc:key:extra",  # wrong arity
        "setting:abc:key",  # wrong scheme
        "extension-setting:xyz:key",  # ext mismatch
        "extension-setting:abc:BAD",  # invalid setting key
    ],
)
def test_reference_malformed_raises(badref):
    with pytest.raises(HarnessSecretRefError, match="invalid reference"):
        normalize_harness_secret_refs({_VALID_EXT: [badref]})
