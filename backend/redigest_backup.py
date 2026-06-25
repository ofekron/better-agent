"""Backup + rollback for re-digestion of a session root.

A *re-digest* replays a provider's native session stream back through
`apply_event` to regenerate the render tree (`<root_id>.json`) and the
event log (`events.jsonl`) after the ingestion pipeline improved. It
only fires for already-finalized (dead-orphan) runs whose
`ingestion_version` is older than the current pipeline version, and only
when the native source still exists.

Because a re-digest overwrites derived state the user can already see,
we snapshot the stale-but-whole derived data BEFORE the re-digest. If
the re-digest succeeds the snapshot is discarded; if it fails we restore
the snapshot so the session keeps its prior (stale) rendering instead of
a half-mutated or empty tree. Stale-but-whole beats not-at-all.

`RedigestBackup` coordinates the THREE caches that back the two files so
a rollback actually sticks (see `rollback`).
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import uuid
from pathlib import Path

from event_ingester import event_ingester
from portable_lock import try_lock_ex, unlock
from session_manager import manager as session_manager

logger = logging.getLogger(__name__)

_BAK_SUFFIX = ".pre-redigest.bak"
_ROOT_LOCKS: dict[str, threading.Lock] = {}
_ROOT_LOCKS_GUARD = threading.Lock()


def _root_json_path(root_id: str) -> Path:
    import session_store
    return Path(session_store.session_file_path(root_id))


def _events_jsonl_path(root_id: str) -> Path:
    return _root_json_path(root_id).parent / root_id / "events.jsonl"


def _atomic_copy(src: Path, dst: Path, token: str) -> None:
    """Copy `src` to `dst` atomically (tmp file in the same dir, then
    os.replace) so a torn backup can never replace a good one."""
    tmp = dst.with_name(f".{dst.name}.{os.getpid()}.{token}.tmp")
    try:
        with src.open("rb") as fsrc, tmp.open("xb") as fdst:
            while True:
                chunk = fsrc.read(1 << 20)
                if not chunk:
                    break
                fdst.write(chunk)
        os.replace(tmp, dst)
    finally:
        tmp.unlink(missing_ok=True)


def _thread_lock_for(root_id: str) -> threading.Lock:
    with _ROOT_LOCKS_GUARD:
        lock = _ROOT_LOCKS.get(root_id)
        if lock is None:
            lock = threading.Lock()
            _ROOT_LOCKS[root_id] = lock
        return lock


def _process_lock_path(root_id: str) -> Path:
    live = _root_json_path(root_id)
    digest = hashlib.sha256(root_id.encode("utf-8")).hexdigest()[:24]
    return live.parent / f".redigest-{digest}.lock"


class _FileSlot:
    __slots__ = ("live", "backup", "had_backup")

    def __init__(self, live: Path, token: str):
        self.live = live
        self.backup = live.with_name(f"{live.name}{_BAK_SUFFIX}.{token}")
        self.had_backup = False


class RecoveryRootLease:
    def __init__(self, root_id: str):
        self.root_id = root_id
        self._thread_lock = _thread_lock_for(root_id)
        self._lock_file = None
        self._cancel_acquire = threading.Event()

    @property
    def held(self) -> bool:
        return self._lock_file is not None

    @property
    def acquire_cancelled(self) -> bool:
        return self._cancel_acquire.is_set()

    def acquire(self) -> "RecoveryRootLease":
        if self.held:
            raise RuntimeError("recovery root lease already held")
        self._thread_lock.acquire()
        try:
            lock_path = _process_lock_path(self.root_id)
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            self._lock_file = lock_path.open("a+b")
            if self._lock_file.tell() == 0:
                self._lock_file.write(b"\0")
                self._lock_file.flush()
            while True:
                if self._cancel_acquire.is_set():
                    raise RuntimeError("recovery root lease acquisition cancelled")
                if try_lock_ex(self._lock_file.fileno()):
                    if self._cancel_acquire.is_set():
                        raise RuntimeError("recovery root lease acquisition cancelled")
                    return self
                self._cancel_acquire.wait(0.05)
        except BaseException:
            if self._lock_file is not None:
                self._lock_file.close()
                self._lock_file = None
            self._thread_lock.release()
            raise

    def cancel_pending_acquire(self) -> None:
        self._cancel_acquire.set()

    def release(self) -> None:
        lock_file = self._lock_file
        if lock_file is None:
            return
        self._lock_file = None
        try:
            unlock(lock_file.fileno())
            lock_file.close()
        finally:
            self._thread_lock.release()


class RedigestBackup:
    """Snapshot a root's derived state, then commit or roll it back.

    Use as::

        backup = RedigestBackup(root_id).capture()
        try:
            redigest()
        except Exception:
            backup.rollback()
            raise
        else:
            backup.commit()
    """

    def __init__(self, root_id: str, *, lease: RecoveryRootLease | None = None):
        self.root_id = root_id
        self._token = uuid.uuid4().hex
        self._slots = [
            _FileSlot(_events_jsonl_path(root_id), self._token),
            _FileSlot(_root_json_path(root_id), self._token),
        ]
        self._lease = lease or RecoveryRootLease(root_id)
        if self._lease.root_id != root_id:
            raise ValueError("redigest backup lease root mismatch")
        self._owns_lease = lease is None
        self._captured = False
        self._settled = False

    def _acquire_transaction(self) -> None:
        if self._owns_lease:
            self._lease.acquire()
            return
        if not self._lease.held:
            raise RuntimeError("borrowed recovery root lease is not held")

    def _release_transaction(self) -> None:
        if self._owns_lease:
            self._lease.release()

    def _cleanup_stale_artifacts(self) -> None:
        for slot in self._slots:
            slot.live.with_name(slot.live.name + _BAK_SUFFIX).unlink(missing_ok=True)
            pattern = slot.live.name + _BAK_SUFFIX + ".*"
            for path in slot.live.parent.glob(pattern):
                if path != slot.backup:
                    path.unlink(missing_ok=True)
            for path in slot.live.parent.glob(f".{pattern}.tmp"):
                path.unlink(missing_ok=True)

    def capture(self) -> "RedigestBackup":
        """Copy every existing derived file to its `.pre-redigest.bak`
        sibling. Files that don't exist yet are recorded as absent so
        `rollback` can recreate that absence (delete the live file)."""
        if self._captured or self._settled:
            raise RuntimeError("redigest backup transaction already used")
        self._acquire_transaction()
        try:
            self._cleanup_stale_artifacts()
            for slot in self._slots:
                if slot.live.exists():
                    _atomic_copy(slot.live, slot.backup, self._token)
                    slot.had_backup = True
            self._captured = True
            return self
        except BaseException:
            for slot in self._slots:
                slot.backup.unlink(missing_ok=True)
            self._release_transaction()
            raise

    def commit(self) -> None:
        """Re-digest succeeded — drop the snapshot."""
        if not self._captured or self._settled:
            return
        try:
            for slot in self._slots:
                slot.backup.unlink(missing_ok=True)
            self._settled = True
        finally:
            self._release_transaction()

    def rollback(self) -> None:
        """Re-digest failed — restore the pre-digest state across all
        three caches that back the two files:

        1. Files: restore `<root_id>.json` and `events.jsonl` from their
           `.bak` (or delete the live file if it had no backup — the
           pre-digest state was "absent").
        2. event_ingester: `close(root_id)` drops the open handle, the
           uid dedup set, and the seq/offset caches so the next write
           re-seeds from the restored `events.jsonl` instead of the
           orphaned old inode.
        3. session_manager: `reload_root_from_disk(root_id)` evicts the
           half-mutated in-memory root AND discards any pending debounced
           persist (which would otherwise be flushed over the restored
           file on the next cold load).

        We deliberately do NOT call `_barrier_journal` here: it raises
        on a wedged executor and would block the rollback. Any partial
        events.jsonl row that a racing shard-executor write re-appends
        after restore is cosmetic — it converges on the next re-digest
        via the uid:sha dedup (one render-tree row regardless of disk
        rows), and the run is left unmarked so the next startup retries.
        """
        if not self._captured:
            return
        if self._settled:
            return
        try:
            # Files first. events.jsonl before root json so a reader that
            # cold-loads between the two sees a consistent pair.
            for slot in self._slots:
                if slot.had_backup:
                    os.replace(slot.backup, slot.live)
                else:
                    slot.live.unlink(missing_ok=True)
            event_ingester.close(self.root_id)
            session_manager.reload_root_from_disk(self.root_id)
            self._settled = True
            logger.info(
                "redigest_backup: rolled back root %s to pre-redigest snapshot",
                self.root_id[:8],
            )
        finally:
            for slot in self._slots:
                slot.backup.unlink(missing_ok=True)
            self._release_transaction()
