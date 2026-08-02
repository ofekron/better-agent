"""Unit tests for ``reasoning_effort`` normalization helpers.

Pure-logic module: alias resolution, set membership, and the per-provider
(Claude SDK) effort mapping. No ``ba_home`` dependency at import time.

Pins:
  1. ``normalize_reasoning_effort``: non-str -> None, alias ``max`` ->
     ``xhigh``, set membership, whitespace/case folding, unknown -> None.
  2. ``require_reasoning_effort``: valid passthrough vs. ``ValueError``.
  3. ``claude_sdk_effort``: non-str -> None, ``xhigh`` -> ``max``,
     low/medium/high passthrough, and an effort that normalizes but is not a
     valid Claude SDK effort -> ``ValueError``.

Run with:
    cd backend && .venv/bin/python -m pytest scripts/test_reasoning_effort_unit.py -v
"""

from __future__ import annotations

import os
import sys

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-reasoning-effort-unit-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest  # noqa: E402

import reasoning_effort as reff  # noqa: E402
from reasoning_effort import (  # noqa: E402
    ALL_REASONING_EFFORTS,
    CLAUDE_REASONING_EFFORTS,
    DEFAULT_REASONING_EFFORT,
    claude_sdk_effort,
    normalize_reasoning_effort,
    require_reasoning_effort,
)


def test_module_constants_coherent():
    assert DEFAULT_REASONING_EFFORT == "medium"
    assert set(CLAUDE_REASONING_EFFORTS).issubset(ALL_REASONING_EFFORTS)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("low", "low"),
        ("medium", "medium"),
        ("high", "high"),
        ("xhigh", "xhigh"),
        ("minimal", "minimal"),
        ("none", "none"),
        ("max", "xhigh"),
        ("MAX", "xhigh"),
        ("  High ", "high"),
        ("Low", "low"),
    ],
)
def test_normalize_valid(raw, expected):
    assert normalize_reasoning_effort(raw) == expected


@pytest.mark.parametrize("bad", [None, 5, 1.5, [], object()])
def test_normalize_non_str_returns_none(bad):
    assert normalize_reasoning_effort(bad) is None


@pytest.mark.parametrize("bad", ["", "   ", "turbo", "maximal", "xhigh2"])
def test_normalize_unknown_returns_none(bad):
    assert normalize_reasoning_effort(bad) is None


def test_require_valid_and_aliased():
    assert require_reasoning_effort("low") == "low"
    assert require_reasoning_effort("Max") == "xhigh"


def test_require_invalid_raises():
    with pytest.raises(ValueError, match="reasoning_effort must be one of"):
        require_reasoning_effort("turbo")
    with pytest.raises(ValueError, match="reasoning_effort must be one of"):
        require_reasoning_effort(None)


def test_claude_sdk_max_alias():
    assert claude_sdk_effort("xhigh") == "max"
    assert claude_sdk_effort("max") == "max"


@pytest.mark.parametrize("eff", ["low", "medium", "high"])
def test_claude_sdk_passthrough(eff):
    assert claude_sdk_effort(eff) == eff


@pytest.mark.parametrize("bad", [123, None, 1.0])
def test_claude_sdk_non_str_returns_none(bad):
    assert claude_sdk_effort(bad) is None


@pytest.mark.parametrize("eff", ["minimal", "none"])
def test_claude_sdk_unsupported_effort_raises(eff):
    with pytest.raises(ValueError, match="Claude reasoning_effort must be one of"):
        claude_sdk_effort(eff)
