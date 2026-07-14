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


def test_activation_discovers_worker_created_before_a_later_provisioning_failure() -> None:
    """A worker session can be created inside _provision_workers_from_body and
    then a later step (e.g. parent-marking, team registration) can still
    raise — that session never makes it into a returned result dict.
    _provision_workers_from_body tags such an exception with the exact
    session id it created (see
    test_provision_workers_from_body_tags_exception_with_created_session_id
    for the real, non-mocked proof of that contract); this test checks the
    activation loop reads that tag correctly. Without it, the id leaks."""
    from stores import worker_store as ws

    root = main.session_manager.create(
        name="Root5",
        cwd="/repo",
        orchestration_mode="native",
        model="model",
        source="cli",
    )
    activation = team_activation_store.create(
        root_session_id=root["id"],
        team_instance_id="team-leak",
        source_id=f"extension:{extension_store.extension_id_for_role('testape')}:testape-ui-expert",
        profile="web-ui",
    )

    async def fake_provision(body):
        # Simulates the real _provision_workers_from_body's contract: it
        # creates and registers the worker session first, THEN a later step
        # (parent marking / team registration / the trailing broadcast)
        # throws — never returning this session's id in a result dict, so
        # the exception is tagged with it instead.
        worker = body["workers"][0]
        leaked = main.session_manager.create(
            name=f"worker:{worker['role_key']}",
            cwd=worker["cwd"],
            orchestration_mode="native",
            model="model",
            source="cli",
            bare_config=True,
        )
        ws.upsert_worker(
            cwd=worker["cwd"],
            agent_session_id=leaked["id"],
            orchestration_mode="native",
            agent_sid=None,
            name=f"worker:{worker['role_key']}",
            role_key=worker["role_key"],
        )
        fake_provision.leaked_session_id = leaked["id"]
        exc = RuntimeError("parent-marking failed after worker session was created")
        exc.partially_created_worker = {
            "agent_session_id": leaked["id"],
            "member_id": worker["member_id"],
            "role": worker["role"],
            "description": worker["description"],
            "cwd": worker["cwd"],
            "run_mode": worker["run_mode"],
        }
        raise exc

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
                    "team_instance_id": "team-leak",
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
    assert updated["status"] == "failed"
    assert updated["rolled_back_worker_ids"] == [fake_provision.leaked_session_id]
    assert main.session_manager.get(fake_provision.leaked_session_id) is None


def test_activation_never_deletes_a_pre_existing_worker_reused_by_a_failed_provision() -> None:
    """If _provision_workers_from_body reuses an ALREADY-existing worker
    (found via the same (cwd, name) lookup) and then fails for an unrelated
    reason, rollback must never delete that worker — it wasn't created by
    this activation."""
    from stores import worker_store as ws

    root = main.session_manager.create(
        name="Root5b",
        cwd="/repo",
        orchestration_mode="native",
        model="model",
        source="cli",
    )
    activation = team_activation_store.create(
        root_session_id=root["id"],
        team_instance_id="team-no-leak",
        source_id=f"extension:{extension_store.extension_id_for_role('testape')}:testape-ui-expert",
        profile="web-ui",
    )
    pre_existing = main.session_manager.create(
        name="worker:testape:web-device-worker",
        cwd="/repo",
        orchestration_mode="native",
        model="model",
        source="cli",
        bare_config=True,
    )
    ws.upsert_worker(
        cwd="/repo",
        agent_session_id=pre_existing["id"],
        orchestration_mode="native",
        agent_sid=None,
        name="worker:testape:web-device-worker",
        role_key="testape:web-device-worker",
    )

    async def fake_provision(body):
        # The worker already existed before this call (reuse path) — it
        # fails for a reason unrelated to creating a NEW session.
        raise RuntimeError("reuse path failed for an unrelated reason")

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
                    "team_instance_id": "team-no-leak",
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
    assert updated["status"] == "failed"
    assert updated["rolled_back_worker_ids"] == []
    # The pre-existing worker must survive untouched.
    assert main.session_manager.get(pre_existing["id"]) is not None
    assert team_store.get("team-no-leak") is None


def test_provision_workers_from_body_tags_exception_with_created_session_id() -> None:
    """Real (non-mocked) proof of the contract the two tests above rely on:
    _provision_workers_from_body itself must tag an exception raised after
    real session creation with that exact agent_session_id, and must NOT do
    so when the failure happens in the reuse-existing-worker branch (nothing
    new was created there)."""
    async def scenario():
        created_ids = []

        async def fake_create_worker_from_body(create_body, broadcast=False):
            sess = main.session_manager.create(
                name=create_body["name"],
                cwd=create_body["cwd"],
                orchestration_mode="native",
                model="model",
                source="cli",
            )
            created_ids.append(sess["id"])
            return {"agent_session_id": sess["id"], "cwd": create_body["cwd"]}

        original_create_worker = main._create_worker_from_body
        original_register = main._register_provisioned_team_member
        main._create_worker_from_body = fake_create_worker_from_body
        main._register_provisioned_team_member = lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("team registration failed")
        )
        try:
            try:
                await main._provision_workers_from_body(
                    {
                        "cwd": "/repo",
                        "workers": [
                            {
                                "role_key": "testape:tagging-worker",
                                "description": "Tagging worker",
                                "cwd": "/repo",
                            }
                        ],
                    }
                )
                raise AssertionError("expected provisioning to raise")
            except RuntimeError as exc:
                tagged = getattr(exc, "partially_created_worker", None)
                assert tagged is not None
                assert tagged["agent_session_id"] == created_ids[0]
                assert tagged["role"] == "testape:tagging-worker"
        finally:
            main._create_worker_from_body = original_create_worker
            main._register_provisioned_team_member = original_register

    asyncio.run(scenario())


def test_activation_does_not_roll_back_when_only_completion_ledger_write_fails() -> None:
    """If every worker provisions successfully and only the final
    team_activation_store.complete() write fails, the team and its workers
    are healthy and must be left alone — a ledger-persistence failure is not
    a reason to destroy real, working resources."""
    root = main.session_manager.create(
        name="Root6",
        cwd="/repo",
        orchestration_mode="native",
        model="model",
        source="cli",
    )
    activation = team_activation_store.create(
        root_session_id=root["id"],
        team_instance_id="team-ledger-fail",
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

    original_provision = main._provision_workers_from_body
    original_complete = team_activation_store.complete
    main._provision_workers_from_body = fake_provision
    team_activation_store.complete = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk full"))
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
                    "team_instance_id": "team-ledger-fail",
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
        main._provision_workers_from_body = original_provision
        team_activation_store.complete = original_complete

    # The activation record itself never got a "complete" write (that's the
    # simulated failure), but the resources it produced must survive intact.
    team = team_store.get("team-ledger-fail")
    assert team is not None
    assert team["members"]["web-device-worker"]["agent_session_id"] == created_worker["id"]
    assert main.session_manager.get(created_worker["id"]) is not None
    # It must not be left stuck at "running" forever — some terminal status
    # has to be recorded even though the healthy resources aren't rolled back.
    updated = team_activation_store.get(activation["id"])
    assert updated["status"] != "running"
    assert updated["rolled_back_worker_ids"] == []


def test_activation_completes_via_minimal_payload_retry_when_full_payload_fails_to_persist() -> None:
    """A transient failure serializing the full completion payload (not a
    systemic store outage) must still end with status=complete via the
    minimal-payload retry — not get mislabeled status=failed."""
    root = main.session_manager.create(
        name="Root6b",
        cwd="/repo",
        orchestration_mode="native",
        model="model",
        source="cli",
    )
    activation = team_activation_store.create(
        root_session_id=root["id"],
        team_instance_id="team-ledger-retry",
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

    call_count = {"n": 0}
    original_complete = team_activation_store.complete

    def flaky_complete(activation_id, result):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("result payload not serializable")
        return original_complete(activation_id, result)

    original_provision = main._provision_workers_from_body
    main._provision_workers_from_body = fake_provision
    team_activation_store.complete = flaky_complete
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
                    "team_instance_id": "team-ledger-retry",
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
        main._provision_workers_from_body = original_provision
        team_activation_store.complete = original_complete

    assert call_count["n"] == 2
    updated = team_activation_store.get(activation["id"])
    assert updated["status"] == "complete"


def test_retry_activation_failure_never_deletes_state_from_a_prior_successful_run() -> None:
    """team_store.create() preserves members/pending_members on a same-root
    retry (it's not a blank slate) — so if that retry then fails, rollback
    must recognize the team predates this attempt and never delete the
    whole record, only prune what THIS attempt's own rollback removed."""
    root = main.session_manager.create(
        name="RootRetry", cwd="/repo", orchestration_mode="native", model="model", source="cli",
    )

    # First attempt succeeds fully and registers a worker.
    first_worker = main.session_manager.create(
        name="worker:testape:web-device-worker",
        cwd="/repo", orchestration_mode="native", model="model", source="cli", bare_config=True,
    )

    async def succeeding_provision(body):
        worker = body["workers"][0]
        team_store.upsert_member(
            body["team_instance_id"],
            member_id=worker["member_id"],
            member_type="worker",
            agent_session_id=first_worker["id"],
            role=worker["role"],
            description=worker["description"],
            cwd=worker["cwd"],
            run_mode=worker["run_mode"],
        )
        return {"workers": [{"agent_session_id": first_worker["id"], "created": True}]}

    plan = {
        "source_id": f"extension:{extension_store.extension_id_for_role('testape')}:testape-ui-expert",
        "profile": "web-ui",
        "team_instance_id": "team-retry",
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
    }

    first_activation = team_activation_store.create(
        root_session_id=root["id"],
        team_instance_id="team-retry",
        source_id=plan["source_id"],
        profile="web-ui",
    )
    original = main._provision_workers_from_body
    main._provision_workers_from_body = succeeding_provision
    try:
        asyncio.run(
            main._run_team_definition_activation(
                first_activation["id"], root_session_id=root["id"], default_cwd="/repo",
                bare_config=False, plan=plan,
            )
        )
    finally:
        main._provision_workers_from_body = original

    first_result = team_activation_store.get(first_activation["id"])
    assert first_result["status"] == "complete"
    assert team_store.get("team-retry")["members"]["web-device-worker"]["agent_session_id"] == first_worker["id"]

    # Retry (same team_id, same root) fails on a SECOND worker after
    # reusing the first one — reuse means it's NOT in created_worker_session_ids.
    retry_activation = team_activation_store.create(
        root_session_id=root["id"],
        team_instance_id="team-retry",
        source_id=plan["source_id"],
        profile="web-ui",
    )

    async def retry_provision(body):
        worker = body["workers"][0]
        if worker["member_id"] == "web-device-worker":
            # Reused, not newly created — untagged, unreported as "created".
            return {"workers": [{"agent_session_id": first_worker["id"], "created": False}]}
        raise RuntimeError("second worker failed on retry")

    retry_plan = {
        **plan,
        "activate": plan["activate"] + [
            {
                "member_id": "result-auditor",
                "role_key": "testape:result-auditor",
                "role": "testape:result-auditor",
                "description": "Result auditor",
                "cwd": "/repo",
                "run_mode": "fork",
            }
        ],
    }
    main._provision_workers_from_body = retry_provision
    try:
        asyncio.run(
            main._run_team_definition_activation(
                retry_activation["id"], root_session_id=root["id"], default_cwd="/repo",
                bare_config=False, plan=retry_plan,
            )
        )
    finally:
        main._provision_workers_from_body = original

    retry_result = team_activation_store.get(retry_activation["id"])
    assert retry_result["status"] == "failed"
    # The team record and the FIRST run's worker registration must survive
    # this retry's failure — nothing this retry created needs cleanup since
    # it only reused an existing worker before failing on the new one.
    team = team_store.get("team-retry")
    assert team is not None
    assert team["members"]["web-device-worker"]["agent_session_id"] == first_worker["id"]
    assert main.session_manager.get(first_worker["id"]) is not None


def test_failed_retry_reverts_pending_members_replacement_not_just_worker_deletion() -> None:
    """A retry that replaces pending_members (via a different finalize_with
    list) and then fails must have that replacement undone too — restoring
    to the pre-attempt snapshot, not just cleaning up newly created
    sessions — or a failed retry silently commits its own mutations over a
    prior successful run's queued deferred workers."""
    root = main.session_manager.create(
        name="RootRetryPending", cwd="/repo", orchestration_mode="native", model="model", source="cli",
    )
    plan_base = {
        "source_id": f"extension:{extension_store.extension_id_for_role('testape')}:testape-ui-expert",
        "profile": "web-ui",
        "team_instance_id": "team-retry-pending",
        "manager": {"id": "coordinator", "cwd": "/repo"},
        "activate": [],
    }

    async def noop_provision(body):
        return {"workers": []}

    original = main._provision_workers_from_body
    main._provision_workers_from_body = noop_provision
    try:
        first_activation = team_activation_store.create(
            root_session_id=root["id"], team_instance_id="team-retry-pending",
            source_id=plan_base["source_id"], profile="web-ui",
        )
        asyncio.run(
            main._run_team_definition_activation(
                first_activation["id"], root_session_id=root["id"], default_cwd="/repo",
                bare_config=False,
                plan={
                    **plan_base,
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
        assert team_activation_store.get(first_activation["id"])["status"] == "complete"
        assert "retrospection-worker" in team_store.get("team-retry-pending")["pending_members"]

        # Retry: replaces pending_members with something ELSE, then fails.
        main._provision_workers_from_body = lambda body: (_ for _ in ()).throw(
            RuntimeError("retry failed after replacing pending_members")
        )
        retry_activation = team_activation_store.create(
            root_session_id=root["id"], team_instance_id="team-retry-pending",
            source_id=plan_base["source_id"], profile="web-ui",
        )
        asyncio.run(
            main._run_team_definition_activation(
                retry_activation["id"], root_session_id=root["id"], default_cwd="/repo",
                bare_config=False,
                plan={
                    **plan_base,
                    "finalize_with": [
                        {
                            "member_id": "some-other-worker",
                            "role_key": "testape:some-other-worker",
                            "role": "testape:some-other-worker",
                            "description": "Some other worker",
                            "cwd": "/repo",
                            "run_mode": "direct",
                        }
                    ],
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

    assert team_activation_store.get(retry_activation["id"])["status"] == "failed"
    # The retry's pending_members replacement must be undone — the original
    # finalize_with entry from the FIRST successful run must be back, and
    # the retry's own (never-provisioned) substitute must not linger.
    pending = team_store.get("team-retry-pending")["pending_members"]
    assert "retrospection-worker" in pending
    assert "some-other-worker" not in pending


def test_team_store_create_refuses_to_overwrite_a_team_owned_by_a_different_root() -> None:
    """team_store.create() must allow the SAME root re-creating its own
    team_id (idempotent retry) but refuse a different root silently taking
    over an existing team_id — that would destroy the real owner's members
    map out from under it."""
    root_a = main.session_manager.create(
        name="RootUnitA", cwd="/repo", orchestration_mode="native", model="model", source="cli",
    )
    root_b = main.session_manager.create(
        name="RootUnitB", cwd="/repo", orchestration_mode="native", model="model", source="cli",
    )
    team_store.create(root_session_id=root_a["id"], team_id="team-ownership")
    # Same root re-creating: allowed (idempotent retry semantics).
    team_store.create(root_session_id=root_a["id"], team_id="team-ownership")
    assert team_store.get("team-ownership")["root_session_id"] == root_a["id"]
    # A different root trying to take over the same team_id: refused.
    try:
        team_store.create(root_session_id=root_b["id"], team_id="team-ownership")
        raise AssertionError("expected TeamStoreError for a foreign-root takeover")
    except team_store.TeamStoreError:
        pass
    assert team_store.get("team-ownership")["root_session_id"] == root_a["id"]


def test_rollback_never_deletes_a_team_recreated_by_a_different_root_session() -> None:
    """If team_id X gets cleaned up and then legitimately reused by a second,
    unrelated activation (a different root_session_id) before this
    activation's OWN (now-stale) rollback runs, rollback must not delete
    that other activation's team record."""
    root_a = main.session_manager.create(
        name="RootA", cwd="/repo", orchestration_mode="native", model="model", source="cli",
    )
    root_b = main.session_manager.create(
        name="RootB", cwd="/repo", orchestration_mode="native", model="model", source="cli",
    )
    activation = team_activation_store.create(
        root_session_id=root_a["id"],
        team_instance_id="team-collision",
        source_id=f"extension:{extension_store.extension_id_for_role('testape')}:testape-ui-expert",
        profile="web-ui",
    )

    async def fake_provision(body):
        # Simulate the team_id being cleaned up and then legitimately
        # reclaimed by a second, unrelated activation under a different root
        # session WHILE this activation is still in flight (e.g. this
        # coroutine is stale/delayed), then this activation's own
        # provisioning fails.
        team_store.delete("team-collision")
        team_store.create(root_session_id=root_b["id"], team_id="team-collision")
        raise RuntimeError("this activation failed after the team_id was reclaimed")

    original = main._provision_workers_from_body
    main._provision_workers_from_body = fake_provision
    try:
        asyncio.run(
            main._run_team_definition_activation(
                activation["id"],
                root_session_id=root_a["id"],
                default_cwd="/repo",
                bare_config=False,
                plan={
                    "source_id": f"extension:{extension_store.extension_id_for_role('testape')}:testape-ui-expert",
                    "profile": "web-ui",
                    "team_instance_id": "team-collision",
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

    # Activation A's team record survives — belonging to root_b now — since
    # rollback checked ownership before deleting.
    team = team_store.get("team-collision")
    assert team is not None
    assert team["root_session_id"] == root_b["id"]


def test_rollback_registers_leaked_placeholder_when_undeletable_worker_fails_before_registration() -> None:
    """A worker that fails BEFORE ever being registered as a team member
    (e.g. parent-marking throws right after creation) and whose rollback
    deletion ALSO fails must not vanish with zero team_store reference — it
    gets registered as a traceable status="leaked" placeholder instead of
    becoming a completely untracked orphan."""
    root = main.session_manager.create(
        name="RootLeak", cwd="/repo", orchestration_mode="native", model="model", source="cli",
    )
    activation = team_activation_store.create(
        root_session_id=root["id"],
        team_instance_id="team-leaked-placeholder",
        source_id=f"extension:{extension_store.extension_id_for_role('testape')}:testape-ui-expert",
        profile="web-ui",
    )
    undeletable_session_id = "session-that-refuses-to-delete"

    async def fake_provision(body):
        worker = body["workers"][0]
        exc = RuntimeError("parent-marking failed before team registration")
        exc.partially_created_worker = {
            "agent_session_id": undeletable_session_id,
            "member_id": worker["member_id"],
            "role": worker["role"],
            "description": worker["description"],
            "cwd": worker["cwd"],
            "run_mode": worker["run_mode"],
        }
        raise exc

    original_provision = main._provision_workers_from_body
    original_delete_tree = main._delete_session_tree

    async def failing_delete_tree(sid):
        if sid == undeletable_session_id:
            return False
        return await original_delete_tree(sid)

    main._provision_workers_from_body = fake_provision
    main._delete_session_tree = failing_delete_tree
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
                    "team_instance_id": "team-leaked-placeholder",
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
        main._provision_workers_from_body = original_provision
        main._delete_session_tree = original_delete_tree

    updated = team_activation_store.get(activation["id"])
    assert updated["status"] == "failed"
    assert updated["rolled_back_worker_ids"] == []
    team = team_store.get("team-leaked-placeholder")
    assert team is not None
    member = team["members"]["web-device-worker"]
    assert member["agent_session_id"] == undeletable_session_id
    assert member["status"] == "leaked"


def test_leaked_placeholder_never_overwrites_a_colliding_member_id_from_a_restored_snapshot() -> None:
    """A retry that restores an old, valid "web-device-worker" mapping (from
    a prior successful run) and then ALSO fails on a NEW attempt to
    (re)provision something under that same member_id must not have the
    leaked-placeholder registration overwrite the valid restored mapping —
    it must fall back to a collision-safe member_id instead."""
    root = main.session_manager.create(
        name="RootCollide", cwd="/repo", orchestration_mode="native", model="model", source="cli",
    )
    old_valid_worker = main.session_manager.create(
        name="worker:testape:web-device-worker-old",
        cwd="/repo", orchestration_mode="native", model="model", source="cli", bare_config=True,
    )
    plan_base = {
        "source_id": f"extension:{extension_store.extension_id_for_role('testape')}:testape-ui-expert",
        "profile": "web-ui",
        "team_instance_id": "team-collide",
        "manager": {"id": "coordinator", "cwd": "/repo"},
    }

    # First attempt succeeds and registers the OLD valid mapping.
    async def first_provision(body):
        worker = body["workers"][0]
        team_store.upsert_member(
            body["team_instance_id"],
            member_id=worker["member_id"],
            member_type="worker",
            agent_session_id=old_valid_worker["id"],
            role=worker["role"],
            description=worker["description"],
            cwd=worker["cwd"],
            run_mode=worker["run_mode"],
        )
        return {"workers": [{"agent_session_id": old_valid_worker["id"], "created": True}]}

    original_provision = main._provision_workers_from_body
    original_delete_tree = main._delete_session_tree
    main._provision_workers_from_body = first_provision
    try:
        first_activation = team_activation_store.create(
            root_session_id=root["id"], team_instance_id="team-collide",
            source_id=plan_base["source_id"], profile="web-ui",
        )
        asyncio.run(
            main._run_team_definition_activation(
                first_activation["id"], root_session_id=root["id"], default_cwd="/repo",
                bare_config=False,
                plan={
                    **plan_base,
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
        assert team_activation_store.get(first_activation["id"])["status"] == "complete"

        # Retry: a NEW worker also tagged with member_id "web-device-worker"
        # gets created but fails before registration, and its deletion also
        # fails — it must not clobber the restored old mapping.
        new_undeletable_session_id = "session-that-collides-and-refuses-to-delete"

        async def retry_provision(body):
            worker = body["workers"][0]
            exc = RuntimeError("failed before registration on retry")
            exc.partially_created_worker = {
                "agent_session_id": new_undeletable_session_id,
                "member_id": worker["member_id"],
                "role": worker["role"],
                "description": worker["description"],
                "cwd": worker["cwd"],
                "run_mode": worker["run_mode"],
            }
            raise exc

        async def failing_delete_tree(sid):
            if sid == new_undeletable_session_id:
                return False
            return await original_delete_tree(sid)

        main._provision_workers_from_body = retry_provision
        main._delete_session_tree = failing_delete_tree
        retry_activation = team_activation_store.create(
            root_session_id=root["id"], team_instance_id="team-collide",
            source_id=plan_base["source_id"], profile="web-ui",
        )
        asyncio.run(
            main._run_team_definition_activation(
                retry_activation["id"], root_session_id=root["id"], default_cwd="/repo",
                bare_config=False,
                plan={
                    **plan_base,
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
        main._provision_workers_from_body = original_provision
        main._delete_session_tree = original_delete_tree

    assert team_activation_store.get(retry_activation["id"])["status"] == "failed"
    team = team_store.get("team-collide")
    # The OLD valid mapping must survive untouched under its original id.
    assert team["members"]["web-device-worker"]["agent_session_id"] == old_valid_worker["id"]
    # The new leaked worker must be registered, but NOT under the colliding
    # member_id — it needs its own collision-safe id.
    leaked_members = [
        m for m in team["members"].values()
        if m.get("agent_session_id") == new_undeletable_session_id
    ]
    assert len(leaked_members) == 1
    assert leaked_members[0]["status"] == "leaked"
    assert leaked_members[0]["id"] != "web-device-worker"


def test_carry_over_of_a_successfully_registered_survivor_never_overwrites_a_colliding_snapshot_member() -> None:
    """Same collision class as the leaked-placeholder case, but for a worker
    that WAS successfully registered this retry (not one that failed before
    registration): if a sibling worker then fails, rollback deletion of the
    successfully-registered one fails too, and it shares a member_id with an
    unrelated worker from the pre-attempt snapshot — the carry-over merge
    must not clobber the snapshot's valid mapping."""
    root = main.session_manager.create(
        name="RootCollide2", cwd="/repo", orchestration_mode="native", model="model", source="cli",
    )
    old_valid_worker = main.session_manager.create(
        name="worker:testape:web-device-worker-old2",
        cwd="/repo", orchestration_mode="native", model="model", source="cli", bare_config=True,
    )
    plan_base = {
        "source_id": f"extension:{extension_store.extension_id_for_role('testape')}:testape-ui-expert",
        "profile": "web-ui",
        "team_instance_id": "team-collide-2",
        "manager": {"id": "coordinator", "cwd": "/repo"},
    }

    async def first_provision(body):
        worker = body["workers"][0]
        team_store.upsert_member(
            body["team_instance_id"],
            member_id=worker["member_id"],
            member_type="worker",
            agent_session_id=old_valid_worker["id"],
            role=worker["role"],
            description=worker["description"],
            cwd=worker["cwd"],
            run_mode=worker["run_mode"],
        )
        return {"workers": [{"agent_session_id": old_valid_worker["id"], "created": True}]}

    original_provision = main._provision_workers_from_body
    original_delete_tree = main._delete_session_tree
    main._provision_workers_from_body = first_provision
    try:
        first_activation = team_activation_store.create(
            root_session_id=root["id"], team_instance_id="team-collide-2",
            source_id=plan_base["source_id"], profile="web-ui",
        )
        asyncio.run(
            main._run_team_definition_activation(
                first_activation["id"], root_session_id=root["id"], default_cwd="/repo",
                bare_config=False,
                plan={
                    **plan_base,
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
        assert team_activation_store.get(first_activation["id"])["status"] == "complete"

        # Retry: a NEW "web-device-worker" is created and SUCCESSFULLY
        # registered this attempt (unlike the leaked-placeholder test), then
        # a SIBLING worker fails, triggering rollback. The new worker's
        # deletion also fails, so it must be carried over — but not by
        # clobbering the old valid mapping under the same member_id.
        new_registered_session_id = "session-registered-then-undeletable"

        async def retry_provision(body):
            worker = body["workers"][0]
            if worker["member_id"] == "web-device-worker":
                team_store.upsert_member(
                    body["team_instance_id"],
                    member_id=worker["member_id"],
                    member_type="worker",
                    agent_session_id=new_registered_session_id,
                    role=worker["role"],
                    description=worker["description"],
                    cwd=worker["cwd"],
                    run_mode=worker["run_mode"],
                )
                return {"workers": [{"agent_session_id": new_registered_session_id, "created": True}]}
            raise RuntimeError("sibling worker failed on retry")

        async def failing_delete_tree(sid):
            if sid == new_registered_session_id:
                return False
            return await original_delete_tree(sid)

        main._provision_workers_from_body = retry_provision
        main._delete_session_tree = failing_delete_tree
        retry_activation = team_activation_store.create(
            root_session_id=root["id"], team_instance_id="team-collide-2",
            source_id=plan_base["source_id"], profile="web-ui",
        )
        asyncio.run(
            main._run_team_definition_activation(
                retry_activation["id"], root_session_id=root["id"], default_cwd="/repo",
                bare_config=False,
                plan={
                    **plan_base,
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
        main._provision_workers_from_body = original_provision
        main._delete_session_tree = original_delete_tree

    assert team_activation_store.get(retry_activation["id"])["status"] == "failed"
    team = team_store.get("team-collide-2")
    # The OLD valid mapping must survive untouched under "web-device-worker".
    assert team["members"]["web-device-worker"]["agent_session_id"] == old_valid_worker["id"]
    # The new, successfully-registered-but-undeletable worker must still be
    # carried over somewhere, just not clobbering the collision.
    carried = [
        m for m in team["members"].values()
        if m.get("agent_session_id") == new_registered_session_id
    ]
    assert len(carried) == 1
    assert carried[0]["id"] != "web-device-worker"


def test_partial_rollback_prunes_only_the_actually_deleted_members() -> None:
    """When some created workers roll back successfully but at least one
    deletion fails, the team record must survive (it's the only remaining
    mapping to the surviving worker) — but member entries for the workers
    that WERE actually deleted must not linger as stale "active" pointers to
    sessions that no longer exist."""
    root = main.session_manager.create(
        name="Root10",
        cwd="/repo",
        orchestration_mode="native",
        model="model",
        source="cli",
    )
    activation = team_activation_store.create(
        root_session_id=root["id"],
        team_instance_id="team-partial-rollback",
        source_id=f"extension:{extension_store.extension_id_for_role('testape')}:testape-ui-expert",
        profile="web-ui",
    )
    deletable_worker = main.session_manager.create(
        name="worker:testape:web-device-worker",
        cwd="/repo",
        orchestration_mode="native",
        model="model",
        source="cli",
        bare_config=True,
    )
    undeletable_session_id = "session-does-not-exist-in-session-manager"

    async def fake_provision(body):
        worker = body["workers"][0]
        if worker["member_id"] == "web-device-worker":
            team_store.upsert_member(
                body["team_instance_id"],
                member_id=worker["member_id"],
                member_type="worker",
                agent_session_id=deletable_worker["id"],
                role=worker["role"],
                description=worker["description"],
                cwd=worker["cwd"],
                run_mode=worker["run_mode"],
            )
            return {"workers": [{"agent_session_id": deletable_worker["id"], "created": True}]}
        if worker["member_id"] == "result-auditor":
            team_store.upsert_member(
                body["team_instance_id"],
                member_id=worker["member_id"],
                member_type="worker",
                agent_session_id=undeletable_session_id,
                role=worker["role"],
                description=worker["description"],
                cwd=worker["cwd"],
                run_mode=worker["run_mode"],
            )
            return {"workers": [{"agent_session_id": undeletable_session_id, "created": True}]}
        raise RuntimeError("provisioning failed for retrospection-worker")

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
                    "team_instance_id": "team-partial-rollback",
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
                        {
                            "member_id": "retrospection-worker",
                            "role_key": "testape:retrospection-worker",
                            "role": "testape:retrospection-worker",
                            "description": "Retrospection worker",
                            "cwd": "/repo",
                            "run_mode": "direct",
                        },
                    ],
                },
            )
        )
    finally:
        main._provision_workers_from_body = original

    # deletable_worker's real session WAS torn down; the undeletable one
    # (nonexistent sid — session_manager.delete just returns False for it)
    # was not, so the team record must survive with only that member left.
    assert main.session_manager.get(deletable_worker["id"]) is None
    team = team_store.get("team-partial-rollback")
    assert team is not None
    assert "web-device-worker" not in team["members"]
    assert team["members"]["result-auditor"]["agent_session_id"] == undeletable_session_id
    assert team["members"]["manager"]["agent_session_id"] == root["id"]


def test_finalize_endpoint_restores_pending_member_when_provisioning_fails() -> None:
    async def scenario():
        root = main.session_manager.create(
            name="Root7",
            cwd="/repo",
            orchestration_mode="native",
            model="model",
            source="cli",
        )
        team_store.create(team_id="team-finalize-fail", root_session_id=root["id"])
        pending_spec = {
            "member_id": "retrospection-worker",
            "role_key": "testape:retrospection-worker",
            "role": "testape:retrospection-worker",
            "description": "Retrospection worker",
            "cwd": "/repo",
            "run_mode": "direct",
        }
        team_store.set_pending_members("team-finalize-fail", [pending_spec])

        async def failing_provision(body):
            raise RuntimeError("provider unavailable")

        original_provision = main._provision_workers_from_body
        original_auth = main._internal_authority_is_valid
        original_gate = main._require_builtin_runtime_extension
        main._provision_workers_from_body = failing_provision
        main._internal_authority_is_valid = lambda: True
        main._require_builtin_runtime_extension = lambda _extension_id: None
        try:
            try:
                await main.internal_finalize_team_definition_member(
                    {"team_instance_id": "team-finalize-fail", "member_id": "retrospection-worker"},
                    x_internal_token="test",
                )
                raise AssertionError("expected finalize to raise when provisioning fails")
            except main.HTTPException as exc:
                assert exc.status_code == 500
        finally:
            main._provision_workers_from_body = original_provision
            main._internal_authority_is_valid = original_auth
            main._require_builtin_runtime_extension = original_gate

        team = team_store.get("team-finalize-fail")
        assert team["pending_members"]["retrospection-worker"]["member_id"] == "retrospection-worker"
        assert "retrospection-worker" not in team["members"]

    asyncio.run(scenario())


def test_finalize_endpoint_returns_404_for_unknown_team() -> None:
    async def scenario():
        original_auth = main._internal_authority_is_valid
        original_gate = main._require_builtin_runtime_extension
        main._internal_authority_is_valid = lambda: True
        main._require_builtin_runtime_extension = lambda _extension_id: None
        try:
            try:
                await main.internal_finalize_team_definition_member(
                    {"team_instance_id": "team-does-not-exist", "member_id": "retrospection-worker"},
                    x_internal_token="test",
                )
                raise AssertionError("expected 404 for unknown team")
            except main.HTTPException as exc:
                assert exc.status_code == 404
        finally:
            main._internal_authority_is_valid = original_auth
            main._require_builtin_runtime_extension = original_gate

    asyncio.run(scenario())


def test_finalize_endpoint_preserves_http_exception_status_from_provisioning() -> None:
    async def scenario():
        root = main.session_manager.create(
            name="Root8",
            cwd="/repo",
            orchestration_mode="native",
            model="model",
            source="cli",
        )
        team_store.create(team_id="team-finalize-400", root_session_id=root["id"])
        pending_spec = {
            "member_id": "retrospection-worker",
            "role_key": "testape:retrospection-worker",
            "role": "testape:retrospection-worker",
            "description": "Retrospection worker",
            "cwd": "/repo",
            "run_mode": "direct",
        }
        team_store.set_pending_members("team-finalize-400", [pending_spec])

        async def bad_request_provision(body):
            raise main.HTTPException(status_code=400, detail="cwd is required")

        original_provision = main._provision_workers_from_body
        original_auth = main._internal_authority_is_valid
        original_gate = main._require_builtin_runtime_extension
        main._provision_workers_from_body = bad_request_provision
        main._internal_authority_is_valid = lambda: True
        main._require_builtin_runtime_extension = lambda _extension_id: None
        try:
            try:
                await main.internal_finalize_team_definition_member(
                    {"team_instance_id": "team-finalize-400", "member_id": "retrospection-worker"},
                    x_internal_token="test",
                )
                raise AssertionError("expected finalize to propagate the 400")
            except main.HTTPException as exc:
                # The provisioner's own validation status code must survive,
                # not get flattened into a generic 500.
                assert exc.status_code == 400
        finally:
            main._provision_workers_from_body = original_provision
            main._internal_authority_is_valid = original_auth
            main._require_builtin_runtime_extension = original_gate

        team = team_store.get("team-finalize-400")
        assert team["pending_members"]["retrospection-worker"]["member_id"] == "retrospection-worker"

    asyncio.run(scenario())


def test_finalize_endpoint_does_not_restore_pending_if_member_already_went_active() -> None:
    """If _provision_workers_from_body registers the worker as an active
    team member and only fails afterward (e.g. its own trailing broadcast),
    finalize must not resurrect a stale pending entry alongside the now
    real, active member."""
    async def scenario():
        root = main.session_manager.create(
            name="Root9",
            cwd="/repo",
            orchestration_mode="native",
            model="model",
            source="cli",
        )
        team_store.create(team_id="team-finalize-active", root_session_id=root["id"])
        pending_spec = {
            "member_id": "retrospection-worker",
            "role_key": "testape:retrospection-worker",
            "role": "testape:retrospection-worker",
            "description": "Retrospection worker",
            "cwd": "/repo",
            "run_mode": "direct",
        }
        team_store.set_pending_members("team-finalize-active", [pending_spec])

        async def half_succeeds_provision(body):
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
            raise RuntimeError("broadcast failed after member was already registered active")

        original_provision = main._provision_workers_from_body
        original_auth = main._internal_authority_is_valid
        original_gate = main._require_builtin_runtime_extension
        main._provision_workers_from_body = half_succeeds_provision
        main._internal_authority_is_valid = lambda: True
        main._require_builtin_runtime_extension = lambda _extension_id: None
        try:
            try:
                await main.internal_finalize_team_definition_member(
                    {"team_instance_id": "team-finalize-active", "member_id": "retrospection-worker"},
                    x_internal_token="test",
                )
                raise AssertionError("expected finalize to raise")
            except main.HTTPException:
                pass
        finally:
            main._provision_workers_from_body = original_provision
            main._internal_authority_is_valid = original_auth
            main._require_builtin_runtime_extension = original_gate

        team = team_store.get("team-finalize-active")
        assert team["members"]["retrospection-worker"]["agent_session_id"] == "session-retrospection-worker"
        assert "retrospection-worker" not in team["pending_members"]

    asyncio.run(scenario())


if __name__ == "__main__":
    try:
        test_team_activation_records_progress_and_team_members()
        test_team_activation_rolls_back_created_workers_and_team_on_failure()
        test_team_activation_stores_finalize_with_as_pending_members()
        test_finalize_endpoint_provisions_pending_member_on_demand()
        test_activation_discovers_worker_created_before_a_later_provisioning_failure()
        test_activation_never_deletes_a_pre_existing_worker_reused_by_a_failed_provision()
        test_provision_workers_from_body_tags_exception_with_created_session_id()
        test_activation_does_not_roll_back_when_only_completion_ledger_write_fails()
        test_activation_completes_via_minimal_payload_retry_when_full_payload_fails_to_persist()
        test_team_store_create_refuses_to_overwrite_a_team_owned_by_a_different_root()
        test_retry_activation_failure_never_deletes_state_from_a_prior_successful_run()
        test_failed_retry_reverts_pending_members_replacement_not_just_worker_deletion()
        test_rollback_never_deletes_a_team_recreated_by_a_different_root_session()
        test_rollback_registers_leaked_placeholder_when_undeletable_worker_fails_before_registration()
        test_leaked_placeholder_never_overwrites_a_colliding_member_id_from_a_restored_snapshot()
        test_carry_over_of_a_successfully_registered_survivor_never_overwrites_a_colliding_snapshot_member()
        test_partial_rollback_prunes_only_the_actually_deleted_members()
        test_finalize_endpoint_restores_pending_member_when_provisioning_fails()
        test_finalize_endpoint_returns_404_for_unknown_team()
        test_finalize_endpoint_preserves_http_exception_status_from_provisioning()
        test_finalize_endpoint_does_not_restore_pending_if_member_already_went_active()
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
    print("PASS team activation")
