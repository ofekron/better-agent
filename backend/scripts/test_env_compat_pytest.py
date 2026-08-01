"""Dedicated pytest coverage for env_compat.py.

The legacy `test_env_compat.py` uses a `__main__` runner, so pytest never
executes its body — under pytest the module sat at ~29%. This file covers
every branch of the env-var compatibility helpers deterministically.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

backend = Path(__file__).resolve().parents[1]
if str(backend) not in sys.path:
    sys.path.insert(0, str(backend))

import env_compat

LEGACY = "BETTER_CLAUDE_BACKEND_URL"
AGENT = "BETTER_AGENT_BACKEND_URL"


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """Each test starts with both names absent."""
    monkeypatch.delenv(AGENT, raising=False)
    monkeypatch.delenv(LEGACY, raising=False)
    yield


# --- agent_env_name -------------------------------------------------------

def test_agent_env_name_renames_prefix():
    assert env_compat.agent_env_name("BETTER_CLAUDE_FOO") == "BETTER_AGENT_FOO"


def test_agent_env_name_rejects_non_legacy_prefix():
    with pytest.raises(ValueError):
        env_compat.agent_env_name("ANTHROPIC_API_KEY")


def test_agent_env_name_empty_suffix_is_valid():
    assert env_compat.agent_env_name("BETTER_CLAUDE_") == "BETTER_AGENT_"


# --- get_env ---------------------------------------------------------------

def test_get_env_prefers_agent_name(monkeypatch):
    monkeypatch.setenv(AGENT, "agent")
    monkeypatch.setenv(LEGACY, "legacy")
    assert env_compat.get_env(LEGACY) == "agent"


def test_get_env_falls_back_to_legacy(monkeypatch):
    monkeypatch.setenv(LEGACY, "legacy")
    assert env_compat.get_env(LEGACY) == "legacy"


def test_get_env_uses_default_when_absent():
    assert env_compat.get_env(LEGACY, default="d") == "d"


def test_get_env_treats_empty_agent_as_missing(monkeypatch):
    # empty-string agent name is falsy -> legacy wins
    monkeypatch.setenv(AGENT, "")
    monkeypatch.setenv(LEGACY, "legacy")
    assert env_compat.get_env(LEGACY) == "legacy"


def test_get_env_empty_everything_yields_default():
    assert env_compat.get_env(LEGACY) == ""


# --- get_env_stripped ------------------------------------------------------

def test_get_env_stripped(monkeypatch):
    monkeypatch.setenv(AGENT, "  trimmed  ")
    assert env_compat.get_env_stripped(LEGACY) == "trimmed"


def test_get_env_stripped_default_when_absent():
    assert env_compat.get_env_stripped(LEGACY, default="  d  ") == "d"


# --- require_env -----------------------------------------------------------

def test_require_env_returns_value(monkeypatch):
    monkeypatch.setenv(AGENT, "val")
    assert env_compat.require_env(LEGACY) == "val"


def test_require_env_raises_when_missing():
    with pytest.raises(RuntimeError):
        env_compat.require_env(LEGACY)


def test_require_env_raises_when_blank(monkeypatch):
    monkeypatch.setenv(AGENT, "   ")
    with pytest.raises(RuntimeError):
        env_compat.require_env(LEGACY)


# --- dual_env family -------------------------------------------------------

def test_dual_env_renders_both_names():
    assert env_compat.dual_env("BETTER_CLAUDE_MODEL", 42) == {
        "BETTER_AGENT_MODEL": "42",
        "BETTER_CLAUDE_MODEL": "42",
    }


def test_dual_env_raw_preserves_object_identity():
    sentinel = {"k": 1}
    out = env_compat.dual_env_raw("BETTER_CLAUDE_REF", sentinel)
    assert out["BETTER_AGENT_REF"] is sentinel
    assert out["BETTER_CLAUDE_REF"] is sentinel


def test_dual_env_many_merges_pairs():
    out = env_compat.dual_env_many(
        {"BETTER_CLAUDE_A": 1, "BETTER_CLAUDE_B": 2}
    )
    assert out == {
        "BETTER_AGENT_A": "1",
        "BETTER_CLAUDE_A": "1",
        "BETTER_AGENT_B": "2",
        "BETTER_CLAUDE_B": "2",
    }
