"""Unit tests for ``runtime_bootstrap.issue()`` input validation.

The full broker roundtrip (secret/hydration served over the runtime socket,
the one-shot handle, lease retirement) is exercised by the standalone
integration script ``test_runtime_bootstrap.py``. These tests pin the
input-validation guards that reject an empty secret or a hydration that is
not a JSON-compatible object BEFORE any broker is started, so they need no
runtime transport.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import _test_home  # noqa: E402

_test_home.isolate("ba-runtime-bootstrap-unit-")

import pytest  # noqa: E402
import runtime_bootstrap  # noqa: E402


def test_issue_requires_a_non_empty_secret():
    with pytest.raises(ValueError, match="secret is required"):
        runtime_bootstrap.issue("")
    # None coerces to an empty string via `str(secret or "")`.
    with pytest.raises(ValueError, match="secret is required"):
        runtime_bootstrap.issue(None)  # type: ignore[arg-type]


def test_issue_rejects_hydration_that_is_not_an_object():
    # A JSON-valid but non-object hydration serializes fine, then fails the
    # `type(hydration) is not dict` shape guard.
    with pytest.raises(ValueError, match="hydration must be an object"):
        runtime_bootstrap.issue("secret", runtime_hydration=["not", "a", "dict"])


def test_issue_rejects_hydration_that_is_not_json_compatible():
    # allow_nan=False makes NaN fail JSON serialization, so the hydration is
    # rejected as not JSON-compatible before any broker is started.
    with pytest.raises(ValueError, match="hydration must be JSON-compatible"):
        runtime_bootstrap.issue("secret", runtime_hydration={"value": float("nan")})
