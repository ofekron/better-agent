import os
import sys
import tempfile
import json
import shutil
from pathlib import Path

import _test_home
_tmp = _test_home.isolate("ba-test-")
os.environ["BETTER_CLAUDE_TEST_AUTH_BYPASS"] = "1"

_HERE = Path(__file__).resolve().parent
_BACKEND = _HERE.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

dist_dir = _BACKEND.parent / "frontend" / "dist"
if not dist_dir.exists():
    dist_dir.mkdir(parents=True, exist_ok=True)
    (dist_dir / "index.html").write_text("<!doctype html><title>stub</title>", encoding="utf-8")

import main  # noqa: E402
import extension_store  # noqa: E402
import config_store  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402


async def _fake_init_target_agent_session(*, bc_session, **_kwargs):
    return f"agent-{bc_session['id']}"


async def _fail_init_target_agent_session(**_kwargs):
    raise AssertionError("bare worker provisioning must not run worker prep")


async def _fake_broadcast_workers_changed(_cwd):
    return None


def _install_team_orchestration_extension() -> None:
    extension_id = extension_store.BUILTIN_TEAM_ORCHESTRATION_EXTENSION_ID
    package = Path(_tmp) / "private-fixtures" / extension_id
    if package.exists():
        shutil.rmtree(package)
    package.mkdir(parents=True)
    manifest = {
        "kind": extension_store.MANIFEST_KIND,
        "id": extension_id,
        "name": extension_id,
        "version": "1.0.0",
        "description": extension_id,
        "surfaces": ["backend_feature"],
        "entrypoints": {},
        "permissions": {},
        "marketplace": {},
    }
    (package / "better-agent-extension.json").write_text(json.dumps(manifest), encoding="utf-8")
    extension_store._install_from_package_dir(  # type: ignore[attr-defined]
        package_dir=package,
        source={
            "type": "better_agent_local",
            "repo_url": str(package.parent),
            "extension_path": package.name,
            "ref": "",
            "commit_sha": extension_id,
        },
        persist=True,
    )
    providers = config_store.list_providers()["providers"]
    provider = providers[0]
    assignments = config_store.get_internal_llm_assignments()
    assignments["default_session"] = {
        "provider_id": provider["id"],
        "model": provider["default_model"],
        "reasoning_effort": provider.get("default_reasoning_effort") or "",
    }
    config_store.set_internal_llm_assignments(assignments)


def _client() -> TestClient:
    _install_team_orchestration_extension()
    return TestClient(main.app, client=("127.0.0.1", 50000))


def _post_team_ui_provision(client: TestClient, payload: dict):
    return client.post(
        "/api/internal/workers/provision-ui",
        json=payload,
        headers={"X-Internal-Token": main.coordinator.internal_token},
    )


def test_provision_workers_is_idempotent_by_role_key():
    init_calls = []

    async def fake_init_with_description(*, bc_session, description, **_kwargs):
        init_calls.append({"name": bc_session["name"], "description": description})
        return f"agent-{bc_session['id']}"

    main.coordinator._init_target_agent_session = fake_init_with_description
    main.coordinator.broadcast_workers_changed = _fake_broadcast_workers_changed
    client = _client()
    payload = {
        "cwd": "/tmp/project",
        "workers": [
            {
                "role_key": "device-worker",
                "description": "Device worker cached task instructions",
                "orchestration_mode": "native",
            },
            {
                "role_key": "graph-optimizer",
                "description": "Graph optimizer cached task instructions",
                "orchestration_mode": "manager",
            },
        ],
    }

    first = _post_team_ui_provision(client, payload)
    assert first.status_code == 200, first.text
    first_workers = first.json()["workers"]
    assert [worker["role_key"] for worker in first_workers] == ["device-worker", "graph-optimizer"]
    assert [worker["name"] for worker in first_workers] == ["worker:device-worker", "worker:graph-optimizer"]
    assert init_calls == [
        {
            "name": "worker:device-worker",
            "description": "Device worker cached task instructions",
        },
        {
            "name": "worker:graph-optimizer",
            "description": "Graph optimizer cached task instructions",
        },
    ]
    assert [worker["registry_cwd"] for worker in first_workers] == ["/tmp/project", "/tmp/project"]
    assert all(worker["created"] is True for worker in first_workers)

    second = _post_team_ui_provision(client, payload)
    assert second.status_code == 200, second.text
    second_workers = second.json()["workers"]
    assert [worker["agent_session_id"] for worker in second_workers] == [
        worker["agent_session_id"] for worker in first_workers
    ]
    assert [worker["registry_cwd"] for worker in second_workers] == ["/tmp/project", "/tmp/project"]
    assert all(worker["created"] is False for worker in second_workers)


def test_provision_workers_remains_idempotent_after_session_title_changes():
    from session_manager import manager as session_manager

    main.coordinator._init_target_agent_session = _fake_init_target_agent_session
    main.coordinator.broadcast_workers_changed = _fake_broadcast_workers_changed
    client = _client()
    payload = {
        "cwd": "/tmp/title-project",
        "workers": [
            {
                "role_key": "testape",
                "description": "TestApe worker seed",
                "orchestration_mode": "team",
            },
        ],
    }

    first = _post_team_ui_provision(client, payload)
    assert first.status_code == 200, first.text
    first_worker = first.json()["workers"][0]
    assert first_worker["name"] == "worker:testape"

    session_manager.rename(first_worker["agent_session_id"], "Execute TestApe e2e testing task")

    second = _post_team_ui_provision(client, payload)
    assert second.status_code == 200, second.text
    second_worker = second.json()["workers"][0]
    assert second_worker["agent_session_id"] == first_worker["agent_session_id"]
    assert second_worker["created"] is False
    assert second_worker["name"] == "worker:testape"
    assert second_worker["display_name"] == "Execute TestApe e2e testing task"

    listed = client.post(
        "/api/internal/workers/list",
        json={"cwd": "/tmp/title-project"},
        headers={"X-Internal-Token": main.coordinator.internal_token},
    )
    assert listed.status_code == 200, listed.text
    listed_worker = listed.json()["workers"][0]
    assert listed_worker["agent_session_id"] == first_worker["agent_session_id"]
    assert listed_worker["name"] == "worker:testape"
    assert listed_worker["display_name"] == "Execute TestApe e2e testing task"


def test_provision_workers_allows_per_worker_cwd():
    init_calls = []

    async def fake_init_with_cwd(*, bc_session, cwd, **_kwargs):
        init_calls.append({"name": bc_session["name"], "cwd": cwd})
        return f"agent-{bc_session['id']}"

    main.coordinator._init_target_agent_session = fake_init_with_cwd
    main.coordinator.broadcast_workers_changed = _fake_broadcast_workers_changed
    client = _client()
    payload = {
        "cwd": "/tmp/default-project",
        "workers": [
            {"role_key": "app-worker", "orchestration_mode": "native"},
            {"role_key": "tool-worker", "cwd": "/tmp/tooling", "orchestration_mode": "native"},
        ],
    }

    response = _post_team_ui_provision(client, payload)

    assert response.status_code == 200, response.text
    workers = response.json()["workers"]
    assert [worker["registry_cwd"] for worker in workers] == ["/tmp/default-project", "/tmp/tooling"]
    assert init_calls == [
        {"name": "worker:app-worker", "cwd": "/tmp/default-project"},
        {"name": "worker:tool-worker", "cwd": "/tmp/tooling"},
    ]


def test_worker_list_projects_pools_from_tags():
    main.coordinator._init_target_agent_session = _fake_init_target_agent_session
    main.coordinator.broadcast_workers_changed = _fake_broadcast_workers_changed
    client = _client()
    payload = {
        "cwd": "/tmp/pool-project",
        "workers": [
            {"role_key": "review-a", "orchestration_mode": "native", "tags": ["review"]},
            {"role_key": "review-b", "orchestration_mode": "native", "tags": ["review"]},
            {"role_key": "build", "orchestration_mode": "native", "tags": ["build"]},
        ],
    }

    response = _post_team_ui_provision(client, payload)
    assert response.status_code == 200, response.text

    listed = client.post(
        "/api/internal/workers/list",
        json={"cwd": "/tmp/pool-project"},
        headers={"X-Internal-Token": main.coordinator.internal_token},
    )
    assert listed.status_code == 200, listed.text
    pools = {pool["tag"]: pool for pool in listed.json()["pools"]}
    assert {worker["name"] for worker in pools["review"]["workers"]} == {
        "worker:review-a",
        "worker:review-b",
    }
    assert len(pools["build"]["workers"]) == 1


def test_pool_workers_receive_peer_context_in_provision_prompt():
    init_prompts = {}

    async def fake_init_with_prompt(*, bc_session, provision_prompt, **_kwargs):
        init_prompts[bc_session["name"]] = provision_prompt
        return f"agent-{bc_session['id']}"

    main.coordinator._init_target_agent_session = fake_init_with_prompt
    main.coordinator.broadcast_workers_changed = _fake_broadcast_workers_changed
    client = _client()
    response = _post_team_ui_provision(client, {
        "cwd": "/tmp/pool-context",
        "workers": [
            {
                "role_key": "review-a",
                "description": "First reviewer",
                "orchestration_mode": "native",
                "tags": ["review"],
            },
            {
                "role_key": "review-b",
                "description": "Second reviewer",
                "orchestration_mode": "native",
                "tags": ["review"],
            },
        ],
    })

    assert response.status_code == 200, response.text
    assert set(init_prompts) == {"worker:review-a", "worker:review-b"}
    for prompt in init_prompts.values():
        assert "<worker_pool>" in prompt
        assert 'name="worker:review-a"' in prompt
        assert 'name="worker:review-b"' in prompt
        assert 'tags="review"' in prompt
        assert "Use mssg(target_session_id, message)" in prompt


def test_existing_named_worker_backfills_pool_tags():
    main.coordinator._init_target_agent_session = _fake_init_target_agent_session
    main.coordinator.broadcast_workers_changed = _fake_broadcast_workers_changed
    client = _client()
    cwd = "/tmp/pool-backfill"

    first = _post_team_ui_provision(client, {
        "cwd": cwd,
        "workers": [{"role_key": "testape", "orchestration_mode": "team"}],
    })
    assert first.status_code == 200, first.text

    second = _post_team_ui_provision(client, {
        "cwd": cwd,
        "workers": [{"role_key": "testape", "orchestration_mode": "team", "tags": ["testape"]}],
    })
    assert second.status_code == 200, second.text
    worker = second.json()["workers"][0]
    assert worker["created"] is False
    assert worker["tags"] == ["testape"]

    listed = client.post(
        "/api/internal/workers/list",
        json={"cwd": cwd},
        headers={"X-Internal-Token": main.coordinator.internal_token},
    )
    assert listed.status_code == 200, listed.text
    pools = {pool["tag"]: pool for pool in listed.json()["pools"]}
    assert [worker["name"] for worker in pools["testape"]["workers"]] == ["worker:testape"]


def test_worker_pool_enqueue_dispatches_to_idle_tagged_worker():
    dispatched = []

    async def fake_submit_team_message(**kwargs):
        dispatched.append(kwargs)
        return {"success": True, "queued_id": "queued"}

    main.coordinator._init_target_agent_session = _fake_init_target_agent_session
    main.coordinator.broadcast_workers_changed = _fake_broadcast_workers_changed
    real_submit = main.coordinator.submit_team_message
    main.coordinator.submit_team_message = fake_submit_team_message
    client = _client()
    sender = main.session_manager.create(
        name="manager",
        cwd="/tmp/pool-dispatch",
        orchestration_mode="team",
    )
    provision = _post_team_ui_provision(client, {
        "cwd": "/tmp/pool-dispatch",
        "workers": [{"role_key": "idle-reviewer", "orchestration_mode": "native", "tags": ["review"]}],
    })
    assert provision.status_code == 200, provision.text

    try:
        response = client.post(
            "/api/internal/worker-pools/enqueue",
            json={"tag": "review", "sender_session_id": sender["id"], "prompt": "review this"},
            headers={"X-Internal-Token": main.coordinator.internal_token},
        )
        assert response.status_code == 200, response.text

        import asyncio
        asyncio.run(main._process_worker_pool_queue("review"))
        assert dispatched
        assert dispatched[0]["sender_session_id"] == sender["id"]
        assert dispatched[0]["message"] == "review this"
        assert dispatched[0]["detach"] is True
        assert dispatched[0]["expect_mssg_response"] is True
    finally:
        main.coordinator.submit_team_message = real_submit


def test_internal_provision_workers_requires_internal_token():
    broadcasts = []

    async def fake_broadcast(cwd):
        broadcasts.append(cwd)

    main.coordinator._init_target_agent_session = _fake_init_target_agent_session
    main.coordinator.broadcast_workers_changed = fake_broadcast
    client = _client()
    payload = {
        "cwd": "/tmp/internal-project",
        "workers": [{"role_key": "device-worker", "orchestration_mode": "native"}],
    }

    denied = client.post("/api/internal/workers/provision", json=payload)
    assert denied.status_code == 403

    allowed = client.post(
        "/api/internal/workers/provision",
        json=payload,
        headers={"X-Internal-Token": main.coordinator.internal_token},
    )
    assert allowed.status_code == 200, allowed.text
    worker = allowed.json()["workers"][0]
    assert worker["role_key"] == "device-worker"
    assert worker["registry_cwd"] == "/tmp/internal-project"
    assert worker["created"] is True
    assert broadcasts == [None]


def test_bare_provision_workers_returns_pending_without_init_turn():
    broadcasts = []

    async def fake_broadcast(cwd):
        broadcasts.append(cwd)

    main.coordinator._init_target_agent_session = _fail_init_target_agent_session
    main.coordinator.broadcast_workers_changed = fake_broadcast
    client = _client()
    payload = {
        "cwd": "/tmp/bare-project",
        "bare_config": True,
        "workers": [{"role_key": "testape:web-device-worker", "orchestration_mode": "native"}],
    }

    response = _post_team_ui_provision(client, payload)

    assert response.status_code == 200, response.text
    worker = response.json()["workers"][0]
    assert worker["role_key"] == "testape:web-device-worker"
    assert worker["agent_sid"] is None
    assert worker["initialized"] is False
    assert worker["created"] is True
    assert broadcasts == [None]


def test_coordinator_target_init_proxy_accepts_ws_callback():
    async def fake_impl(_coordinator, *, ws_callback=None, provision_prompt=None, **_kwargs):
        assert ws_callback is not None
        assert provision_prompt == "custom provision"
        return "agent-from-proxy"

    original = main.coordinator.__class__._init_target_agent_session

    async def run_check():
        return await original(
            main.coordinator,
            bc_session={"id": "worker-session"},
            model="glm-5.2",
            cwd="/tmp/project",
            description="worker",
            cancel_event=main.asyncio.Event(),
            ws_callback=lambda _event: None,
            provision_prompt="custom provision",
        )

    import orchs.manager._approval as approval
    old_impl = approval.init_target_agent_session
    approval.init_target_agent_session = fake_impl
    try:
        result = main.asyncio.run(run_check())
    finally:
        approval.init_target_agent_session = old_impl
    assert result == "agent-from-proxy"


def test_target_init_accepts_custom_provision_prompt():
    import orchs.manager._approval as approval

    seen = {}
    original_agent = approval.SubprocessAgent

    class FakeAgent:
        def __init__(self, *, agent_session_id, cwd):
            seen["agent_session_id"] = agent_session_id
            seen["cwd"] = cwd

        async def init(self, _coordinator, *, prep_prompt, **_kwargs):
            seen["prep_prompt"] = prep_prompt
            return "agent-machine"

    async def run_check():
        return await approval.init_target_agent_session(
            main.coordinator,
            bc_session={"id": "machine-worker", "orchestration_mode": "native"},
            model="glm-5.2",
            cwd="/tmp/project",
            description="worker:requirements:pipeline-operator",
            cancel_event=main.asyncio.Event(),
            provision_prompt="caller supplied provision",
        )

    approval.SubprocessAgent = FakeAgent
    try:
        result = main.asyncio.run(run_check())
    finally:
        approval.SubprocessAgent = original_agent

    assert result == "agent-machine"
    assert seen["agent_session_id"] == "machine-worker"
    assert seen["prep_prompt"] == "caller supplied provision"


def test_target_init_anchors_prep_events_to_provisioning_message():
    import orchs.manager._approval as approval
    from event_ingester import event_ingester
    from paths import ba_home
    from provider import StreamEvent
    from session_manager import manager as session_manager

    captured = {}
    original_provider_for_session = main.coordinator.provider_for_session

    class FakeProvider:
        def start_run(self, *, queue, loop, target_message_id=None, **_kwargs):
            captured["target_message_id"] = target_message_id

            async def emit():
                await queue.put(StreamEvent("agent_message", {
                    "type": "assistant",
                    "uuid": "prep-render-1",
                    "message": {"content": [{"type": "text", "text": "ready"}]},
                }))
                await queue.put(StreamEvent("complete", {"session_id": "agent-prepped"}))

            loop.create_task(emit())

        def cancel_turn(self, _run_id):
            captured["cancelled"] = True

    worker = session_manager.create(
        name="worker:provisioning-anchor",
        orchestration_mode="native",
        cwd="/tmp/project",
        model="glm-5.2",
        source="internal",
    )

    async def run_check():
        return await approval.init_target_agent_session(
            main.coordinator,
            bc_session=worker,
            model="glm-5.2",
            cwd="/tmp/project",
            description="worker",
            cancel_event=main.asyncio.Event(),
            provision_prompt="prep prompt",
        )

    main.coordinator.provider_for_session = lambda _sid: FakeProvider()
    try:
        result = main.asyncio.run(run_check())
    finally:
        main.coordinator.provider_for_session = original_provider_for_session

    assert result == "agent-prepped"
    refreshed = session_manager.get(worker["id"])
    messages = refreshed["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert [m.get("source") for m in messages] == ["provisioning", "provisioning"]
    provisioning_assistant_id = messages[1]["id"]
    assert captured["target_message_id"] == provisioning_assistant_id

    events_path = ba_home() / "sessions" / worker["id"] / "events.jsonl"
    rows = [
        json.loads(line)
        for line in events_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    prep_rows = [
        row for row in rows
        if row.get("type") == "agent_message"
        and (row.get("data") or {}).get("uuid") == "prep-render-1"
    ]
    assert prep_rows
    assert {row.get("msg_id") for row in prep_rows} == {provisioning_assistant_id}
    assert not event_ingester.root_events_by_sid(worker["id"]).get(worker["id"])


def test_concurrent_provision_of_same_worker_creates_exactly_one():
    """Two concurrent provisions of the same (name, cwd) must create exactly
    ONE worker. Self-proving: with the real per-(name,cwd) lock -> 1 create;
    with the lock neutered -> 2 creates (the race the lock kills)."""
    import asyncio as _asyncio

    body = {"cwd": "/tmp/race-project", "workers": [
        {"role_key": "singleton", "orchestration_mode": "native"}]}
    created_names: set[tuple[str, str]] = set()
    create_order: list[str] = []

    def fake_find(cwd, name):
        return (
            {"agent_session_id": "bc-1", "name": name, "cwd": cwd,
             "registry_cwd": cwd, "orchestration_mode": "native",
             "agent_sid": "agent-x", "initialized": True,
             "diverged": False, "delegation_count": 0}
            if (name, cwd) in created_names else None
        )

    async def fake_create(b, *_args, **_kwargs):
        create_order.append(b["name"])
        # Yield so a second unlocked coroutine can also enter create before
        # either records the worker — this is the race window. With the lock
        # held, the second coroutine is parked on lock acquisition here, not
        # inside create, so only one create runs.
        await _asyncio.sleep(0)
        created_names.add((b["name"], b["cwd"]))
        return {"agent_session_id": f"bc-{len(create_order)}", "name": b["name"],
                "cwd": b["cwd"], "registry_cwd": b["cwd"],
                "orchestration_mode": "native", "agent_sid": "agent-new",
                "initialized": True, "diverged": False, "delegation_count": 0}

    class _NoLock:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

    real_find = main._find_worker_by_session_name
    real_create = main._create_worker_from_body
    real_lock = main._provision_lock
    main._find_worker_by_session_name = fake_find
    main._create_worker_from_body = fake_create
    try:
        # Locked (real): concurrent provisions serialize -> 1 create.
        created_names.clear()
        create_order.clear()
        main._provision_lock = real_lock
        async def _two_provisions():
            return await _asyncio.gather(
                main._provision_workers_from_body(body),
                main._provision_workers_from_body(body),
            )

        results = _asyncio.run(_two_provisions())
        assert len(create_order) == 1, f"locked: expected 1 create, got {len(create_order)}"
        # One call created, the other reused the SAME singleton id.
        created_flags = [w["created"] for r in results for w in r["workers"]]
        assert sorted(created_flags) == [False, True], created_flags
        ids = {w["agent_session_id"] for r in results for w in r["workers"]}
        assert ids == {"bc-1"}, ids

        # Unlocked (neutered): the race window is reachable -> 2 creates.
        created_names.clear()
        create_order.clear()
        main._provision_lock = lambda _name, _cwd: _NoLock()
        _asyncio.run(_two_provisions())
        assert len(create_order) == 2, f"unlocked: expected 2 creates (race), got {len(create_order)}"
    finally:
        main._find_worker_by_session_name = real_find
        main._create_worker_from_body = real_create
        main._provision_lock = real_lock


def test_provision_broadcasts_created_worker_before_later_failure():
    import asyncio as _asyncio

    broadcasts = []
    create_order = []
    body = {
        "cwd": "/tmp/partial-project",
        "workers": [
            {"role_key": "created-worker", "orchestration_mode": "native"},
            {"role_key": "failing-worker", "orchestration_mode": "native"},
        ],
    }

    async def fake_broadcast(cwd):
        broadcasts.append(cwd)

    def fake_find(_cwd, _name):
        return None

    async def fake_create(b, *_args, **_kwargs):
        create_order.append(b["role_key"])
        if b["role_key"] == "failing-worker":
            raise RuntimeError("init failed")
        return {
            "agent_session_id": "bc-created",
            "name": b["name"],
            "cwd": b["cwd"],
            "registry_cwd": b["cwd"],
            "orchestration_mode": "native",
            "agent_sid": "agent-created",
            "initialized": True,
            "diverged": False,
            "delegation_count": 0,
        }

    real_find = main._find_worker_by_session_name
    real_create = main._create_worker_from_body
    real_broadcast = main.coordinator.broadcast_workers_changed
    main._find_worker_by_session_name = fake_find
    main._create_worker_from_body = fake_create
    main.coordinator.broadcast_workers_changed = fake_broadcast
    try:
        try:
            _asyncio.run(main._provision_workers_from_body(body))
        except RuntimeError as exc:
            assert str(exc) == "init failed"
        else:
            raise AssertionError("expected failing worker to abort the batch")
    finally:
        main._find_worker_by_session_name = real_find
        main._create_worker_from_body = real_create
        main.coordinator.broadcast_workers_changed = real_broadcast

    assert create_order == ["created-worker", "failing-worker"]
    assert broadcasts == [None]


if __name__ == "__main__":
    test_provision_workers_is_idempotent_by_role_key()
    test_provision_workers_remains_idempotent_after_session_title_changes()
    test_provision_workers_allows_per_worker_cwd()
    test_worker_list_projects_pools_from_tags()
    test_pool_workers_receive_peer_context_in_provision_prompt()
    test_existing_named_worker_backfills_pool_tags()
    test_worker_pool_enqueue_dispatches_to_idle_tagged_worker()
    test_internal_provision_workers_requires_internal_token()
    test_bare_provision_workers_returns_pending_without_init_turn()
    test_coordinator_target_init_proxy_accepts_ws_callback()
    test_target_init_accepts_custom_provision_prompt()
    test_target_init_anchors_prep_events_to_provisioning_message()
    test_concurrent_provision_of_same_worker_creates_exactly_one()
    test_provision_broadcasts_created_worker_before_later_failure()
    print("PASS: provision workers is idempotent by role key")
