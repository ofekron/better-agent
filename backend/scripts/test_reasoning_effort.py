from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import reasoning_effort as re  # noqa: E402


# --- normalize_reasoning_effort ----------------------------------------------

def test_normalize_non_string_returns_none():
    assert re.normalize_reasoning_effort(123) is None
    assert re.normalize_reasoning_effort(None) is None
    assert re.normalize_reasoning_effort(["high"]) is None


def test_normalize_strips_and_lowercases():
    assert re.normalize_reasoning_effort("  HIGH ") == "high"


def test_normalize_max_maps_to_xhigh():
    assert re.normalize_reasoning_effort("max") == "xhigh"
    assert re.normalize_reasoning_effort("MAX") == "xhigh"


def test_normalize_valid_efforts():
    for effort in re.ALL_REASONING_EFFORTS:
        assert re.normalize_reasoning_effort(effort) == effort


def test_normalize_unknown_returns_none():
    assert re.normalize_reasoning_effort("ultra") is None
    assert re.normalize_reasoning_effort("") is None


# --- require_reasoning_effort ------------------------------------------------

def test_require_returns_normalized():
    assert re.require_reasoning_effort("low") == "low"
    assert re.require_reasoning_effort(" Max ") == "xhigh"


def test_require_invalid_raises_with_allowed_list():
    with pytest.raises(ValueError) as exc:
        re.require_reasoning_effort("nope")
    msg = str(exc.value)
    assert "reasoning_effort must be one of:" in msg
    for effort in re.ALL_REASONING_EFFORTS:
        assert effort in msg


def test_require_non_string_raises():
    with pytest.raises(ValueError):
        re.require_reasoning_effort(5)


# --- claude_sdk_effort -------------------------------------------------------

def test_claude_sdk_none_for_invalid():
    assert re.claude_sdk_effort("nope") is None
    assert re.claude_sdk_effort(None) is None


def test_claude_sdk_xhigh_maps_to_max():
    assert re.claude_sdk_effort("xhigh") == "max"
    assert re.claude_sdk_effort("max") == "max"


def test_claude_sdk_supported_pass_through():
    for effort in ("low", "medium", "high"):
        assert re.claude_sdk_effort(effort) == effort


@pytest.mark.parametrize("effort", ["none", "minimal"])
def test_claude_sdk_unsupported_raises(effort):
    # "none"/"minimal" normalize fine but are not Claude SDK efforts
    with pytest.raises(ValueError) as exc:
        re.claude_sdk_effort(effort)
    msg = str(exc.value)
    assert "Claude reasoning_effort must be one of:" in msg
    for ce in re.CLAUDE_REASONING_EFFORTS:
        assert ce in msg
