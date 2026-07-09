from __future__ import annotations

import copy
import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Optional

from paths import ba_home

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_loaded = False
_records: dict[str, dict[str, Any]] = {}
_write_cv = threading.Condition()
_pending_writes: dict[str, dict[str, Any]] = {}
_active_writes = 0
_writer_started = False
_certification_lock = threading.Lock()
_certification_generation = 0

_MANIFEST_VERSION = 1
_MANIFEST_NAME = ".manifest.json"


def _projection_dir() -> Path:
    return ba_home() / "queue_recovery_projection"


def _record_path(session_id: str) -> Path:
    return _projection_dir() / f"{session_id}.json"


def _manifest_path() -> Path:
    return _projection_dir() / _MANIFEST_NAME


def _session_files_fingerprint() -> dict[str, list[int]]:
    import session_store

    fingerprint: dict[str, list[int]] = {}
    for path in session_store._session_json_files():
        try:
            st = path.stat()
        except OSError:
            continue
        fingerprint[path.stem] = [int(st.st_mtime_ns), int(st.st_size)]
    return fingerprint


def _load_manifest() -> Optional[dict[str, list[int]]]:
    try:
        raw = json.loads(_manifest_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict) or raw.get("version") != _MANIFEST_VERSION:
        return None
    sessions = raw.get("sessions")
    if not isinstance(sessions, dict):
        return None
    clean: dict[str, list[int]] = {}
    for sid, signature in sessions.items():
        if (
            isinstance(sid, str)
            and isinstance(signature, list)
            and len(signature) == 2
            and all(isinstance(part, int) for part in signature)
        ):
            clean[sid] = [int(signature[0]), int(signature[1])]
        else:
            return None
    return clean


def _write_manifest(fingerprint: dict[str, list[int]]) -> None:
    path = _manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(
        prefix=".manifest.", suffix=".json.tmp", dir=path.parent,
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            json.dump({
                "version": _MANIFEST_VERSION,
                "sessions": fingerprint,
                "updated_at": time.time(),
            }, fh, separators=(",", ":"))
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def projection_is_current() -> bool:
    manifest = _load_manifest()
    return manifest is not None and manifest == _session_files_fingerprint()


def mark_current() -> None:
    with _certification_lock:
        _write_manifest(_session_files_fingerprint())


def certification_generation() -> int:
    with _certification_lock:
        return _certification_generation


def mark_dirty() -> None:
    global _certification_generation
    with _certification_lock:
        _certification_generation += 1
        try:
            _manifest_path().unlink(missing_ok=True)
        except OSError:
            logger.exception("failed to invalidate queue projection manifest")


def mark_current_if_generation(expected_generation: int) -> bool:
    with _certification_lock:
        if _certification_generation != expected_generation:
            return False
        _write_manifest(_session_files_fingerprint())
        return True


def ensure_current_or_rebuild() -> bool:
    """Ensure queue projection can be used as startup source of truth.

    Fast path: if the projection manifest's session-file fingerprint still
    matches the current session corpus, load only the projection records and
    avoid reading every full session JSON. If any session file changed since
    the manifest was written (including crash windows where a session persist
    landed but a background projection write did not), rebuild from the
    authoritative session snapshots. Returns True when a rebuild ran.
    """
    if projection_is_current():
        with _lock:
            # Startup should trust the durable projection files, not any
            # inherited in-memory cache a test/hot-reload process may hold.
            _records.clear()
            global _loaded
            _loaded = False
            _load_locked()
        return False
    rebuild_from_disk()
    return True


def _load_locked() -> None:
    global _loaded
    if _loaded:
        return
    _records.clear()
    projection_dir = _projection_dir()
    if projection_dir.is_dir():
        for path in projection_dir.glob("*.json"):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            sid = record.get("id") if isinstance(record, dict) else None
            if isinstance(sid, str):
                _records[sid] = copy.deepcopy(record)
    _loaded = True


def _write_record_locked(record: dict[str, Any]) -> None:
    session_id = record["id"]
    path = _record_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(
        prefix=f".{session_id}.", suffix=".json.tmp", dir=path.parent,
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            json.dump(record, fh)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _ensure_writer_locked() -> None:
    global _writer_started
    if _writer_started:
        return
    thread = threading.Thread(
        target=_writer_loop,
        name="queue-projection-writer",
        daemon=True,
    )
    _writer_started = True
    thread.start()


def _writer_loop() -> None:
    global _active_writes
    while True:
        with _write_cv:
            while not _pending_writes:
                _write_cv.wait()
            session_id, record = _pending_writes.popitem()
            _active_writes += 1
        try:
            with _lock:
                if _records.get(session_id) != record:
                    continue
                record_to_write = copy.deepcopy(record)
            _write_record_locked(record_to_write)
            with _lock:
                latest = _records.get(session_id)
                if latest is None or latest == record_to_write:
                    continue
                latest_to_write = copy.deepcopy(latest)
            with _write_cv:
                _pending_writes[session_id] = latest_to_write
                _write_cv.notify()
        except Exception:
            logger.exception(
                "failed to write queue recovery projection for session %s",
                session_id,
            )
        finally:
            with _write_cv:
                _active_writes -= 1
                _write_cv.notify_all()


def _user_message_projection(messages: Iterable[Any]) -> dict[str, Any]:
    client_ids: list[str] = []
    lifecycle_ids: list[str] = []
    user_messages: list[dict[str, Any]] = []
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        user_messages.append(copy.deepcopy(msg))
        client_id = msg.get("client_id")
        if isinstance(client_id, str) and client_id:
            client_ids.append(client_id)
        lifecycle_id = msg.get("lifecycle_msg_id")
        if isinstance(lifecycle_id, str) and lifecycle_id:
            lifecycle_ids.append(lifecycle_id)
    return {
        "user_messages": user_messages,
        "user_client_ids": client_ids,
        "user_lifecycle_msg_ids": lifecycle_ids,
    }


def _queued_prompt_is_pending(
    prompt: dict[str, Any],
    user_client_ids: set[str],
    user_lifecycle_ids: set[str],
) -> bool:
    client_id = prompt.get("client_id")
    if isinstance(client_id, str) and client_id and client_id in user_client_ids:
        return False
    lifecycle_id = prompt.get("lifecycle_msg_id")
    if (
        isinstance(lifecycle_id, str)
        and lifecycle_id
        and lifecycle_id in user_lifecycle_ids
    ):
        return False
    return True


def project_session(session: dict[str, Any]) -> Optional[dict[str, Any]]:
    sid = session.get("id")
    if not isinstance(sid, str) or not sid:
        return None
    user_projection = _user_message_projection(session.get("messages") or [])
    user_client_ids = set(user_projection["user_client_ids"])
    user_lifecycle_ids = set(user_projection["user_lifecycle_msg_ids"])
    queued = [
        copy.deepcopy(prompt)
        for prompt in (session.get("queued_prompts") or [])
        if isinstance(prompt, dict)
        and _queued_prompt_is_pending(prompt, user_client_ids, user_lifecycle_ids)
    ]
    return {
        "id": sid,
        "model": session.get("model"),
        "cwd": session.get("cwd"),
        "queued_prompts": queued,
        **user_projection,
    }


def upsert_from_session(session: dict[str, Any]) -> None:
    record = project_session(session)
    if record is None:
        return
    upsert_record(record)


def upsert_record(record: dict[str, Any]) -> None:
    if not isinstance(record.get("id"), str) or not record["id"]:
        return
    with _lock:
        _load_locked()
        if _records.get(record["id"]) == record:
            return
        _records[record["id"]] = record
        _write_record_locked(record)


def upsert_record_background(record: dict[str, Any]) -> None:
    session_id = record.get("id")
    if not isinstance(session_id, str) or not session_id:
        return
    with _write_cv:
        with _lock:
            _load_locked()
            if _records.get(session_id) == record:
                return
            _records[session_id] = record
            _pending_writes[session_id] = record
        _ensure_writer_locked()
        _write_cv.notify()


def flush_pending_writes(timeout: Optional[float] = None) -> bool:
    deadline = None if timeout is None else time.monotonic() + timeout
    with _write_cv:
        while _pending_writes or _active_writes:
            wait_for = None if deadline is None else deadline - time.monotonic()
            if wait_for is not None and wait_for <= 0:
                return False
            _write_cv.wait(wait_for)
    return True


def get(session_id: str) -> Optional[dict[str, Any]]:
    with _lock:
        _load_locked()
        record = _records.get(session_id)
        return copy.deepcopy(record) if record is not None else None


def get_many(session_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    ids = [sid for sid in session_ids if sid]
    if not ids:
        return {}
    with _lock:
        _load_locked()
        return {
            sid: copy.deepcopy(record)
            for sid in ids
            if (record := _records.get(sid)) is not None
        }


def queued_prompts(session_id: str) -> list[dict[str, Any]]:
    record = get(session_id)
    if not record:
        return []
    return [
        prompt for prompt in record.get("queued_prompts") or []
        if isinstance(prompt, dict)
    ]


def _walk_nodes(node: dict[str, Any]) -> Iterable[dict[str, Any]]:
    yield node
    for fork in node.get("forks") or []:
        if isinstance(fork, dict):
            yield from _walk_nodes(fork)


def rebuild_from_disk() -> int:
    import session_store

    rebuilt: dict[str, dict[str, Any]] = {}
    for path in session_store._session_json_files():
        try:
            root = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(root, dict):
            continue
        for node in _walk_nodes(root):
            record = project_session(node)
            if record is not None:
                rebuilt[record["id"]] = record
    with _lock:
        _load_locked()
        projection_dir = _projection_dir()
        valid_projection_paths: set[Path] = set()
        if projection_dir.is_dir():
            for path in projection_dir.glob("*.json"):
                remove = True
                try:
                    record = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    record = None
                sid = record.get("id") if isinstance(record, dict) else None
                if isinstance(sid, str) and path == _record_path(sid):
                    if sid in rebuilt:
                        valid_projection_paths.add(path)
                        remove = False
                if not remove:
                    continue
                try:
                    path.unlink()
                except OSError:
                    pass
        for sid in set(_records) - set(rebuilt):
            _records.pop(sid, None)
            path = _record_path(sid)
            if path in valid_projection_paths:
                try:
                    path.unlink()
                except OSError:
                    pass
        for sid, record in rebuilt.items():
            if _records.get(sid) == record:
                continue
            _records[sid] = record
            _write_record_locked(record)
    try:
        _write_manifest(_session_files_fingerprint())
    except Exception:
        logger.exception("failed to write queue recovery projection manifest")
    return len(rebuilt)


def list_queued_records() -> list[dict[str, Any]]:
    with _lock:
        _load_locked()
        return [
            copy.deepcopy(record)
            for _sid, record in sorted(_records.items())
            if any(isinstance(prompt, dict) for prompt in record.get("queued_prompts") or [])
        ]


def queued_counts() -> dict[str, int]:
    with _lock:
        _load_locked()
        return {
            sid: len([
                prompt for prompt in record.get("queued_prompts") or []
                if isinstance(prompt, dict)
            ])
            for sid, record in _records.items()
            if any(isinstance(prompt, dict) for prompt in record.get("queued_prompts") or [])
        }
