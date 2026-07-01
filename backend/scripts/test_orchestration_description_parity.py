"""Locks the single-source-of-truth for orchestration-tool descriptions.

The descriptions of mssg / ask / delegate_task / create_session /
create_sub_session / create_worker live in ONE module
(orchestration_tool_descriptions) and are consumed identically by all three
provider runners:
  - Claude  -> runner.py
  - Codex   -> runner_codex.py
  - Gemini  -> communicate_mcp.py (FastMCP)

This test fails if any provider forks its own copy, if a description goes
empty (the historical Gemini defect), or if the key disambiguator that keeps
each tool distinct from its neighbours is edited away.

Run: python backend/scripts/test_orchestration_description_parity.py
"""
import os
import sys
import tempfile

# Isolate state dir BEFORE importing backend modules (project rule).
import _test_home
_test_home.isolate("bc_desc_parity_")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import orchestration_tool_descriptions as otd  # noqa: E402
import runner  # noqa: E402
import runner_codex  # noqa: E402
import communicate_mcp  # noqa: E402

# tool name -> (shared constant, [substrings that MUST survive any edit])
SPEC = {
    "mssg": (otd.MSSG_DESCRIPTION, ["backend accepts", "final report"]),
    "ask": (
        otd.ASK_DESCRIPTION,
        ["wait_and_grab_last_mssg_in_turn", "continue_and_expect_mssg_back_async", "fork",
         "delegate_to_session"],
    ),
    "delegate_task": (
        otd.DELEGATE_TASK_DESCRIPTION,
        ["DETACHED", "does NOT hold your turn", "delegate_to_session"],
    ),
    "create_session": (otd.CREATE_SESSION_DESCRIPTION, ["STANDALONE", "create_worker"]),
    "create_sub_session": (otd.CREATE_SUB_SESSION_DESCRIPTION, ["hidden"]),
    "create_worker": (otd.CREATE_WORKER_DESCRIPTION, ["TEAM", "approval"]),
    "ensure_named_worker": (
        otd.ENSURE_NAMED_WORKER_DESCRIPTION,
        ["Idempotently", "singleton", "STABLE, REUSABLE"],
    ),
}

# The `_`-prefixed alias each runner imports must BE the same object (no fork).
_CLAUDE_ALIASES = {
    "mssg": runner._MSSG_DESCRIPTION,
    "ask": runner._ASK_DESCRIPTION,
    "delegate_task": runner._DELEGATE_TASK_DESCRIPTION,
    "create_session": runner._CREATE_SESSION_DESCRIPTION,
    "create_sub_session": runner._CREATE_SUB_SESSION_DESCRIPTION,
    "create_worker": runner._CREATE_WORKER_DESCRIPTION,
    "ensure_named_worker": runner._ENSURE_NAMED_WORKER_DESCRIPTION,
}
_CODEX_ALIASES = {
    "mssg": runner_codex._MSSG_DESCRIPTION,
    "ask": runner_codex._ASK_DESCRIPTION,
    "delegate_task": runner_codex._DELEGATE_TASK_DESCRIPTION,
    "create_session": runner_codex._CREATE_SESSION_DESCRIPTION,
    "create_sub_session": runner_codex._CREATE_SUB_SESSION_DESCRIPTION,
    "create_worker": runner_codex._CREATE_WORKER_DESCRIPTION,
    "ensure_named_worker": runner_codex._ENSURE_NAMED_WORKER_DESCRIPTION,
}


def _gemini_descriptions() -> dict[str, str]:
    """Build the Gemini stdio FastMCP server and read what the model would see."""
    server = communicate_mcp.build_server()
    return {t.name: (t.description or "") for t in server._tool_manager.list_tools()}


def test_non_empty_and_meaningful():
    for name, (desc, substrings) in SPEC.items():
        assert desc and desc.strip(), f"{name} description is empty"
        for sub in substrings:
            assert sub in desc, f"{name} description lost disambiguator: {sub!r}"


def test_claude_and_codex_share_one_source():
    for name, (shared, _) in SPEC.items():
        assert _CLAUDE_ALIASES[name] is shared, f"Claude forked {name} description"
        assert _CODEX_ALIASES[name] is shared, f"Codex forked {name} description"


def test_gemini_exposes_same_descriptions():
    gem = _gemini_descriptions()
    for name, (shared, _) in SPEC.items():
        assert name in gem, f"Gemini server missing tool {name}"
        assert gem[name] == shared, (
            f"Gemini {name} description diverges from the shared source "
            f"(empty-description defect would reappear here)"
        )


def main() -> int:
    test_non_empty_and_meaningful()
    test_claude_and_codex_share_one_source()
    test_gemini_exposes_same_descriptions()
    print("orchestration description parity: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
