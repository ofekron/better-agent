from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import _test_home
import pytest

_TEST_HOME = Path(_test_home.isolate("ba-test-node-provider-credential-sync-"))
_HERE = Path(__file__).resolve().parent
_BACKEND = _HERE.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import config_store  # noqa: E402
import extension_api  # noqa: E402
import machine_nodes_api  # noqa: E402
import node_link  # noqa: E402
import node_provider_credential_sync as credential_sync  # noqa: E402
import node_registry_store  # noqa: E402
import node_rpc_handlers  # noqa: E402
import node_store  # noqa: E402
from stores import node_provider_credential_grants as grants  # noqa: E402


PROVIDERS = {
    "zai-claude": {
        "id": "zai-claude",
        "name": "Z.AI Claude",
        "generation": "zai-generation",
        "execution_revision": 1,
        "mode": "api_key",
    },
    "other": {
        "id": "other",
        "name": "Other",
        "generation": "other-generation",
        "execution_revision": 1,
        "mode": "api_key",
    },
}


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch):
    for path in (grants._path(), Path(str(grants._path()) + ".lock")):
        path.unlink(missing_ok=True)
    monkeypatch.setattr(
        node_registry_store,
        "get",
        lambda node_id: {
            "node_id": node_id,
            "secret_hash": "$argon2id$current-node-authority",
        },
    )
    monkeypatch.setattr(
        config_store,
        "get_provider",
        lambda provider_id: dict(PROVIDERS[provider_id])
        if provider_id in PROVIDERS
        else None,
    )
    monkeypatch.setattr(
        config_store,
        "list_providers",
        lambda: {"providers": [dict(provider) for provider in PROVIDERS.values()]},
    )
    monkeypatch.setattr(
        config_store,
        "export_provider_sync_state",
        lambda provider_ids=None: {
            "provider_state_authority": {
                "generation": "provider-state-generation",
                "revision": 7,
                "digest": "provider-state-digest",
            },
            "default_provider_id": "zai-claude",
            "providers": [dict(provider) for provider in PROVIDERS.values()],
            **(
                {
                    "provider_api_keys": [
                        {"provider_id": provider_id, "api_key": f"secret-{provider_id}"}
                        for provider_id in provider_ids
                    ]
                }
                if provider_ids
                else {}
            ),
        },
    )
    connection = SimpleNamespace(
        ws=SimpleNamespace(
            url=SimpleNamespace(scheme="ws"),
            client=SimpleNamespace(host="127.0.0.1"),
        )
    )
    monkeypatch.setattr(node_store, "get_connection", lambda _node_id: connection)
    monkeypatch.setattr(node_link, "connection_allows_credential_sync", lambda _conn: True)
    credential_sync._reset_for_tests()
    yield
    credential_sync._reset_for_tests()
    for path in (grants._path(), Path(str(grants._path()) + ".lock")):
        path.unlink(missing_ok=True)


def _ack(params: dict) -> dict:
    state = params["provider_state"]
    return {
        "provider_api_key_count": len(state.get("provider_api_keys", [])),
        "provider_api_key_ids": [
            item["provider_id"] for item in state.get("provider_api_keys", [])
        ],
        "provider_state_authority": state["provider_state_authority"],
        "sync_status": "credentials_applied",
    }


def test_authorize_persists_pending_before_selected_only_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    async def call(node_id, method, params, **kwargs):
        seen.update(
            node_id=node_id,
            method=method,
            params=params,
            kwargs=kwargs,
            status_during_send=grants.public_for_node(node_id)[0]["status"],
        )
        return _ack(params)

    monkeypatch.setattr(node_rpc_handlers, "call_local_or_remote", call)
    result = asyncio.run(
        credential_sync.authorize_and_sync("lenovo", ["zai-claude"])
    )

    assert result["provider_credentials"] == [
        {
            "node_id": "lenovo",
            "provider_id": "zai-claude",
            "provider_name": "Z.AI Claude",
            "status": "synced",
            "authorized_at": result["provider_credentials"][0]["authorized_at"],
            "updated_at": result["provider_credentials"][0]["updated_at"],
        }
    ]
    assert seen["status_during_send"] == "pending"
    assert seen["node_id"] == "lenovo"
    assert seen["method"] == "sync_provider_config"
    assert seen["kwargs"] == {
        "secure_transport_required": True,
        "version_ready_required": True,
        "expected_connection": node_store.get_connection("lenovo"),
    }
    sent_keys = seen["params"]["provider_state"]["provider_api_keys"]
    assert [item["provider_id"] for item in sent_keys] == ["zai-claude"]


def test_failed_send_persists_scrubbed_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def call(*_args, **_kwargs):
        raise RuntimeError("rpc exploded with secret-zai-claude")

    monkeypatch.setattr(node_rpc_handlers, "call_local_or_remote", call)
    with pytest.raises(RuntimeError, match="credential sync failed"):
        asyncio.run(credential_sync.authorize_and_sync("lenovo", ["zai-claude"]))

    [status] = grants.public_for_node("lenovo")
    assert status["status"] == "failed"
    assert status["failure_code"] == "rpc_failed"
    assert "secret-zai-claude" not in str(status)
    assert "exploded" not in str(status)


def test_ack_mismatch_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    async def call(_node_id, _method, params, **_kwargs):
        ack = _ack(params)
        ack["provider_api_key_ids"] = []
        return ack

    monkeypatch.setattr(node_rpc_handlers, "call_local_or_remote", call)
    with pytest.raises(RuntimeError, match="acknowledgement mismatch"):
        asyncio.run(credential_sync.authorize_and_sync("lenovo", ["zai-claude"]))
    assert grants.public_for_node("lenovo")[0]["status"] == "failed"


def test_replay_sends_only_valid_grants(monkeypatch: pytest.MonkeyPatch) -> None:
    authority = credential_sync.node_authority("lenovo")
    grants.authorize(
        node_id="lenovo",
        node_authority=authority,
        provider_id="zai-claude",
        provider_generation="zai-generation",
        provider_name="Z.AI Claude",
        operation_id="operation-replay",
    )
    sent: list[list[str]] = []

    async def call(_node_id, _method, params, **kwargs):
        sent.append(
            [
                item["provider_id"]
                for item in params["provider_state"]["provider_api_keys"]
            ]
        )
        assert kwargs["version_ready_required"] is False
        return _ack(params)

    monkeypatch.setattr(node_rpc_handlers, "call_local_or_remote", call)
    result = asyncio.run(
        credential_sync.replay_grants("lenovo", version_ready_required=False)
    )
    assert sent == [["zai-claude"]]
    assert result["provider_credentials"][0]["status"] == "synced"


def test_replay_without_grants_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    async def unexpected(*_args, **_kwargs):
        raise AssertionError("credential RPC must not run without consent")

    monkeypatch.setattr(node_rpc_handlers, "call_local_or_remote", unexpected)
    assert asyncio.run(credential_sync.replay_grants("lenovo")) == {
        "node_id": "lenovo",
        "provider_credentials": [],
    }
    asyncio.run(credential_sync.ensure_for_admission("lenovo", "zai-claude"))


def test_admission_resyncs_stale_acknowledged_provider_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []

    async def call(_node_id, _method, params, **_kwargs):
        provider = next(
            item
            for item in params["provider_state"]["providers"]
            if item["id"] == "zai-claude"
        )
        calls.append(provider["execution_revision"])
        return _ack(params)

    monkeypatch.setattr(node_rpc_handlers, "call_local_or_remote", call)
    asyncio.run(credential_sync.authorize_and_sync("lenovo", ["zai-claude"]))
    monkeypatch.setitem(PROVIDERS["zai-claude"], "execution_revision", 2)
    asyncio.run(credential_sync.ensure_for_admission("lenovo", "zai-claude"))
    assert calls == [1, 2]


def test_insecure_transport_fails_before_export(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(node_link, "connection_allows_credential_sync", lambda _conn: False)
    with pytest.raises(RuntimeError, match="WSS or loopback"):
        asyncio.run(credential_sync.authorize_and_sync("lenovo", ["zai-claude"]))
    assert grants.public_for_node("lenovo") == []


def test_explicit_route_delegates_selected_credentials_to_consent_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    async def authorize(node_id: str, provider_ids: object) -> dict:
        seen["node_id"] = node_id
        seen["provider_ids"] = provider_ids
        return {
            "node_id": node_id,
            "provider_credentials": [
                {"provider_id": "zai-claude", "status": "synced"}
            ],
        }

    monkeypatch.setattr(credential_sync, "authorize_and_sync", authorize)

    class Request:
        method = "POST"

        async def body(self) -> bytes:
            return json.dumps(
                {
                    "include_secrets": True,
                    "provider_ids": ["zai-claude"],
                }
            ).encode("utf-8")

    response = asyncio.run(
        extension_api._dispatch_machine_nodes_core_backend(
            "nodes/lenovo/sync-providers",
            Request(),
        )
    )
    assert response is not None
    assert response.status_code == 200
    assert seen == {"node_id": "lenovo", "provider_ids": ["zai-claude"]}
    payload = json.loads(response.body)
    assert payload["provider_credentials"] == [
        {"provider_id": "zai-claude", "status": "synced"}
    ]


def test_node_snapshot_projection_is_secret_free() -> None:
    authority = credential_sync.node_authority("lenovo")
    grants.authorize(
        node_id="lenovo",
        node_authority=authority,
        provider_id="zai-claude",
        provider_generation="zai-generation",
        provider_name="Z.AI Claude",
        operation_id="operation-snapshot",
    )
    projected = credential_sync.project_node_snapshots(
        [
            {"id": "primary", "role": "primary"},
            {"id": "lenovo", "role": "worker_node"},
        ]
    )
    assert "provider_credentials" not in projected[0]
    assert projected[1]["provider_credentials"][0]["status"] == "pending"
    assert "node_authority" not in str(projected)
    assert "provider_generation" not in str(projected)
    assert "api_key" not in str(projected)


def test_node_revoke_removes_credential_grants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = credential_sync.node_authority("lenovo")
    grants.authorize(
        node_id="lenovo",
        node_authority=authority,
        provider_id="zai-claude",
        provider_generation="zai-generation",
        provider_name="Z.AI Claude",
        operation_id="operation-revoke",
    )
    monkeypatch.setattr(machine_nodes_api, "require_role_internal", lambda _role: None)
    monkeypatch.setattr(node_registry_store, "remove", lambda _node_id: True)

    async def forget(_node_id: str) -> None:
        return None

    monkeypatch.setattr(node_store, "forget", forget)
    result = asyncio.run(
        machine_nodes_api.internal_revoke_node(
            {"node_id": "lenovo"},
            x_internal_token="test",
        )
    )
    assert result == {"status": "revoked", "node_id": "lenovo"}
    assert grants.public_for_node("lenovo") == []


def test_connection_replacement_before_send_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_connection = node_store.get_connection("lenovo")
    replacement = SimpleNamespace(ws=original_connection.ws)
    current = {"connection": original_connection}
    original_export = config_store.export_provider_sync_state

    def export(provider_ids=None):
        result = original_export(provider_ids)
        current["connection"] = replacement
        return result

    async def unexpected(*_args, **_kwargs):
        raise AssertionError("credentials must not be sent on a replacement connection")

    monkeypatch.setattr(node_store, "get_connection", lambda _node_id: current["connection"])
    monkeypatch.setattr(config_store, "export_provider_sync_state", export)
    monkeypatch.setattr(node_rpc_handlers, "call_local_or_remote", unexpected)

    with pytest.raises(
        credential_sync.CredentialSyncConnectionChanged,
        match="connection changed",
    ):
        asyncio.run(credential_sync.authorize_and_sync("lenovo", ["zai-claude"]))


def test_reapproval_during_rpc_cannot_ack_old_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = {"secret_hash": "$argon2id$old-authority"}
    monkeypatch.setattr(
        node_registry_store,
        "get",
        lambda node_id: {"node_id": node_id, "secret_hash": registry["secret_hash"]},
    )

    async def call(_node_id, _method, params, **_kwargs):
        registry["secret_hash"] = "$argon2id$new-authority"
        return _ack(params)

    monkeypatch.setattr(node_rpc_handlers, "call_local_or_remote", call)
    with pytest.raises(RuntimeError, match="authority changed"):
        asyncio.run(credential_sync.authorize_and_sync("lenovo", ["zai-claude"]))
    assert grants.public_for_node("lenovo") == []


def test_stale_prune_emits_empty_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = credential_sync.node_authority("lenovo")
    grants.authorize(
        node_id="lenovo",
        node_authority=authority,
        provider_id="zai-claude",
        provider_generation="stale-generation",
        provider_name="Z.AI Claude",
        operation_id="stale-operation",
    )
    projections: list[list[dict]] = []
    credential_sync.add_listener(
        lambda _node_id, public: projections.append(public)
    )

    asyncio.run(credential_sync.replay_grants("lenovo"))

    assert projections == [[]]


def test_blocking_grant_store_work_does_not_block_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import time

    original_begin = grants.begin_sync

    def slow_begin(**kwargs):
        time.sleep(0.05)
        return original_begin(**kwargs)

    monkeypatch.setattr(grants, "begin_sync", slow_begin)
    monkeypatch.setattr(
        node_rpc_handlers,
        "call_local_or_remote",
        lambda *_args, **_kwargs: None,
    )

    async def run() -> None:
        ticked = asyncio.Event()

        async def tick() -> None:
            await asyncio.sleep(0.01)
            ticked.set()

        task = asyncio.create_task(tick())
        with pytest.raises(RuntimeError):
            await credential_sync.authorize_and_sync("lenovo", ["zai-claude"])
        assert ticked.is_set()
        await task

    asyncio.run(run())


def test_overlapping_syncs_are_serialized_per_node(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    async def call(_node_id, _method, params, **_kwargs):
        calls.append(len(calls) + 1)
        if len(calls) == 1:
            first_started.set()
            await release_first.wait()
        return _ack(params)

    monkeypatch.setattr(node_rpc_handlers, "call_local_or_remote", call)

    async def run() -> None:
        first = asyncio.create_task(
            credential_sync.authorize_and_sync("lenovo", ["zai-claude"])
        )
        await first_started.wait()
        second = asyncio.create_task(
            credential_sync.authorize_and_sync("lenovo", ["zai-claude"])
        )
        await asyncio.sleep(0)
        assert calls == [1]
        release_first.set()
        await asyncio.gather(first, second)

    asyncio.run(run())
    assert calls == [1, 2]
    assert grants.public_for_node("lenovo")[0]["status"] == "synced"


def test_admission_without_grant_allows_manually_configured_plain_ws(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = node_store.get_connection("lenovo")
    monkeypatch.setattr(
        node_link,
        "connection_allows_credential_sync",
        lambda _connection: False,
    )
    asyncio.run(
        credential_sync.ensure_for_admission(
            "lenovo",
            "zai-claude",
            expected_connection=connection,
        )
    )


def test_synced_grant_does_not_require_secure_transport_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def call(_node_id, _method, params, **_kwargs):
        return _ack(params)

    monkeypatch.setattr(node_rpc_handlers, "call_local_or_remote", call)
    asyncio.run(credential_sync.authorize_and_sync("lenovo", ["zai-claude"]))
    connection = node_store.get_connection("lenovo")
    monkeypatch.setattr(
        node_link,
        "connection_allows_credential_sync",
        lambda _connection: False,
    )
    asyncio.run(
        credential_sync.ensure_for_admission(
            "lenovo",
            "zai-claude",
            expected_connection=connection,
        )
    )


def test_malformed_acknowledgement_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def call(_node_id, _method, params, **_kwargs):
        ack = _ack(params)
        ack["provider_api_key_ids"] = [{}]
        return ack

    monkeypatch.setattr(node_rpc_handlers, "call_local_or_remote", call)
    with pytest.raises(RuntimeError, match="acknowledgement mismatch"):
        asyncio.run(credential_sync.authorize_and_sync("lenovo", ["zai-claude"]))
    assert grants.public_for_node("lenovo")[0]["status"] == "failed"
