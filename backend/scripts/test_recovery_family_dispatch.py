"""Locks the single recovery-replay dispatch (run_recovery._replay_for_family
+ _recovery_family) introduced to replace the two duplicated kind dispatches.

Key regressions:
- openai routes through the GEMINI reader even though OpenAIProvider subclasses
  Provider (not GeminiProvider) — dispatch is by manifest family, NOT class MRO.
- codex routes through the rollout reader (carrying context_window) at BOTH the
  full-recovery and the rate-limit-check sites (the latter used to fall through
  to the claude reader).
- claude carries the unmatched orphan-subagent list; gemini-family carries
  neither context_window nor unmatched.

Uses a temp BETTER_AGENT_HOME so no real session state is touched.
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP_HOME = tempfile.mkdtemp(prefix="recovery_family_test_home_")
os.environ["BETTER_AGENT_HOME"] = _TMP_HOME
os.environ.setdefault("BETTER_CLAUDE_HOME", _TMP_HOME)

_BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BACKEND))

import run_recovery as rr  # noqa: E402


def test_recovery_family_resolution():
    assert rr._recovery_family({"provider_kind": "codex"}) == "codex"
    assert rr._recovery_family({"provider_kind": "openai"}) == "gemini"
    assert rr._recovery_family({"provider_kind": "agy"}) == "gemini"
    assert rr._recovery_family({"provider_kind": "copilot"}) == "gemini"
    assert rr._recovery_family({"provider_kind": "gemini"}) == "gemini"
    assert rr._recovery_family({"provider_kind": "claude"}) == "claude"
    # fugu currently uses the claude reader (pre-existing, flagged)
    assert rr._recovery_family({"provider_kind": "fugu"}) == "claude"
    # unknown / missing fall back to the claude reader
    assert rr._recovery_family({"provider_kind": "nope"}) == "claude"
    assert rr._recovery_family(None) == "claude"


def _patch_readers():
    calls = {}

    def _gemini(rd):
        calls["gemini"] = rd
        return [{"e": "g"}]

    def _codex(rd):
        calls["codex"] = rd
        return [{"e": "c"}], 4096

    def _claude(rd, *, unmatched_out=None):
        calls["claude"] = rd
        if unmatched_out is not None:
            unmatched_out.append({"orphan": 1})
        return [{"e": "cl"}]

    rr._replay_from_gemini_jsonl = _gemini
    rr._replay_from_codex_rollout = _codex
    rr._replay_from_claude_jsonl = _claude
    return calls


def test_dispatch_routes_and_preserves_extras():
    orig = (rr._replay_from_gemini_jsonl, rr._replay_from_codex_rollout, rr._replay_from_claude_jsonl)
    calls = _patch_readers()
    try:
        rd = Path("/tmp/x")
        # openai → gemini reader (NOT claude) despite Provider parent class
        g = rr._replay_for_family("gemini", rd)
        assert "gemini" in calls and g.events == [{"e": "g"}]
        assert g.context_window is None and g.unmatched == []

        # codex → rollout reader, context_window preserved
        c = rr._replay_for_family("codex", rd)
        assert "codex" in calls and c.events == [{"e": "c"}]
        assert c.context_window == 4096 and c.unmatched == []

        # claude → claude reader, unmatched orphan list preserved
        cl = rr._replay_for_family("claude", rd)
        assert cl.events == [{"e": "cl"}]
        assert cl.unmatched == [{"orphan": 1}] and cl.context_window is None
    finally:
        (rr._replay_from_gemini_jsonl, rr._replay_from_codex_rollout, rr._replay_from_claude_jsonl) = orig


if __name__ == "__main__":
    test_recovery_family_resolution()
    test_dispatch_routes_and_preserves_extras()
    print("ok")
