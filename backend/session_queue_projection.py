from __future__ import annotations

import copy
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

import session_store
from session_queue_projection_runtime import (
    ProjectionRuntime,
    StorageContext,
    registry,
)
from session_queue_projection_dispatcher import (
    registry as dispatcher_registry,
)


_ENUMERATOR_CONTRACT = "session_store.root-json-files.v1"


def storage_context(
    storage_identity: Optional[Path] = None,
) -> StorageContext:
    identity = (
        session_store._sessions_dir()
        if storage_identity is None
        else Path(storage_identity)
    ).resolve()

    def session_files() -> tuple[Path, ...]:
        with session_store._storage_identity_scope(identity, wait=True):
            return tuple(session_store._session_json_files())

    return StorageContext.create(
        identity,
        session_files=session_files,
        enumerator_contract=_ENUMERATOR_CONTRACT,
    )


def runtime(
    storage_identity: Optional[Path] = None,
) -> ProjectionRuntime:
    return registry.acquire(storage_context(storage_identity))


def dispatcher(storage_identity: Optional[Path] = None):
    context = storage_context(storage_identity)
    return dispatcher_registry.acquire(context, registry.acquire(context))


@contextmanager
def producer_lease(
    storage_identity: Optional[Path] = None,
) -> Iterator[None]:
    with dispatcher(storage_identity).producer():
        yield


def _projection_dir(storage_identity: Optional[Path] = None) -> Path:
    return storage_context(storage_identity).database_path.parent


def _database_path(storage_identity: Optional[Path] = None) -> Path:
    return storage_context(storage_identity).database_path


def _record_path(
    session_id: str,
    generation: Optional[str] = None,
    *,
    storage_identity: Optional[Path] = None,
) -> Path:
    del session_id, generation
    return _database_path(storage_identity)


def _session_files_fingerprint(
    storage_identity: Optional[Path] = None,
) -> dict[str, list[int]]:
    return storage_context(storage_identity).fingerprint()


def projection_is_current(
    *, storage_identity: Optional[Path] = None,
) -> bool:
    return runtime(storage_identity).projection_is_current()


def certification_generation(
    *, storage_identity: Optional[Path] = None,
) -> int:
    return runtime(storage_identity).certification_generation()


def mark_dirty(*, storage_identity: Optional[Path] = None) -> int:
    return runtime(storage_identity).mark_dirty()


def upsert_record(
    record: dict[str, Any],
    *,
    storage_identity: Optional[Path] = None,
) -> None:
    submissions = dispatcher(storage_identity)
    submissions.drain()
    runtime(storage_identity).upsert(record)


def upsert_record_background(
    record: dict[str, Any],
    *,
    storage_identity: Optional[Path] = None,
) -> None:
    dispatcher(storage_identity).submit(record)


def note_persisted_tree(
    root: dict[str, Any],
    *,
    storage_identity: Optional[Path] = None,
) -> int:
    records = [
        record
        for node in _walk_nodes(root)
        if (record := project_session(node)) is not None
    ]
    submissions = dispatcher(storage_identity)
    for record in records:
        submissions.submit(record)
    submissions.drain()
    return runtime(storage_identity).note_persisted(records)


def upsert_from_session(
    session: dict[str, Any],
    *,
    storage_identity: Optional[Path] = None,
) -> None:
    record = project_session(session)
    if record is not None:
        upsert_record(record, storage_identity=storage_identity)


def delete_records(
    session_ids: Iterable[str],
    *,
    storage_identity: Optional[Path] = None,
) -> None:
    submissions = dispatcher(storage_identity)
    submissions.drain()
    runtime(storage_identity).delete(session_ids)


def delete_record(
    session_id: str,
    *,
    storage_identity: Optional[Path] = None,
) -> None:
    delete_records((session_id,), storage_identity=storage_identity)


def flush_pending_writes(
    timeout: Optional[float] = None,
    *,
    storage_identity: Optional[Path] = None,
) -> bool:
    dispatcher(storage_identity).drain(timeout)
    return runtime(storage_identity).flush(timeout)


def get(
    session_id: str,
    *,
    storage_identity: Optional[Path] = None,
) -> Optional[dict[str, Any]]:
    pending = dispatcher(storage_identity).overlay((session_id,))
    if session_id in pending:
        return pending[session_id]
    return runtime(storage_identity).get(session_id)


def get_many(
    session_ids: Iterable[str],
    *,
    storage_identity: Optional[Path] = None,
) -> dict[str, dict[str, Any]]:
    ids = tuple(session_ids)
    records = runtime(storage_identity).get_many(ids)
    records.update(dispatcher(storage_identity).overlay(ids))
    return records


def queued_prompts(
    session_id: str,
    *,
    storage_identity: Optional[Path] = None,
) -> list[dict[str, Any]]:
    record = get(session_id, storage_identity=storage_identity)
    return [
        item
        for item in (record or {}).get("queued_prompts", [])
        if isinstance(item, dict)
    ]


def rebuild_from_disk(
    *, storage_identity: Optional[Path] = None,
) -> int:
    dispatcher(storage_identity).drain()
    return runtime(storage_identity).rebuild(_project_root)


def ensure_current_or_rebuild(
    *, storage_identity: Optional[Path] = None,
) -> bool:
    dispatcher(storage_identity).drain()
    projection_runtime = runtime(storage_identity)
    rebuilt = projection_runtime.ensure_current_or_rebuild(_project_root)
    if not rebuilt:
        _purge_dead_records(projection_runtime)
    return rebuilt


def list_queued_records(
    *, storage_identity: Optional[Path] = None,
) -> list[dict[str, Any]]:
    dispatcher(storage_identity).drain()
    selected = runtime(storage_identity).records(
        lambda record: bool(record.get("queued_prompts")),
    )
    return sorted(selected, key=lambda record: record["id"])


def queued_counts(
    *, storage_identity: Optional[Path] = None,
) -> dict[str, int]:
    dispatcher(storage_identity).drain()
    selected = runtime(storage_identity).records(
        lambda record: bool(record.get("queued_prompts")),
    )
    return {
        record["id"]: len(record.get("queued_prompts") or [])
        for record in selected
    }


def shutdown(
    *,
    storage_identity: Optional[Path] = None,
    timeout: Optional[float] = None,
    certify: bool = False,
) -> None:
    context = storage_context(storage_identity)
    dispatcher_registry.close(context, timeout=timeout)
    registry.shutdown(context, timeout=timeout, certify=certify)
    dispatcher_registry.release(context)


def debug_snapshot(
    *, storage_identity: Optional[Path] = None,
) -> dict[str, Any]:
    return runtime(storage_identity).debug_snapshot()


def _compact_ack(message: dict[str, Any]) -> Optional[dict[str, Any]]:
    client_id = message.get("client_id")
    if not isinstance(client_id, str) or not client_id:
        return None
    ack: dict[str, Any] = {"client_id": client_id}
    for key in ("id", "lifecycle_msg_id", "seq", "timestamp"):
        if message.get(key) is not None:
            ack[key] = copy.deepcopy(message[key])
    return ack


def _user_message_projection(messages: Iterable[Any]) -> dict[str, Any]:
    acks: dict[str, dict[str, Any]] = {}
    lifecycle_ids: list[str] = []
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        ack = _compact_ack(message)
        if ack is not None:
            acks[ack["client_id"]] = ack
        lifecycle_id = message.get("lifecycle_msg_id")
        if isinstance(lifecycle_id, str) and lifecycle_id:
            lifecycle_ids.append(lifecycle_id)
    return {
        "user_message_acks": acks,
        "user_lifecycle_msg_ids": list(dict.fromkeys(lifecycle_ids)),
    }


def project_session(
    session: dict[str, Any],
) -> Optional[dict[str, Any]]:
    sid = session.get("id")
    if not isinstance(sid, str) or not sid:
        return None
    users = _user_message_projection(session.get("messages") or [])
    client_ids = set(users["user_message_acks"])
    lifecycle_ids = set(users["user_lifecycle_msg_ids"])
    queued = []
    for prompt in session.get("queued_prompts") or []:
        if not isinstance(prompt, dict):
            continue
        if (
            prompt.get("client_id") in client_ids
            or prompt.get("lifecycle_msg_id") in lifecycle_ids
        ):
            continue
        queued.append(copy.deepcopy(prompt))
    return {
        "id": sid,
        "model": session.get("model"),
        "cwd": session.get("cwd"),
        "queued_prompts": queued,
        **users,
    }


def _walk_nodes(node: dict[str, Any]) -> Iterable[dict[str, Any]]:
    yield node
    for fork in node.get("forks") or []:
        if isinstance(fork, dict):
            yield from _walk_nodes(fork)


def _project_root(root: dict[str, Any]) -> Iterable[dict[str, Any]]:
    for node in _walk_nodes(root):
        record = project_session(node)
        if record is not None:
            yield record


def _purge_dead_records(projection_runtime: ProjectionRuntime) -> None:
    from session_manager import manager as session_manager

    dead = [
        record["id"]
        for record in projection_runtime.records()
        if not session_manager.is_live_session(record["id"])
    ]
    if dead:
        projection_runtime.delete(dead)
