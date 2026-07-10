from __future__ import annotations

import copy
import json
import logging
import os
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Optional

import perf
import portable_lock
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
_record_generations: dict[str, int] = {}
_deleted_generations: dict[str, int] = {}

_MANIFEST_VERSION = 2
_MANIFEST_NAME = ".manifest.json"


def _projection_dir() -> Path:
    return ba_home() / "queue_recovery_projection"


def _generation_dir(generation: str) -> Path:
    return _projection_dir() / "generations" / generation


def _record_path(session_id: str, generation: Optional[str] = None) -> Path:
    if generation:
        return _generation_dir(generation) / "records" / f"{session_id}.json"
    return _projection_dir() / f"{session_id}.json"


def _manifest_path() -> Path:
    return _projection_dir() / _MANIFEST_NAME


def _dirty_path() -> Path:
    return _projection_dir() / ".dirty"


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def _corpus_transaction():
    path = _projection_dir() / ".projection.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as lock:
        portable_lock.lock_ex(lock.fileno())
        try:
            yield
        finally:
            portable_lock.unlock(lock.fileno())


def _session_files_fingerprint() -> dict[str, list[int]]:
    import session_store

    fingerprint: dict[str, list[int]] = {}
    home = ba_home()
    for path in session_store._session_json_files():
        try:
            st = path.stat()
        except OSError:
            continue
        fingerprint[path.relative_to(home).as_posix()] = [
            int(st.st_dev), int(st.st_ino), int(st.st_mtime_ns),
            int(st.st_ctime_ns), int(st.st_size),
        ]
    return fingerprint


def _load_manifest_payload() -> Optional[dict[str, Any]]:
    try:
        raw = json.loads(_manifest_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict) or raw.get("version") != _MANIFEST_VERSION:
        return None
    sessions = raw.get("sessions")
    generation = raw.get("generation")
    if not isinstance(sessions, dict) or not isinstance(generation, str) or not generation:
        return None
    clean: dict[str, list[int]] = {}
    for sid, signature in sessions.items():
        if (
            isinstance(sid, str)
            and isinstance(signature, list)
            and len(signature) == 5
            and all(isinstance(part, int) for part in signature)
        ):
            clean[sid] = [int(part) for part in signature]
        else:
            return None
    return {"sessions": clean, "generation": generation}


def _load_manifest() -> Optional[dict[str, list[int]]]:
    payload = _load_manifest_payload()
    return payload["sessions"] if payload is not None else None


def _write_manifest(fingerprint: dict[str, list[int]], generation: str) -> None:
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
                "generation": generation,
                "updated_at": time.time(),
            }, fh, separators=(",", ":"))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
        _fsync_dir(path.parent)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def projection_is_current() -> bool:
    if _dirty_path().exists():
        return False
    manifest = _load_manifest()
    return manifest is not None and manifest == _session_files_fingerprint()


def mark_current() -> None:
    rebuild_from_disk()


def certification_generation() -> int:
    with _certification_lock:
        return _certification_generation


def mark_dirty() -> int:
    global _certification_generation
    with _certification_lock:
        _certification_generation += 1
        try:
            path = _dirty_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch(exist_ok=True)
        except OSError:
            logger.exception("failed to invalidate queue projection manifest")
        return _certification_generation


def mark_current_if_generation(
    expected_generation: int,
    expected_fingerprint: Optional[dict[str, list[int]]] = None,
    projection_generation: Optional[str] = None,
) -> bool:
    with _certification_lock:
        if _certification_generation != expected_generation:
            return False
        fingerprint = _session_files_fingerprint()
        if expected_fingerprint is not None and fingerprint != expected_fingerprint:
            return False
        if not projection_generation:
            return False
        _write_manifest(fingerprint, projection_generation)
        _dirty_path().unlink(missing_ok=True)
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
    while True:
        payload = _load_manifest_payload()
        records_dir = (
            _generation_dir(payload["generation"]) / "records"
            if payload is not None
            else _projection_dir()
        )
        loaded: dict[str, dict[str, Any]] = {}
        if records_dir.is_dir():
            for path in records_dir.glob("*.json"):
                try:
                    record = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                sid = record.get("id") if isinstance(record, dict) else None
                if isinstance(sid, str):
                    loaded[sid] = record
        if _load_manifest_payload() == payload:
            _records.update(copy.deepcopy(loaded))
            break
    _loaded = True


def _write_record_locked(
    record: dict[str, Any], generation: Optional[str] = None,
) -> None:
    session_id = record["id"]
    path = _record_path(session_id, generation)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(
        prefix=f".{session_id}.", suffix=".json.tmp", dir=path.parent,
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            json.dump(record, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
        _fsync_dir(path.parent)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _write_generation_sidecar(generation: str, name: str, value: Any) -> None:
    path = _generation_dir(generation) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(prefix=f".{name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as file:
            json.dump(value, file, separators=(",", ":"))
            file.flush()
            os.fsync(file.fileno())
        os.replace(tmp_path, path)
        _fsync_dir(path.parent)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _validate_generation(generation: str, expected_ids: set[str]) -> bool:
    records_dir = _generation_dir(generation) / "records"
    actual_ids: set[str] = set()
    try:
        for path in records_dir.glob("*.json"):
            record = json.loads(path.read_text(encoding="utf-8"))
            sid = record.get("id") if isinstance(record, dict) else None
            if not isinstance(sid, str) or sid != path.stem:
                return False
            actual_ids.add(sid)
        complete = json.loads(
            (_generation_dir(generation) / "complete.json").read_text(encoding="utf-8"),
        )
    except (OSError, json.JSONDecodeError):
        return False
    return actual_ids == expected_ids and complete.get("records") == sorted(expected_ids)


def _cleanup_generations(keep: str) -> None:
    import shutil

    root = _projection_dir() / "generations"
    try:
        generations = tuple(root.iterdir())
    except OSError:
        return
    retained = {keep}
    retained.update(
        path.name for path in sorted(
            generations,
            key=lambda item: item.stat().st_mtime_ns if item.exists() else 0,
            reverse=True,
        )[:3]
    )
    for path in generations:
        if path.name not in retained:
            shutil.rmtree(path, ignore_errors=True)


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
                if latest is None:
                    _record_path(session_id).unlink(missing_ok=True)
                    continue
                if latest == record_to_write:
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
        _record_generations[record["id"]] = mark_dirty()
        _deleted_generations.pop(record["id"], None)
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
            _record_generations[session_id] = mark_dirty()
            _deleted_generations.pop(session_id, None)
            _pending_writes[session_id] = record
        _ensure_writer_locked()
        _write_cv.notify()


def delete_records(session_ids: Iterable[str]) -> None:
    ids = tuple(dict.fromkeys(str(sid) for sid in session_ids if sid))
    if not ids:
        return
    with _write_cv:
        with _lock:
            _load_locked()
            changed = [sid for sid in ids if sid in _records or sid in _pending_writes]
            if not changed:
                return
            generation = mark_dirty()
            for sid in changed:
                _records.pop(sid, None)
                _pending_writes.pop(sid, None)
                _deleted_generations[sid] = generation
                _record_generations.pop(sid, None)
    for sid in changed:
        try:
            _record_path(sid).unlink(missing_ok=True)
        except OSError:
            logger.exception("failed to delete queue recovery projection %s", sid)


def delete_record(session_id: str) -> None:
    delete_records((session_id,))


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
    started = time.perf_counter()
    with _lock:
        perf.record(
            "queue_projection.get_many.lock_wait",
            (time.perf_counter() - started) * 1000.0,
        )
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


def _scan_complete_snapshot() -> tuple[dict[str, dict[str, Any]], dict[str, list[int]]]:
    import session_store

    while True:
        before = _session_files_fingerprint()
        rebuilt: dict[str, dict[str, Any]] = {}
        with perf.timed("queue_projection.rebuild.scan"):
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
        after = _session_files_fingerprint()
        if before == after:
            return rebuilt, after


def _rebuild_from_disk_locked() -> int:
    generation = certification_generation()
    rebuilt, corpus_fingerprint = _scan_complete_snapshot()
    perf.record_count("queue_projection.rebuild.rows", len(rebuilt))

    while True:
        with _lock:
            _load_locked()
            current_generation = certification_generation()
            for sid, mutation_generation in _record_generations.items():
                if mutation_generation > generation and sid in _records:
                    rebuilt[sid] = copy.deepcopy(_records[sid])
            for sid, mutation_generation in _deleted_generations.items():
                if mutation_generation > generation:
                    rebuilt.pop(sid, None)
            generation = current_generation
        projection_generation = uuid.uuid4().hex
        prior_payload = _load_manifest_payload()
        prior_ids: set[str] = set()
        if prior_payload is not None:
            prior_records_dir = _generation_dir(prior_payload["generation"]) / "records"
            prior_ids = {path.stem for path in prior_records_dir.glob("*.json")}
        with perf.timed("queue_projection.rebuild.write"):
            try:
                for record in rebuilt.values():
                    _write_record_locked(record, projection_generation)
                record_ids = set(rebuilt)
                _write_generation_sidecar(
                    projection_generation, "deletes.json",
                    {"deleted": sorted(prior_ids - record_ids)},
                )
                _write_generation_sidecar(
                    projection_generation, "complete.json",
                    {"records": sorted(record_ids)},
                )
                if not _validate_generation(projection_generation, record_ids):
                    raise RuntimeError("queue projection generation validation failed")
                _fsync_dir(_generation_dir(projection_generation) / "records")
                _fsync_dir(_generation_dir(projection_generation))
            except BaseException:
                import shutil
                shutil.rmtree(_generation_dir(projection_generation), ignore_errors=True)
                raise
        if _session_files_fingerprint() != corpus_fingerprint:
            import shutil
            shutil.rmtree(_generation_dir(projection_generation), ignore_errors=True)
            rebuilt, corpus_fingerprint = _scan_complete_snapshot()
            continue
        if certification_generation() != current_generation:
            import shutil
            shutil.rmtree(_generation_dir(projection_generation), ignore_errors=True)
            continue
        if not mark_current_if_generation(
            current_generation, corpus_fingerprint, projection_generation,
        ):
            import shutil
            shutil.rmtree(_generation_dir(projection_generation), ignore_errors=True)
            rebuilt, corpus_fingerprint = _scan_complete_snapshot()
            continue
        with perf.timed("queue_projection.rebuild.swap"):
            with _lock:
                for sid, mutation_generation in _record_generations.items():
                    if mutation_generation > current_generation and sid in _records:
                        rebuilt[sid] = copy.deepcopy(_records[sid])
                for sid, mutation_generation in _deleted_generations.items():
                    if mutation_generation > current_generation:
                        rebuilt.pop(sid, None)
                _records.clear()
                _records.update(copy.deepcopy(rebuilt))
                global _loaded
                _loaded = True
        _cleanup_generations(projection_generation)
        with _lock:
            for generations in (_record_generations, _deleted_generations):
                for sid, mutation_generation in tuple(generations.items()):
                    if mutation_generation <= current_generation:
                        generations.pop(sid, None)
        return len(rebuilt)


def rebuild_from_disk() -> int:
    with _corpus_transaction():
        return _rebuild_from_disk_locked()


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
