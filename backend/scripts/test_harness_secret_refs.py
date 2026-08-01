from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest  # noqa: E402

from harness_secret_refs import (  # noqa: E402
    HarnessSecretRefError,
    normalize_harness_secret_refs,
)

# Reference shape: "extension-setting:<extension_id>:<setting_key>".
# EXTENSION_ID_RE        = ^[a-z0-9][a-z0-9_.-]{2,79}$   (3..80 chars)
# EXTENSION_SETTING_KEY_RE = ^[a-z][a-z0-9_]{0,62}$       (1..63 chars)

_VALID_ID = "foo.bar"
_VALID_REF = "extension-setting:foo.bar:api_key"


# --- value must be a dict ----------------------------------------------------

@pytest.mark.parametrize("bad", [[], None, "x", 0, ("a", "b")])
def test_rejects_non_dict_value(bad) -> None:
    with pytest.raises(HarnessSecretRefError, match="must be an object"):
        normalize_harness_secret_refs(bad)


# --- extension id validation -------------------------------------------------

def test_rejects_non_str_extension_id() -> None:
    # type check short-circuits before the regex ever runs
    with pytest.raises(HarnessSecretRefError, match="extension id is invalid"):
        normalize_harness_secret_refs({1: [_VALID_REF]})


@pytest.mark.parametrize("bad_id", ["ab", "", "FOO.bar", "a" * 81, "foo bar"])
def test_rejects_invalid_extension_id_format(bad_id: str) -> None:
    with pytest.raises(HarnessSecretRefError, match="extension id is invalid"):
        normalize_harness_secret_refs({bad_id: [_VALID_REF]})


@pytest.mark.parametrize("good_id", ["abc", "a" * 80, "1_-."])
def test_accepts_extension_id_boundary_lengths(good_id: str) -> None:
    ref = f"extension-setting:{good_id}:api_key"
    assert normalize_harness_secret_refs({good_id: [ref]}) == {good_id: [ref]}


# --- refs must be a list -----------------------------------------------------

@pytest.mark.parametrize("bad_refs", ["api_key", None, 5, {}])
def test_rejects_refs_not_list(bad_refs) -> None:
    with pytest.raises(HarnessSecretRefError, match=r"foo\.bar must be a list"):
        normalize_harness_secret_refs({_VALID_ID: bad_refs})


def test_empty_refs_list_allowed() -> None:
    assert normalize_harness_secret_refs({_VALID_ID: []}) == {_VALID_ID: []}


# --- reference validation ----------------------------------------------------

def test_rejects_non_str_reference() -> None:
    with pytest.raises(HarnessSecretRefError, match="invalid reference"):
        normalize_harness_secret_refs({_VALID_ID: [5]})


def test_rejects_duplicate_reference() -> None:
    with pytest.raises(HarnessSecretRefError, match="invalid reference"):
        normalize_harness_secret_refs({_VALID_ID: [_VALID_REF, _VALID_REF]})


@pytest.mark.parametrize(
    "bad_ref",
    [
        "extension-setting:foo.bar",            # too few parts
        "extension-setting:foo.bar:a:b",        # too many parts
        "ext-setting:foo.bar:api_key",          # wrong prefix
        "extension-setting:other.baz:api_key",  # wrong extension id
        "extension-setting:foo.bar:API_KEY",    # setting key uppercase
        "extension-setting:foo.bar:1key",       # setting key starts with digit
        f"extension-setting:foo.bar:{'a' * 64}",  # setting key too long
    ],
)
def test_rejects_malformed_reference(bad_ref: str) -> None:
    with pytest.raises(HarnessSecretRefError, match="invalid reference"):
        normalize_harness_secret_refs({_VALID_ID: [bad_ref]})


def test_accepts_single_char_setting_key() -> None:
    ref = "extension-setting:foo.bar:a"
    assert normalize_harness_secret_refs({_VALID_ID: [ref]}) == {_VALID_ID: [ref]}


# --- normalization structure -------------------------------------------------

def test_normalizes_single_valid_reference() -> None:
    out = normalize_harness_secret_refs({_VALID_ID: [_VALID_REF]})
    assert out == {_VALID_ID: [_VALID_REF]}


def test_normalizes_multiple_extensions_preserving_order() -> None:
    payload = {
        "foo.bar": ["extension-setting:foo.bar:api_key", "extension-setting:foo.bar:token"],
        "baz.qux": ["extension-setting:baz.qux:secret"],
    }
    out = normalize_harness_secret_refs(payload)
    assert list(out.keys()) == ["foo.bar", "baz.qux"]
    assert out["foo.bar"] == ["extension-setting:foo.bar:api_key", "extension-setting:foo.bar:token"]
    assert out["baz.qux"] == ["extension-setting:baz.qux:secret"]


def test_does_not_mutate_input() -> None:
    refs = [_VALID_REF]
    payload = {_VALID_ID: refs}
    normalize_harness_secret_refs(payload)
    assert payload == {_VALID_ID: refs}
    assert refs == [_VALID_REF]


def test_returns_distinct_container() -> None:
    refs = [_VALID_REF]
    payload = {_VALID_ID: refs}
    out = normalize_harness_secret_refs(payload)
    assert out is not payload
    assert out[_VALID_ID] is not refs


def test_empty_dict_returns_empty() -> None:
    assert normalize_harness_secret_refs({}) == {}
