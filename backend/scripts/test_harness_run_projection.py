from __future__ import annotations

import sys
from pathlib import Path


HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import harness_run_projection
import harness_profile_resolver
import extension_store
from capability_contexts import provider_capability_contexts


PROVIDER_FILES = [
    "provider_agy.py",
    "provider_amp.py",
    "provider_claude.py",
    "provider_codex.py",
    "provider_copilot.py",
    "provider_cursor.py",
    "provider_kimi.py",
    "provider_openai.py",
    "provider_opencode.py",
    "provider_pi.py",
    "provider_qwen.py",
]


def test_empty_snapshot_is_noop() -> None:
    inputs = {
        "bare_config": False,
        "capability_contexts": [{"name": "existing", "content": "keep"}],
        "provider_run_config": {"keep": True},
        "extra_mcp_servers": ["existing"],
            "active_capability_ids": ["cap"],
            "disabled_runtime_skills": ["runtime"],
            "disabled_builtin_extensions": ["ext"],
            "disabled_builtin_tools": ["tool"],
        "resolved_harness_run_config": {},
    }
    assert harness_run_projection.apply_to_inputs(inputs) == inputs


def test_active_snapshot_uses_renderable_context_shape() -> None:
    raw_context = {
        "source_id": "profile:test",
        "capability_id": "profile:test",
        "name": "Profile Test",
        "category": "harness-profile",
        "outputs": [
            {
                "provider_kind": "codex",
                "provider_name": "Codex",
                "content_kind": "instructions",
                "content": "render me",
            }
        ],
    }
    flattened = provider_capability_contexts([raw_context], "codex")
    assert flattened and flattened[0]["content"] == "render me"
    projected = harness_run_projection.apply_to_inputs(
        {
            "capability_contexts": [],
            "resolved_harness_run_config": {
                "profile_id": "profile",
                "bare_config": True,
                "capability_contexts": flattened,
            },
        }
    )
    assert projected["bare_config"] is True
    assert projected["capability_contexts"] == flattened
    assert projected["capability_contexts"][0]["content"] == "render me"


def test_active_snapshot_merges_run_local_policy() -> None:
    projected = harness_run_projection.apply_to_inputs(
        {
            "provider_run_config": {
                "fork_parent_line_count": 31,
                "mcp_servers": {"caller": {"command": "caller"}},
                "skills": {"caller": "caller"},
            },
            "extra_mcp_servers": ["shared", "session-only"],
            "disabled_builtin_extensions": ["session.disabled"],
            "disabled_builtin_tools": ["ask"],
            "disabled_runtime_skills": ["session.skill"],
            "resolved_harness_run_config": {
                "profile_id": "profile",
                "provider_run_config": {
                    "mcp_servers": {"profile": {"command": "profile"}},
                    "skills": {"profile": "profile"},
                },
                "extra_mcp_servers": ["profile-only", "shared"],
                "disabled_builtin_extensions": ["profile.disabled"],
                "disabled_builtin_tools": ["mssg"],
                "disabled_runtime_skills": ["profile.skill"],
            },
        }
    )
    assert projected["provider_run_config"]["fork_parent_line_count"] == 31
    assert set(projected["provider_run_config"]["mcp_servers"]) == {"profile", "caller"}
    assert set(projected["provider_run_config"]["skills"]) == {"profile", "caller"}
    assert projected["extra_mcp_servers"] == ["profile-only", "shared", "session-only"]
    assert projected["disabled_builtin_extensions"] == [
        "profile.disabled",
        "session.disabled",
    ]
    assert projected["disabled_builtin_tools"] == ["mssg", "ask"]
    assert projected["disabled_runtime_skills"] == ["profile.skill", "session.skill"]


def test_provider_bare_gate_reads_profile_snapshot() -> None:
    for filename in PROVIDER_FILES:
        source = (BACKEND / filename).read_text(encoding="utf-8")
        assert "resolve_extension_run_policy(" in source, filename
        assert '_bare = bool(run_policy["bare_config"])' in source, filename
        assert '"bare_config": _bare' in source, filename
        assert "user_facing=bool(user_facing) and not _bare" in source or filename == "provider_claude.py", filename


def test_selected_extension_skills_become_run_local_skills(tmp_root: Path | None = None) -> None:
    import tempfile
    import shutil

    root = Path(tempfile.mkdtemp(prefix="ba-profile-skill-")) if tmp_root is None else tmp_root
    try:
        skill_dir = root / "skills" / "profile-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("---\nname: profile-skill\n---\nUse me.\n", encoding="utf-8")
        record = {
            "manifest": {
                "id": "personal.harness",
                "entrypoints": {
                    "skills": [{"name": "profile-skill", "path": "skills/profile-skill"}],
                },
            },
        }
        original = harness_profile_resolver.extension_store.runtime_package_root_for_record
        harness_profile_resolver.extension_store.runtime_package_root_for_record = lambda _record: root
        try:
            entries = harness_profile_resolver._skill_entries({
                "personal.harness": {
                    "record": record,
                    "instance": {"skills": ["profile-skill"]},
                },
            })
        finally:
            harness_profile_resolver.extension_store.runtime_package_root_for_record = original
        assert entries["profile-skill"].endswith("Use me.\n")
    finally:
        if tmp_root is None:
            shutil.rmtree(root, ignore_errors=True)


def test_setting_overlays_apply_without_secret_values() -> None:
    original_get = extension_store.get_extension
    original_schema = extension_store._setting_schema_list
    original_load = extension_store._load_ext_settings
    try:
        extension_store.get_extension = lambda extension_id: {"manifest": {"id": extension_id}}
        extension_store._setting_schema_list = lambda _extension_id: [
            {"key": "mode", "type": "string", "default": "global"},
            {"key": "token", "type": "secret"},
        ]
        extension_store._load_ext_settings = lambda: {
            "extensions": {"personal.harness": {"values": {"mode": "stored"}}}
        }
        settings = extension_store.resolve_all_settings(
            "personal.harness",
            inputs={
                "resolved_harness_run_config": {
                    "launcher_projection": {
                        "extension_setting_overlays": {
                            "personal.harness": {
                                "mode": {"value": "profile", "schema_hash": "ok"},
                                "token": {"value": "must-not-apply", "schema_hash": "ok"},
                            },
                        },
                    },
                },
            },
            include_secrets=False,
        )
    finally:
        extension_store.get_extension = original_get
        extension_store._setting_schema_list = original_schema
        extension_store._load_ext_settings = original_load
    assert settings == {"mode": "profile", "token": ""}


def main() -> int:
    for test in (
        test_empty_snapshot_is_noop,
        test_active_snapshot_uses_renderable_context_shape,
        test_active_snapshot_merges_run_local_policy,
        test_provider_bare_gate_reads_profile_snapshot,
        test_selected_extension_skills_become_run_local_skills,
        test_setting_overlays_apply_without_secret_values,
    ):
        test()
    print("PASS harness run projection")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
