from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-team-activation-")
os.environ["BETTER_CLAUDE_TEST_AUTH_BYPASS"] = "1"
os.environ["BETTER_AGENT_MARKETPLACE_EXTENSION_REPO_PATH"] = _TMP_HOME

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import extension_store  # noqa: E402
import main  # noqa: E402
import team_activation_store  # noqa: E402
import team_store  # noqa: E402


def teardown_module():
    shutil.rmtree(_TMP_HOME, ignore_errors=True)


def test_team_activation_records_progress_and_team_members() -> None:
    root = main.session_manager.create(
        name="Root",
        cwd="/repo",
        orchestration_mode="native",
        model="model",
        source="cli",
    )
    activation = team_activation_store.create(
        root_session_id=root["id"],
        team_instance_id="team-web",
        source_id=f"extension:{extension_store.BUILTIN_TESTAPE_EXTENSION_ID}:testape-ui-expert",
        profile="web-ui",
    )
    calls = []

    async def fake_provision(body):
        calls.append(body)
        worker = body["workers"][0]
        team_store.upsert_member(
            body["team_instance_id"],
            member_id=worker["member_id"],
            member_type="worker",
            agent_session_id=f"session-{worker['member_id']}",
            role=worker["role"],
            description=worker["description"],
            cwd=worker["cwd"],
            run_mode=worker["run_mode"],
        )
        return {"workers": [{"agent_session_id": f"session-{worker['member_id']}"}]}

    original = main._provision_workers_from_body
    main._provision_workers_from_body = fake_provision
    try:
        asyncio.run(
            main._run_team_definition_activation(
                activation["id"],
                root_session_id=root["id"],
                default_cwd="/repo",
                bare_config=False,
                plan={
                    "source_id": f"extension:{extension_store.BUILTIN_TESTAPE_EXTENSION_ID}:testape-ui-expert",
                    "profile": "web-ui",
                    "team_instance_id": "team-web",
                    "manager": {"id": "coordinator", "cwd": "/repo"},
                    "activate": [
                        {
                            "member_id": "web-device-worker",
                            "role_key": "testape:web-device-worker",
                            "role": "testape:web-device-worker",
                            "description": "Web device worker",
                            "cwd": "/repo",
                            "run_mode": "direct",
                        }
                    ],
                },
            )
        )
    finally:
        main._provision_workers_from_body = original

    updated = team_activation_store.get(activation["id"])
    assert updated["status"] == "complete"
    assert [step["label"] for step in updated["steps"]] == [
        "create runtime team",
        "register manager",
        "provision web-device-worker",
        "provisioned web-device-worker",
    ]
    assert calls[0]["team_instance_id"] == "team-web"
    assert calls[0]["workers"][0]["member_id"] == "web-device-worker"
    team = team_store.get("team-web")
    assert team["members"]["manager"]["agent_session_id"] == root["id"]
    assert team["members"]["web-device-worker"]["agent_session_id"] == "session-web-device-worker"


if __name__ == "__main__":
    try:
        test_team_activation_records_progress_and_team_members()
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
    print("PASS team activation")
