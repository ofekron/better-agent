#!/usr/bin/env python3
"""Capability-gated MCP servers must launch through the out-of-process launcher.

Regression: an MCP entrypoint can gate on the per-session active-capability set
via `predicate: {contains: {active_capability_ids: <cap-id>}}` (e.g. testape).
The in-process path (Claude `native_mcp_server_configs`) evaluates the predicate
with `active_capability_ids` present in the run inputs, so it gates correctly.

But CLI providers (Codex/Gemini/Agy) deliver native MCP via the launcher: the
backend builds a launcher entry, and the launcher SUBPROCESS re-resolves the
real config from env via `extension_mcp_launcher._runtime_inputs()`, then the
manifest predicate is re-evaluated. If the active-capability set is not threaded
into the launcher env, the predicate fails closed in the subprocess and the
server refuses to start ("extension MCP unavailable") even though the tool was
advertised — a loaded capability whose MCP never comes up.

This test pins the env round-trip both directions so the two delivery paths stay
consistent.

Run with:
    cd backend && .venv/bin/python scripts/test_launcher_capability_predicate.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import _test_home
_test_home.isolate("ba-launcher-cap-")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import extension_mcp_launcher  # noqa: E402
import extension_store  # noqa: E402


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"PASS {message}")


def test_launcher_env_carries_active_capability_ids() -> None:
    """_native_mcp_launcher_env must emit the active-capability set so the
    launcher subprocess can re-evaluate a `contains` predicate."""
    env = extension_store._native_mcp_launcher_env(
        {
            "backend_url": "http://localhost:8000",
            "app_session_id": "sid-1",
            "active_capability_ids": ["ofek.testape:testape", "x.y:z"],
        }
    )
    # Dual-written (BETTER_AGENT_* and legacy BETTER_CLAUDE_*).
    check(
        env.get("BETTER_AGENT_ACTIVE_CAPABILITY_IDS") == "ofek.testape:testape,x.y:z",
        "launcher env emits BETTER_AGENT_ACTIVE_CAPABILITY_IDS",
    )
    check(
        env.get("BETTER_CLAUDE_ACTIVE_CAPABILITY_IDS") == "ofek.testape:testape,x.y:z",
        "launcher env emits legacy BETTER_CLAUDE_ACTIVE_CAPABILITY_IDS",
    )


def test_launcher_env_empty_when_no_active_capabilities() -> None:
    env = extension_store._native_mcp_launcher_env(
        {"backend_url": "http://localhost:8000", "app_session_id": "sid-1"}
    )
    check(
        env.get("BETTER_AGENT_ACTIVE_CAPABILITY_IDS") == "",
        "launcher env writes empty active-capability set when none active",
    )


def test_launcher_env_carries_provisioned_tool_profile() -> None:
    env = extension_store._native_mcp_launcher_env(
        {
            "backend_url": "http://localhost:8000",
            "app_session_id": "sid-1",
            "provisioned_tool_profile": "requirements_processor",
        }
    )
    check(
        env.get("BETTER_AGENT_PROVISIONED_TOOL_PROFILE") == "requirements_processor",
        "launcher env emits BETTER_AGENT_PROVISIONED_TOOL_PROFILE",
    )
    check(
        env.get("BETTER_CLAUDE_PROVISIONED_TOOL_PROFILE") == "requirements_processor",
        "launcher env emits legacy BETTER_CLAUDE_PROVISIONED_TOOL_PROFILE",
    )


def test_launcher_runtime_inputs_parse_active_capability_ids(monkeypatch=None) -> None:
    """The launcher subprocess must parse the env back into a list so the
    re-resolved predicate sees the same active set the entry was built with."""
    import os

    saved = {
        k: os.environ.get(k)
        for k in (
            "BETTER_AGENT_ACTIVE_CAPABILITY_IDS",
            "BETTER_CLAUDE_ACTIVE_CAPABILITY_IDS",
            "BETTER_AGENT_PROVISIONED_TOOL_PROFILE",
            "BETTER_CLAUDE_PROVISIONED_TOOL_PROFILE",
        )
    }
    try:
        os.environ["BETTER_AGENT_ACTIVE_CAPABILITY_IDS"] = "ofek.testape:testape,a.b:c"
        os.environ["BETTER_AGENT_PROVISIONED_TOOL_PROFILE"] = "requirements_processor"
        os.environ.pop("BETTER_CLAUDE_ACTIVE_CAPABILITY_IDS", None)
        os.environ.pop("BETTER_CLAUDE_PROVISIONED_TOOL_PROFILE", None)
        inputs = extension_mcp_launcher._runtime_inputs()
        check(
            inputs.get("active_capability_ids") == ["ofek.testape:testape", "a.b:c"],
            "launcher _runtime_inputs parses active_capability_ids from env",
        )
        check(
            inputs.get("provisioned_tool_profile") == "requirements_processor",
            "launcher _runtime_inputs parses provisioned_tool_profile from env",
        )

        os.environ["BETTER_AGENT_ACTIVE_CAPABILITY_IDS"] = ""
        inputs = extension_mcp_launcher._runtime_inputs()
        check(
            inputs.get("active_capability_ids") == [],
            "launcher _runtime_inputs yields empty list when env is empty",
        )
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_predicate_round_trips_through_launcher_env() -> None:
    """End-to-end: the active set written by the builder, parsed by the launcher,
    satisfies a `contains` predicate identical to testape's."""
    builder_inputs = {
        "backend_url": "http://localhost:8000",
        "app_session_id": "sid-1",
        "active_capability_ids": ["ofek.testape:testape"],
    }
    env = extension_store._native_mcp_launcher_env(builder_inputs)

    import os

    saved = os.environ.get("BETTER_AGENT_ACTIVE_CAPABILITY_IDS")
    try:
        os.environ["BETTER_AGENT_ACTIVE_CAPABILITY_IDS"] = env[
            "BETTER_AGENT_ACTIVE_CAPABILITY_IDS"
        ]
        os.environ.pop("BETTER_CLAUDE_ACTIVE_CAPABILITY_IDS", None)
        relaunch_inputs = extension_mcp_launcher._runtime_inputs()
        predicate = extension_store._validate_mcp_predicate(
            {"contains": {"active_capability_ids": "ofek.testape:testape"}}
        )
        check(
            extension_store._mcp_predicate_matches(predicate, relaunch_inputs),
            "capability predicate matches after launcher env round-trip (cap active)",
        )

        # Without the capability the same predicate must fail closed.
        os.environ["BETTER_AGENT_ACTIVE_CAPABILITY_IDS"] = ""
        relaunch_inputs = extension_mcp_launcher._runtime_inputs()
        check(
            not extension_store._mcp_predicate_matches(predicate, relaunch_inputs),
            "capability predicate fails closed after round-trip (cap inactive)",
        )
    finally:
        if saved is None:
            os.environ.pop("BETTER_AGENT_ACTIVE_CAPABILITY_IDS", None)
        else:
            os.environ["BETTER_AGENT_ACTIVE_CAPABILITY_IDS"] = saved


def main() -> int:
    test_launcher_env_carries_active_capability_ids()
    test_launcher_env_empty_when_no_active_capabilities()
    test_launcher_env_carries_provisioned_tool_profile()
    test_launcher_runtime_inputs_parse_active_capability_ids()
    test_predicate_round_trips_through_launcher_env()
    print("\nPASS launcher capability predicate round-trip")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
