#!/usr/bin/env python3
"""Which providers actually register the built-in MCP servers.

`builtin_mcp_config.with_builtin_mcp_servers` computes a server set for every
provider kind, but a runner that never calls it ships none of them. The gap
between "computed" and "registered" is invisible from the config layer alone,
so it is pinned here: today 4 of the 13 runnable kinds wire the built-ins, and
the other 9 give their agent no `ui`, `open-config-panel`, or `capabilities`
tools at all.

This test does not endorse that split — it makes changing it deliberate. A
runner that starts or stops wiring the built-ins fails here until the table is
updated, and `needs-user-approval-tests` reads the same table to decide which
vendors are worth spending live model quota on.
"""
from __future__ import annotations

import importlib
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

# Runners that call `with_builtin_mcp_servers`, i.e. the only ones whose CLI
# learns about ui / open-config-panel / capabilities. Claude is in the set via
# in-process SDK servers rather than the stdio assembler.
RUNNERS_WITH_BUILTIN_MCP = {
    "runner",
    "runner_gemini",
    "runner_codex",
    "runner_agy",
}

# Runners exposing the communicate tool set as a real MCP server.
RUNNERS_WITH_COMMUNICATE_MCP = {"runner", "runner_gemini"}

# Runners exposing the same tools as per-turn dynamic tools instead.
RUNNERS_WITH_DYNAMIC_COMMUNICATE = {"runner_codex"}


def _runner_modules() -> dict[str, str]:
    out: dict[str, str] = {}
    for kind in provider_manifest.all_kinds():
        spec = provider_manifest.spec_for(kind)
        if spec and spec.runner_module:
            out[kind] = spec.runner_module
    return out


def test_builtin_assembler_callers_match_the_table():
    """A runner imports `with_builtin_mcp_servers` at module scope exactly when
    it is in the table — importing it is what registering the servers requires."""
    for runner_module in sorted(set(_runner_modules().values())):
        module = importlib.import_module(runner_module)
        wires = hasattr(module, "with_builtin_mcp_servers")
        expected = runner_module in RUNNERS_WITH_BUILTIN_MCP
        # Claude builds its servers in-process via the SDK, so it never imports
        # the stdio assembler even though it does register the same servers.
        if runner_module == "runner":
            assert not wires
            continue
        assert wires == expected, (
            f"{runner_module}: wires built-in MCP servers={wires}, "
            f"table says {expected}"
        )


def test_communicate_server_callers_match_the_table():
    for runner_module in sorted(set(_runner_modules().values())):
        if runner_module == "runner":
            continue  # in-process SDK server, no stdio helper to detect
        module = importlib.import_module(runner_module)
        wires = hasattr(module, "_with_communicate_mcp")
        expected = runner_module in RUNNERS_WITH_COMMUNICATE_MCP
        assert wires == expected, (
            f"{runner_module}: wires the communicate MCP server={wires}, "
            f"table says {expected}"
        )


def test_kinds_without_builtin_mcp_are_enumerated():
    """The uncovered kinds are named, so shrinking the gap is a visible diff."""
    uncovered = sorted(
        kind
        for kind, runner_module in _runner_modules().items()
        if runner_module not in RUNNERS_WITH_BUILTIN_MCP
    )
    assert uncovered == [
        "amp",
        "copilot",
        "cursor",
        "kimi",
        "openai",
        "opencode",
        "pi",
        "qwen",
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


def test_communicate_reaches_every_builtin_wired_kind():
    """Every kind that gets the built-ins must reach the communicate tools by
    one of the two supported routes, or its agent silently loses mssg/ask/chat."""
    servers = RUNNERS_WITH_COMMUNICATE_MCP | RUNNERS_WITH_DYNAMIC_COMMUNICATE
    missing = sorted(
        kind
        for kind, runner_module in _runner_modules().items()
        if runner_module in RUNNERS_WITH_BUILTIN_MCP and runner_module not in servers
    )
    assert missing == ["agy"], (
        f"communicate availability changed: kinds with built-ins but no "
        f"communicate route are now {missing}"
    )


if __name__ == "__main__":
    try:
        test_builtin_assembler_callers_match_the_table()
        test_communicate_server_callers_match_the_table()
        test_kinds_without_builtin_mcp_are_enumerated()
        test_codex_does_not_host_the_ui_server()
        test_communicate_reaches_every_builtin_wired_kind()
        print("OK")
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
