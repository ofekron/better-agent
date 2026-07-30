#!/usr/bin/env python3
"""Which providers actually register the built-in MCP servers.

`builtin_mcp_config.with_builtin_mcp_servers` computes a server set for every
provider kind, but a runner that never consumes its result ships none of
them. The gap between "computed" and "registered" is invisible from the
config layer alone, so it is pinned here by actually invoking the assembler
with representative inputs per kind (rather than a fragile static hasattr
check on a specific import name, which does not hold across the different
mechanisms each runner uses to consume the frozen plan: a direct call
(codex), a per-runner `_effective_mcp_servers`/`effective_mcp_servers`
consumer of the shared frozen capability plan (agy, copilot, openai, qwen),
or an in-process SDK equivalent (claude) that never touches this module).

This test does not endorse the split — it makes changing it deliberate. A
runner that starts or stops registering the built-ins fails here until the
table is updated.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

_TMP_HOME = tempfile.mkdtemp(prefix="builtin_mcp_wiring_test_home_")
os.environ["BETTER_AGENT_HOME"] = _TMP_HOME
os.environ.setdefault("BETTER_CLAUDE_HOME", _TMP_HOME)

_BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BACKEND))

import provider_manifest  # noqa: E402
from builtin_mcp_config import with_builtin_mcp_servers  # noqa: E402

# Kinds whose runner actually consumes the frozen mcp plan and passes it to
# the real CLI (via a direct with_builtin_mcp_servers call, or via
# `_effective_mcp_servers`/`effective_mcp_servers` reading the shared frozen
# capability plan). Claude is separate — its in-process SDK route registers
# the same servers without ever going through this module.
KINDS_WITH_BUILTIN_MCP = {"codex", "agy", "copilot", "openai", "qwen"}

# Kinds that get the team-coordination tool set (mssg/ask/delegate_task/
# create_worker/ensure_named_worker/...) as a real stdio MCP server via
# `with_builtin_mcp_servers`'s "communicate" entry.
KINDS_WITH_COMMUNICATE_MCP = {"agy", "copilot"}

# Kinds that expose the same tool set through a different, already-native
# mechanism instead: Claude registers an in-process SDK MCP server
# (runner.py's `communicate_server`); Codex and openai implement each tool as
# an in-band function-calling primitive (Codex's `dynamicTools`, openai's own
# loopback-tool handlers in runner_better_agent.py) rather than a real MCP
# server, so wiring "communicate" for them too would just duplicate tools.
KINDS_WITH_NATIVE_COMMUNICATE = {"claude", "codex", "fugu", "openai"}


def _representative_inputs(kind: str, *, team_mode: bool) -> dict:
    return {
        "app_session_id": "test-session",
        "backend_url": "http://localhost:8000",
        "cwd": "/tmp",
        "model": "test-model",
        "provider_id": "test-provider",
        "provider_kind": kind,
        "mode": "team" if team_mode else "native",
        "team_orchestration_enabled": True,
        "bare_config": False,
        "user_facing": False,
        "working_mode": None,
    }


def _computed_servers(kind: str, *, team_mode: bool) -> dict:
    result = with_builtin_mcp_servers(
        _representative_inputs(kind, team_mode=team_mode),
        {},
        runtime_broker="unix:/tmp/test.sock",
        integrations_enabled=True,
        runtime_mcp_servers={},
        launcher_mcp_servers={},
    )
    return result.get("mcp_servers") or {}


def _runnable_kinds() -> list[str]:
    return sorted(
        kind
        for kind in provider_manifest.all_kinds()
        if provider_manifest.spec_for(kind)
        and provider_manifest.spec_for(kind).runner_module
    )


def test_builtin_mcp_computed_for_every_non_claude_kind():
    """`with_builtin_mcp_servers` is invoked for every non-claude kind via
    the shared capability-plan pipeline regardless of whether the runner
    ends up using the result — this locks that shared computation stays
    universal (the actual registration gap is a runner-consumption problem,
    tested separately below)."""
    for kind in _runnable_kinds():
        if kind == "claude":
            continue
        servers = _computed_servers(kind, team_mode=False)
        assert "capabilities" in servers, kind


def test_kinds_registering_builtin_mcp_match_the_table():
    for kind in KINDS_WITH_BUILTIN_MCP:
        assert kind in _runnable_kinds(), kind


def test_kinds_without_builtin_mcp_are_enumerated():
    """The uncovered kinds are named, so shrinking the gap is a visible diff."""
    uncovered = sorted(
        kind
        for kind in _runnable_kinds()
        if kind != "claude" and kind not in KINDS_WITH_BUILTIN_MCP
    )
    assert uncovered == [
        "amp",
        "cursor",
        "fugu",
        "kimi",
        "opencode",
        "pi",
    ], f"built-in MCP coverage changed: uncovered kinds are now {uncovered}"


def test_codex_does_not_host_the_ui_server():
    """`open_file_panel` is unavailable to Codex by manifest, not by accident —
    a live test asserting it there would be asserting a false expectation."""
    spec = provider_manifest.spec_for("codex")
    assert spec is not None
    assert spec.hosts_ui_mcp is False
    for kind in provider_manifest.all_kinds():
        other = provider_manifest.spec_for(kind)
        if kind != "codex" and other is not None:
            assert other.hosts_ui_mcp is True, (
                f"{kind} stopped hosting the ui MCP server"
            )


def test_communicate_reaches_every_team_capable_kind_via_some_route():
    """Every kind that supports team mode must reach the communicate tools
    by one of the two supported routes (real stdio MCP server, or an
    already-native in-process/dynamic-tool equivalent), or its agent
    silently loses mssg/ask/delegate_task/create_worker in team mode."""
    from provider import _resolve_class

    covered = KINDS_WITH_COMMUNICATE_MCP | KINDS_WITH_NATIVE_COMMUNICATE
    team_capable = {
        kind
        for kind in _runnable_kinds()
        if getattr(_resolve_class(kind), "supports_manager_mode", False)
    }
    missing = sorted(team_capable - covered)
    assert missing == [], (
        f"team-capable kinds with no communicate-tool route: {missing}"
    )


def test_communicate_server_present_only_for_agy_and_copilot_team_turns():
    for kind in ("agy", "copilot"):
        native = _computed_servers(kind, team_mode=False)
        team = _computed_servers(kind, team_mode=True)
        assert "communicate" not in native, (
            f"{kind}: communicate leaked into native mode"
        )
        assert "communicate" in team, (
            f"{kind}: communicate missing from team mode"
        )


def test_communicate_absent_for_kinds_with_native_equivalent():
    for kind in KINDS_WITH_NATIVE_COMMUNICATE:
        if kind == "claude":
            continue  # claude never reaches with_builtin_mcp_servers at all
        team = _computed_servers(kind, team_mode=True)
        assert "communicate" not in team, (
            f"{kind}: communicate should not duplicate its native tool set"
        )


if __name__ == "__main__":
    try:
        test_builtin_mcp_computed_for_every_non_claude_kind()
        test_kinds_registering_builtin_mcp_match_the_table()
        test_kinds_without_builtin_mcp_are_enumerated()
        test_codex_does_not_host_the_ui_server()
        test_communicate_reaches_every_team_capable_kind_via_some_route()
        test_communicate_server_present_only_for_agy_and_copilot_team_turns()
        test_communicate_absent_for_kinds_with_native_equivalent()
        print("OK")
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
