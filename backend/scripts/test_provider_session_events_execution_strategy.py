from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from scripts import _test_home  # noqa: E402

TEST_HOME = _test_home.TestHome.acquire("ba-test-session-events-strategy-")

from codex_execution_common import ExecutionContractError  # noqa: E402
from provider_manifest import session_events_family_kinds  # noqa: E402
from provider_session_events_execution_strategy import (  # noqa: E402
    _COMMON_INPUT_FIELDS,
    _POLICY_FIELDS,
    _PROJECTION_FIELDS,
    _STRATEGIES,
    SessionEventsExecutionStrategy,
    _fields,
    external_execution_kinds,
    strategy_for,
)


# --------------------------------------------------------------------------- #
# external_execution_kinds
# --------------------------------------------------------------------------- #


def test_external_execution_kinds_is_family_minus_agy():
    kinds = external_execution_kinds()
    assert isinstance(kinds, frozenset)
    assert kinds == session_events_family_kinds() - {"agy"}
    assert "agy" not in kinds


def test_external_execution_kinds_contains_every_registered_strategy():
    kinds = external_execution_kinds()
    assert set(_STRATEGIES) <= kinds


# --------------------------------------------------------------------------- #
# strategy_for
# --------------------------------------------------------------------------- #


def test_strategy_for_round_trips_every_valid_kind():
    for kind in external_execution_kinds() & set(_STRATEGIES):
        assert strategy_for(kind) is _STRATEGIES[kind]
        assert strategy_for(kind).kind == kind


def test_strategy_for_rejects_excluded_family_member_agy():
    # "agy" is a session_events family member but deliberately excluded from
    # the external set and absent from _STRATEGIES.
    with pytest.raises(ExecutionContractError):
        strategy_for("agy")


def test_strategy_for_rejects_unknown_kind():
    with pytest.raises(ExecutionContractError):
        strategy_for("not-a-real-execution-kind")


# --------------------------------------------------------------------------- #
# SessionEventsExecutionStrategy dataclass
# --------------------------------------------------------------------------- #


def test_strategy_defaults_are_off():
    strat = SessionEventsExecutionStrategy(
        "x", "x", ".x", (), frozenset({"prompt"})
    )
    assert strat.permission is False
    assert strat.resume_kind == ""


def test_strategy_is_frozen():
    strat = SessionEventsExecutionStrategy(
        "x", "x", ".x", (), frozenset({"prompt"})
    )
    with pytest.raises(Exception):
        strat.kind = "y"  # type: ignore[misc]


def test_every_strategy_kind_matches_its_registry_key():
    for key, strat in _STRATEGIES.items():
        assert strat.kind == key


def test_cli_command_is_none_only_for_openai():
    none_cli = {k for k, s in _STRATEGIES.items() if s.cli_command is None}
    assert none_cli == {"openai"}


# --------------------------------------------------------------------------- #
# _fields
# --------------------------------------------------------------------------- #


def test_fields_with_no_extras_is_common_union_policy_union_projection():
    assert _fields() == _COMMON_INPUT_FIELDS | _POLICY_FIELDS | _PROJECTION_FIELDS


def test_fields_adds_extras():
    base = _COMMON_INPUT_FIELDS | _POLICY_FIELDS | _PROJECTION_FIELDS
    assert _fields("fork") == base | {"fork"}
    assert _fields("fork", "reasoning_effort") == base | {"fork", "reasoning_effort"}


def test_fields_returns_fresh_frozenset():
    # No shared mutable state between calls.
    a = _fields("fork")
    b = _fields("reasoning_effort")
    assert "fork" in a and "fork" not in b
    assert "reasoning_effort" in b and "reasoning_effort" not in a


# --------------------------------------------------------------------------- #
# Specific strategy declarations (lock the contract)
# --------------------------------------------------------------------------- #


def test_amp_strategy_contract():
    s = _STRATEGIES["amp"]
    assert s.cli_command == "amp"
    assert s.config_default == ".config/amp"
    assert s.config_files == ("settings.json",)
    assert s.permission is True
    assert s.resume_kind == ""
    assert s.input_fields == _fields("fork")


def test_copilot_strategy_contract():
    s = _STRATEGIES["copilot"]
    assert s.cli_command == "copilot"
    assert s.config_default == ".copilot"
    assert s.config_files == ()
    assert s.permission is False
    assert s.resume_kind == "copilot"
    assert s.input_fields == _fields("config_dir")


def test_cursor_strategy_contract():
    s = _STRATEGIES["cursor"]
    assert s.cli_command == "cursor-agent"
    assert s.config_default == ".cursor"
    assert s.config_files == ("cli-config.json", "mcp.json")
    assert s.permission is True
    assert s.input_fields == _fields()


def test_kimi_strategy_contract():
    s = _STRATEGIES["kimi"]
    assert s.cli_command == "kimi"
    assert s.config_default == ".kimi"
    assert s.config_files == ("config.toml", "kimi.json", "mcp.json")
    assert s.permission is False
    assert s.resume_kind == ""


def test_openai_strategy_contract():
    s = _STRATEGIES["openai"]
    assert s.cli_command is None
    assert s.config_default == ""
    assert s.config_files == ()
    assert s.permission is True
    assert s.resume_kind == "openai"
    assert s.input_fields == _fields(
        "continuation_chain",
        "disallowed_tools",
        "fork",
        "mssg_sender_session_id",
        "reasoning_effort",
        "setting_sources",
        "supervised",
        "supervisor_agent_session_id",
    )


def test_opencode_strategy_contract():
    s = _STRATEGIES["opencode"]
    assert s.cli_command == "opencode"
    assert s.config_default == "."
    assert s.config_files == (
        ".config/opencode/opencode.jsonc",
        ".local/share/opencode/opencode.db",
    )
    assert s.permission is True
    assert s.input_fields == _fields("fork", "mssg_sender_session_id", "reasoning_effort")


def test_pi_strategy_contract():
    s = _STRATEGIES["pi"]
    assert s.cli_command == "pi"
    assert s.config_default == "."
    assert s.config_files == (".pi/agent/auth.json", ".pi/agent/models.json")
    assert s.permission is True
    assert s.resume_kind == "pi"
    assert s.input_fields == _fields("fork", "reasoning_effort", "supervised")


def test_qwen_strategy_contract():
    s = _STRATEGIES["qwen"]
    assert s.cli_command == "qwen"
    assert s.config_default == ".qwen"
    assert s.config_files == ("settings.json", "oauth_creds.json", "output-language.md")
    assert s.permission is True
    assert s.input_fields == _fields("provider_mode", "reasoning_effort", "supervised")
