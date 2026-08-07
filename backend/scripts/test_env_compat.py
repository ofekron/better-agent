"""Regression + unit tests for Better Agent env-var compatibility.

Dual-mode: pytest-collectible (``def test_*``) and standalone
(``python scripts/test_env_compat.py`` runs ``main()``).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

backend = Path(__file__).resolve().parents[1]
if str(backend) not in sys.path:
    sys.path.insert(0, str(backend))

from env_compat import (  # noqa: E402
    agent_env_name,
    dual_env,
    dual_env_many,
    dual_env_raw,
    get_env,
    get_env_stripped,
    require_env,
)
from provider import build_better_agent_run_env  # noqa: E402


def _clear(*names: str) -> None:
    for name in names:
        os.environ.pop(name, None)


# --- pure-function unit tests (pytest-collectible; also invoked from main()) ---


def test_agent_env_name_maps_prefix() -> None:
    assert agent_env_name("BETTER_CLAUDE_FOO") == "BETTER_AGENT_FOO"


def test_agent_env_name_rejects_non_prefixed() -> None:
    for bad in ("CLAUDE_FOO", "BETTER_AGENT_FOO", "", "better_claude_x"):
        with pytest.raises(ValueError, match="BETTER_CLAUDE_"):
            agent_env_name(bad)


def test_get_env_prefers_agent_over_legacy() -> None:
    _clear("BETTER_AGENT_X", "BETTER_CLAUDE_X")
    try:
        os.environ["BETTER_CLAUDE_X"] = "legacy"
        os.environ["BETTER_AGENT_X"] = "agent"
        assert get_env("BETTER_CLAUDE_X") == "agent"
    finally:
        _clear("BETTER_AGENT_X", "BETTER_CLAUDE_X")


def test_get_env_falls_back_to_legacy_when_agent_unset() -> None:
    _clear("BETTER_AGENT_X", "BETTER_CLAUDE_X")
    try:
        os.environ["BETTER_CLAUDE_X"] = "legacy"
        assert get_env("BETTER_CLAUDE_X") == "legacy"
    finally:
        _clear("BETTER_AGENT_X", "BETTER_CLAUDE_X")


def test_get_env_empty_agent_string_falls_through_to_legacy() -> None:
    _clear("BETTER_AGENT_X", "BETTER_CLAUDE_X")
    try:
        os.environ["BETTER_AGENT_X"] = ""
        os.environ["BETTER_CLAUDE_X"] = "legacy"
        assert get_env("BETTER_CLAUDE_X") == "legacy"
    finally:
        _clear("BETTER_AGENT_X", "BETTER_CLAUDE_X")


def test_get_env_uses_default_when_unset() -> None:
    _clear("BETTER_AGENT_X", "BETTER_CLAUDE_X")
    assert get_env("BETTER_CLAUDE_X", "fallback") == "fallback"
    assert get_env("BETTER_CLAUDE_X") == ""


def test_get_env_stripped_trims_whitespace() -> None:
    _clear("BETTER_AGENT_X", "BETTER_CLAUDE_X")
    try:
        os.environ["BETTER_AGENT_X"] = "  val  "
        assert get_env_stripped("BETTER_CLAUDE_X") == "val"
        assert get_env_stripped("BETTER_CLAUDE_X", "fallback") == "val"
    finally:
        _clear("BETTER_AGENT_X", "BETTER_CLAUDE_X")


def test_get_env_stripped_default_when_unset() -> None:
    _clear("BETTER_AGENT_X", "BETTER_CLAUDE_X")
    assert get_env_stripped("BETTER_CLAUDE_X") == ""


def test_require_env_returns_value_when_present() -> None:
    _clear("BETTER_AGENT_X", "BETTER_CLAUDE_X")
    try:
        os.environ["BETTER_AGENT_X"] = "value"
        assert require_env("BETTER_CLAUDE_X") == "value"
    finally:
        _clear("BETTER_AGENT_X", "BETTER_CLAUDE_X")


def test_require_env_raises_when_missing() -> None:
    _clear("BETTER_AGENT_X", "BETTER_CLAUDE_X")
    with pytest.raises(RuntimeError, match="is required"):
        require_env("BETTER_CLAUDE_X")


def test_dual_env_renders_both_keys_as_strings() -> None:
    assert dual_env("BETTER_CLAUDE_N", 42) == {
        "BETTER_AGENT_N": "42",
        "BETTER_CLAUDE_N": "42",
    }


def test_dual_env_raw_keeps_native_type() -> None:
    payload = {"a": 1}
    assert dual_env_raw("BETTER_CLAUDE_N", payload) == {
        "BETTER_AGENT_N": payload,
        "BETTER_CLAUDE_N": payload,
    }


def test_dual_env_many_merges_pairs() -> None:
    out = dual_env_many({"BETTER_CLAUDE_A": 1, "BETTER_CLAUDE_B": "x"})
    assert out == {
        "BETTER_AGENT_A": "1",
        "BETTER_CLAUDE_A": "1",
        "BETTER_AGENT_B": "x",
        "BETTER_CLAUDE_B": "x",
    }


# --- provider integration regression (standalone only; mutates the live env) ---


def _provider_run_env_check() -> None:
    _clear("BETTER_AGENT_BACKEND_URL", "BETTER_CLAUDE_BACKEND_URL")
    os.environ["BETTER_CLAUDE_BACKEND_URL"] = "http://legacy"
    assert get_env("BETTER_CLAUDE_BACKEND_URL") == "http://legacy"
    os.environ["BETTER_AGENT_BACKEND_URL"] = "http://agent"
    assert get_env("BETTER_CLAUDE_BACKEND_URL") == "http://agent"
    assert require_env("BETTER_CLAUDE_BACKEND_URL") == "http://agent"

    pair = dual_env("BETTER_CLAUDE_MODEL", "sonnet")
    assert pair == {
        "BETTER_AGENT_MODEL": "sonnet",
        "BETTER_CLAUDE_MODEL": "sonnet",
    }

    run_env = build_better_agent_run_env(
        backend_url="http://backend",
        internal_token="token",
        app_session_id="sid",
        cwd="/repo",
        model="model",
        provider_id="provider",
        bare_config=False,
        user_facing=True,
        disabled_builtin_extensions=["b", "a"],
    )
    for suffix in (
        "BACKEND_URL",
        "APP_SESSION_ID",
        "CWD",
        "MODEL",
        "PROVIDER_ID",
        "BARE_CONFIG",
        "USER_FACING",
        "DISABLED_BUILTIN_EXTENSIONS",
    ):
        assert run_env[f"BETTER_AGENT_{suffix}"] == run_env[f"BETTER_CLAUDE_{suffix}"]
    assert "BETTER_AGENT_INTERNAL_TOKEN" not in run_env
    assert "BETTER_CLAUDE_INTERNAL_TOKEN" not in run_env
    assert run_env["BETTER_AGENT_DISABLED_BUILTIN_EXTENSIONS"] == "a,b"


_UNIT_TESTS = (
    test_agent_env_name_maps_prefix,
    test_agent_env_name_rejects_non_prefixed,
    test_get_env_prefers_agent_over_legacy,
    test_get_env_falls_back_to_legacy_when_agent_unset,
    test_get_env_empty_agent_string_falls_through_to_legacy,
    test_get_env_uses_default_when_unset,
    test_get_env_stripped_trims_whitespace,
    test_get_env_stripped_default_when_unset,
    test_require_env_returns_value_when_present,
    test_require_env_raises_when_missing,
    test_dual_env_renders_both_keys_as_strings,
    test_dual_env_raw_keeps_native_type,
    test_dual_env_many_merges_pairs,
)


def main() -> int:
    _provider_run_env_check()
    for test in _UNIT_TESTS:
        test()
    print("PASS env compatibility")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
