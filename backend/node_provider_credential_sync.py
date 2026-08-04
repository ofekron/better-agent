from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import uuid
from collections.abc import Awaitable, Callable

from stores import node_provider_credential_grants as grants


StatusListener = Callable[[str, list[dict]], Awaitable[None] | None]
logger = logging.getLogger(__name__)
_listeners: list[StatusListener] = []
_node_locks: dict[str, asyncio.Lock] = {}


class CredentialSyncConnectionChanged(RuntimeError):
    pass


def add_listener(listener: StatusListener) -> None:
    if listener not in _listeners:
        _listeners.append(listener)


def remove_listener(listener: StatusListener) -> None:
    if listener in _listeners:
        _listeners.remove(listener)


def node_authority(node_id: str) -> str:
    import node_registry_store

    record = node_registry_store.get(node_id)
    if not isinstance(record, dict):
        raise ValueError(f"node {node_id!r} is not approved")
    secret_hash = record.get("secret_hash")
    if not isinstance(secret_hash, str) or not secret_hash:
        raise ValueError(f"node {node_id!r} has no credential authority")
    return hashlib.sha256(secret_hash.encode("utf-8")).hexdigest()


def _provider(provider_id: str) -> dict:
    import config_store

    provider = config_store.get_provider(provider_id)
    if not isinstance(provider, dict):
        raise ValueError(f"provider {provider_id!r} is not configured")
    if provider.get("mode") != "api_key":
        raise ValueError(f"provider {provider_id!r} does not use API-key credentials")
    generation = provider.get("generation")
    name = provider.get("name")
    if not isinstance(generation, str) or not generation:
        raise ValueError(f"provider {provider_id!r} has no generation authority")
    if not isinstance(name, str) or not name:
        raise ValueError(f"provider {provider_id!r} has no display name")
    return provider


def _selected_providers(provider_ids: object) -> list[dict]:
    if not isinstance(provider_ids, list | tuple) or not provider_ids:
        raise ValueError("provider_ids must list credentials to send")
    selected: list[dict] = []
    seen: set[str] = set()
    for value in provider_ids:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("provider_ids must contain non-empty strings")
        provider_id = value.strip()
        if provider_id in seen:
            continue
        seen.add(provider_id)
        selected.append(_provider(provider_id))
    return selected


def _provider_authorities() -> dict[str, tuple[str, int]]:
    import config_store

    state = config_store.export_provider_sync_state()
    providers = state.get("providers", []) if isinstance(state, dict) else []
    return {
        str(provider["id"]): (
            str(provider["generation"]),
            provider["execution_revision"],
        )
        for provider in providers
        if isinstance(provider, dict)
        and isinstance(provider.get("id"), str)
        and isinstance(provider.get("generation"), str)
        and isinstance(provider.get("execution_revision"), int)
        and not isinstance(provider.get("execution_revision"), bool)
        and provider["execution_revision"] >= 0
    }


def _valid_records(
    node_id: str,
    provider_authorities: dict[str, tuple[str, int]] | None = None,
) -> list[dict]:
    try:
        authority = node_authority(node_id)
    except ValueError:
        grants.remove_node(node_id)
        return []
    authorities = (
        provider_authorities
        if provider_authorities is not None
        else _provider_authorities()
    )
    return grants.valid_for_node(
        node_id,
        authority,
        {
            provider_id: provider_authority[0]
            for provider_id, provider_authority in authorities.items()
        },
    )


def _valid_records_with_prune(
    node_id: str,
    provider_authorities: dict[str, tuple[str, int]] | None = None,
) -> tuple[list[dict], bool]:
    try:
        authority = node_authority(node_id)
    except ValueError:
        return [], grants.remove_node(node_id)
    authorities = provider_authorities or _provider_authorities()
    return grants.valid_for_node_with_prune(
        node_id,
        authority,
        {
            provider_id: provider_authority[0]
            for provider_id, provider_authority in authorities.items()
        },
    )


def snapshot(node_id: str) -> list[dict]:
    _valid_records(node_id)
    return grants.public_for_node(node_id)


def project_node_snapshots(nodes: list[dict]) -> list[dict]:
    projected: list[dict] = []
    for node in nodes:
        row = dict(node)
        node_id = str(row.get("id") or "")
        if node_id and row.get("role") == "worker_node":
            row["provider_credentials"] = snapshot(node_id)
        projected.append(row)
    return projected


async def _emit(node_id: str) -> None:
    public = await asyncio.to_thread(snapshot, node_id)
    for listener in tuple(_listeners):
        try:
            result = listener(node_id, public)
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.exception(
                "node provider credential status listener failed for %s",
                node_id,
            )


def _assert_secure_transport(node_id: str, expected_connection=None):
    import node_link
    import node_store

    connection = node_store.get_connection(node_id)
    if expected_connection is not None and connection is not expected_connection:
        raise CredentialSyncConnectionChanged(
            "node connection changed during credential sync"
        )
    if connection is None:
        raise RuntimeError(f"node {node_id!r} is not connected")
    if not node_link.connection_allows_credential_sync(connection):
        raise RuntimeError(
            "provider credential sync requires a WSS or loopback node connection"
        )
    return connection


def _mark_records(
    records: list[dict],
    status: str,
    failure_code: str = "",
    acknowledged_revisions: dict[str, int] | None = None,
) -> bool:
    updated = True
    for record in records:
        marked = grants.mark_status(
            node_id=record["node_id"],
            node_authority=record["node_authority"],
            provider_id=record["provider_id"],
            provider_generation=record["provider_generation"],
            operation_id=record["operation_id"],
            status=status,
            failure_code=failure_code,
            acknowledged_execution_revision=(
                (acknowledged_revisions or {}).get(record["provider_id"])
            ),
        )
        updated = marked is not None and updated
    return updated


def _ack_matches(payload: dict, result: object, provider_ids: list[str]) -> bool:
    if not isinstance(result, dict):
        return False
    acknowledged_ids = result.get("provider_api_key_ids")
    return (
        isinstance(acknowledged_ids, list)
        and all(
            isinstance(provider_id, str) and bool(provider_id)
            for provider_id in acknowledged_ids
        )
        and set(acknowledged_ids) == set(provider_ids)
        and len(acknowledged_ids) == len(provider_ids)
        and result.get("sync_status")
        in {"applied", "credentials_applied", "unchanged"}
        and result.get("provider_state_authority")
        == payload.get("provider_state_authority")
    )


def _lock_for_node(node_id: str) -> asyncio.Lock:
    lock = _node_locks.get(node_id)
    if lock is None:
        lock = asyncio.Lock()
        _node_locks[node_id] = lock
    return lock


def _begin_records(records: list[dict], operation_id: str) -> list[dict] | None:
    begun: list[dict] = []
    for record in records:
        current = grants.begin_sync(
            node_id=record["node_id"],
            node_authority=record["node_authority"],
            provider_id=record["provider_id"],
            provider_generation=record["provider_generation"],
            operation_id=operation_id,
        )
        if current is None:
            return None
        begun.append(current)
    return begun


def _records_authority_is_current(node_id: str, records: list[dict]) -> bool:
    try:
        authority = node_authority(node_id)
        provider_authorities = _provider_authorities()
    except ValueError:
        return False
    return all(
        record["node_authority"] == authority
        and provider_authorities.get(record["provider_id"], (None, None))[0]
        == record["provider_generation"]
        for record in records
    ) and grants.operations_are_current(records)


async def _sync_records(
    node_id: str,
    records: list[dict],
    *,
    version_ready_required: bool,
    expected_connection,
) -> dict:
    if not records:
        return {"node_id": node_id, "provider_credentials": []}
    _assert_secure_transport(node_id, expected_connection)
    operation_id = str(uuid.uuid4())
    records = await asyncio.to_thread(_begin_records, records, operation_id)
    if records is None:
        raise RuntimeError("provider credential sync authority changed")
    await _emit(node_id)

    provider_ids = [record["provider_id"] for record in records]
    try:
        import config_store
        from node_rpc_handlers import call_local_or_remote

        provider_state = await asyncio.to_thread(
            config_store.export_provider_sync_state,
            provider_ids,
        )
    except Exception:
        await asyncio.to_thread(
            _mark_records,
            records,
            "failed",
            "credential_unavailable",
        )
        await _emit(node_id)
        raise RuntimeError(
            "provider credential sync failed: credential unavailable"
        ) from None

    if not await asyncio.to_thread(
        _records_authority_is_current,
        node_id,
        records,
    ):
        await _emit(node_id)
        raise RuntimeError("provider credential sync authority changed")
    _assert_secure_transport(node_id, expected_connection)

    try:
        result = await call_local_or_remote(
            node_id,
            "sync_provider_config",
            {"provider_state": provider_state},
            secure_transport_required=True,
            version_ready_required=version_ready_required,
            expected_connection=expected_connection,
        )
    except Exception:
        await asyncio.to_thread(_mark_records, records, "failed", "rpc_failed")
        await _emit(node_id)
        raise RuntimeError("provider credential sync failed") from None

    if not await asyncio.to_thread(
        _records_authority_is_current,
        node_id,
        records,
    ):
        await _emit(node_id)
        raise RuntimeError("provider credential sync authority changed")
    _assert_secure_transport(node_id, expected_connection)

    if not _ack_matches(provider_state, result, provider_ids):
        await asyncio.to_thread(_mark_records, records, "failed", "ack_mismatch")
        await _emit(node_id)
        raise RuntimeError("provider credential sync acknowledgement mismatch")

    acknowledged_revisions = {
        provider["id"]: provider["execution_revision"]
        for provider in provider_state.get("providers", [])
        if isinstance(provider, dict)
        and provider.get("id") in provider_ids
        and isinstance(provider.get("execution_revision"), int)
        and not isinstance(provider.get("execution_revision"), bool)
    }
    if set(acknowledged_revisions) != set(provider_ids):
        await asyncio.to_thread(_mark_records, records, "failed", "ack_mismatch")
        await _emit(node_id)
        raise RuntimeError("provider credential sync acknowledgement mismatch")
    marked = await asyncio.to_thread(
        _mark_records,
        records,
        "synced",
        "",
        acknowledged_revisions,
    )
    if not marked:
        await _emit(node_id)
        raise RuntimeError("provider credential sync authority changed")
    await _emit(node_id)
    return {
        **result,
        "node_id": node_id,
        "provider_credentials": await asyncio.to_thread(snapshot, node_id),
    }


async def authorize_and_sync(node_id: str, provider_ids: object) -> dict:
    async with _lock_for_node(node_id):
        expected_connection = _assert_secure_transport(node_id)
        authority, selected = await asyncio.gather(
            asyncio.to_thread(node_authority, node_id),
            asyncio.to_thread(_selected_providers, provider_ids),
        )
        operation_id = str(uuid.uuid4())
        records = await asyncio.to_thread(
            lambda: [
                grants.authorize(
                    node_id=node_id,
                    node_authority=authority,
                    provider_id=provider["id"],
                    provider_generation=provider["generation"],
                    provider_name=provider["name"],
                    operation_id=operation_id,
                )
                for provider in selected
            ]
        )
        await _emit(node_id)
        return await _sync_records(
            node_id,
            records,
            version_ready_required=True,
            expected_connection=expected_connection,
        )


async def replay_grants(
    node_id: str,
    *,
    version_ready_required: bool = False,
    provider_ids: set[str] | None = None,
) -> dict:
    async with _lock_for_node(node_id):
        expected_connection = _assert_secure_transport(node_id)
        records, pruned = await asyncio.to_thread(
            _valid_records_with_prune,
            node_id,
        )
        if pruned:
            await _emit(node_id)
        if provider_ids is not None:
            records = [
                record for record in records if record["provider_id"] in provider_ids
            ]
        return await _sync_records(
            node_id,
            records,
            version_ready_required=version_ready_required,
            expected_connection=expected_connection,
        )


async def ensure_for_admission(
    node_id: str,
    provider_id: str,
    *,
    expected_connection=None,
) -> None:
    async with _lock_for_node(node_id):
        provider_authorities = await asyncio.to_thread(_provider_authorities)
        records, pruned = await asyncio.to_thread(
            _valid_records_with_prune,
            node_id,
            provider_authorities,
        )
        if pruned:
            await _emit(node_id)
        matching = [
            record for record in records if record["provider_id"] == provider_id
        ]
        if not matching:
            return
        current_authority = provider_authorities.get(provider_id)
        current_revision = current_authority[1] if current_authority else None
        if (
            matching[0]["status"] == "synced"
            and matching[0].get("acknowledged_execution_revision")
            == current_revision
        ):
            return
        expected_connection = _assert_secure_transport(
            node_id,
            expected_connection,
        )
        await _sync_records(
            node_id,
            matching,
            version_ready_required=True,
            expected_connection=expected_connection,
        )


def remove_node(node_id: str) -> bool:
    return grants.remove_node(node_id)


def _reset_for_tests() -> None:
    _listeners.clear()
    _node_locks.clear()
