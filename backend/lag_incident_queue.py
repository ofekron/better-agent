from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import random
import stat
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

import perf
from paths import ba_home
from secret_redaction import redact_secrets
from portable_lock import lock_ex, unlock


logger = logging.getLogger(__name__)

_MAX_PENDING = 256
_MAX_PAYLOAD_BYTES = 18_000
_RETRY_BASE_SECONDS = 1.0
_RETRY_MAX_SECONDS = 60.0
_RETRY_JITTER_RATIO = 0.2
_FILE_SUFFIX = ".json"
_lock = threading.RLock()
_wake: asyncio.Event | None = None
_loop: asyncio.AbstractEventLoop | None = None
_task: asyncio.Task | None = None
_stopping = False
_destination_generation = 0
_depth_cache = 0
_DEPTH_META_NAME = ".depth.meta"
_DEPTH_LOCK_NAME = ".depth.lock"
_DEPTH_VERSION = 1
_DIRFD_SUPPORTED = (
    os.name != "nt"
    and os.open in os.supports_dir_fd
    and os.stat in os.supports_dir_fd
    and os.unlink in os.supports_dir_fd
    and os.listdir in os.supports_fd
)
_STRING_LIMITS = {
    "requirement_ref": 80,
    "summary": 300,
    "assistant_message": 2_000,
    "evidence": 16_000,
    "source": 120,
    "severity": 20,
    "dump_path": 1_000,
    "lag_label": 200,
}
_ALLOWED_FIELDS = set(_STRING_LIMITS) | {"lag_seconds", "stack_names"}
EntryIdentity = tuple[int, int, int, int, str]


@dataclass(frozen=True)
class DispatchOutcome:
    acknowledged: bool
    retryable: bool = True
    retry_after: float | None = None


DispatchResult = bool | DispatchOutcome


class LagIncidentSpoolFull(RuntimeError):
    pass


def _spool_dir() -> Path:
    return ba_home() / "lag-incidents"


@contextmanager
def _depth_process_lock(root: Path):
    path = root / _DEPTH_LOCK_NAME
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.fchmod(fd, 0o600)
        lock_ex(fd)
        yield
    finally:
        unlock(fd)
        os.close(fd)


def _write_depth_metadata_locked(root: Path, depth_value: int, generation: int) -> None:
    target = root / _DEPTH_META_NAME
    temporary = root / f".{_DEPTH_META_NAME}.{uuid.uuid4().hex}.tmp"
    body = json.dumps({
        "version": _DEPTH_VERSION,
        "generation": max(0, int(generation)),
        "depth": max(0, int(depth_value)),
    }, separators=(",", ":")).encode("utf-8")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb", closefd=False) as stream:
                stream.write(body)
                stream.flush()
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, target)
        os.chmod(target, 0o600)
        _fsync_parent_portable(root)
    finally:
        temporary.unlink(missing_ok=True)


def _read_depth_metadata_locked(root: Path) -> tuple[int, int] | None:
    try:
        info = (root / _DEPTH_META_NAME).lstat()
        if not stat.S_ISREG(info.st_mode):
            return None
        data = json.loads((root / _DEPTH_META_NAME).read_text(encoding="utf-8"))
        if data.get("version") != _DEPTH_VERSION:
            return None
        return max(0, int(data["depth"])), max(0, int(data["generation"]))
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None


def _reconcile_depth_projection() -> int:
    root = _secure_spool_dir()
    with _depth_process_lock(root):
        actual = len(_pending_files(strict=True))
        metadata = _read_depth_metadata_locked(root)
        generation = (metadata[1] if metadata else 0) + 1
        _write_depth_metadata_locked(root, actual, generation)
    _set_depth(actual)
    return actual


def _update_depth_projection(delta: int) -> int:
    del delta
    root = _secure_spool_dir()
    with _depth_process_lock(root):
        metadata = _read_depth_metadata_locked(root)
        actual = len(_pending_files(strict=True))
        generation = metadata[1] if metadata else 0
        _write_depth_metadata_locked(root, actual, generation + 1)
    _set_depth(actual)
    return actual


def _secure_spool_dir() -> Path:
    state_root = ba_home().resolve()
    root = _spool_dir()
    root.mkdir(parents=True, exist_ok=True)
    root_info = root.lstat()
    is_junction = bool(getattr(root, "is_junction", lambda: False)())
    if not stat.S_ISDIR(root_info.st_mode) or root.is_symlink() or is_junction:
        raise RuntimeError("lag incident spool must be a real directory")
    resolved = root.resolve()
    if not resolved.is_relative_to(state_root):
        raise RuntimeError("lag incident spool escapes state root")
    return root


def _open_spool_dir_fd(root: Path) -> int:
    flags = os.O_RDONLY
    flags |= getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    return os.open(root, flags)


def _fsync_dir(dir_fd: int) -> None:
    os.fsync(dir_fd)


def _fsync_parent_portable(root: Path) -> None:
    if os.name == "nt":
        return
    dir_fd = _open_spool_dir_fd(root)
    try:
        _fsync_dir(dir_fd)
    finally:
        os.close(dir_fd)


def _entry_stat_identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size


def _entry_identity(dir_fd: int, name: str) -> tuple[int, int, int, int]:
    info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("spool entry must be a regular file")
    return _entry_stat_identity(info)


def _open_entry(dir_fd: int, name: str) -> tuple[int, tuple[int, int, int, int]]:
    before = _entry_identity(dir_fd, name)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    file_fd = os.open(name, flags, dir_fd=dir_fd)
    try:
        opened = os.fstat(file_fd)
        if not stat.S_ISREG(opened.st_mode) or _entry_stat_identity(opened) != before:
            raise ValueError("spool entry identity changed")
        return file_fd, before
    except Exception:
        os.close(file_fd)
        raise


def _unlink_if_identity(dir_fd: int, name: str, identity: EntryIdentity) -> None:
    file_fd, before = _open_entry(dir_fd, name)
    try:
        with os.fdopen(file_fd, "rb", closefd=False) as stream:
            raw = stream.read(_MAX_PAYLOAD_BYTES + 1)
        after = _entry_stat_identity(os.fstat(file_fd))
    finally:
        os.close(file_fd)
    if before != identity[:4] or after != identity[:4]:
        raise ValueError("spool entry identity changed before acknowledgement")
    if hashlib.sha256(raw).hexdigest() != identity[4]:
        raise ValueError("spool entry content changed before acknowledgement")
    if _entry_identity(dir_fd, name) != identity[:4]:
        raise ValueError("spool entry identity changed before acknowledgement")
    os.unlink(name, dir_fd=dir_fd)
    _fsync_dir(dir_fd)


def _pending_files(*, strict: bool = False) -> list[Path]:
    dir_fd: int | None = None
    try:
        root = _secure_spool_dir()
        if not _DIRFD_SUPPORTED:
            entries = [
                (entry.stat(follow_symlinks=False).st_mtime_ns, Path(entry.path))
                for entry in os.scandir(root)
                if entry.name.endswith(_FILE_SUFFIX)
            ]
            return [path for _, path in sorted(entries, key=lambda item: (item[0], item[1].name))]
        dir_fd = _open_spool_dir_fd(root)
        entries: list[tuple[int, str]] = []
        for name in os.listdir(dir_fd):
            if not name.endswith(_FILE_SUFFIX):
                continue
            info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            entries.append((info.st_mtime_ns, name))
        return [root / name for _, name in sorted(entries)]
    except OSError:
        if strict:
            raise
        logger.exception("lag-incident-queue: cannot list spool")
        return []
    finally:
        if dir_fd is not None:
            os.close(dir_fd)


def depth() -> int:
    with _lock:
        return _depth_cache


def _set_depth(value: int) -> None:
    global _depth_cache
    with _lock:
        _depth_cache = max(0, int(value))


def _adjust_depth(delta: int) -> None:
    global _depth_cache
    with _lock:
        _depth_cache = max(0, _depth_cache + int(delta))


def _normalize_payload(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict) or set(payload) - _ALLOWED_FIELDS:
        raise ValueError("payload has invalid fields")
    normalized: dict[str, object] = {}
    for key, limit in _STRING_LIMITS.items():
        value = payload.get(key)
        if value is None:
            continue
        if not isinstance(value, str) or len(value) > limit:
            raise ValueError(f"invalid {key}")
        normalized[key] = redact_secrets(value)
    lag_seconds = payload.get("lag_seconds")
    if lag_seconds is not None:
        if isinstance(lag_seconds, bool) or not isinstance(lag_seconds, (int, float)):
            raise ValueError("invalid lag_seconds")
        normalized_lag = float(lag_seconds)
        if not math.isfinite(normalized_lag):
            raise ValueError("invalid lag_seconds")
        normalized["lag_seconds"] = normalized_lag
    stack_names = payload.get("stack_names")
    if stack_names is not None:
        if (
            not isinstance(stack_names, list)
            or len(stack_names) > 16
            or any(not isinstance(item, str) or len(item) > 120 for item in stack_names)
        ):
            raise ValueError("invalid stack_names")
        normalized["stack_names"] = [redact_secrets(item) for item in stack_names]
    required_strings = ("requirement_ref", "summary", "source", "severity")
    if any(not normalized.get(key) for key in required_strings):
        raise ValueError("payload is missing required strings")
    ref = str(normalized["requirement_ref"])
    if not ref.startswith("bug:lag-watchdog:"):
        raise ValueError("invalid lag incident reference")
    return normalized


def _encode(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _validated_payload(raw: bytes, *, require_redacted: bool = True) -> dict[str, object]:
    if len(raw) > _MAX_PAYLOAD_BYTES:
        raise ValueError("payload exceeds spool limit")
    payload = _normalize_payload(json.loads(raw))
    ref = str(payload["requirement_ref"])
    if require_redacted and _encode(payload) != raw:
        raise ValueError("payload is not canonical")
    return payload


def enqueue(payload_bytes: bytes) -> bool:
    global _depth_cache
    payload = _validated_payload(payload_bytes, require_redacted=False)
    payload_bytes = _encode(payload)
    if len(payload_bytes) > _MAX_PAYLOAD_BYTES:
        raise ValueError("payload exceeds spool limit")
    digest = str(payload["requirement_ref"]).rsplit(":", 1)[-1]
    if len(digest) != 16 or any(ch not in "0123456789abcdef" for ch in digest):
        raise ValueError("invalid lag incident digest")
    root = _secure_spool_dir()
    destination_name = f"{digest}{_FILE_SUFFIX}"
    with perf.timed("lag_incident.spool_write"):
        with _lock:
            if not _DIRFD_SUPPORTED:
                destination = root / destination_name
                try:
                    info = destination.lstat()
                except FileNotFoundError:
                    pass
                else:
                    if not stat.S_ISREG(info.st_mode):
                        raise RuntimeError("lag incident entry must be a regular file")
                    _notify_dispatcher()
                    return False
                if len(_pending_files(strict=True)) >= _MAX_PENDING:
                    raise LagIncidentSpoolFull("lag incident spool is full")
                temporary = root / f".{digest}.{uuid.uuid4().hex}.tmp"
                try:
                    with open(temporary, "xb") as stream:
                        os.chmod(temporary, 0o600)
                        stream.write(payload_bytes)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, destination)
                    _fsync_parent_portable(root)
                finally:
                    temporary.unlink(missing_ok=True)
                perf.record_count("lag_incident.enqueued")
                _update_depth_projection(1)
                _notify_dispatcher()
                return True
            dir_fd = _open_spool_dir_fd(root)
            temporary_name = f".{digest}.{uuid.uuid4().hex}.tmp"
            try:
                try:
                    _entry_identity(dir_fd, destination_name)
                except FileNotFoundError:
                    pass
                else:
                    _notify_dispatcher()
                    return False
                if len(_pending_files(strict=True)) >= _MAX_PENDING:
                    raise LagIncidentSpoolFull("lag incident spool is full")
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                temporary_fd = os.open(temporary_name, flags, 0o600, dir_fd=dir_fd)
                try:
                    with os.fdopen(temporary_fd, "wb", closefd=False) as stream:
                        stream.write(payload_bytes)
                        stream.flush()
                    os.fsync(temporary_fd)
                finally:
                    os.close(temporary_fd)
                os.replace(
                    temporary_name,
                    destination_name,
                    src_dir_fd=dir_fd,
                    dst_dir_fd=dir_fd,
                )
                _fsync_dir(dir_fd)
            finally:
                try:
                    os.unlink(temporary_name, dir_fd=dir_fd)
                except FileNotFoundError:
                    pass
                os.close(dir_fd)
    perf.record_count("lag_incident.enqueued")
    _update_depth_projection(1)
    _notify_dispatcher()
    return True


def _notify_dispatcher() -> None:
    loop = _loop
    wake = _wake
    if loop is None or wake is None or loop.is_closed():
        return
    try:
        loop.call_soon_threadsafe(wake.set)
    except RuntimeError:
        return


def notify_destination_changed() -> None:
    """Wake a paused dispatcher after extension availability or grants change."""
    global _destination_generation
    _destination_generation += 1
    _notify_dispatcher()


def _read(path: Path) -> tuple[dict[str, object], float, EntryIdentity] | None:
    dir_fd: int | None = None
    file_fd: int | None = None
    try:
        root = _secure_spool_dir()
        if not _DIRFD_SUPPORTED:
            if path.parent.resolve() != root.resolve():
                raise ValueError("spool entry escapes spool directory")
            before = path.lstat()
            if not stat.S_ISREG(before.st_mode):
                raise ValueError("spool entry must be a regular file")
            with open(path, "rb") as stream:
                opened = os.fstat(stream.fileno())
                if _entry_stat_identity(opened) != _entry_stat_identity(before):
                    raise ValueError("spool entry identity changed")
                raw = stream.read(_MAX_PAYLOAD_BYTES + 1)
                after = os.fstat(stream.fileno())
            if _entry_stat_identity(after) != _entry_stat_identity(before):
                raise ValueError("spool entry changed during read")
            identity = (*_entry_stat_identity(before), hashlib.sha256(raw).hexdigest())
            payload = _validated_payload(raw)
            digest = str(payload["requirement_ref"]).rsplit(":", 1)[-1]
            if path.name != f"{digest}{_FILE_SUFFIX}":
                raise ValueError("filename does not match incident reference")
            return payload, before.st_mtime, identity
        dir_fd = _open_spool_dir_fd(root)
        file_fd, identity = _open_entry(dir_fd, path.name)
        with os.fdopen(file_fd, "rb", closefd=False) as stream:
            raw = stream.read(_MAX_PAYLOAD_BYTES + 1)
        after = _entry_stat_identity(os.fstat(file_fd))
        if after != identity:
            raise ValueError("spool entry changed during read")
        full_identity = (*identity, hashlib.sha256(raw).hexdigest())
        payload = _validated_payload(raw)
        digest = str(payload["requirement_ref"]).rsplit(":", 1)[-1]
        if path.name != f"{digest}{_FILE_SUFFIX}":
            raise ValueError("filename does not match incident reference")
        return payload, identity[2] / 1_000_000_000.0, full_identity
    except (OSError, UnicodeError, ValueError, RuntimeError, RecursionError, json.JSONDecodeError):
        logger.exception("lag-incident-queue: refusing malformed spool entry name=%s", path.name)
        return None
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if dir_fd is not None:
            os.close(dir_fd)


def _acknowledge(path: Path, identity: EntryIdentity) -> None:
    root = _secure_spool_dir()
    if not _DIRFD_SUPPORTED:
        if path.parent.resolve() != root.resolve():
            raise ValueError("spool entry escapes spool directory")
        current = path.lstat()
        if not stat.S_ISREG(current.st_mode) or _entry_stat_identity(current) != identity[:4]:
            raise ValueError("spool entry identity changed before acknowledgement")
        with open(path, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if _entry_stat_identity(opened) != identity[:4]:
                raise ValueError("spool entry identity changed before acknowledgement")
            raw = stream.read(_MAX_PAYLOAD_BYTES + 1)
            after = os.fstat(stream.fileno())
        if _entry_stat_identity(after) != identity[:4]:
            raise ValueError("spool entry changed before acknowledgement")
        if hashlib.sha256(raw).hexdigest() != identity[4]:
            raise ValueError("spool entry content changed before acknowledgement")
        if _entry_stat_identity(path.lstat()) != identity[:4]:
            raise ValueError("spool entry identity changed before acknowledgement")
        path.unlink()
        _fsync_parent_portable(root)
        return
    dir_fd = _open_spool_dir_fd(root)
    try:
        _unlink_if_identity(dir_fd, path.name, identity)
    finally:
        os.close(dir_fd)


async def _drain_outcome(
    dispatch: Callable[[bytes], Awaitable[DispatchResult]],
) -> DispatchOutcome:
    try:
        await asyncio.to_thread(_secure_spool_dir)
        pending = await asyncio.to_thread(_pending_files, strict=True)
        _set_depth(len(pending))
    except (OSError, RuntimeError):
        logger.exception("lag-incident-queue: cannot securely open spool")
        perf.record_count("lag_incident.retry")
        perf.record_count("lag_incident.circuit_open")
        return DispatchOutcome(False)
    for path in pending:
        if _stopping:
            return DispatchOutcome(True)
        loaded = await asyncio.to_thread(_read, path)
        if loaded is None:
            continue
        payload, enqueued_at, identity = loaded
        perf.record("lag_incident.enqueue_age", max(0.0, time.time() - enqueued_at) * 1000.0)
        body = _encode(payload)
        started = time.perf_counter()
        try:
            result = await dispatch(body)
            outcome = result if isinstance(result, DispatchOutcome) else DispatchOutcome(bool(result))
        except Exception:
            outcome = DispatchOutcome(False)
            logger.exception("lag-incident-queue: dispatch raised")
        finally:
            perf.record("lag_incident.dispatch", (time.perf_counter() - started) * 1000.0)
        if not outcome.acknowledged:
            perf.record_count("lag_incident.retry")
            perf.record_count("lag_incident.circuit_open")
            return outcome
        try:
            await asyncio.to_thread(_acknowledge, path, identity)
            await asyncio.to_thread(_update_depth_projection, -1)
            perf.record_count("lag_incident.acknowledged")
        except (OSError, ValueError):
            logger.exception("lag-incident-queue: cannot acknowledge entry name=%s", path.name)
            perf.record_count("lag_incident.retry")
            perf.record_count("lag_incident.circuit_open")
            return DispatchOutcome(False)
    return DispatchOutcome(True)


async def _drain(dispatch: Callable[[bytes], Awaitable[DispatchResult]]) -> bool:
    """Return whether dispatch should retry; retained for queue test callers."""
    return not (await _drain_outcome(dispatch)).acknowledged


def _retry_delay(failures: int) -> float:
    if _RETRY_BASE_SECONDS <= 0:
        bounded = 0.0
    else:
        max_exponent = max(0, math.ceil(math.log2(_RETRY_MAX_SECONDS / _RETRY_BASE_SECONDS)))
        exponent = min(max(0, failures - 1), max_exponent)
        bounded = min(_RETRY_MAX_SECONDS, _RETRY_BASE_SECONDS * (2 ** exponent))
    jitter = bounded * _RETRY_JITTER_RATIO
    return max(0.0, bounded + random.uniform(-jitter, jitter))


async def _wait_retry_delay(delay: float) -> None:
    assert _wake is not None
    destination_generation = _destination_generation
    deadline = time.monotonic() + delay
    while not _stopping:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        try:
            await asyncio.wait_for(_wake.wait(), timeout=remaining)
        except TimeoutError:
            return
        _wake.clear()
        if _destination_generation != destination_generation:
            return


async def _run(dispatch: Callable[[bytes], Awaitable[DispatchResult]]) -> None:
    assert _wake is not None
    failures = 0
    while True:
        await _wake.wait()
        _wake.clear()
        if _stopping:
            return
        outcome = await _drain_outcome(dispatch)
        if not outcome.acknowledged:
            failures += 1
            delay = _retry_delay(failures)
            if outcome.retry_after is not None:
                delay = max(delay, min(_RETRY_MAX_SECONDS, max(0.0, outcome.retry_after)))
            perf.record("lag_incident.retry_backoff", delay * 1000.0)
            await _wait_retry_delay(delay)
            if _stopping:
                return
            _wake.set()
        else:
            failures = 0


def start(dispatch: Callable[[bytes], Awaitable[DispatchResult]]) -> None:
    global _loop, _wake, _task, _stopping
    loop = asyncio.get_running_loop()
    if _task is not None and not _task.done():
        return
    _loop = loop
    _wake = asyncio.Event()
    _stopping = False
    perf.register_queue("lag_incidents", depth)
    async def reconcile_depth() -> None:
        try:
            await asyncio.to_thread(_reconcile_depth_projection)
        except Exception:
            logger.exception("lag-incident-queue: depth reconciliation failed")
    loop.create_task(reconcile_depth(), name="lag-incident-depth-reconcile")
    _task = loop.create_task(_run(dispatch), name="lag-incident-dispatcher")
    _wake.set()


async def stop() -> None:
    global _loop, _wake, _task, _stopping
    task = _task
    _stopping = True
    perf.unregister_queue("lag_incidents")
    if task is None:
        _loop = None
        _wake = None
        return
    assert _wake is not None
    _wake.set()
    await asyncio.shield(task)
    _task = None
    _loop = None
    _wake = None
