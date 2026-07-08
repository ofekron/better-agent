"""Regression tests: background execution is forbidden on every claude run.

The per-turn runner process must be able to die at turn end without
orphaning or killing user work, so claude must never start work that
outlives the turn. Three fail-closed layers (single source:
runs_dir.BACKGROUND_WORK_TOOLS and the *_ENV names):
  1. build_env sets the CLI's native master switch
     (CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1), disables cross-exit bg
     adoption, and strips opt-in auto-backgrounding.
  2. every input.json disallowed_tools carries the background-interaction
     tools (plus timer tools), including under a session-level override.
  3. the runner's PreToolUse hook denies any tool input that still
     requests run_in_background / remote isolation.

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
    TIMER_TOOLS,
)

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _mk_provider():
    from provider_claude import ClaudeProvider
    return ClaudeProvider({"id": "test-no-bg"})


def test_build_env_disables_background() -> bool:
    provider = _mk_provider()
    os.environ[AUTO_BACKGROUND_ENV] = "1"  # hostile ambient opt-in
    try:
        env = provider.build_env()
    finally:
        os.environ.pop(AUTO_BACKGROUND_ENV, None)
    ok = True
    if env.get(BACKGROUND_TASKS_DISABLE_ENV) != "1":
        print(f"{FAIL} build_env must set {BACKGROUND_TASKS_DISABLE_ENV}=1")
        ok = False
    if env.get(BG_EXIT_HANDOFF_DISABLE_ENV) != "1":
        print(f"{FAIL} build_env must set {BG_EXIT_HANDOFF_DISABLE_ENV}=1")
        ok = False
    if AUTO_BACKGROUND_ENV in env:
        print(f"{FAIL} build_env must strip ambient {AUTO_BACKGROUND_ENV}")
        ok = False
    if ok:
        print(f"{PASS} build_env disables background execution natively")
    return ok


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
        browser_harness_enabled=False, open_file_panel_enabled=False,
        continuation_chain=None, provider_run_config=None,
        capability_contexts=None, target_message_id=None, turn_run_id=None,
        disabled_builtin_extensions=None, provisioned_tool_profile="",
    )
    return payload["disallowed_tools"]


def test_payload_always_strips_bg_tools() -> bool:
    provider = _mk_provider()
    ok = True
    for label, override in (
        ("default", None),
        ("session-override", ["SomeCustomTool"]),
    ):
        tools = _payload_disallowed(provider, override)
        missing = [
            n for n in (*BACKGROUND_WORK_TOOLS, *TIMER_TOOLS)
            if n not in tools
        ]
        if missing:
            print(f"{FAIL} {label} payload missing strips: {missing}")
            ok = False
    if ok:
        print(f"{PASS} input.json always strips background + timer tools")
    return ok


def test_hook_denies_background_input() -> bool:
    from runner import _deny_background_tool_use

    def run(tool_input):
        return asyncio.run(_deny_background_tool_use(
            {"tool_name": "Bash", "tool_input": tool_input}, None, None,
        ))

    ok = True
    for label, tool_input in (
        ("Bash run_in_background", {"command": "sleep 99", "run_in_background": True}),
        ("Task run_in_background", {"prompt": "x", "run_in_background": True}),
        ("Agent remote isolation", {"prompt": "x", "isolation": "remote"}),
    ):
        out = run(tool_input)
        decision = (out.get("hookSpecificOutput") or {}).get("permissionDecision")
        if decision != "deny":
            print(f"{FAIL} hook must deny {label}, got {out!r}")
            ok = False

    for label, benign in (
        ("foreground bash", {"command": "ls"}),
        ("explicit false", {"command": "ls", "run_in_background": False}),
        ("worktree isolation", {"prompt": "x", "isolation": "worktree"}),
        ("empty input", {}),
    ):
        out = run(benign)
        if out != {}:
            print(f"{FAIL} hook must not touch {label}, got {out!r}")
            ok = False
    if ok:
        print(f"{PASS} PreToolUse hook denies bg/remote, passes foreground")
    return ok


def test_hooks_wired_into_options() -> bool:
    from runner import _background_policy_hooks
    hooks = _background_policy_hooks()
    matchers = hooks.get("PreToolUse") or []
    if not matchers or not matchers[0].hooks:
        print(f"{FAIL} PreToolUse policy hook not built")
        return False
    if matchers[0].matcher is not None:
        print(f"{FAIL} policy hook must match ALL tools (matcher=None)")
        return False
    print(f"{PASS} policy hook covers all tools via PreToolUse")
    return True


def main() -> int:
    results = [
        test_build_env_disables_background(),
        test_payload_always_strips_bg_tools(),
        test_hook_denies_background_input(),
        test_hooks_wired_into_options(),
    ]
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
