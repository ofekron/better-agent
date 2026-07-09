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

import logging
import os
from pathlib import Path

from event_ingester import event_ingester
from session_manager import manager as session_manager

logger = logging.getLogger(__name__)

_BAK_SUFFIX = ".pre-redigest.bak"


def _root_json_path(root_id: str) -> Path:
    import session_store
    return Path(session_store.session_file_path(root_id))


def _events_jsonl_path(root_id: str) -> Path:
    return _root_json_path(root_id).parent / root_id / "events.jsonl"


def _atomic_copy(src: Path, dst: Path) -> None:
    """Copy `src` to `dst` atomically (tmp file in the same dir, then
    os.replace) so a torn backup can never replace a good one."""
    tmp = dst.with_name(f".{dst.name}.{os.getpid()}.tmp")
    with src.open("rb") as fsrc, tmp.open("wb") as fdst:
        while True:
            chunk = fsrc.read(1 << 20)
            if not chunk:
                break
            fdst.write(chunk)
    os.replace(tmp, dst)


class _FileSlot:
    __slots__ = ("live", "backup", "had_backup")

    def __init__(self, live: Path):
        self.live = live
        self.backup = live.with_name(live.name + _BAK_SUFFIX)
        self.had_backup = False


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

    def __init__(self, root_id: str):
        self.root_id = root_id
        self._slots = [
            _FileSlot(_root_json_path(root_id)),
            _FileSlot(_events_jsonl_path(root_id)),
        ]
        self._captured = False
        self._settled = False

    def capture(self) -> "RedigestBackup":
        """Copy every existing derived file to its `.pre-redigest.bak`
        sibling. Files that don't exist yet are recorded as absent so
        `rollback` can recreate that absence (delete the live file)."""
        for slot in self._slots:
            if slot.live.exists():
                _atomic_copy(slot.live, slot.backup)
                slot.had_backup = True
        self._captured = True
        return self

    def commit(self) -> None:
        """Re-digest succeeded — drop the snapshot."""
        for slot in self._slots:
            slot.backup.unlink(missing_ok=True)
        self._settled = True

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
