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
        source_id=f"extension:{extension_store.extension_id_for_role('testape')}:testape-ui-expert",
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
                    "source_id": f"extension:{extension_store.extension_id_for_role('testape')}:testape-ui-expert",
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


def test_team_activation_rolls_back_created_workers_and_team_on_failure() -> None:
    root = main.session_manager.create(
        name="Root2",
        cwd="/repo",
        orchestration_mode="native",
        model="model",
        source="cli",
    )
    activation = team_activation_store.create(
        root_session_id=root["id"],
        team_instance_id="team-rollback",
        source_id=f"extension:{extension_store.extension_id_for_role('testape')}:testape-ui-expert",
        profile="web-ui",
    )
    created_worker = main.session_manager.create(
        name="worker:testape:web-device-worker",
        cwd="/repo",
        orchestration_mode="native",
        model="model",
        source="cli",
        bare_config=True,
    )

    async def fake_provision(body):
        worker = body["workers"][0]
        if worker["member_id"] == "web-device-worker":
            team_store.upsert_member(
                body["team_instance_id"],
                member_id=worker["member_id"],
                member_type="worker",
                agent_session_id=created_worker["id"],
                role=worker["role"],
                description=worker["description"],
                cwd=worker["cwd"],
                run_mode=worker["run_mode"],
            )
            return {"workers": [{"agent_session_id": created_worker["id"], "created": True}]}
        raise RuntimeError("provisioning failed for result-auditor")

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
                    "source_id": f"extension:{extension_store.extension_id_for_role('testape')}:testape-ui-expert",
                    "profile": "web-ui",
                    "team_instance_id": "team-rollback",
                    "manager": {"id": "coordinator", "cwd": "/repo"},
                    "activate": [
                        {
                            "member_id": "web-device-worker",
                            "role_key": "testape:web-device-worker",
                            "role": "testape:web-device-worker",
                            "description": "Web device worker",
                            "cwd": "/repo",
                            "run_mode": "direct",
                        },
                        {
                            "member_id": "result-auditor",
                            "role_key": "testape:result-auditor",
                            "role": "testape:result-auditor",
                            "description": "Result auditor",
                            "cwd": "/repo",
                            "run_mode": "fork",
                        },
                    ],
                },
            )
        )
    finally:
        main._provision_workers_from_body = original

    updated = team_activation_store.get(activation["id"])
    assert updated["status"] == "failed"
    assert updated["rolled_back_worker_ids"] == [created_worker["id"]]
    assert team_store.get("team-rollback") is None
    assert main.session_manager.get(created_worker["id"]) is None
    # The manager/root session belongs to the caller, not this activation —
    # rollback must never delete it.
    assert main.session_manager.get(root["id"]) is not None


def test_team_activation_stores_finalize_with_as_pending_members() -> None:
    root = main.session_manager.create(
        name="Root3",
        cwd="/repo",
        orchestration_mode="native",
        model="model",
        source="cli",
    )
    activation = team_activation_store.create(
        root_session_id=root["id"],
        team_instance_id="team-finalize",
        source_id=f"extension:{extension_store.extension_id_for_role('testape')}:testape-ui-expert",
        profile="web-ui",
    )

    async def fake_provision(body):
        worker = body["workers"][0]
        return {"workers": [{"agent_session_id": f"session-{worker['member_id']}", "created": True}]}

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
                    "source_id": f"extension:{extension_store.extension_id_for_role('testape')}:testape-ui-expert",
                    "profile": "web-ui",
                    "team_instance_id": "team-finalize",
                    "manager": {"id": "coordinator", "cwd": "/repo"},
                    "activate": [],
                    "finalize_with": [
                        {
                            "member_id": "retrospection-worker",
                            "role_key": "testape:retrospection-worker",
                            "role": "testape:retrospection-worker",
                            "description": "Retrospection worker",
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
    team = team_store.get("team-finalize")
    assert "retrospection-worker" not in team["members"]
    assert team["pending_members"]["retrospection-worker"]["member_id"] == "retrospection-worker"


def test_finalize_endpoint_provisions_pending_member_on_demand() -> None:
    async def scenario():
        root = main.session_manager.create(
            name="Root4",
            cwd="/repo",
            orchestration_mode="native",
            model="model",
            source="cli",
        )
        team_store.create(team_id="team-finalize-ep", root_session_id=root["id"])
        team_store.set_pending_members(
            "team-finalize-ep",
            [
                {
                    "member_id": "retrospection-worker",
                    "role_key": "testape:retrospection-worker",
                    "role": "testape:retrospection-worker",
                    "description": "Retrospection worker",
                    "cwd": "/repo",
                    "run_mode": "direct",
                }
            ],
        )

        async def fake_provision(body):
            worker = body["workers"][0]
            team_store.upsert_member(
                body["team_instance_id"],
                member_id=worker["member_id"],
                member_type="worker",
                agent_session_id="session-retrospection-worker",
                role=worker["role"],
                description=worker["description"],
                cwd=worker["cwd"],
                run_mode=worker["run_mode"],
            )
            return {"workers": [{"agent_session_id": "session-retrospection-worker", "created": True}]}

        original_provision = main._provision_workers_from_body
        original_auth = main._internal_authority_is_valid
        original_gate = main._require_builtin_runtime_extension
        main._provision_workers_from_body = fake_provision
        main._internal_authority_is_valid = lambda: True
        main._require_builtin_runtime_extension = lambda _extension_id: None
        try:
            response = await main.internal_finalize_team_definition_member(
                {"team_instance_id": "team-finalize-ep", "member_id": "retrospection-worker"},
                x_internal_token="test",
            )
        finally:
            main._provision_workers_from_body = original_provision
            main._internal_authority_is_valid = original_auth
            main._require_builtin_runtime_extension = original_gate

        assert response["success"] is True
        assert response["workers"][0]["agent_session_id"] == "session-retrospection-worker"
        team = team_store.get("team-finalize-ep")
        assert "retrospection-worker" not in team["pending_members"]
        assert team["members"]["retrospection-worker"]["agent_session_id"] == "session-retrospection-worker"

    asyncio.run(scenario())


if __name__ == "__main__":
    try:
        test_team_activation_records_progress_and_team_members()
        test_team_activation_rolls_back_created_workers_and_team_on_failure()
        test_team_activation_stores_finalize_with_as_pending_members()
        test_finalize_endpoint_provisions_pending_member_on_demand()
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
    print("PASS team activation")
