from __future__ import annotations

import os
import resource
import sys
from unittest.mock import patch

import pytest

import _test_home

_test_home.isolate("bc-test-fd-limits-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import fd_limits  # noqa: E402
from fd_limits import desired_soft_nofile, raise_fd_limit  # noqa: E402


# --- desired_soft_nofile: pure branch logic, no side effects ---

def test_returns_none_when_ceiling_already_at_or_below_soft() -> None:
    # hard below target -> ceiling = min(target, hard) = hard; hard <= soft.
    assert desired_soft_nofile(soft=100, hard=50, target=200, infinity=-1) is None


def test_returns_none_when_soft_already_meets_target() -> None:
    # ceiling = target = soft -> no change (never lowers, equal is no-op).
    assert desired_soft_nofile(soft=200, hard=300, target=200, infinity=-1) is None


def test_returns_ceiling_bounded_by_hard_limit() -> None:
    # hard < target -> ceiling = hard; hard > soft -> raise to hard.
    assert desired_soft_nofile(soft=100, hard=150, target=200, infinity=-1) == 150


def test_returns_target_directly_when_hard_unbounded() -> None:
    # hard == infinity sentinel: min(target, hard) would collapse to -1 and
    # *shrink* the limit, so the unbounded branch uses target directly.
    assert desired_soft_nofile(soft=100, hard=-1, target=200, infinity=-1) == 200


def test_unbounded_hard_still_noop_when_soft_above_target() -> None:
    assert desired_soft_nofile(soft=1000, hard=-1, target=200, infinity=-1) is None


def test_default_target_constant() -> None:
    assert fd_limits.TARGET_SOFT_NOFILE == 65536


# --- raise_fd_limit: wiring around the resource syscall boundary ---

def test_raise_fd_limit_sets_higher_soft() -> None:
    with patch.object(resource, "getrlimit", return_value=(256, 65536)) as mock_get, \
            patch.object(resource, "setrlimit") as mock_set:
        raise_fd_limit(target=65536)
    mock_get.assert_called_once_with(resource.RLIMIT_NOFILE)
    # ceiling = min(65536, 65536) = 65536 > 256 -> raise soft, keep hard.
    mock_set.assert_called_once_with(resource.RLIMIT_NOFILE, (65536, 65536))


def test_raise_fd_limit_skips_when_no_raise_needed() -> None:
    with patch.object(resource, "getrlimit", return_value=(70000, 70000)), \
            patch.object(resource, "setrlimit") as mock_set:
        raise_fd_limit(target=65536)
    mock_set.assert_not_called()  # ceiling 65536 <= soft 70000 -> None


def test_raise_fd_limit_clamps_to_hard_below_target() -> None:
    with patch.object(resource, "getrlimit", return_value=(100, 1000)), \
            patch.object(resource, "setrlimit") as mock_set:
        raise_fd_limit(target=65536)
    # ceiling = min(65536, 1000) = 1000 > 100 -> raise to hard (1000).
    mock_set.assert_called_once_with(resource.RLIMIT_NOFILE, (1000, 1000))


def test_raise_fd_limit_noop_when_resource_unavailable() -> None:
    # Windows / non-POSIX: `import resource` fails inside the function.
    with patch.dict(sys.modules, {"resource": None}):
        assert raise_fd_limit() is None
