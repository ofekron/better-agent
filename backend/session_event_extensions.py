from __future__ import annotations

import json
import logging
import queue
import threading
import time
from dataclasses import dataclass
from typing import Any

import extension_backend_loader
import extension_store
import session_local_projection
from env_compat import get_env

logger = logging.getLogger(__name__)
_EXTENSION_WORKERS = 1
_EXTENSION_QUEUE_SIZE = 1000
_EXTENSION_QUEUES: dict[str, "queue.Queue[ExtensionHookJob]"] = {}
_EXTENSION_QUEUE_GUARD: Any = None
_HOOK_SNAPSHOT_TTL_S = 1.0
_HOOK_SNAPSHOT: "SessionEventHookSnapshot | None" = None
_HOOK_SNAPSHOT_SIGNATURE: tuple[Any, ...] | None = None
_HOOK_SNAPSHOT_CHECKED_AT = 0.0
_HOOK_SNAPSHOT_REFRESHING = False
_HOOK_SNAPSHOT_GUARD = threading.Lock()
_SESSION_EVENT_RECONCILE_ATTEMPT_SIGNATURE: Any = object()


def _queue_guard():
    import threading

    global _EXTENSION_QUEUE_GUARD
    if _EXTENSION_QUEUE_GUARD is None:
        _EXTENSION_QUEUE_GUARD = threading.Lock()
    return _EXTENSION_QUEUE_GUARD


@dataclass(frozen=True)
class SessionEventHookSpec:
    extension_id: str
    path: str
    readable_fields: frozenset[str]
    mutable_fields: frozenset[str]


@dataclass(frozen=True)
class ExtensionHookJob:
    spec: SessionEventHookSpec
    session_id: str
    normalized: dict[str, Any]
    session_fields: dict[str, Any]
    use_sdk: bool


@dataclass(frozen=True)
class SessionEventHookSnapshot:
    specs: tuple[SessionEventHookSpec, ...]
    builtin_todos_enabled: bool


_EMPTY_HOOK_SNAPSHOT = SessionEventHookSnapshot(
    specs=(),
    builtin_todos_enabled=False,
)


def _session_event_hooks() -> list[tuple[str, str]]:
    return extension_store.session_event_hooks()


def _extension_store_signature() -> tuple[int, int] | None:
    try:
        path = extension_store._store_path()
        st = path.stat()
    except Exception:
        return None
    return (st.st_mtime_ns, st.st_size)


def _hook_snapshot_signature() -> tuple[Any, ...]:
    return (
        id(extension_store.session_event_hooks),
        id(extension_store.session_field_read_allowlist),
        id(extension_store.session_field_allowlist),
        id(extension_store.is_builtin_feature_enabled),
        _extension_store_signature(),
    )


def invalidate_hook_snapshot() -> None:
    global _HOOK_SNAPSHOT, _HOOK_SNAPSHOT_SIGNATURE, _HOOK_SNAPSHOT_CHECKED_AT
    global _SESSION_EVENT_RECONCILE_ATTEMPT_SIGNATURE
    with _HOOK_SNAPSHOT_GUARD:
        _HOOK_SNAPSHOT = None
        _HOOK_SNAPSHOT_SIGNATURE = None
        _HOOK_SNAPSHOT_CHECKED_AT = 0.0
        _SESSION_EVENT_RECONCILE_ATTEMPT_SIGNATURE = object()


def _reconcile_session_event_extensions_if_needed() -> None:
    global _SESSION_EVENT_RECONCILE_ATTEMPT_SIGNATURE
    signature = _extension_store_signature()
    if _SESSION_EVENT_RECONCILE_ATTEMPT_SIGNATURE == signature:
        return
    try:
        # Session-event hooks are part of the runtime path, not just the
        # Settings UI. On a fresh home the bundled/local extension records may
        # not exist yet because `list_extensions()`/`get_extension()` are pure
        # reads; the reconciling list endpoint is not guaranteed to have been
        # called before the first provider event. Reconcile here before
        # discovering hooks so the builtin Todos projection (and any local
        # session-event hooks) work on the first event after install/startup.
        extension_store.list_extensions_with_reconciliation(include_hidden=True)
    except Exception:
        logger.exception("session-event extension reconciliation failed")
    finally:
        _SESSION_EVENT_RECONCILE_ATTEMPT_SIGNATURE = _extension_store_signature()


def _load_hook_snapshot() -> SessionEventHookSnapshot:
    _reconcile_session_event_extensions_if_needed()
    try:
        hooks = _session_event_hooks()
    except Exception:
        logger.exception("session-event hook discovery failed")
        hooks = []
    specs: list[SessionEventHookSpec] = []
    for extension_id, path in hooks:
        try:
            specs.append(
                SessionEventHookSpec(
                    extension_id=extension_id,
                    path=path,
                    readable_fields=frozenset(
                        extension_store.session_field_read_allowlist(extension_id),
                    ),
                    mutable_fields=frozenset(
                        extension_store.session_field_allowlist(extension_id),
                    ),
                ),
            )
        except Exception:
            logger.exception("session-event hook spec failed for %s", extension_id)
    return SessionEventHookSnapshot(
        specs=tuple(specs),
        builtin_todos_enabled=_builtin_todos_enabled_uncached(),
    )


def hook_snapshot() -> SessionEventHookSnapshot:
    global _HOOK_SNAPSHOT, _HOOK_SNAPSHOT_SIGNATURE, _HOOK_SNAPSHOT_CHECKED_AT
    now = time.monotonic()
    signature = _hook_snapshot_signature()
    with _HOOK_SNAPSHOT_GUARD:
        if (
            _HOOK_SNAPSHOT is not None
            and _HOOK_SNAPSHOT_SIGNATURE == signature
            and now - _HOOK_SNAPSHOT_CHECKED_AT < _HOOK_SNAPSHOT_TTL_S
        ):
            return _HOOK_SNAPSHOT
    snapshot = _load_hook_snapshot()
    with _HOOK_SNAPSHOT_GUARD:
        _HOOK_SNAPSHOT = snapshot
        _HOOK_SNAPSHOT_SIGNATURE = signature
        _HOOK_SNAPSHOT_CHECKED_AT = time.monotonic()
    return snapshot


def _refresh_hook_snapshot(signature: tuple[Any, ...]) -> None:
    global _HOOK_SNAPSHOT, _HOOK_SNAPSHOT_SIGNATURE
    global _HOOK_SNAPSHOT_CHECKED_AT, _HOOK_SNAPSHOT_REFRESHING
    try:
        snapshot = _load_hook_snapshot()
        with _HOOK_SNAPSHOT_GUARD:
            _HOOK_SNAPSHOT = snapshot
            _HOOK_SNAPSHOT_SIGNATURE = signature
            _HOOK_SNAPSHOT_CHECKED_AT = time.monotonic()
    finally:
        with _HOOK_SNAPSHOT_GUARD:
            _HOOK_SNAPSHOT_REFRESHING = False


def hook_snapshot_nonblocking() -> SessionEventHookSnapshot:
    global _HOOK_SNAPSHOT, _HOOK_SNAPSHOT_SIGNATURE
    global _HOOK_SNAPSHOT_CHECKED_AT, _HOOK_SNAPSHOT_REFRESHING
    now = time.monotonic()
    signature = _hook_snapshot_signature()
    with _HOOK_SNAPSHOT_GUARD:
        snapshot = _HOOK_SNAPSHOT
        if (
            snapshot is not None
            and _HOOK_SNAPSHOT_SIGNATURE == signature
            and now - _HOOK_SNAPSHOT_CHECKED_AT < _HOOK_SNAPSHOT_TTL_S
        ):
            return snapshot
        signature_changed = _HOOK_SNAPSHOT_SIGNATURE != signature
        # Cold cache (first event after start) and config changes (extension
        # enable/disable, store rewrite) MUST load synchronously: returning the
        # empty/stale snapshot here would silently DROP the triggering event —
        # the builtin todos projection would never fire for the first
        # TodoWrite / TaskCreate / update_topic of the session, and a
        # just-disabled extension would still mutate the session. A stale-vs-
        # fresh signature is the load-bearing distinction, so a pure TTL expiry
        # (same signature) still serves the warm snapshot and refreshes async.
        if snapshot is None or signature_changed:
            load_inline = True
        else:
            load_inline = False
            if not _HOOK_SNAPSHOT_REFRESHING:
                _HOOK_SNAPSHOT_REFRESHING = True
                thread = threading.Thread(
                    target=_refresh_hook_snapshot,
                    args=(signature,),
                    name="session-event-hook-snapshot",
                    daemon=True,
                )
                thread.start()
    if not load_inline:
        return snapshot or _EMPTY_HOOK_SNAPSHOT
    # Load outside the guard — `_load_hook_snapshot` takes the extension-store
    # file lock, which must never nest under the in-process snapshot guard.
    fresh = _load_hook_snapshot()
    with _HOOK_SNAPSHOT_GUARD:
        _HOOK_SNAPSHOT = fresh
        _HOOK_SNAPSHOT_SIGNATURE = signature
        _HOOK_SNAPSHOT_CHECKED_AT = time.monotonic()
    return fresh


def session_event_hook_specs() -> list[SessionEventHookSpec]:
    return list(hook_snapshot().specs)


def _allowed_fields(
    extension_id: str,
    fields: dict[str, Any],
    allowed: frozenset[str] | None = None,
) -> dict[str, Any]:
    if allowed is None:
        allowed = frozenset(extension_store.session_field_allowlist(extension_id))
    return {field: value for field, value in fields.items() if field in allowed}


def _readable_fields(
    extension_id: str,
    fields: dict[str, Any],
    allowed: frozenset[str] | None = None,
) -> dict[str, Any]:
    if allowed is None:
        allowed = frozenset(extension_store.session_field_read_allowlist(extension_id))
    return {field: value for field, value in fields.items() if field in allowed}


def _builtin_todos_enabled_uncached() -> bool:
    try:
        return extension_store.is_builtin_feature_enabled(
            extension_store.BUILTIN_TODOS_EXTENSION_ID,
        )
    except Exception:
        logger.exception("builtin todos feature check failed")
        return False


def _builtin_todos_enabled() -> bool:
    return hook_snapshot_nonblocking().builtin_todos_enabled


def _external_hook_specs(
    hooks: list[SessionEventHookSpec],
) -> list[SessionEventHookSpec]:
    return [
        spec for spec in hooks
        if spec.extension_id != extension_store.BUILTIN_TODOS_EXTENSION_ID
    ]


def _project_builtin_fields(
    normalized: dict[str, Any],
    *,
    current_todos: list,
    current_tasks: list,
) -> dict[str, Any]:
    if not _builtin_todos_enabled():
        return {}
    return session_local_projection.project_event_fields(
        normalized,
        current_todos=current_todos,
        current_tasks=current_tasks,
    )


def project_event(
    session_id: str,
    normalized: dict[str, Any],
    *,
    current_todos: list,
    current_tasks: list,
    hooks: list[SessionEventHookSpec] | None = None,
) -> dict[str, Any]:
    return _project_builtin_fields(
        normalized,
        current_todos=current_todos,
        current_tasks=current_tasks,
    )


def _apply_builtin_event(
    session_id: str,
    normalized: dict[str, Any],
    *,
    use_sdk: bool,
) -> bool:
    if not _builtin_todos_enabled():
        return False
    _enqueue_external_hook(ExtensionHookJob(
        spec=SessionEventHookSpec(
            extension_id=extension_store.BUILTIN_TODOS_EXTENSION_ID,
            path="",
            readable_fields=frozenset({"current_todos", "current_tasks"}),
            mutable_fields=frozenset({"current_todos", "current_tasks"}),
        ),
        session_id=session_id,
        normalized=dict(normalized),
        session_fields={},
        use_sdk=use_sdk,
    ))
    return True


def _apply_external_hooks(
    session_id: str,
    normalized: dict[str, Any],
    *,
    hooks: list[SessionEventHookSpec],
    use_sdk: bool,
) -> bool:
    from session_manager import manager as session_manager

    session_fields = {
        "current_todos": session_manager.get_current_todos_snapshot(session_id),
        "current_tasks": session_manager.get_current_tasks_snapshot(session_id),
    }
    for spec in hooks:
        _enqueue_external_hook(ExtensionHookJob(
            spec=spec,
            session_id=session_id,
            normalized=dict(normalized),
            session_fields=_readable_fields(
                spec.extension_id,
                session_fields,
                spec.readable_fields,
            ),
            use_sdk=use_sdk,
        ))
    return bool(hooks)


def _queue_for_extension(extension_id: str) -> "queue.Queue[ExtensionHookJob]":
    with _queue_guard():
        q = _EXTENSION_QUEUES.get(extension_id)
        if q is not None:
            return q
        import threading

        q = queue.Queue(maxsize=_EXTENSION_QUEUE_SIZE)
        _EXTENSION_QUEUES[extension_id] = q
        for idx in range(_EXTENSION_WORKERS):
            thread = threading.Thread(
                target=_extension_worker,
                args=(extension_id, q),
                name=f"session-event-{extension_id}-{idx}",
                daemon=True,
            )
            thread.start()
        return q


def _enqueue_external_hook(job: ExtensionHookJob) -> None:
    q = _queue_for_extension(job.spec.extension_id)
    try:
        q.put_nowait(job)
    except queue.Full:
        logger.warning(
            "session-event hook queue full for %s; dropping event",
            job.spec.extension_id,
        )


def drain_for_tests() -> None:
    with _queue_guard():
        queues = list(_EXTENSION_QUEUES.values())
    for q in queues:
        q.join()


def _extension_worker(
    extension_id: str,
    q: "queue.Queue[ExtensionHookJob]",
) -> None:
    while True:
        job = q.get()
        try:
            _run_extension_hook_job(job)
        except Exception:
            logger.exception("session-event hook worker failed for %s", extension_id)
        finally:
            q.task_done()


def _run_extension_hook_job(job: ExtensionHookJob) -> None:
    if job.spec.extension_id == extension_store.BUILTIN_TODOS_EXTENSION_ID:
        _run_builtin_todos_job(job)
        return
    payload: dict[str, Any] = {
        "session_id": job.session_id,
        "app_session_id": job.session_id,
        "event": job.normalized,
        "session_fields": job.session_fields,
        "use_sdk": job.use_sdk,
    }
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    try:
        status, content = extension_backend_loader.invoke_extension_backend_sync(
            job.spec.extension_id,
            job.spec.path.lstrip("/"),
            method="POST",
            body_bytes=body,
            base_url=get_env("BETTER_CLAUDE_BACKEND_URL", "http://localhost:8000"),
        )
    except Exception:
        logger.exception("session-event hook %s dispatch failed", job.spec.extension_id)
        return
    if status >= 400:
        logger.warning(
            "session-event hook %s failed with status %s",
            job.spec.extension_id, status,
        )
        return
    try:
        result = json.loads(content.decode("utf-8"))
    except Exception:
        result = {}
    fields = result.get("session_fields") or result.get("fields") or {}
    if not isinstance(fields, dict):
        return
    allowed = _allowed_fields(
        job.spec.extension_id,
        fields,
        job.spec.mutable_fields,
    )
    if not allowed:
        return
    from session_manager import manager as session_manager

    for field, value in allowed.items():
        session_manager.apply_session_field(job.session_id, str(field), value)


def _run_builtin_todos_job(job: ExtensionHookJob) -> None:
    from session_manager import manager as session_manager

    current_todos = session_manager.get_current_todos_snapshot(job.session_id)
    current_tasks = session_manager.get_current_tasks_snapshot(job.session_id)
    fields = _project_builtin_fields(
        job.normalized,
        current_todos=current_todos,
        current_tasks=current_tasks,
    )
    if fields.get("current_todos") == current_todos:
        fields.pop("current_todos", None)
    if fields.get("current_tasks") == current_tasks:
        fields.pop("current_tasks", None)
    for field, value in fields.items():
        session_manager.apply_session_field(job.session_id, str(field), value)


def apply_event(session_id: str, normalized: dict[str, Any], *, use_sdk: bool) -> bool:
    snapshot = hook_snapshot_nonblocking()
    hooks = list(snapshot.specs)
    if not hooks and not snapshot.builtin_todos_enabled:
        return False
    return _apply_event_locked(session_id, normalized, hooks=hooks, use_sdk=use_sdk)


def _apply_event_locked(
    session_id: str,
    normalized: dict[str, Any],
    *,
    hooks: list[SessionEventHookSpec],
    use_sdk: bool,
) -> bool:
    changed = _apply_builtin_event(session_id, normalized, use_sdk=use_sdk)
    hooks = _external_hook_specs(hooks)
    if not hooks:
        return changed
    return _apply_external_hooks(
        session_id,
        normalized,
        hooks=hooks,
        use_sdk=use_sdk,
    ) or changed
