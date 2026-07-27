from __future__ import annotations

import copy
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import _test_home
_TMP_HOME = _test_home.isolate("bc-harness-snapshot-merge-")

import harness_profile_resolver as r


def _ctx(source_id: str, providers: list[str]) -> dict:
    return {
        "source_id": source_id,
        "outputs": [
            {"provider_kind": kind, "content": f"{source_id}-{kind}"} for kind in providers
        ],
    }


def _ext(ext_id: str, mcps=None, skills=None, instructions=None) -> dict:
    return {
        ext_id: {
            "mcp_servers": list(mcps or []),
            "skills": list(skills or []),
            "instruction_names": list(instructions or []),
            "setting_overlays": {},
        }
    }


def test_merge_temporal_capability_wins_on_conflict() -> None:
    base = {"capability_contexts": [_ctx("cap.a", ["claude"])]}
    temporal = {"capability_contexts": [_ctx("cap.a", ["codex"])]}
    merged = r.merge_profile_snapshots(base, temporal)
    by_source = {c["source_id"]: c for c in merged["capability_contexts"]}
    # Temporal wins by source_id; the base entry is replaced, not duplicated.
    assert len(merged["capability_contexts"]) == 1
    assert {o["provider_kind"] for o in by_source["cap.a"]["outputs"]} == {"codex"}


def test_merge_capability_contexts_additive() -> None:
    base = {"capability_contexts": [_ctx("cap.a", ["claude"])]}
    temporal = {"capability_contexts": [_ctx("cap.b", ["codex"])]}
    merged = r.merge_profile_snapshots(base, temporal)
    assert {c["source_id"] for c in merged["capability_contexts"]} == {"cap.a", "cap.b"}


def test_merge_extension_instances_additive() -> None:
    base = {"extension_instances": _ext("ext.1", mcps=["m1"], skills=["s1"])}
    temporal = {"extension_instances": _ext("ext.1", mcps=["m2"], skills=["s1", "s2"])}
    merged = r.merge_profile_snapshots(base, temporal)
    inst = merged["extension_instances"]["ext.1"]
    assert set(inst["mcp_servers"]) == {"m1", "m2"}
    assert set(inst["skills"]) == {"s1", "s2"}


def test_merge_temporal_extension_setting_overlays_drive_transport_projection() -> None:
    base = {
        "extension_setting_overlays": {
            "ofek-dev.model-traffic": {
                "capture_enabled": False,
                "zeroing_enabled": False,
            }
        },
        "launcher_projection": {
            "extension_setting_overlays": {
                "ofek-dev.model-traffic": {
                    "capture_enabled": False,
                    "zeroing_enabled": False,
                }
            }
        },
    }
    temporal = {
        "extension_setting_overlays": {
            "ofek-dev.model-traffic": {"capture_enabled": True}
        }
    }
    merged = r.merge_profile_snapshots(base, temporal)
    expected = {
        "capture_enabled": True,
        "zeroing_enabled": False,
    }
    assert merged["extension_setting_overlays"]["ofek-dev.model-traffic"] == expected
    assert (
        merged["launcher_projection"]["extension_setting_overlays"][
            "ofek-dev.model-traffic"
        ]
        == expected
    )


def test_merge_does_not_mutate_inputs() -> None:
    base = {
        "capability_contexts": [_ctx("cap.a", ["claude"])],
        "disabled_builtin_tools": ["tool.x"],
        "extension_instances": _ext("ext.1", mcps=["m1"]),
    }
    temporal = {
        "capability_contexts": [_ctx("cap.b", ["codex"])],
        "disabled_builtin_tools": ["tool.y"],
    }
    base_before = copy.deepcopy(base)
    temporal_before = copy.deepcopy(temporal)
    r.merge_profile_snapshots(base, temporal)
    assert base == base_before, "merge_profile_snapshots mutated its base input"
    assert temporal == temporal_before, "merge_profile_snapshots mutated its temporal input"


def test_merge_disabled_lists_union_preserves_session_boundary() -> None:
    # A turn profile may only ADD restrictions; it must not relax a disable
    # declared at the session level.
    base = {
        "disabled_builtin_tools": ["tool.session"],
        "disabled_builtin_extensions": ["ext.session"],
        "disabled_runtime_skills": ["skill.session"],
    }
    temporal = {
        "disabled_builtin_tools": ["tool.turn"],
        "disabled_builtin_extensions": [],
        "disabled_runtime_skills": ["skill.session", "skill.turn"],
    }
    merged = r.merge_profile_snapshots(base, temporal)
    assert set(merged["disabled_builtin_tools"]) == {"tool.session", "tool.turn"}
    assert set(merged["disabled_builtin_extensions"]) == {"ext.session"}
    assert set(merged["disabled_runtime_skills"]) == {"skill.session", "skill.turn"}


def test_merge_no_temporal_returns_base_unchanged() -> None:
    base = {"capability_contexts": [_ctx("cap.a", ["claude"])]}
    assert r.merge_profile_snapshots(base, None) is base
    assert r.merge_profile_snapshots(base, {}) is base


def test_parity_does_not_copy_provider_content() -> None:
    # A capability scoped to one provider must NOT have its content cloned into
    # the other providers — that would inject provider-specific instructions
    # where they do not apply. Canonical per-provider resolution owns fan-out.
    snapshot = {"capability_contexts": [_ctx("cap.a", ["claude"])]}
    result = r._enforce_cross_provider_parity(snapshot)
    outputs = result["capability_contexts"][0]["outputs"]
    assert {o["provider_kind"] for o in outputs} == {"claude"}, (
        "parity enforcement must not fabricate outputs for other providers"
    )


def test_parity_fails_closed_on_duplicate_provider_output() -> None:
    snapshot = {
        "capability_contexts": [
            {
                "source_id": "cap.dup",
                "outputs": [
                    {"provider_kind": "claude", "content": "first"},
                    {"provider_kind": "claude", "content": "second"},
                ],
            }
        ]
    }
    try:
        r._enforce_cross_provider_parity(snapshot)
    except r.HarnessProfileResolutionError:
        return
    raise AssertionError("duplicate provider output must raise a resolution error")


def test_resolve_for_session_no_temporal_returns_base_snapshot() -> None:
    base = {
        "profile_id": "default",
        "capability_contexts": [_ctx("cap.a", ["claude"])],
    }
    session = {
        "harness_profile_snapshot": base,
        "turn_harness_profile_snapshot": None,
    }
    resolved = r.resolve_for_session(session)
    # Base snapshot returned without temporal merge; content untouched.
    assert resolved is base


def test_resolve_for_session_merges_temporal() -> None:
    base = {
        "profile_id": "default",
        "capability_contexts": [_ctx("cap.a", ["claude"])],
        "disabled_builtin_tools": ["tool.session"],
    }
    session = {
        "harness_profile_snapshot": base,
        "turn_harness_profile_snapshot": {
            "capability_contexts": [_ctx("cap.b", ["codex"])],
            "disabled_builtin_tools": ["tool.turn"],
        },
    }
    resolved = r.resolve_for_session(session)
    assert {c["source_id"] for c in resolved["capability_contexts"]} == {"cap.a", "cap.b"}
    assert set(resolved["disabled_builtin_tools"]) == {"tool.session", "tool.turn"}
    # Original session snapshots must not be mutated by resolution.
    assert set(base["disabled_builtin_tools"]) == {"tool.session"}


def main() -> None:
    test_merge_temporal_capability_wins_on_conflict()
    test_merge_capability_contexts_additive()
    test_merge_extension_instances_additive()
    test_merge_temporal_extension_setting_overlays_drive_transport_projection()
    test_merge_does_not_mutate_inputs()
    test_merge_disabled_lists_union_preserves_session_boundary()
    test_merge_no_temporal_returns_base_unchanged()
    test_parity_does_not_copy_provider_content()
    test_parity_fails_closed_on_duplicate_provider_output()
    test_resolve_for_session_no_temporal_returns_base_snapshot()
    test_resolve_for_session_merges_temporal()
    print("PASS harness profile snapshot merge")


if __name__ == "__main__":
    main()
