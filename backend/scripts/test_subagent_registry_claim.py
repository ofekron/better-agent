"""Test that _SubagentRegistry.claim matches named subagents whose meta
agentType differs from the tool-name fallback.

Bug: when an Agent tool_use omits ``subagent_type`` from its input,
``register`` falls back to the tool name ("Agent"). But the meta file
carries the real ``agentType`` (e.g. "general-purpose"). The old claim
did exact match on both fields, so these named subagents were never
matched and their events were silently dropped.

Reproduces the adv-reviewer gap: call_87e2fdc579454fd09cb2ca74 registered
as subagent_type="Agent" but the meta file said agentType="general-purpose".
"""
import pytest
from claude_jsonl_enrich import _SubagentRegistry


def test_exact_match_still_works():
    """Explicit subagent_type matches meta agentType (pre-existing behavior)."""
    reg = _SubagentRegistry()
    reg.register("call_explore_1", "Explore", "Find files")
    assert reg.claim("Explore", "Find files") == "call_explore_1"


def test_named_agent_matches_by_description_fallback():
    """Agent tool without subagent_type → fallback "Agent" → matches
    meta's agentType="general-purpose" via description-only fallback."""
    reg = _SubagentRegistry()
    reg.register("call_87e2fdc579454fd09cb2ca74", "Agent",
                 "Adversarial review of DB abstraction")
    assert reg.claim("general-purpose",
                     "Adversarial review of DB abstraction") == "call_87e2fdc579454fd09cb2ca74"


def test_named_task_matches_by_description_fallback():
    """Same for Task tool — fallback "Task" matches any agentType."""
    reg = _SubagentRegistry()
    reg.register("call_task_1", "Task", "Run migration")
    assert reg.claim("general-purpose", "Run migration") == "call_task_1"


def test_no_match_wrong_description():
    """Description mismatch → no claim, even with tool-name type."""
    reg = _SubagentRegistry()
    reg.register("call_1", "Agent", "Review code")
    assert reg.claim("general-purpose", "Different description") is None


def test_no_cross_steal_between_same_type():
    """Two pending Agents with different descriptions — claim matches
    the right one, doesn't steal from the other."""
    reg = _SubagentRegistry()
    reg.register("call_adv", "Agent", "Adversarial review")
    reg.register("call_fix", "Agent", "Fix all bugs")
    assert reg.claim("general-purpose", "Fix all bugs") == "call_fix"
    assert reg.claim("general-purpose", "Adversarial review") == "call_adv"


def test_exact_match_takes_priority_over_fallback():
    """If both exact and fallback could match, exact wins."""
    reg = _SubagentRegistry()
    reg.register("call_exact", "general-purpose", "Review")
    reg.register("call_fallback", "Agent", "Review")
    # Exact match on first entry wins
    assert reg.claim("general-purpose", "Review") == "call_exact"
    # Fallback match on remaining entry
    assert reg.claim("general-purpose", "Review") == "call_fallback"


def test_unclaimed_stays_in_queue():
    """Unmatched entries stay for later claims."""
    reg = _SubagentRegistry()
    reg.register("call_1", "Agent", "Do X")
    reg.register("call_2", "Explore", "Find Y")
    # Only Explore matches
    assert reg.claim("Explore", "Find Y") == "call_2"
    # Agent still pending
    assert reg.claim("general-purpose", "Do X") == "call_1"


def test_real_scenario_adv_reviewer():
    """Full reproduction of the reported bug scenario."""
    reg = _SubagentRegistry()

    # Simulate the sequence from events.jsonl:
    # 1. Primary agent spawns adv-reviewer (no subagent_type → "Agent")
    reg.register("call_87e2fdc579454fd09cb2ca74", "Agent",
                 "Adversarial review of DB abstraction")

    # 2. Primary agent spawns verification subagents (explicit subagent_type)
    reg.register("call_48bdef62101b4eac94706828", "Explore",
                 "Verify finding #1 pymysql import")
    reg.register("call_3388c318b0ae4efa9e1f2ecb", "Explore",
                 "Verify finding #2 execute_many")
    reg.register("call_e5b43f976f39478789415ce9", "Explore",
                 "Verify finding #3 trigger issue")
    reg.register("call_3b0f8bab8b0f4c6aafed9c8a", "Explore",
                 "Verify findings #4-#14")

    # 3. Primary agent spawns re-review (no subagent_type → "Agent")
    reg.register("call_06aaaa9b34074d4092c23064", "Agent",
                 "Re-review after fixes applied")

    # Now simulate claims from meta files (order may vary):

    # Explore subagents claim by exact match
    assert reg.claim("Explore", "Verify finding #1 pymysql import") == "call_48bdef62101b4eac94706828"
    assert reg.claim("Explore", "Verify finding #2 execute_many") == "call_3388c318b0ae4efa9e1f2ecb"
    assert reg.claim("Explore", "Verify finding #3 trigger issue") == "call_e5b43f976f39478789415ce9"
    assert reg.claim("Explore", "Verify findings #4-#14") == "call_3b0f8bab8b0f4c6aafed9c8a"

    # The bug: adv-reviewer meta has agentType="general-purpose" but
    # was registered as "Agent". Old code returned None here.
    assert reg.claim("general-purpose",
                     "Adversarial review of DB abstraction") == "call_87e2fdc579454fd09cb2ca74"

    # Re-review also uses fallback match
    assert reg.claim("general-purpose",
                     "Re-review after fixes applied") == "call_06aaaa9b34074d4092c23064"

    # Queue should be empty now
    assert reg.claim("general-purpose", "anything") is None
