from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-team-definitions-")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import extension_store  # noqa: E402
import team_definitions  # noqa: E402


DEFINITION = {
    "schema_version": 1,
    "name": "testape-ui-expert",
    "manager": {
        "id": "coordinator",
        "orchestration_mode": "native",
        "cwd": "$TESTAPE_ROOT",
    },
    "catalog": {
        "workers": [
            {
                "id": "web-device-worker",
                "type": "worker",
                "role_key": "testape:web-device-worker",
                "cwd": "$TARGET_REPO",
                "orchestration_mode": "native",
                "run_mode": "direct",
                "prompt_ref": "/prompts/web.md",
            },
            {
                "id": "result-auditor",
                "type": "worker",
                "role_key": "testape:result-auditor",
                "cwd": "$TARGET_REPO",
                "orchestration_mode": "native",
                "run_mode": "fork",
                "prompt_ref": "/prompts/auditor.md",
                "node_id": "primary",
                "capability_contexts": [{"id": "ctx-1", "kind": "provider"}],
            },
            {
                "id": "retrospection-worker",
                "type": "worker",
                "role_key": "testape:retrospection-worker",
                "cwd": "$TARGET_REPO",
                "orchestration_mode": "native",
                "run_mode": "direct",
            },
        ]
    },
    "profiles": {
        "web-ui": {
            "activate": ["web-device-worker", "result-auditor"],
            "finalize_with": ["retrospection-worker"],
        },
        "full": {"activate": "*"},
    },
}


def teardown_module():
    shutil.rmtree(_TMP_HOME, ignore_errors=True)


def _patch_sources(monkeypatch) -> None:
    monkeypatch.setattr(
        extension_store,
        "team_definition_sources",
        lambda: [
            {
                "source_id": f"extension:{extension_store.BUILTIN_TESTAPE_EXTENSION_ID}:testape-ui-expert",
                "extension_id": extension_store.BUILTIN_TESTAPE_EXTENSION_ID,
                "extension_name": "Testape",
                "name": "testape-ui-expert",
                "path": "/tmp/ui-expert.json",
                "definition": DEFINITION,
            }
        ],
    )


def test_build_plan_selects_profile_workers(monkeypatch) -> None:
    _patch_sources(monkeypatch)

    plan = team_definitions.build_plan(
        source_id=f"extension:{extension_store.BUILTIN_TESTAPE_EXTENSION_ID}:testape-ui-expert",
        profile="web-ui",
        team_instance_id="team-1",
        variables={
            "TARGET_REPO": "/repo",
            "TESTAPE_ROOT": "/workspace/testape",
        },
    )

    assert plan["manager"]["cwd"] == "/workspace/testape"
    assert [item["member_id"] for item in plan["activate"]] == [
        "web-device-worker",
        "result-auditor",
    ]
    assert plan["activate"][0]["team_instance_id"] == "team-1"
    assert plan["activate"][0]["cwd"] == "/repo"
    assert plan["activate"][1]["run_mode"] == "fork"
    assert plan["activate"][1]["node_id"] == "primary"
    assert plan["activate"][1]["capability_contexts"] == [{"id": "ctx-1", "kind": "provider"}]
    assert [item["member_id"] for item in plan["finalize_with"]] == ["retrospection-worker"]


def test_build_plan_supports_full_profile(monkeypatch) -> None:
    _patch_sources(monkeypatch)

    plan = team_definitions.build_plan(
        source_id="testape-ui-expert",
        profile="full",
        team_instance_id="team-1",
        variables={"TARGET_REPO": "/repo"},
    )

    assert [item["member_id"] for item in plan["activate"]] == [
        "result-auditor",
        "retrospection-worker",
        "web-device-worker",
    ]


def test_validate_definition_rejects_unknown_profile_worker() -> None:
    broken = {
        **DEFINITION,
        "profiles": {"bad": {"activate": ["missing-worker"]}},
    }

    try:
        team_definitions.validate_definition(broken)
    except team_definitions.TeamDefinitionError as exc:
        assert "unknown worker" in str(exc)
        return
    raise AssertionError("unknown profile worker was accepted")


if __name__ == "__main__":
    class MonkeyPatch:
        def setattr(self, obj, name, value):
            setattr(obj, name, value)

    try:
        test_build_plan_selects_profile_workers(MonkeyPatch())
        test_build_plan_supports_full_profile(MonkeyPatch())
        test_validate_definition_rejects_unknown_profile_worker()
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
    print("PASS team definitions")
