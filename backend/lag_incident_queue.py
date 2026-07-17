from __future__ import annotations

import asyncio
import contextvars
import functools
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
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

import perf
from paths import ba_home
from secret_redaction import redact_secrets
from portable_lock import lock_ex, unlock


logger = logging.getLogger(__name__)

# Isolates this module's continuous background spool-processing/poll loop
# (unbounded-duration file I/O run on every dispatcher wake cycle) from the
# process-wide default executor, so it can't delay unrelated
# `asyncio.to_thread` callers elsewhere in the backend that happen to share
# the default pool. Not latency-sensitive itself, so a small worker count is
# fine.
_SPOOL_IO_EXECUTOR = ThreadPoolExecutor(
    max_workers=4,
    thread_name_prefix="lag-incident-io",
)


async def _to_thread(func, /, *args, **kwargs):
    """`asyncio.to_thread`, routed through the dedicated spool-I/O executor
    instead of the shared default pool."""
    loop = asyncio.get_running_loop()
    ctx = contextvars.copy_context()
    call = functools.partial(ctx.run, func, *args, **kwargs)
    return await loop.run_in_executor(_SPOOL_IO_EXECUTOR, call)


_MAX_PENDING = 256
_MAX_PAYLOAD_BYTES = 18_000
_MAX_TOTAL_ENTRIES = 2_048
_MAX_TOTAL_BYTES = 16 * 1024 * 1024
# Parked entries wait for a destination that may never come back; cap them so
# a permanently blocked destination cannot grow the spool without bound.
# Pruning drops oldest-first down to the target, amortizing the scan.
_MAX_PARKED = 256
_PARKED_PRUNE_TARGET = 192
_BACKPRESSURE_RESERVE_ENTRIES = 64
_BACKPRESSURE_RESERVE_BYTES = 1024 * 1024
_RETRY_BASE_SECONDS = 1.0
_RETRY_MAX_SECONDS = 900.0
_RETRY_JITTER_RATIO = 0.2
_FILE_SUFFIX = ".json"
_PARKED_SUFFIX = ".parked"
_OVERFLOW_SUFFIX = ".overflow"
_OVERFLOW_LEDGER_NAME = ".overflow.ledger"
_OVERFLOW_LEDGER_VERSION = 2
_lock = threading.RLock()
_wake: asyncio.Event | None = None
_loop: asyncio.AbstractEventLoop | None = None
_task: asyncio.Task | None = None
_stopping = False
_destination_generation = 0
_dispatch_generation = 0
_depth_cache = 0
_DEPTH_META_NAME = ".depth.meta"
_DEPTH_LOCK_NAME = ".depth.lock"
_DEPTH_VERSION = 2
_INVENTORY_FIELDS = ("depth", "pending_count", "parked_count", "active_bytes")
_RETRY_META_NAME = ".retry.meta"
_RETRY_META_VERSION = 2
_DESTINATION_META_NAME = ".destination.meta"
_DESTINATION_META_VERSION = 2
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
    destination_unavailable: bool = False


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


def _write_inventory_locked(root: Path, inventory: dict[str, int], generation: int) -> None:
    target = root / _DEPTH_META_NAME
    temporary = root / f".{_DEPTH_META_NAME}.{uuid.uuid4().hex}.tmp"
    record: dict[str, int] = {"version": _DEPTH_VERSION, "generation": max(0, int(generation))}
    for field in _INVENTORY_FIELDS:
        record[field] = max(0, int(inventory[field]))
    body = json.dumps(record, separators=(",", ":")).encode("utf-8")
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


def _load_retry_state() -> tuple[int, float]:
    root = _secure_spool_dir()
    try:
        data = json.loads((root / _RETRY_META_NAME).read_text(encoding="utf-8"))
        if data.get("version") != _RETRY_META_VERSION:
            return 0, 0.0
        failures = max(0, int(data.get("failures") or 0))
        remaining = max(0.0, min(_RETRY_MAX_SECONDS, float(data.get("remaining_seconds") or 0.0)))
        saved_epoch = float(data.get("saved_epoch") or 0.0)
        now = time.time()
        elapsed = now - saved_epoch
        if not all(math.isfinite(value) for value in (remaining, saved_epoch, elapsed)):
            return 0, 0.0
        if elapsed < 0.0:
            elapsed = 0.0
        elif elapsed > _RETRY_MAX_SECONDS * 2:
            elapsed = remaining
        return failures, now + max(0.0, remaining - elapsed)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return 0, 0.0


def _save_retry_state(failures: int, next_attempt_epoch: float) -> None:
    root = _secure_spool_dir()
    target = root / _RETRY_META_NAME
    if failures <= 0:
        target.unlink(missing_ok=True)
        _fsync_parent_portable(root)
        return
    temporary = root / f".{_RETRY_META_NAME}.{uuid.uuid4().hex}.tmp"
    try:
        now = time.time()
        remaining = max(0.0, min(_RETRY_MAX_SECONDS, next_attempt_epoch - now))
        body = json.dumps({
            "version": _RETRY_META_VERSION,
            "failures": failures,
            "saved_epoch": now,
            "remaining_seconds": remaining,
        }, separators=(",", ":")).encode("utf-8")
        with open(temporary, "xb") as stream:
            os.chmod(temporary, 0o600)
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        _fsync_parent_portable(root)
    finally:
        temporary.unlink(missing_ok=True)


def _read_inventory_locked(root: Path) -> tuple[dict[str, int], int] | None:
    try:
        info = (root / _DEPTH_META_NAME).lstat()
        if not stat.S_ISREG(info.st_mode):
            return None
        data = json.loads((root / _DEPTH_META_NAME).read_text(encoding="utf-8"))
        if data.get("version") != _DEPTH_VERSION:
            return None
        inventory: dict[str, int] = {}
        for field in _INVENTORY_FIELDS:
            value = data[field]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                return None
            inventory[field] = value
        return inventory, max(0, int(data["generation"]))
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None


def _load_destination_meta_locked(root: Path) -> tuple[int, int | None, str]:
    try:
        raw = (root / _DESTINATION_META_NAME).read_bytes()
        if len(raw) > 256:
            raise RuntimeError("destination metadata exceeds limit")
        data = json.loads(raw)
        if data.get("version") != _DESTINATION_META_VERSION:
            raise RuntimeError("invalid destination metadata version")
        generation = max(0, int(data["generation"]))
        blocked = data.get("blocked_generation")
        identity = data.get("identity")
        if not isinstance(identity, str) or len(identity) > 256:
            raise RuntimeError("invalid destination identity")
        return generation, None if blocked is None else max(0, int(blocked)), identity
    except FileNotFoundError:
        return 0, None, ""
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise RuntimeError("invalid destination metadata") from exc


def _write_destination_meta_locked(
    root: Path, generation: int, blocked: int | None, identity: str = "",
) -> None:
    target = root / _DESTINATION_META_NAME
    temporary = root / f".{_DESTINATION_META_NAME}.{uuid.uuid4().hex}.tmp"
    body = json.dumps({
        "version": _DESTINATION_META_VERSION,
        "generation": generation,
        "blocked_generation": blocked,
        "identity": identity,
    }, separators=(",", ":")).encode()
    try:
        with open(temporary, "xb") as stream:
            os.chmod(temporary, 0o600)
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        _fsync_parent_portable(root)
    finally:
        temporary.unlink(missing_ok=True)


def _rebuild_inventory_locked(root: Path) -> dict[str, int]:
    """Authoritative inventory from one spool scan; caller holds the process lock."""
    pending, active, active_bytes = _active_inventory(root)
    overflow = len(_reconcile_overflow_refs_locked(root))
    return {
        "depth": active + overflow,
        "pending_count": pending,
        "parked_count": active - pending,
        "active_bytes": active_bytes,
    }


def _inventory_for_update_locked(root: Path) -> tuple[dict[str, int], int]:
    """Load the O(1) inventory projection, rebuilding and persisting it when
    missing or invalid; caller holds the process lock."""
    metadata = _read_inventory_locked(root)
    if metadata is not None:
        return metadata
    with perf.timed("lag_incident.depth_reconcile"):
        inventory = _rebuild_inventory_locked(root)
    _write_inventory_locked(root, inventory, 1)
    return inventory, 1


def _reconcile_depth_projection() -> int:
    root = _secure_spool_dir()
    with _depth_process_lock(root):
        inventory = _rebuild_inventory_locked(root)
        metadata = _read_inventory_locked(root)
        generation = (metadata[1] if metadata else 0) + 1
        _write_inventory_locked(root, inventory, generation)
    _set_depth(inventory["depth"])
    return inventory["depth"]


def _apply_inventory_delta(
    *, depth: int = 0, pending: int = 0, parked: int = 0, active_bytes: int = 0,
) -> int:
    """Adjust the persisted inventory after a spool mutation. When the
    projection is missing it is rebuilt from the spool — which already
    reflects the mutation — so the deltas are discarded in that case."""
    root = _secure_spool_dir()
    with _depth_process_lock(root):
        metadata = _read_inventory_locked(root)
        if metadata is None:
            with perf.timed("lag_incident.depth_reconcile"):
                inventory = _rebuild_inventory_locked(root)
            generation = 0
        else:
            inventory, generation = metadata
            inventory["depth"] = max(0, inventory["depth"] + int(depth))
            inventory["pending_count"] = max(0, inventory["pending_count"] + int(pending))
            inventory["parked_count"] = max(0, inventory["parked_count"] + int(parked))
            inventory["active_bytes"] = max(0, inventory["active_bytes"] + int(active_bytes))
        with perf.timed("lag_incident.depth_commit"):
            _write_inventory_locked(root, inventory, generation + 1)
    _set_depth(inventory["depth"])
    return inventory["depth"]


def _prune_parked_locked(root: Path, inventory: dict[str, int]) -> None:
    """Drop oldest parked incidents beyond _MAX_PARKED down to the prune
    target, adjusting the inventory in place; caller holds the process lock."""
    if inventory["parked_count"] <= _MAX_PARKED:
        return
    parked = _spool_files(_PARKED_SUFFIX, strict=True)
    excess = len(parked) - _PARKED_PRUNE_TARGET
    dropped = 0
    for source in parked[:excess]:
        try:
            size = source.lstat().st_size
            source.unlink()
        except OSError:
            continue
        dropped += 1
        inventory["depth"] = max(0, inventory["depth"] - 1)
        inventory["parked_count"] = max(0, inventory["parked_count"] - 1)
        inventory["active_bytes"] = max(0, inventory["active_bytes"] - size)
    if dropped:
        _fsync_parent_portable(root)
        perf.record_count("lag_incident.parked_pruned", dropped)


def _record_parked_shift_locked(root: Path) -> None:
    """Account a pending→parked move (depth unchanged) and enforce the parked
    cap; caller holds the process lock. A missing projection stays missing —
    the next loader rebuilds it from the spool."""
    metadata = _read_inventory_locked(root)
    if metadata is None:
        return
    inventory, generation = metadata
    inventory["pending_count"] = max(0, inventory["pending_count"] - 1)
    inventory["parked_count"] += 1
    _prune_parked_locked(root, inventory)
    _write_inventory_locked(root, inventory, generation + 1)
    _set_depth(inventory["depth"])


def _active_inventory(root: Path) -> tuple[int, int, int]:
    """Return pending count, active count, and bytes in one directory pass."""
    pending = 0
    active = 0
    total_bytes = 0
    with perf.timed("lag_incident.inventory_scan"):
        for item in os.scandir(root):
            if not (item.name.endswith(_FILE_SUFFIX) or item.name.endswith(_PARKED_SUFFIX)):
                continue
            info = item.stat(follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode):
                continue
            active += 1
            total_bytes += info.st_size
            if item.name.endswith(_FILE_SUFFIX):
                pending += 1
    return pending, active, total_bytes


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
    return _spool_files(_FILE_SUFFIX, strict=strict)


def _count_spool(suffix: str) -> int:
    """Count spool entries by suffix WITHOUT per-file os.stat or sorting.

    `parked_depth` is read by the perf rollup (perf.flush) directly on the
    event loop every ROLLUP_SECS. `_spool_files` stats + sorts every entry,
    which blocked the loop (~1.6s at 1669 parked files). A depth gauge only
    needs the count, so list names and count — O(1) syscall regardless of N.
    """
    dir_fd: int | None = None
    try:
        root = _secure_spool_dir()
        if not _DIRFD_SUPPORTED:
            with os.scandir(root) as entries:
                return sum(1 for entry in entries if entry.name.endswith(suffix))
        dir_fd = _open_spool_dir_fd(root)
        return sum(1 for name in os.listdir(dir_fd) if name.endswith(suffix))
    finally:
        if dir_fd is not None:
            os.close(dir_fd)


def parked_depth() -> int:
    try:
        return _count_spool(_PARKED_SUFFIX)
    except (OSError, RuntimeError):
        return 0


def _load_overflow_ledger_locked(root: Path) -> list[dict[str, object]]:
    path = root / _OVERFLOW_LEDGER_NAME
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return []
    hard_entries = _MAX_TOTAL_ENTRIES + _BACKPRESSURE_RESERVE_ENTRIES
    hard_bytes = _MAX_TOTAL_BYTES + _BACKPRESSURE_RESERVE_BYTES
    if len(raw) > min(hard_bytes, hard_entries * 160 + 128):
        raise RuntimeError("lag incident overflow ledger exceeds byte quota")
    data = json.loads(raw)
    if data.get("version") != _OVERFLOW_LEDGER_VERSION or not isinstance(data.get("entries"), list):
        raise RuntimeError("invalid lag incident overflow ledger")
    entries = data["entries"]
    if len(entries) > hard_entries:
        raise RuntimeError("lag incident overflow ledger exceeds count quota")
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"digest", "enqueued_ns", "name", "size"}:
            raise RuntimeError("invalid lag incident overflow entry")
        digest = entry["digest"]
        if not isinstance(digest, str) or len(digest) != 16:
            raise RuntimeError("invalid lag incident overflow digest")
        name = entry["name"]
        size = entry["size"]
        if name != f"{digest}{_OVERFLOW_SUFFIX}" or not isinstance(size, int) or not 0 <= size <= _MAX_PAYLOAD_BYTES + 1:
            raise RuntimeError("invalid lag incident overflow reference")
    return entries


def _write_overflow_ledger_locked(root: Path, entries: list[dict[str, object]]) -> None:
    target = root / _OVERFLOW_LEDGER_NAME
    if not entries:
        target.unlink(missing_ok=True)
        _fsync_parent_portable(root)
        return
    body = json.dumps(
        {"version": _OVERFLOW_LEDGER_VERSION, "entries": entries},
        separators=(",", ":"),
    ).encode("utf-8")
    if (
        len(entries) > _MAX_TOTAL_ENTRIES + _BACKPRESSURE_RESERVE_ENTRIES
        or len(body) > _MAX_TOTAL_BYTES + _BACKPRESSURE_RESERVE_BYTES
    ):
        raise LagIncidentSpoolFull("lag incident spool quota exhausted")
    temporary = root / f".{_OVERFLOW_LEDGER_NAME}.{uuid.uuid4().hex}.tmp"
    try:
        with open(temporary, "xb") as stream:
            os.chmod(temporary, 0o600)
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        _fsync_parent_portable(root)
    finally:
        temporary.unlink(missing_ok=True)


def _reconcile_overflow_refs_locked(root: Path) -> list[dict[str, object]]:
    try:
        entries = _load_overflow_ledger_locked(root)
    except (RuntimeError, ValueError, TypeError, json.JSONDecodeError):
        ledger = root / _OVERFLOW_LEDGER_NAME
        if ledger.exists():
            os.replace(ledger, root / f"{_OVERFLOW_LEDGER_NAME}.corrupt.{uuid.uuid4().hex}")
            _fsync_parent_portable(root)
        entries = []
        perf.record_count("lag_incident.overflow_ledger_quarantined")
    known = {str(entry["name"]) for entry in entries}
    changed = False
    for item in os.scandir(root):
        if not item.name.endswith(_OVERFLOW_SUFFIX) or item.name in known:
            continue
        info = item.stat(follow_symlinks=False)
        digest = item.name[:-len(_OVERFLOW_SUFFIX)]
        if not stat.S_ISREG(info.st_mode) or len(digest) != 16:
            continue
        entries.append({
            "digest": digest,
            "enqueued_ns": info.st_mtime_ns,
            "name": item.name,
            "size": min(info.st_size, _MAX_PAYLOAD_BYTES + 1),
        })
        changed = True
    entries.sort(key=lambda entry: (int(entry["enqueued_ns"]), str(entry["name"])))
    if changed:
        _write_overflow_ledger_locked(root, entries)
    return entries


def _overflow_depth() -> int:
    root = _secure_spool_dir()
    with _depth_process_lock(root):
        return len(_load_overflow_ledger_locked(root))


def _overflow_quota_error(
    inventory: dict[str, int],
    entries: list[dict[str, object]],
    payload: bytes,
    projected: bytes,
    *,
    use_reserve: bool,
) -> str | None:
    entry_limit = _MAX_TOTAL_ENTRIES + (_BACKPRESSURE_RESERVE_ENTRIES if use_reserve else 0)
    byte_limit = _MAX_TOTAL_BYTES + (_BACKPRESSURE_RESERVE_BYTES if use_reserve else 0)
    active_count = inventory["pending_count"] + inventory["parked_count"]
    if active_count + len(entries) >= entry_limit:
        return "lag incident spool count quota exhausted"
    overflow_bytes = sum(int(item["size"]) for item in entries)
    if inventory["active_bytes"] + overflow_bytes + len(payload) + len(projected) > byte_limit:
        return "lag incident spool byte quota exhausted"
    return None


def _append_overflow(payload: bytes, digest: str, *, use_reserve: bool = False) -> bool:
    root = _secure_spool_dir()
    with _depth_process_lock(root):
        inventory, generation = _inventory_for_update_locked(root)
        entries = _reconcile_overflow_refs_locked(root)
        if (
            any(entry["digest"] == digest for entry in entries)
            or (root / f"{digest}{_FILE_SUFFIX}").exists()
            or (root / f"{digest}{_PARKED_SUFFIX}").exists()
        ):
            return False
        name = f"{digest}{_OVERFLOW_SUFFIX}"
        destination = root / name
        temporary = root / f".{digest}.{uuid.uuid4().hex}.tmp"
        candidate = entries + [{
            "digest": digest,
            "enqueued_ns": time.time_ns(),
            "name": name,
            "size": len(payload),
        }]
        projected = json.dumps(
            {"version": _OVERFLOW_LEDGER_VERSION, "entries": candidate},
            separators=(",", ":"),
        ).encode("utf-8")
        error = _overflow_quota_error(inventory, entries, payload, projected, use_reserve=use_reserve)
        if error is not None:
            inventory = _rebuild_inventory_locked(root)
            generation += 1
            _write_inventory_locked(root, inventory, generation)
            error = _overflow_quota_error(inventory, entries, payload, projected, use_reserve=use_reserve)
            if error is not None:
                raise LagIncidentSpoolFull(error)
        try:
            with open(temporary, "xb") as stream:
                os.chmod(temporary, 0o600)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
            _fsync_parent_portable(root)
            _write_overflow_ledger_locked(root, candidate)
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        finally:
            temporary.unlink(missing_ok=True)
        inventory["depth"] += 1
        _write_inventory_locked(root, inventory, generation + 1)
        _set_depth(inventory["depth"])
    return True


def enqueue_backpressure(payload_bytes: bytes) -> bool:
    """Use the bounded immutable reserve after normal spool backpressure."""
    payload = _validated_payload(payload_bytes, require_redacted=False)
    canonical = _encode(payload)
    digest = str(payload["requirement_ref"]).rsplit(":", 1)[-1]
    created = _append_overflow(canonical, digest, use_reserve=True)
    if created:
        perf.record_count("lag_incident.backpressure_reserved")
    _notify_dispatcher()
    return created


def _spool_files(suffix: str, *, strict: bool = False) -> list[Path]:
    dir_fd: int | None = None
    try:
        root = _secure_spool_dir()
        if not _DIRFD_SUPPORTED:
            entries = [
                (entry.stat(follow_symlinks=False).st_mtime_ns, Path(entry.path))
                for entry in os.scandir(root)
                if entry.name.endswith(suffix)
            ]
            return [path for _, path in sorted(entries, key=lambda item: (item[0], item[1].name))]
        dir_fd = _open_spool_dir_fd(root)
        entries: list[tuple[int, str]] = []
        for name in os.listdir(dir_fd):
            if not name.endswith(suffix):
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


def _publish_payload(root: Path, name: str, payload: bytes) -> None:
    temporary = root / f".{name}.{uuid.uuid4().hex}.tmp"
    try:
        with open(temporary, "xb") as stream:
            os.chmod(temporary, 0o600)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, root / name)
        _fsync_parent_portable(root)
    finally:
        temporary.unlink(missing_ok=True)


def _enqueue_quota_error(
    root: Path,
    inventory: dict[str, int],
    entries: list[dict[str, object]],
    payload_bytes: int,
) -> str | None:
    active_count = inventory["pending_count"] + inventory["parked_count"]
    if active_count + len(entries) >= _MAX_TOTAL_ENTRIES:
        return "lag incident spool count quota exhausted"
    overflow_bytes = sum(int(item["size"]) for item in entries)
    ledger = root / _OVERFLOW_LEDGER_NAME
    ledger_bytes = ledger.stat(follow_symlinks=False).st_size if ledger.exists() else 0
    if inventory["active_bytes"] + overflow_bytes + ledger_bytes + payload_bytes > _MAX_TOTAL_BYTES:
        return "lag incident spool byte quota exhausted"
    return None


def enqueue(payload_bytes: bytes) -> bool:
    payload = _validated_payload(payload_bytes, require_redacted=False)
    payload_bytes = _encode(payload)
    if len(payload_bytes) > _MAX_PAYLOAD_BYTES:
        raise ValueError("payload exceeds spool limit")
    digest = str(payload["requirement_ref"]).rsplit(":", 1)[-1]
    if len(digest) != 16 or any(ch not in "0123456789abcdef" for ch in digest):
        raise ValueError("invalid lag incident digest")
    root = _secure_spool_dir()
    destination_name = f"{digest}{_FILE_SUFFIX}"
    parked_name = f"{digest}{_PARKED_SUFFIX}"
    with perf.timed("lag_incident.spool_write"):
        with _lock:
            lock_started = time.perf_counter()
            with _depth_process_lock(root):
                perf.record("lag_incident.spool_lock_wait", (time.perf_counter() - lock_started) * 1000.0)
                # Inventory first: a rebuild reconciles the overflow ledger,
                # so the ledger must be read after it to stay authoritative.
                inventory, generation = _inventory_for_update_locked(root)
                entries = _load_overflow_ledger_locked(root)
                names = {destination_name, parked_name, f"{digest}{_OVERFLOW_SUFFIX}"}
                if any((root / name).exists() for name in names) or any(
                    entry["digest"] == digest for entry in entries
                ):
                    _notify_dispatcher()
                    return False
                error = _enqueue_quota_error(root, inventory, entries, len(payload_bytes))
                if error is not None:
                    # Verify against a fresh scan before rejecting so
                    # projection drift can never refuse capacity that exists.
                    inventory = _rebuild_inventory_locked(root)
                    generation += 1
                    _write_inventory_locked(root, inventory, generation)
                    entries = _load_overflow_ledger_locked(root)
                    error = _enqueue_quota_error(root, inventory, entries, len(payload_bytes))
                    if error is not None:
                        raise LagIncidentSpoolFull(error)
                dest_generation, blocked, _identity = _load_destination_meta_locked(root)
                if blocked == dest_generation:
                    _publish_payload(root, parked_name, payload_bytes)
                    inventory["depth"] += 1
                    inventory["parked_count"] += 1
                    inventory["active_bytes"] += len(payload_bytes)
                    _prune_parked_locked(root, inventory)
                    _write_inventory_locked(root, inventory, generation + 1)
                    _set_depth(inventory["depth"])
                    perf.record_count("lag_incident.parked_same_generation")
                    _notify_dispatcher()
                    return True
                if inventory["pending_count"] < _MAX_PENDING:
                    with perf.timed("lag_incident.payload_publish"):
                        _publish_payload(root, destination_name, payload_bytes)
                    inventory["pending_count"] += 1
                    inventory["active_bytes"] += len(payload_bytes)
                else:
                    name = f"{digest}{_OVERFLOW_SUFFIX}"
                    with perf.timed("lag_incident.payload_publish"):
                        _publish_payload(root, name, payload_bytes)
                    entries.append({
                        "digest": digest,
                        "enqueued_ns": time.time_ns(),
                        "name": name,
                        "size": len(payload_bytes),
                    })
                    _write_overflow_ledger_locked(root, entries)
                    perf.record_count("lag_incident.overflow_enqueued")
                inventory["depth"] += 1
                _write_inventory_locked(root, inventory, generation + 1)
                _set_depth(inventory["depth"])
    perf.record_count("lag_incident.enqueued")
    _notify_dispatcher()
    return True


def _promote_overflow() -> bool:
    root = _secure_spool_dir()
    with _lock:
        if len(_pending_files(strict=True)) >= _MAX_PENDING:
            return False
        with _depth_process_lock(root):
            entries = _reconcile_overflow_refs_locked(root)
            if not entries:
                return False
            entry = entries[0]
            digest = str(entry["digest"])
            destination = root / f"{digest}{_FILE_SUFFIX}"
            promoted_size: int | None = None
            if not destination.exists():
                source = root / str(entry["name"])
                info = source.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_size != int(entry["size"]):
                    raise RuntimeError("lag incident overflow reference is corrupt")
                with open(source, "rb") as stream:
                    payload = stream.read(_MAX_PAYLOAD_BYTES + 1)
                if len(payload) != int(entry["size"]):
                    raise RuntimeError("lag incident overflow payload changed")
                _validated_payload(payload)
                temporary = root / f".{digest}.{uuid.uuid4().hex}.tmp"
                try:
                    with open(temporary, "xb") as stream:
                        os.chmod(temporary, 0o600)
                        stream.write(payload)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, destination)
                    _fsync_parent_portable(root)
                finally:
                    temporary.unlink(missing_ok=True)
                promoted_size = len(payload)
            (root / str(entry["name"])).unlink(missing_ok=True)
            _write_overflow_ledger_locked(root, entries[1:])
            metadata = _read_inventory_locked(root)
            if metadata is not None:
                inventory, generation = metadata
                if promoted_size is None:
                    # Duplicate overflow reference removed; one fewer entry.
                    inventory["depth"] = max(0, inventory["depth"] - 1)
                else:
                    inventory["pending_count"] += 1
                    inventory["active_bytes"] += promoted_size
                _write_inventory_locked(root, inventory, generation + 1)
                _set_depth(inventory["depth"])
    perf.record_count("lag_incident.overflow_promoted")
    return True


def _reactivate_parked() -> int:
    """Single pass: one parked listing, one pending count, one ledger load and
    at most one ledger write — never one scan per parked entry."""
    root = _secure_spool_dir()
    moved = 0
    with _lock:
        with _depth_process_lock(root):
            parked = _spool_files(_PARKED_SUFFIX, strict=True)
            if parked:
                entries = _reconcile_overflow_refs_locked(root)
                ledger_digests = {str(entry["digest"]) for entry in entries}
                pending_count = len(_pending_files(strict=True))
                ledger_changed = False
                for source in parked:
                    stem = source.name[:-len(_PARKED_SUFFIX)]
                    active = root / f"{stem}{_FILE_SUFFIX}"
                    if active.exists() or stem in ledger_digests:
                        source.unlink()
                    elif pending_count < _MAX_PENDING:
                        os.replace(source, active)
                        pending_count += 1
                    else:
                        with open(source, "rb") as stream:
                            payload = stream.read(_MAX_PAYLOAD_BYTES + 1)
                        if len(payload) > _MAX_PAYLOAD_BYTES:
                            # Oversized entries never came from enqueue; leave
                            # them for the cap pruner rather than propagate.
                            continue
                        _publish_payload(root, f"{stem}{_OVERFLOW_SUFFIX}", payload)
                        entries.append({
                            "digest": stem,
                            "enqueued_ns": time.time_ns(),
                            "name": f"{stem}{_OVERFLOW_SUFFIX}",
                            "size": len(payload),
                        })
                        ledger_digests.add(stem)
                        ledger_changed = True
                        source.unlink()
                    moved += 1
                if ledger_changed:
                    _write_overflow_ledger_locked(root, entries)
                _fsync_parent_portable(root)
        while _promote_overflow():
            pass
        _reconcile_depth_projection()
    if moved:
        perf.record_count("lag_incident.parked_reactivated", moved)
    return moved


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
    root = _secure_spool_dir()
    with _depth_process_lock(root):
        generation, _blocked, destination_identity = _load_destination_meta_locked(root)
        _destination_generation = max(_destination_generation, generation) + 1
        _write_destination_meta_locked(
            root, _destination_generation, None, destination_identity,
        )
    _reactivate_parked()
    _notify_dispatcher()


def _destination_state() -> tuple[int, int | None]:
    root = _secure_spool_dir()
    with _depth_process_lock(root):
        generation, blocked, _identity = _load_destination_meta_locked(root)
        return generation, blocked


def synchronize_destination(identity: str) -> bool:
    """Persist and publish one authoritative destination identity transition."""
    global _destination_generation
    if not isinstance(identity, str) or not identity or len(identity) > 256:
        raise ValueError("destination identity must be a bounded non-empty string")
    root = _secure_spool_dir()
    with _depth_process_lock(root):
        try:
            generation, _blocked, previous_identity = _load_destination_meta_locked(root)
        except RuntimeError:
            generation, previous_identity = 0, ""
        if identity == previous_identity:
            _destination_generation = max(_destination_generation, generation)
            return False
        _destination_generation = max(_destination_generation, generation) + 1
        _write_destination_meta_locked(root, _destination_generation, None, identity)
    _reactivate_parked()
    _notify_dispatcher()
    return True


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
    except FileNotFoundError:
        return None
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


def _park(
    path: Path,
    identity: EntryIdentity,
    *,
    only_if_generation_blocked: bool = False,
    expected_generation: int | None = None,
) -> bool:
    root = _secure_spool_dir()
    destination_name = f"{path.stem}{_PARKED_SUFFIX}"
    with _depth_process_lock(root):
        generation, _blocked, destination_identity = _load_destination_meta_locked(root)
        if expected_generation is not None and generation != expected_generation:
            return False
        if only_if_generation_blocked and _blocked != generation:
            return False
        if not only_if_generation_blocked:
            _write_destination_meta_locked(
                root, generation, generation, destination_identity,
            )
        if not _DIRFD_SUPPORTED:
            current = path.lstat()
            if not stat.S_ISREG(current.st_mode) or _entry_stat_identity(current) != identity[:4]:
                raise ValueError("spool entry identity changed before parking")
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != identity[4]:
                raise ValueError("spool entry content changed before parking")
            os.replace(path, root / destination_name)
            _fsync_parent_portable(root)
            _record_parked_shift_locked(root)
            return True
        dir_fd = _open_spool_dir_fd(root)
        try:
            file_fd, before = _open_entry(dir_fd, path.name)
            try:
                with os.fdopen(file_fd, "rb", closefd=False) as stream:
                    raw = stream.read(_MAX_PAYLOAD_BYTES + 1)
            finally:
                os.close(file_fd)
            if before != identity[:4] or hashlib.sha256(raw).hexdigest() != identity[4]:
                raise ValueError("spool entry changed before parking")
            os.replace(path.name, destination_name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            _fsync_dir(dir_fd)
        finally:
            os.close(dir_fd)
        _record_parked_shift_locked(root)
    return True


def _park_blocked_generation_entries() -> int:
    moved = 0
    while True:
        progressed = False
        for path in _pending_files(strict=True):
            loaded = _read(path)
            if loaded is None:
                continue
            _payload, _enqueued_at, identity = loaded
            if _park(path, identity, only_if_generation_blocked=True):
                moved += 1
                progressed = True
        if _promote_overflow():
            progressed = True
        if not progressed:
            return moved


async def _drain_outcome(
    dispatch: Callable[[bytes], Awaitable[DispatchResult]],
) -> DispatchOutcome:
    try:
        await _to_thread(_secure_spool_dir)
        while await _to_thread(_promote_overflow):
            pass
        pending = await _to_thread(_pending_files, strict=True)
        generation, blocked_generation = await _to_thread(_destination_state)
        if blocked_generation == generation:
            await _to_thread(_park_blocked_generation_entries)
            pending = await _to_thread(_pending_files, strict=True)
        overflow_depth = await _to_thread(_overflow_depth)
        parked = await _to_thread(_count_spool, _PARKED_SUFFIX)
        _set_depth(len(pending) + overflow_depth + parked)
        perf.record("lag_incident.overflow_depth", float(overflow_depth))
    except (OSError, RuntimeError):
        logger.exception("lag-incident-queue: cannot securely open spool")
        perf.record_count("lag_incident.retry")
        perf.record_count("lag_incident.circuit_open")
        return DispatchOutcome(False)
    for path in pending:
        if _stopping:
            return DispatchOutcome(True)
        loaded = await _to_thread(_read, path)
        if loaded is None:
            continue
        payload, enqueued_at, identity = loaded
        perf.record("lag_incident.enqueue_age", max(0.0, time.time() - enqueued_at) * 1000.0)
        body = _encode(payload)
        started = time.perf_counter()
        try:
            global _dispatch_generation
            _dispatch_generation = _destination_generation
            result = await dispatch(body)
            outcome = result if isinstance(result, DispatchOutcome) else DispatchOutcome(bool(result))
        except Exception:
            outcome = DispatchOutcome(False)
            logger.exception("lag-incident-queue: dispatch raised")
        finally:
            perf.record("lag_incident.dispatch", (time.perf_counter() - started) * 1000.0)
        if not outcome.acknowledged:
            if not outcome.retryable:
                try:
                    parked = await _to_thread(
                        _park,
                        path,
                        identity,
                        expected_generation=_dispatch_generation,
                    )
                    if parked:
                        perf.record_count("lag_incident.parked")
                        if outcome.destination_unavailable:
                            moved = await _to_thread(_park_blocked_generation_entries)
                            if moved:
                                perf.record_count("lag_incident.parked", moved)
                        continue
                except (OSError, ValueError):
                    logger.exception("lag-incident-queue: cannot park entry name=%s", path.name)
            perf.record_count("lag_incident.retry")
            perf.record_count("lag_incident.circuit_open")
            return outcome
        try:
            await _to_thread(_acknowledge, path, identity)
            promoted = await _to_thread(_promote_overflow)
            await _to_thread(
                functools.partial(
                    _apply_inventory_delta,
                    depth=-1,
                    pending=-1,
                    active_bytes=-identity[3],
                )
            )
            perf.record_count("lag_incident.acknowledged")
            if promoted and _wake is not None:
                _wake.set()
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
    global _destination_generation
    assert _wake is not None
    _destination_generation, _blocked_generation = await _to_thread(_destination_state)
    failures, next_attempt_epoch = await _to_thread(_load_retry_state)
    observed_destination_generation = _destination_generation
    while True:
        await _wake.wait()
        _wake.clear()
        if _stopping:
            return
        if _destination_generation != observed_destination_generation:
            observed_destination_generation = _destination_generation
            await _to_thread(_reactivate_parked)
        persisted_delay = max(0.0, next_attempt_epoch - time.time())
        if persisted_delay:
            perf.record("lag_incident.persisted_retry_delay", persisted_delay * 1000.0)
            generation = _destination_generation
            await _wait_retry_delay(min(_RETRY_MAX_SECONDS, persisted_delay))
            if _stopping:
                return
            if _destination_generation != generation:
                next_attempt_epoch = 0.0
        outcome = await _drain_outcome(dispatch)
        if not outcome.acknowledged:
            failures += 1
            delay = _retry_delay(failures)
            if outcome.retry_after is not None:
                delay = max(delay, min(_RETRY_MAX_SECONDS, max(0.0, outcome.retry_after)))
            perf.record("lag_incident.retry_backoff", delay * 1000.0)
            next_attempt_epoch = time.time() + delay
            await _to_thread(_save_retry_state, failures, next_attempt_epoch)
            if _destination_generation != _dispatch_generation:
                next_attempt_epoch = 0.0
                await _to_thread(_save_retry_state, failures, 0.0)
                _wake.set()
                continue
            generation = _destination_generation
            await _wait_retry_delay(delay)
            if _stopping:
                return
            if _destination_generation != generation:
                next_attempt_epoch = 0.0
                await _to_thread(_save_retry_state, failures, 0.0)
            _wake.set()
        else:
            failures = 0
            next_attempt_epoch = 0.0
            await _to_thread(_save_retry_state, 0, 0.0)


def start(dispatch: Callable[[bytes], Awaitable[DispatchResult]]) -> None:
    global _loop, _wake, _task, _stopping
    loop = asyncio.get_running_loop()
    if _task is not None and not _task.done():
        return
    _loop = loop
    _wake = asyncio.Event()
    _stopping = False
    perf.register_queue("lag_incidents", depth)
    perf.register_queue("lag_incidents_parked", parked_depth)
    async def reconcile_depth() -> None:
        try:
            await _to_thread(_reconcile_depth_projection)
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
    perf.unregister_queue("lag_incidents_parked")
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
