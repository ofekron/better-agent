"""Regression tests: bash/subagent background execution is forbidden on
every claude run.

The per-turn runner process must be able to die at turn end without
orphaning or killing user work, so claude must never start bash/subagent
work that outlives the turn. Three fail-closed layers (single source:
runs_dir.BACKGROUND_WORK_TOOLS and the *_ENV names):
  1. build_env sets the CLI's native master switch
     (CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1), disables cross-exit bg
     adoption, and strips opt-in auto-backgrounding.
  2. every input.json disallowed_tools carries the background-interaction
     tools (plus timer tools), including under a session-level override.
  3. the runner's PreToolUse hook denies any tool input that still
     requests run_in_background / remote isolation.

CLI-internal Workflow tasks are the deliberate exception (the runner
drains them before finalizing — see test_runner_workflow_drain.py), so
the model-facing task tools (runs_dir.TASK_INTERACTION_TOOLS) must NOT
be stripped.

Run with:
    cd backend && .venv/bin/python scripts/test_no_background_policy.py
"""
from __future__ import annotations

import asyncio
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home  # noqa: E402
_test_home.isolate("bc-test-no-bg-policy-")

from runs_dir import (  # noqa: E402
    AUTO_BACKGROUND_ENV,
    BACKGROUND_TASKS_DISABLE_ENV,
    BACKGROUND_WORK_TOOLS,
    BG_EXIT_HANDOFF_DISABLE_ENV,
    TASK_INTERACTION_TOOLS,
    TIMER_TOOLS,
)

PASS = "\x1b[32mPASS\x1b[0m"


def _mk_provider():
    from provider_claude import ClaudeProvider
    return ClaudeProvider({"id": "test-no-bg"})


def test_build_env_disables_background() -> None:
    provider = _mk_provider()
    os.environ[AUTO_BACKGROUND_ENV] = "1"  # hostile ambient opt-in
    try:
        env = provider.build_env()
    finally:
        os.environ.pop(AUTO_BACKGROUND_ENV, None)
    assert env.get(BACKGROUND_TASKS_DISABLE_ENV) == "1", (
        f"build_env must set {BACKGROUND_TASKS_DISABLE_ENV}=1")
    assert env.get(BG_EXIT_HANDOFF_DISABLE_ENV) == "1", (
        f"build_env must set {BG_EXIT_HANDOFF_DISABLE_ENV}=1")
    assert AUTO_BACKGROUND_ENV not in env, (
        f"build_env must strip ambient {AUTO_BACKGROUND_ENV}")
    print(f"{PASS} build_env disables background execution natively")


def _payload_disallowed(provider, disallowed_tools) -> list[str]:
    payload, _bare, _mode, _url = provider._build_input_payload(
        prompt="hi", images=None, files=None, cwd="/tmp", model="sonnet",
        reasoning_effort=None, session_id=None, mode="native",
        app_session_id="00000000-0000-0000-0000-000000000001",
        source="cli", disallowed_tools=disallowed_tools,
        setting_sources=None, backend_url="http://127.0.0.1:1",
        internal_token="t", fork=False, supervised=False,
        supervisor_agent_session_id=None, worker_agent_session_id=None,
        mssg_sender_session_id=None, is_worker=False,
        browser_harness_enabled=False, user_facing=False,
        continuation_chain=None, provider_run_config=None,
        capability_contexts=None, target_message_id=None, turn_run_id=None,
        disabled_builtin_extensions=None, provisioned_tool_profile="",
    )
    return payload["disallowed_tools"]


def test_payload_always_strips_bg_tools() -> None:
    provider = _mk_provider()
    for label, override in (
        ("default", None),
        ("session-override", ["SomeCustomTool"]),
    ):
        tools = _payload_disallowed(provider, override)
        missing = [
            n for n in (*BACKGROUND_WORK_TOOLS, *TIMER_TOOLS)
            if n not in tools
        ]
        assert not missing, f"{label} payload missing strips: {missing}"
    print(f"{PASS} input.json always strips background + timer tools")


def test_payload_keeps_task_interaction_tools() -> None:
    """Workflow support: the model must keep TaskOutput/TaskStop so it
    can read and stop the CLI's background Workflow tasks. The provider
    must never strip them on its own — under any override."""
    provider = _mk_provider()
    for label, override in (
        ("default", None),
        ("session-override", ["SomeCustomTool"]),
    ):
        tools = _payload_disallowed(provider, override)
        stripped = [n for n in TASK_INTERACTION_TOOLS if n in tools]
        assert not stripped, f"{label} payload must not strip {stripped}"
    print(f"{PASS} input.json keeps task-interaction tools available")


def test_hook_denies_background_input() -> None:
    from runner import _deny_background_tool_use

    def run(tool_input):
        return asyncio.run(_deny_background_tool_use(
            {"tool_name": "Bash", "tool_input": tool_input}, None, None,
        ))

    for label, tool_input in (
        ("Bash run_in_background", {"command": "sleep 99", "run_in_background": True}),
        ("Task run_in_background", {"prompt": "x", "run_in_background": True}),
        ("Agent remote isolation", {"prompt": "x", "isolation": "remote"}),
    ):
        out = run(tool_input)
        decision = (out.get("hookSpecificOutput") or {}).get("permissionDecision")
        assert decision == "deny", f"hook must deny {label}, got {out!r}"

    for label, benign in (
        ("foreground bash", {"command": "ls"}),
        ("explicit false", {"command": "ls", "run_in_background": False}),
        ("worktree isolation", {"prompt": "x", "isolation": "worktree"}),
        ("empty input", {}),
    ):
        out = run(benign)
        assert out == {}, f"hook must not touch {label}, got {out!r}"
    print(f"{PASS} PreToolUse hook denies bg/remote, passes foreground")


def test_hooks_wired_into_options() -> None:
    from runner import _background_policy_hooks
    hooks = _background_policy_hooks()
    matchers = hooks.get("PreToolUse") or []
    assert matchers and matchers[0].hooks, "PreToolUse policy hook not built"
    assert matchers[0].matcher is None, "policy hook must match ALL tools (matcher=None)"
    print(f"{PASS} policy hook covers all tools via PreToolUse")


def main() -> int:
    test_build_env_disables_background()
    test_payload_always_strips_bg_tools()
    test_payload_keeps_task_interaction_tools()
    test_hook_denies_background_input()
    test_hooks_wired_into_options()
    return 0


if __name__ == "__main__":
    sys.exit(main())
