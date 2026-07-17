"""Layer 1: Single writer for Better Agent session events JSONL.

All event sources (the orchestrator's per-event save callback and the
on-API-call native-jsonl migration in `main._migrate_native_jsonl`)
feed into this ingester, which appends enriched events to a per-root-session
JSONL file. This file is the single source of truth for all session events.

File location: beside the session root file, under <root_id>/events.jsonl
State location: beside the session root file, under <root_id>/ingester_state.json
"""

import hashlib
import bisect
import copy
import json
import logging
import os
import re
import threading
import time
import tempfile
from collections import OrderedDict
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from file_ref_resolver import rewrite_event_data_isolated
from event_shape import event_uuid, frontend_event_from_journal_row
from session_manager import manager as session_manager
import perf
import session_store

logger = logging.getLogger(__name__)

_UUID_KEY = "uuid"
_EVENT_SUMMARIES_VERSION = 5
_MAX_OPEN_APPEND_HANDLES = 64
_FULL_SCAN_CACHE_MAX_BYTES = 64 * 1024 * 1024
# Stable-storage fsync cadence for the background flusher. `fh.flush()`
# (kernel page-cache visibility — what cross-process tailers and readers
# actually need) stays synchronous on the ingest path; only `os.fsync()`
# (OS/power-crash durability, beyond the clean-restart convergence
# invariant) is deferred and batched here. See `_mark_fsync_dirty`.
_FSYNC_INTERVAL = 0.25
_BCFILE_LINK_RE = re.compile(r"`?\[([^\]\n]+)\]\(bcfile:[^)\s]+\)`?")
_CHAIN_META_VERSION = 1
_CHAIN_ZERO = bytes(32)
_CHAIN_INTERVAL = 256


def _ref_ctx_for_root(root_id: str) -> tuple[Optional[str], bool]:
    """Look up the session's (cwd, is_remote) for file-ref resolution."""
    try:
        fields = session_store.summary_fields_many([root_id], ("cwd", "node_id"))
        sess = fields.get(root_id)
        if isinstance(sess, dict):
            from file_ref_resolver import assume_exists_for_session
            is_remote = assume_exists_for_session(sess)
            cwd = sess.get("cwd")
            if isinstance(cwd, str) and cwd:
                return cwd, is_remote
            return None, is_remote
    except Exception:
        logger.debug("cwd lookup failed for root %s", root_id, exc_info=True)
    return None, False


class EventIngester:
    # INVARIANT: events.jsonl per root is SINGLE-WRITER-PROCESS. This
    # ingester's caches (`_seq`, `_seen_uuids`, `_max_seq_by_sid`) plus
    # the per-root `threading.Lock` only guard intra-process concurrency.
    # If a second backend process appends to the same file the caches
    # go stale — same constraint that already applied before caching
    # was added.
    def __init__(self) -> None:
        self._handles: OrderedDict[str, tuple[Path, Any]] = OrderedDict()
        self._seq: dict[str, int] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()
        # Background stable-storage flusher. Root ids that have flushed
        # (kernel-visible) but not yet fsync'd land in `_fsync_dirty`; a
        # daemon thread fsyncs each still-open handle after a dirty event.
        # `_fsync_thread` is started lazily and `shutdown()` joins it;
        # `close_all()` only drains handles so the singleton remains reusable.
        self._fsync_dirty: set[str] = set()
        self._fsync_dirty_epoch: dict[str, int] = {}
        self._projection_failed_epoch: dict[str, int] = {}
        self._fsync_cond = threading.Condition()
        self._fsync_thread: Optional[threading.Thread] = None
        self._fsync_stop = threading.Event()
        # Per-root UUID sets for dedup. Bounded: cleared on close().
        self._seen_uuids: dict[str, set[str]] = {}
        self._seen_event_owners: dict[str, dict[str, set[Optional[str]]]] = {}
        # Per-root UID-only set: tracks every UID that has any data row
        # on events.jsonl, irrespective of data hash. Used by the
        # `dedupe_by_uid_only=True` path (today: `_v7_to_v8_migrate`)
        # to suppress duplicate rows when the live row is already on
        # disk in a different shape (manager_event wrapper) than the
        # snapshot's normalized inner (agent_message). Kept in sync
        # with `_seen_uuids` at every write + boot scan.
        self._seen_uids_only: dict[str, set[str]] = {}
        # Per-root max-seq-by-sid cache. Populated by `_ensure_open`'s
        # boot scan AND by a fallback scan in `max_seq_by_sid` (when
        # REST hits a root before any ingest). Updated incrementally
        # by `_emit` so the REST snapshot endpoint stays O(1) instead
        # of re-scanning the whole jsonl per request.
        self._max_seq_by_sid: dict[str, dict[str, int]] = {}
        self._render_seq_by_sid: dict[str, dict[str, int]] = {}
        # Per-root seq → byte-offset index for `read_events`'s
        # after_seq fast path. `_seq_offsets[root_id][i]` = byte offset
        # of the JSONL line whose `seq == i+1` (0-indexed list, 1-indexed
        # seqs). Populated by `_ensure_open` boot scan AND by
        # `_scan_from`'s full-scan path (cold REST). Updated
        # incrementally by `_emit`. Memory bound: 8 bytes × N events
        # per root; ~160 KB for a 20K-event session.
        self._seq_offsets: dict[str, list[int]] = {}
        # Per-root write-side EOF byte offset. Tracked manually
        # (`+= len(line.encode("utf-8"))` after each `fh.write`) because
        # text-mode `fh.tell()` is opaque on writes. Initialized at
        # `_ensure_open` to the file's post-truncation size.
        self._next_offset: dict[str, int] = {}
        # Per-root summaries cache: (file_size, summaries_dict,
        # resolutions). `resolutions` maps an orphan row's journal seq to
        # the msg_id a write-time `event_ownership_resolved` fact later
        # assigned it. Append-only (facts never mutate) so it is built
        # incrementally in the same tail pass as the summaries — never a
        # full rescan. Summary byte_start/byte_end bound each message's
        # EFFECTIVE rows (its own contiguous run UNION any resolved-in
        # orphan ranges), so a single span read + effective-owner filter
        # reconstructs the message without a scan.
        self._summaries_cache: dict[
            str, tuple[int, dict[str, dict], dict[int, str]]
        ] = {}
        # Per-root full-scan cache for read_events(after_seq=0):
        # (file_size, all_entries_list). Multiple callers (hydrate, todos,
        # reconcile) share one cached scan. Byte-budgeted by the journal
        # high-water so parsed multi-GB histories do not stay resident.
        self._full_scan_cache: OrderedDict[str, tuple[int, list[dict]]] = OrderedDict()
        self._full_scan_cache_bytes: dict[str, int] = {}
        self._full_scan_cache_total_bytes = 0
        self._root_events_cache: dict[str, tuple[int, dict[str, list[dict]]]] = {}
        self._root_events_version: dict[str, int] = {}
        self._root_events_candidate_version: dict[str, int] = {}
        self._latest_render_uid_by_sid: dict[str, dict[str, tuple[int, str]]] = {}
        self._write_seed_signatures: dict[str, tuple[int, int, int, int]] = {}
        self._chain_digests: dict[str, list[dict]] = {}
        self._chain_head_digest: dict[str, str] = {}
        self._chain_generation: dict[str, int] = {}
        self._chain_meta_identity: dict[str, tuple[int, int, int, int, int]] = {}
        self._chain_checkpoint: dict[str, dict] = {}
        self._durable_chain_head: dict[str, tuple[int, str, int]] = {}

    def _root_dir(self, root_id: str) -> Path:
        return Path(session_store.session_file_path(root_id)).parent / root_id

    def _events_path(self, root_id: str) -> Path:
        return self._root_dir(root_id) / "events.jsonl"

    def _event_meta_path(self, root_id: str) -> Path:
        return self._root_dir(root_id) / "event_meta.json"

    def _event_summaries_path(self, root_id: str) -> Path:
        return self._root_dir(root_id) / "event_summaries.json"

    def _event_chain_path(self, root_id: str) -> Path:
        return self._root_dir(root_id) / "event_chain.json"

    def _hydration_ack_path(self, root_id: str) -> Path:
        return self._root_dir(root_id) / "hydration_index_ack.json"

    @staticmethod
    def _chain_next(previous: bytes, line: bytes) -> bytes:
        return hashlib.sha256(previous + line).digest()

    @staticmethod
    def _chain_identity(st: os.stat_result) -> tuple[int, int, int, int, int]:
        return (
            int(st.st_dev), int(st.st_ino), int(st.st_ctime_ns),
            int(st.st_mtime_ns), int(st.st_size),
        )

    def _drop_full_scan_cache_locked(self, root_id: str) -> None:
        cached = self._full_scan_cache.pop(root_id, None)
        if cached is None:
            self._full_scan_cache_bytes.pop(root_id, None)
            return
        bytes_used = self._full_scan_cache_bytes.pop(root_id, cached[0])
        self._full_scan_cache_total_bytes = max(
            0, self._full_scan_cache_total_bytes - bytes_used,
        )

    def _remember_full_scan_cache_locked(
        self, root_id: str, byte_end: int, entries: list[dict],
    ) -> None:
        existing = self._full_scan_cache.get(root_id)
        if existing is not None and existing[0] >= byte_end:
            # A concurrent scan for the same root (possible now that
            # large scans run with `self._locks[root_id]` released —
            # see `_scan_from`/`_extend_full_scan`/`_scan_max_seq`)
            # already installed a cache at least as complete as this
            # one. Never regress the shared cache to a smaller byte
            # high-water.
            return
        self._drop_full_scan_cache_locked(root_id)
        if byte_end > _FULL_SCAN_CACHE_MAX_BYTES:
            perf.record_count("ingest.full_scan_cache.skip_oversize", 1)
            perf.record("ingest.full_scan_cache.bytes", self._full_scan_cache_total_bytes)
            return
        self._full_scan_cache[root_id] = (byte_end, entries)
        self._full_scan_cache_bytes[root_id] = byte_end
        self._full_scan_cache_total_bytes += byte_end
        self._full_scan_cache.move_to_end(root_id)
        evicted = 0
        while self._full_scan_cache_total_bytes > _FULL_SCAN_CACHE_MAX_BYTES:
            old_root_id, _ = self._full_scan_cache.popitem(last=False)
            old_bytes = self._full_scan_cache_bytes.pop(old_root_id, 0)
            self._full_scan_cache_total_bytes = max(
                0, self._full_scan_cache_total_bytes - old_bytes,
            )
            evicted += 1
        if evicted:
            perf.record_count("ingest.full_scan_cache.evicted_roots", evicted)
        perf.record("ingest.full_scan_cache.bytes", self._full_scan_cache_total_bytes)

    @staticmethod
    def _event_file_signature(path: Path) -> Optional[tuple[int, int]]:
        try:
            st = path.stat()
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    @staticmethod
    def _event_file_identity(path: Path) -> Optional[tuple[int, int, int, int]]:
        try:
            st = path.stat()
        except OSError:
            return None
        return (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size)

    def _seed_write_caches_locked(
        self,
        root_id: str,
        entries: list[dict],
        seq_offsets: list[int],
        clean_end: int,
        identity: tuple[int, int, int, int],
    ) -> None:
        started = time.perf_counter()
        seen: set[str] = set()
        seen_owners: dict[str, set[Optional[str]]] = {}
        seen_uids: set[str] = set()
        sid_max: dict[str, int] = {}
        render_sid_max: dict[str, int] = {}
        render_projection_version = 0
        root_event_candidate_seqs: set[int] = set()
        resolved_root_event_seqs: set[int] = set()
        for entry in entries:
            data = entry.get("data") or {}
            uid = self._extract_uuid(data)
            dedup_data = self._dedup_data_for_hash(data)
            try:
                payload = json.dumps(dedup_data, sort_keys=True).encode()
                raw_hash = hashlib.sha256(payload).hexdigest()
            except (TypeError, ValueError):
                raw_hash = str(hash(str(dedup_data)))
            data_hash = f"{uid}:{raw_hash}" if uid else f":{raw_hash}"
            seen.add(data_hash)
            seen_owners.setdefault(data_hash, set()).add(entry.get("msg_id"))
            if uid:
                seen_uids.add(uid)
            sid_val = entry.get("sid")
            seq_val = entry.get("seq")
            if not isinstance(sid_val, str) or not isinstance(seq_val, int):
                continue
            sid_max[sid_val] = max(seq_val, sid_max.get(sid_val, 0))
            if self._affects_render_projection(entry):
                render_sid_max[sid_val] = max(
                    seq_val, render_sid_max.get(sid_val, 0),
                )
            if not self._affects_root_events_projection(entry):
                continue
            render_projection_version += 1
            if self._affects_root_events_candidate(entry):
                root_event_candidate_seqs.add(seq_val)
            elif entry.get("type") == "event_ownership_resolved":
                event_seq = data.get("event_seq")
                if isinstance(event_seq, int):
                    resolved_root_event_seqs.add(event_seq)
        self._seq[root_id] = len(entries)
        self._seq_offsets[root_id] = seq_offsets
        self._next_offset[root_id] = clean_end
        self._seen_uuids[root_id] = seen
        self._seen_event_owners[root_id] = seen_owners
        self._seen_uids_only[root_id] = seen_uids
        self._max_seq_by_sid[root_id] = sid_max
        self._render_seq_by_sid[root_id] = render_sid_max
        self._root_events_version[root_id] = render_projection_version
        self._root_events_candidate_version[root_id] = len(
            root_event_candidate_seqs - resolved_root_event_seqs
        )
        self._write_seed_signatures[root_id] = identity
        perf.record_count("ingest.bootstrap.seed_rows", len(entries))
        perf.record(
            "ingest.bootstrap.seed",
            (time.perf_counter() - started) * 1000.0,
        )

    def _load_event_meta_sidecar_locked(
        self, root_id: str, path: Path,
    ) -> Optional[dict[str, int]]:
        signature = self._event_file_signature(path)
        if signature is None:
            return None
        try:
            sidecar = json.loads(
                self._event_meta_path(root_id).read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError, TypeError):
            return None
        if (
            sidecar.get("mtime_ns") != signature[0]
            or sidecar.get("size") != signature[1]
        ):
            return None
        max_by_sid = {
            str(k): int(v)
            for k, v in (sidecar.get("max_seq_by_sid") or {}).items()
            if isinstance(k, str)
        }
        render_by_sid = {
            str(k): int(v)
            for k, v in (sidecar.get("render_seq_by_sid") or {}).items()
            if isinstance(k, str)
        }
        self._max_seq_by_sid[root_id] = max_by_sid
        self._render_seq_by_sid[root_id] = render_by_sid
        self._root_events_version[root_id] = int(sidecar.get("root_events_version") or 0)
        self._root_events_candidate_version[root_id] = int(
            sidecar.get("root_events_candidate_version") or 0
        )
        root_events = sidecar.get("root_events_by_sid")
        if isinstance(root_events, dict):
            self._root_events_cache[root_id] = (
                self._root_events_version[root_id],
                {
                    str(sid): events
                    for sid, events in root_events.items()
                    if isinstance(sid, str) and isinstance(events, list)
                },
            )
        self._seq[root_id] = int(sidecar.get("seq") or 0)
        self._next_offset[root_id] = signature[1]
        return dict(max_by_sid)

    def _write_event_meta_sidecar_locked(
        self,
        root_id: str,
        path: Path,
        *,
        max_by_sid: dict[str, int],
        render_by_sid: dict[str, int],
        root_events_version: int,
        root_events_candidate_version: int,
        seq: int,
        root_events_by_sid: Optional[dict[str, list[dict]]] = None,
    ) -> None:
        signature = self._event_file_signature(path)
        if signature is None:
            return
        sidecar_path = self._event_meta_path(root_id)
        tmp_path = sidecar_path.with_suffix(".json.tmp")
        payload = {
            "mtime_ns": signature[0],
            "size": signature[1],
            "seq": seq,
            "max_seq_by_sid": max_by_sid,
            "render_seq_by_sid": render_by_sid,
            "root_events_version": root_events_version,
            "root_events_candidate_version": root_events_candidate_version,
            "root_events_by_sid": root_events_by_sid or {},
        }
        try:
            tmp_path.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(tmp_path, sidecar_path)
        except OSError:
            try:
                tmp_path.unlink()
            except OSError:
                pass

    def _load_event_summaries_sidecar_locked(
        self, root_id: str, path: Path, tail: int,
    ) -> Optional[tuple[dict[str, dict], dict[int, str]]]:
        signature = self._event_file_signature(path)
        if signature is None:
            return None
        try:
            sidecar = json.loads(
                self._event_summaries_path(root_id).read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError, TypeError):
            return None
        if (
            sidecar.get("mtime_ns") != signature[0]
            or sidecar.get("size") != signature[1]
            or sidecar.get("tail") != tail
            or sidecar.get("summary_version") != _EVENT_SUMMARIES_VERSION
        ):
            return None
        summaries = sidecar.get("summaries")
        resolutions = sidecar.get("resolutions")
        seq_offsets = sidecar.get("seq_offsets")
        if (
            not isinstance(summaries, dict)
            or not isinstance(resolutions, dict)
            or not self._valid_seq_offsets(seq_offsets, signature[1])
        ):
            return None
        clean_resolutions: dict[int, str] = {}
        for seq, msg_id in resolutions.items():
            try:
                seq_int = int(seq)
            except (TypeError, ValueError):
                continue
            if isinstance(msg_id, str):
                clean_resolutions[seq_int] = msg_id
        clean_offsets = list(seq_offsets)
        self._seq_offsets[root_id] = clean_offsets
        self._seq[root_id] = len(clean_offsets)
        self._next_offset[root_id] = signature[1]
        self._summaries_cache[root_id] = (signature[1], summaries, clean_resolutions)
        return summaries, clean_resolutions

    @staticmethod
    def _valid_seq_offsets(value: Any, file_size: int) -> bool:
        if not isinstance(value, list):
            return False
        previous = -1
        for item in value:
            if not isinstance(item, int) or isinstance(item, bool):
                return False
            if item <= previous:
                return False
            if item < 0 or item >= file_size:
                return False
            previous = item
        return bool(value) or file_size == 0

    def _write_event_summaries_sidecar_locked(
        self,
        root_id: str,
        path: Path,
        *,
        tail: int,
        summaries: dict[str, dict],
        resolutions: dict[int, str],
    ) -> None:
        signature = self._event_file_signature(path)
        if signature is None:
            return
        seq_offsets = self._seq_offsets.get(root_id)
        if (
            seq_offsets is None
            or self._next_offset.get(root_id) != signature[1]
            or self._seq.get(root_id) != len(seq_offsets)
            or not self._valid_seq_offsets(seq_offsets, signature[1])
        ):
            return
        sidecar_path = self._event_summaries_path(root_id)
        tmp_path = sidecar_path.with_suffix(".json.tmp")
        payload = {
            "summary_version": _EVENT_SUMMARIES_VERSION,
            "mtime_ns": signature[0],
            "size": signature[1],
            "tail": tail,
            "summaries": summaries,
            "resolutions": {str(k): v for k, v in resolutions.items()},
            "seq_offsets": seq_offsets,
        }
        try:
            tmp_path.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(tmp_path, sidecar_path)
        except OSError:
            try:
                tmp_path.unlink()
            except OSError:
                pass

    def _open_append_handle(self, root_id: str, path: Path) -> Any:
        fh = open(path, "a", encoding="utf-8")
        with self._guard:
            self._handles[root_id] = (path, fh)
            self._handles.move_to_end(root_id)
        self._prune_append_handles(exclude_root_id=root_id)
        return fh

    def _close_handle_locked(self, root_id: str) -> None:
        with self._guard:
            pair = self._handles.pop(root_id, None)
        if not pair:
            return
        path, fh = pair
        import hydration_index_store
        journal_guard = hydration_index_store.journal_guard(root_id, path)
        journal_guard.__enter__()
        # Drain durability for this handle synchronously — once closed
        # the background flusher can no longer reach it.
        try:
            fh.flush()
            if not self._chain_handle_current_locked(root_id, path, fh):
                hydration_index_store.invalidate(root_id, path)
                raise RuntimeError("event journal changed before close durability fence")
            hydration_index_store.prepare_durable_append_receipt(
                root_id, path, int(self._next_offset.get(root_id, 0)),
                self._chain_head_digest.get(root_id, _CHAIN_ZERO.hex()),
            )
            os.fsync(fh.fileno())
            if self._chain_handle_current_locked(root_id, path, fh):
                self._persist_chain_head_locked(
                    root_id, path, fh, journal_durable=True,
                )
                hydration_index_store.flush_writer_projection(root_id, path)
            with self._fsync_cond:
                self._fsync_dirty.discard(root_id)
                self._fsync_dirty_epoch.pop(root_id, None)
        except (OSError, RuntimeError):
            logger.debug("close fsync failed for %s", root_id, exc_info=True)
        finally:
            fh.close()
            journal_guard.__exit__(None, None, None)

    def _prune_append_handles(self, *, exclude_root_id: str) -> None:
        # Skip victims whose per-root lock is currently held (a concurrent
        # write) and try the NEXT-oldest instead of abandoning the whole
        # prune — otherwise the cache grows past the cap whenever the
        # LRU-oldest root happens to be mid-write, leaking fds.
        skipped: set[str] = set()
        while True:
            with self._guard:
                if len(self._handles) <= _MAX_OPEN_APPEND_HANDLES:
                    return
                victim_id = next(
                    (rid for rid in self._handles
                     if rid != exclude_root_id and rid not in skipped),
                    None,
                )
                if victim_id is None:
                    return
            victim_lock = self._locks.get(victim_id)
            if victim_lock is None or not victim_lock.acquire(blocking=False):
                skipped.add(victim_id)
                continue
            try:
                self._close_handle_locked(victim_id)
            finally:
                victim_lock.release()

    # -- background stable-storage flusher -------------------------------
    # Why: `os.fsync()` per ingest event is the single biggest blocking
    # cost on the ingestion hot path (reqs [26]/[27]). It only buys
    # OS/power-crash durability, which is beyond the clean-restart
    # convergence invariant — `fh.flush()` already makes the line
    # kernel-visible so cross-process tailers and in-process readers
    # observe it immediately, and raises on write failure so the
    # tailer's cursor-advance rule is preserved. We batch the fsync on
    # a daemon thread instead.
    def _mark_fsync_dirty(self, root_id: str) -> None:
        """Called after a synchronous `fh.flush()`: the new bytes are
        kernel-visible, so record that the root needs a deferred fsync."""
        with self._fsync_cond:
            if self._fsync_thread is None:
                self._start_fsync_thread_locked()
            self._fsync_dirty_epoch[root_id] = (
                self._fsync_dirty_epoch.get(root_id, 0) + 1
            )
            self._fsync_dirty.add(root_id)
            self._fsync_cond.notify_all()

    def _start_fsync_thread_locked(self) -> None:
        if self._fsync_thread is None:
            self._fsync_stop.clear()
            t = threading.Thread(
                target=self._fsync_loop, name="event-ingester-fsync",
                daemon=True,
            )
            self._fsync_thread = t
            t.start()

    def _fsync_loop(self) -> None:
        while not self._fsync_stop.is_set():
            with self._fsync_cond:
                dirty = sorted(
                    root_id for root_id in self._fsync_dirty
                    if self._projection_failed_epoch.get(root_id)
                    != self._fsync_dirty_epoch.get(root_id, 0)
                )
                if not dirty:
                    self._fsync_cond.wait()
                if self._fsync_stop.is_set():
                    return
                dirty = sorted(
                    root_id for root_id in self._fsync_dirty
                    if self._projection_failed_epoch.get(root_id)
                    != self._fsync_dirty_epoch.get(root_id, 0)
                )
            # Fsync outside `_fsync_cond` so a slow disk can't block
            # dirty-marking. Re-fetch the CURRENT handle under `_guard`
            # per root: an evicted/closed root's data was fsync'd in the
            # close path, so skipping it is correct; this also never
            # touches a recycled fd.
            for root_id in dirty:
                root_lock = self._locks.get(root_id)
                if root_lock is None:
                    continue
                with root_lock:
                    with self._fsync_cond:
                        if root_id not in self._fsync_dirty:
                            continue
                        epoch = self._fsync_dirty_epoch.get(root_id, 0)
                        if self._projection_failed_epoch.get(root_id) == epoch:
                            continue
                    with self._guard:
                        current = self._handles.get(root_id)
                    if current is None:
                        continue
                    path, fh = current
                    import hydration_index_store
                    journal_guard = hydration_index_store.journal_guard(root_id, path)
                    journal_guard.__enter__()
                    try:
                        if not self._chain_handle_current_locked(root_id, path, fh):
                            import hydration_index_store
                            hydration_index_store.invalidate(root_id, path)
                            perf.record_count("ingest.chain.external_mutation_detected")
                            journal_guard.__exit__(None, None, None)
                            continue
                        fh.flush()
                        hydration_index_store.prepare_durable_append_receipt(
                            root_id, path, int(self._next_offset.get(root_id, 0)),
                            self._chain_head_digest.get(root_id, _CHAIN_ZERO.hex()),
                        )
                        os.fsync(fh.fileno())
                        self._persist_chain_head_locked(
                            root_id, path, fh, journal_durable=True,
                        )
                        hydration_index_store.flush_writer_projection(root_id, path)
                    except hydration_index_store.WriterProjectionError:
                        logger.error(
                            "background hydration projection failed for %s; retrying",
                            root_id, exc_info=True,
                        )
                        with self._fsync_cond:
                            self._projection_failed_epoch[root_id] = epoch
                        journal_guard.__exit__(None, None, None)
                        continue
                    except RuntimeError:
                        import hydration_index_store
                        hydration_index_store.invalidate(root_id, path)
                        perf.record_count("ingest.chain.external_mutation_detected")
                        journal_guard.__exit__(None, None, None)
                        continue
                    except OSError:
                        logger.error(
                            "background fsync/metadata publish failed for %s; durability at risk",
                            root_id, exc_info=True,
                        )
                        journal_guard.__exit__(None, None, None)
                        continue
                    journal_guard.__exit__(None, None, None)
                    with self._fsync_cond:
                        if self._fsync_dirty_epoch.get(root_id) == epoch:
                            self._fsync_dirty.discard(root_id)
                            self._projection_failed_epoch.pop(root_id, None)

    def _fsync_dirty_now(self) -> None:
        """Synchronous fsync of every currently-dirty root. Used by
        `close_all` so pending background durability isn't lost. Does
        NOT stop the flusher — the singleton is reused after `close_all`."""
        with self._fsync_cond:
            dirty = sorted(self._fsync_dirty)
        for root_id in dirty:
            root_lock = self._locks.get(root_id)
            if root_lock is None:
                continue
            with root_lock:
                with self._guard:
                    pair = self._handles.get(root_id)
                if pair is None:
                    continue
                path, fh = pair
                try:
                    fh.flush()
                    import hydration_index_store
                    if not self._chain_handle_current_locked(root_id, path, fh):
                        hydration_index_store.invalidate(root_id, path)
                        raise RuntimeError(
                            "event journal changed before shutdown durability fence"
                        )
                    hydration_index_store.prepare_durable_append_receipt(
                        root_id, path, int(self._next_offset.get(root_id, 0)),
                        self._chain_head_digest.get(root_id, _CHAIN_ZERO.hex()),
                    )
                    os.fsync(fh.fileno())
                    self._persist_chain_head_locked(
                        root_id, path, fh, journal_durable=True,
                    )
                    hydration_index_store.flush_writer_projection(root_id, path)
                    with self._fsync_cond:
                        self._fsync_dirty.discard(root_id)
                except (OSError, RuntimeError):
                    try:
                        import hydration_index_store
                        hydration_index_store.invalidate(root_id, path)
                    except Exception:
                        logger.exception(
                            "shutdown projection invalidation failed for %s", root_id,
                        )
                    with self._fsync_cond:
                        self._fsync_dirty.discard(root_id)
                    logger.error("shutdown fsync failed for %s; durability at risk",
                                 root_id, exc_info=True)

    def _load_chain_meta_locked(self, root_id: str, path: Path) -> dict | None:
        try:
            raw = json.loads(self._event_chain_path(root_id).read_text(encoding="utf-8"))
            st = path.stat()
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None
        identity = self._chain_identity(st)
        if (
            raw.get("version") != _CHAIN_META_VERSION
            or tuple(raw.get("identity") or ()) != identity
            or int(raw.get("size") or 0) != st.st_size
            or int(raw.get("seq") or 0) < 0
            or not isinstance(raw.get("digest"), str)
            or len(raw["digest"]) != 64
            or int(raw.get("generation") or 0) < 1
        ):
            return None
        try:
            bytes.fromhex(str(raw["digest"]))
        except ValueError:
            return None
        ladder = raw.get("ladder") or []
        if not isinstance(ladder, list):
            return None
        previous_seq = 0
        previous_size = 0
        for point in ladder:
            if not isinstance(point, dict):
                return None
            point_seq = point.get("seq")
            point_size = point.get("size")
            point_digest = point.get("digest")
            if (
                not isinstance(point_seq, int) or isinstance(point_seq, bool)
                or not isinstance(point_size, int) or isinstance(point_size, bool)
                or point_seq <= previous_seq
                or point_seq % _CHAIN_INTERVAL != 0
                or point_seq > int(raw.get("seq") or 0)
                or point_size <= previous_size
                or point_size > st.st_size
                or not isinstance(point_digest, str)
                or len(point_digest) != 64
            ):
                return None
            try:
                bytes.fromhex(point_digest)
            except ValueError:
                return None
            previous_seq = point_seq
            previous_size = point_size
        checksum = hashlib.sha256(
            json.dumps(ladder, separators=(",", ":"), sort_keys=True).encode()
        ).hexdigest()
        if raw.get("ladder_checksum") != checksum:
            return None
        self._chain_digests[root_id] = [dict(point) for point in ladder]
        self._chain_head_digest[root_id] = str(raw["digest"])
        self._chain_generation[root_id] = int(raw["generation"])
        self._chain_meta_identity[root_id] = identity
        checkpoint = raw.get("checkpoint")
        if isinstance(checkpoint, dict):
            self._chain_checkpoint[root_id] = dict(checkpoint)
        return raw

    def _chain_handle_current_locked(self, root_id: str, path: Path, fh: Any) -> bool:
        expected = self._chain_meta_identity.get(root_id)
        if expected is None:
            return False
        try:
            current = self._chain_identity(os.fstat(fh.fileno()))
            path_identity = self._chain_identity(path.stat())
        except OSError:
            return False
        return (
            current == expected
            and path_identity == expected
            and current == path_identity
            and current[4] == int(self._next_offset.get(root_id, -1))
        )

    def _write_chain_meta_locked(
        self,
        root_id: str,
        path: Path,
        *,
        seq: int,
        byte_end: int,
        digest: str,
        generation: int,
        checkpoint: dict | None = None,
    ) -> dict:
        st = path.stat()
        identity = self._chain_identity(st)
        expected = self._chain_meta_identity.get(root_id)
        if expected is not None and identity != expected:
            raise RuntimeError("event journal identity changed before chain publish")
        if byte_end != st.st_size:
            raise OSError("event chain byte fence does not match journal size")
        payload = {
            "version": _CHAIN_META_VERSION,
            "seq": int(seq),
            "size": int(byte_end),
            "digest": digest,
            "generation": int(generation),
            "identity": list(identity),
            "checkpoint": checkpoint,
            "ladder": self._chain_digests.get(root_id, []),
        }
        prior_head = self._durable_chain_head.get(root_id)
        if prior_head is not None and prior_head[0] <= byte_end:
            try:
                existing_payload = json.loads(
                    self._event_chain_path(root_id).read_text(encoding="utf-8"),
                )
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                existing_payload = {}
            try:
                acknowledged = json.loads(
                    self._hydration_ack_path(root_id).read_text(encoding="utf-8"),
                )
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                acknowledged = {}
            ack_matches_prior = (
                int(acknowledged.get("offset", -1)) == prior_head[0]
                and acknowledged.get("digest") == prior_head[1]
                and int(acknowledged.get("dev", -1)) == identity[0]
                and int(acknowledged.get("ino", -1)) == identity[1]
            )
            existing_authority = existing_payload.get("append_authority")
            if prior_head[:2] == (int(byte_end), digest):
                if isinstance(existing_authority, dict):
                    payload["append_authority"] = existing_authority
            else:
                predecessor_size = prior_head[0]
                predecessor_digest = prior_head[1]
                if (
                    not ack_matches_prior
                    and isinstance(existing_authority, dict)
                    and int(existing_authority.get("size", -1)) == prior_head[0]
                    and existing_authority.get("digest") == prior_head[1]
                ):
                    predecessor_size = int(existing_authority["predecessor_size"])
                    predecessor_digest = str(existing_authority["predecessor_digest"])
                payload["append_authority"] = {
                    "predecessor_size": predecessor_size,
                    "predecessor_digest": predecessor_digest,
                    "size": int(byte_end),
                    "digest": digest,
                    "generation": int(generation),
                }
        payload["ladder_checksum"] = hashlib.sha256(
            json.dumps(payload["ladder"], separators=(",", ":"), sort_keys=True).encode()
        ).hexdigest()
        target = self._event_chain_path(root_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=target.parent,
        )
        temp = Path(name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, target)
            dir_fd = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        finally:
            temp.unlink(missing_ok=True)
        self._chain_generation[root_id] = generation
        self._chain_meta_identity[root_id] = identity
        self._durable_chain_head[root_id] = (int(byte_end), digest, int(generation))
        if checkpoint is None:
            self._chain_checkpoint.pop(root_id, None)
        else:
            self._chain_checkpoint[root_id] = dict(checkpoint)
        return payload

    def _persist_chain_head_locked(
        self, root_id: str, path: Path, fh: Any, *, journal_durable: bool = False,
    ) -> dict:
        if not journal_durable:
            fh.flush()
            os.fsync(fh.fileno())
        generation = self._chain_generation.get(root_id, 0) + 1
        return self._write_chain_meta_locked(
            root_id,
            path,
            seq=int(self._seq.get(root_id, 0)),
            byte_end=int(self._next_offset.get(root_id, 0)),
            digest=self._chain_head_digest.get(root_id, _CHAIN_ZERO.hex()),
            generation=generation,
            checkpoint=self._chain_checkpoint.get(root_id),
        )

    def _seed_chain_locked(
        self,
        root_id: str,
        seq: int,
        digest: str,
        ladder: list[dict],
        path: Path,
        clean_end: int,
        *,
        repaired: bool,
        verified_predecessor: tuple[int, str, int] | None = None,
    ) -> None:
        self._chain_digests[root_id] = ladder
        self._chain_head_digest[root_id] = digest
        try:
            raw_prior = json.loads(self._event_chain_path(root_id).read_text(encoding="utf-8"))
            prior_generation = max(0, int(raw_prior.get("generation") or 0))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            prior_generation = 0
        prior = self._load_chain_meta_locked(root_id, path)
        if prior is not None and int(prior.get("seq") or 0) == seq:
            if prior.get("digest") == digest:
                self._chain_digests[root_id] = list(prior.get("ladder") or [])
                self._chain_generation[root_id] = int(prior["generation"])
                self._chain_meta_identity[root_id] = tuple(prior["identity"])
                self._durable_chain_head[root_id] = (
                    int(prior["size"]), str(prior["digest"]),
                    int(prior["generation"]),
                )
                checkpoint = prior.get("checkpoint")
                if isinstance(checkpoint, dict):
                    self._chain_checkpoint[root_id] = dict(checkpoint)
                perf.record_count("ingest.chain.restore.tail_only")
                return
        # A rebuild is an in-memory bootstrap, not a durability boundary.
        # Publishing the sidecar here would put two fsyncs on the first
        # ingest call. The grouped background fence publishes it after the
        # append; explicit checkpoint/close paths publish synchronously.
        self._chain_generation[root_id] = prior_generation
        self._chain_meta_identity[root_id] = self._chain_identity(path.stat())
        self._chain_checkpoint.pop(root_id, None)
        if verified_predecessor is not None:
            # The full rebuild scan independently recomputed the SHA256
            # append chain from byte 0 and, partway through, reproduced
            # the exact (size, digest) the last durable event_chain.json
            # checkpoint claims -- cryptographic proof that checkpoint is
            # a genuine, untampered prefix of the current file, not stale
            # metadata. That's strictly stronger evidence than an
            # in-process receipt, so it's safe to seed the next write's
            # `append_authority` predecessor from it directly, restoring
            # durable-authority continuity across process restarts
            # without weakening what counts as "authoritative".
            self._durable_chain_head[root_id] = verified_predecessor
            perf.record_count("ingest.chain.restore.verified_prefix")
        else:
            self._durable_chain_head.pop(root_id, None)
        perf.record_count("ingest.chain.rebuilt")
        if repaired:
            perf.record_count("ingest.chain.torn_tail_repaired")

    def _scan_chain_from_scratch_locked(
        self, root_id: str, path: Path, *, collect_entries: bool,
    ) -> dict:
        """Full linear rescan of the events journal from byte 0.

        Recomputes the SHA256 append-chain digest and sparse ladder, and
        truncates a torn trailing line left by a crash between fsync and
        the trailing newline. Also looks for a byte offset where this
        from-scratch scan reproduces the (size, digest) of the last
        durable event_chain.json checkpoint, even when that checkpoint no
        longer validates as a full match against the current file (e.g.
        more was appended after the checkpoint was written, durably on
        disk, before this process started). Finding that offset is
        cryptographic proof the checkpoint is a genuine, untampered
        prefix -- not merely stale metadata -- so it can seed a valid
        `append_authority` predecessor for the next write instead of
        losing durable-authority continuity across the gap.

        When `collect_entries` is True, also parses and returns each
        entry plus its line-start byte offset, for `_ensure_open`'s
        read-cache seed.
        """
        old_size: Optional[int] = None
        old_digest: Optional[str] = None
        old_generation: Optional[int] = None
        try:
            raw_prior = json.loads(self._event_chain_path(root_id).read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            raw_prior = None
        if (
            isinstance(raw_prior, dict)
            and isinstance(raw_prior.get("size"), int)
            and isinstance(raw_prior.get("digest"), str)
            and isinstance(raw_prior.get("generation"), int)
        ):
            old_size = raw_prior["size"]
            old_digest = raw_prior["digest"]
            old_generation = raw_prior["generation"]

        digest = _CHAIN_ZERO
        ladder: list[dict] = []
        chain_pending = bytearray()
        seq = 0
        torn_offset: Optional[int] = None
        verified_predecessor: tuple[int, str, int] | None = None
        entries: Optional[list[dict]] = [] if collect_entries else None
        seq_offsets: Optional[list[int]] = [] if collect_entries else None
        with open(path, "rb") as source:
            scan_before = self._chain_identity(os.fstat(source.fileno()))
            while True:
                line_start = source.tell()
                raw = source.readline()
                if not raw:
                    break
                chain_pending.extend(raw)
                text = raw.decode("utf-8", errors="replace").rstrip("\n")
                if not text.strip():
                    torn_offset = None
                    continue
                try:
                    entry = json.loads(text)
                except json.JSONDecodeError:
                    if torn_offset is None:
                        torn_offset = line_start
                    continue
                torn_offset = None
                if collect_entries:
                    entries.append(entry)
                    seq_offsets.append(line_start)
                seq += 1
                digest = self._chain_next(digest, bytes(chain_pending))
                chain_pending.clear()
                current_end = source.tell()
                if (
                    old_size is not None
                    and current_end == old_size
                    and digest.hex() == old_digest
                ):
                    verified_predecessor = (old_size, old_digest, old_generation)
                if seq % _CHAIN_INTERVAL == 0:
                    ladder.append({
                        "seq": seq,
                        "size": current_end,
                        "digest": digest.hex(),
                    })
            scan_after = self._chain_identity(os.fstat(source.fileno()))
        if scan_before != scan_after or self._chain_identity(path.stat()) != scan_after:
            raise OSError("events journal changed during chain rebuild")
        if torn_offset is not None:
            logger.warning(
                "event_ingester: truncating torn trailing line at "
                "offset %d in %s", torn_offset, path,
            )
            with open(path, "r+b") as target:
                target.truncate(torn_offset)
                target.flush()
                os.fsync(target.fileno())
            dir_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        clean_end = path.stat().st_size
        return {
            "seq": seq,
            "digest": digest.hex(),
            "ladder": ladder,
            "clean_end": clean_end,
            "torn": torn_offset is not None,
            "verified_predecessor": verified_predecessor,
            "entries": entries,
            "seq_offsets": seq_offsets,
        }

    def _rebuild_chain_only_locked(self, root_id: str, path: Path) -> None:
        """Rebuild only the sparse integrity projection in bounded memory.

        See `_scan_chain_from_scratch_locked` for the scan/verified-
        predecessor details.
        """
        scan = self._scan_chain_from_scratch_locked(root_id, path, collect_entries=False)
        self._seq[root_id] = scan["seq"]
        self._next_offset[root_id] = scan["clean_end"]
        self._seed_chain_locked(
            root_id, scan["seq"], scan["digest"], scan["ladder"], path, scan["clean_end"],
            repaired=scan["torn"],
            verified_predecessor=scan["verified_predecessor"],
        )
        perf.record_count("ingest.chain.streaming_rebuild_rows", scan["seq"])
        perf.record("ingest.chain.streaming_rebuild_bytes", scan["clean_end"])

    def _ensure_chain_head_locked(self, root_id: str, path: Path) -> tuple[Path, Any, dict]:
        meta = self._load_chain_meta_locked(root_id, path) if path.exists() else None
        with self._guard:
            pair = self._handles.get(root_id)
        if meta is None:
            if pair is not None:
                self._close_handle_locked(root_id)
                pair = None
            self._rebuild_chain_only_locked(root_id, path)
        if pair is None:
            fh = self._open_append_handle(root_id, path)
        else:
            fh = pair[1]
        meta = self._load_chain_meta_locked(root_id, path)
        if meta is not None and root_id not in self._durable_chain_head:
            # The on-disk chain meta already matches the file exactly (no
            # rebuild was needed) -- it's a valid predecessor for the next
            # write's `append_authority`. Without this, the first write in
            # a fresh process to touch an already-valid root would find
            # `_durable_chain_head` empty and silently omit
            # `append_authority` from its own chain-meta write, breaking
            # hydration_index_store's durable-authority chain even though
            # nothing was ever actually stale.
            self._durable_chain_head[root_id] = (
                int(meta["size"]), str(meta["digest"]), int(meta["generation"]),
            )
        if meta is None:
            with self._fsync_cond:
                self._fsync_dirty.discard(root_id)
            meta = self._persist_chain_head_locked(root_id, path, fh)
        return path, fh, meta

    def _ensure_open(self, root_id: str) -> tuple[Path, Any]:
        with self._guard:
            cached = self._handles.get(root_id)
            if cached is not None:
                if self._chain_handle_current_locked(root_id, cached[0], cached[1]):
                    self._handles.move_to_end(root_id)
                    return cached
        if cached is not None:
            perf.record_count("ingest.chain.external_mutation_detected")
            try:
                import hydration_index_store
                hydration_index_store.invalidate(root_id, cached[0])
            except Exception:
                logger.exception("failed to invalidate hydration projection for %s", root_id)
            self._close_handle_locked(root_id)
        root_dir = self._root_dir(root_id)
        root_dir.mkdir(parents=True, exist_ok=True)
        path = self._events_path(root_id)
        if not path.exists():
            path.touch(mode=0o600)
        # Gate on the dedup set being seeded, NOT just `_seq`: `cursor()`
        # caches `_seq[root_id]` from a cheap line-count scan WITHOUT
        # seeding `_seen_event_owners`/`_seen_uuids`. If we early-returned
        # on `_seq` alone, the first ingest after a `cursor()` call (every
        # subscribed session — `add_subscriber` runs `cursor()`) would
        # skip the disk seed, leaving the dedup sets empty. The dual
        # writers (SDK callback `apply_event` + jsonl tailer
        # `ingest_orphan`) would then both write the same event with no
        # dedup → duplicate rows → duplicate rendered content.
        identity = self._event_file_identity(path)
        if (
            root_id in self._seq
            and root_id in self._seen_event_owners
            and identity is not None
            and self._write_seed_signatures.get(root_id) == identity
        ):
            perf.record_count("ingest.bootstrap.reused_read_scan", 1)
            return path, self._open_append_handle(root_id, path)
        # Same scan also seeds the seq → byte-offset index so read_events
        # can fast-path-skip the after_seq prefix. See
        # `_scan_chain_from_scratch_locked` for torn-tail recovery and
        # verified-predecessor details.
        if path.exists():
            scan = self._scan_chain_from_scratch_locked(root_id, path, collect_entries=True)
            entries = scan["entries"]
            seq_offsets = scan["seq_offsets"]
            clean_end = scan["clean_end"]
            chain_seq = scan["seq"]
            chain_digest_hex = scan["digest"]
            chain_ladder = scan["ladder"]
            torn = scan["torn"]
            verified_predecessor = scan["verified_predecessor"]
        else:
            entries = []
            seq_offsets = []
            clean_end = 0
            chain_seq = 0
            chain_digest_hex = _CHAIN_ZERO.hex()
            chain_ladder = []
            torn = False
            verified_predecessor = None
        identity = self._event_file_identity(path)
        if identity is None:
            identity = (0, 0, 0, clean_end)
        self._seed_write_caches_locked(
            root_id, entries, seq_offsets, clean_end, identity,
        )
        self._seed_chain_locked(
            root_id, chain_seq, chain_digest_hex, chain_ladder,
            path, clean_end, repaired=torn,
            verified_predecessor=verified_predecessor,
        )
        self._locks.setdefault(root_id, threading.Lock())
        fh = self._open_append_handle(root_id, path)
        return path, fh

    def _is_duplicate_event_owner(
        self,
        root_id: str,
        data_hash: str,
        msg_id: Optional[str],
    ) -> bool:
        owners = self._seen_event_owners.setdefault(root_id, {})
        seen_msg_ids = owners.setdefault(data_hash, set())
        if msg_id in seen_msg_ids:
            return True
        if msg_id is None and seen_msg_ids:
            return True
        seen_msg_ids.add(msg_id)
        return False

    @staticmethod
    def _canonical_data_for_storage(
        event_type: str,
        data: dict,
        cwd: Optional[str],
        assume_exists: bool,
    ) -> dict:
        # Narrow copy-on-write isolation (rewrites only a few leaf text
        # fields) instead of a full deepcopy of the whole payload — see
        # `rewrite_event_data_isolated`. Isolation always on, rewrite best
        # effort (matches the prior deepcopy-then-rewrite semantics).
        try:
            return rewrite_event_data_isolated(
                event_type, data, cwd, assume_exists=assume_exists,
            )
        except Exception:
            logger.debug("file_ref_resolver rewrite failed", exc_info=True)
            return copy.deepcopy(data)

    @classmethod
    def _dedup_data_for_hash(cls, data: dict) -> dict:
        def neutralize(value: Any) -> Any:
            if isinstance(value, str):
                return _BCFILE_LINK_RE.sub(r"\1", value)
            if isinstance(value, list):
                return [neutralize(item) for item in value]
            if isinstance(value, dict):
                return {key: neutralize(item) for key, item in value.items()}
            return value

        return neutralize(data)

    @staticmethod
    def _extract_uuid(data: dict) -> Optional[str]:
        if not isinstance(data, dict):
            return None
        uid = data.get(_UUID_KEY)
        if uid:
            return uid
        # Raw enriched claude line: {"data": {"uuid": ...}}
        inner = data.get("data")
        if isinstance(inner, dict):
            uid = inner.get(_UUID_KEY)
            if uid:
                return uid
        # Orchestrator WS wrapper: {"event": {"data": {"uuid": ...}}}
        event = data.get("event")
        if isinstance(event, dict):
            event_data = event.get("data")
            if isinstance(event_data, dict):
                uid = event_data.get(_UUID_KEY)
                if uid:
                    return uid
        return None

    def _emit(
        self,
        fh,
        root_id: str,
        seq: int,
        sid: str,
        event_type: str,
        data: dict,
        source: str,
        run_id: Optional[str],
        msg_id: Optional[str],
    ) -> dict:
        """Build the entry from canonical event data and write one JSONL line.

        INVARIANT: the single construction path for both `ingest` and
        `ingest_batch` — they must never diverge in entry shape. Does
        NOT flush (flush cadence differs per caller) and does NOT
        compute/assign seq.

        INVARIANT: file-ref canonicalization happens before dedup
        hashing in `ingest` / `ingest_batch`; `_emit` persists exactly
        that canonical payload.
        """
        entry = {
            "seq": seq,
            "ts": datetime.now(timezone.utc).isoformat(),
            "sid": sid,
            "type": event_type,
            "data": data,
            "source": source,
        }
        if run_id is not None:
            entry["run_id"] = run_id
        if msg_id is not None:
            entry["msg_id"] = msg_id
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        line_bytes = line.encode("utf-8")
        # INVARIANT: ingest/ingest_batch hold `_locks[root_id]` across
        # this method, so all 3 cache updates below are serialized
        # against the fallback scans in `max_seq_by_sid` and
        # `_scan_from`. All updates reflect the line that was just
        # written — pre-flush is fine because the file-handle is still
        # owned by this process.
        offset_for_this_line = self._next_offset.get(root_id, 0)
        append_before = os.fstat(fh.fileno())
        fh.write(line)
        ladder = self._chain_digests.setdefault(root_id, [])
        previous = bytes.fromhex(self._chain_head_digest.get(root_id, _CHAIN_ZERO.hex()))
        head_digest = self._chain_next(previous, line_bytes).hex()
        self._chain_head_digest[root_id] = head_digest
        # `_next_offset` and `_seq_offsets` MUST update together; if
        # either is dropped a future read_events would seek to the wrong
        # offset OR future _emit would record a stale offset.
        self._next_offset[root_id] = (
            offset_for_this_line + len(line_bytes)
        )
        if seq % _CHAIN_INTERVAL == 0:
            ladder.append({"seq": seq, "size": self._next_offset[root_id], "digest": head_digest})
        try:
            import hydration_index_store
            hydration_index_store.note_authoritative_append(
                root_id, Path(fh.name), offset_for_this_line, self._next_offset[root_id],
                previous.hex(), head_digest, sid,
            )
        except Exception:
            logger.debug("hydration append receipt update failed", exc_info=True)
        self._seq_offsets.setdefault(root_id, []).append(offset_for_this_line)
        # `_max_seq_by_sid` update preserved from P1.
        cache = self._max_seq_by_sid.setdefault(root_id, {})
        if seq > cache.get(sid, 0):
            cache[sid] = seq
        if self._affects_render_projection(entry):
            render_cache = self._render_seq_by_sid.setdefault(root_id, {})
            if seq > render_cache.get(sid, 0):
                render_cache[sid] = seq
            uid = self._extract_uuid(entry.get("data") or {})
            if uid:
                latest_cache = self._latest_render_uid_by_sid.setdefault(root_id, {})
                latest = latest_cache.get(sid)
                if latest is None or seq >= latest[0]:
                    latest_cache[sid] = (seq, uid)
        if self._affects_root_events_projection(entry):
            self._root_events_version[root_id] = (
                self._root_events_version.get(root_id, 0) + 1
            )
            self._update_root_events_cache_for_entry(root_id, entry)
            if self._affects_root_events_candidate(entry):
                self._root_events_candidate_version[root_id] = (
                    self._root_events_candidate_version.get(root_id, 0) + 1
                )
        return entry

    @staticmethod
    def _enqueue_search_projection(root_id: str, entry: dict) -> None:
        try:
            import session_search_projection
            session_search_projection.note_event_written(root_id, entry)
        except Exception:
            logger.debug("session search projection enqueue failed", exc_info=True)

    def ingest(
        self,
        root_id: str,
        sid: str,
        event_type: str,
        data: dict,
        *,
        source: str,
        run_id: Optional[str] = None,
        msg_id: Optional[str] = None,
        cwd_override: Optional[str] = None,
        dedupe_by_uid_only: bool = False,
    ) -> int:
        with perf.timed("ingest.live"):
            return self._ingest_impl(
                root_id, sid, event_type, data,
                source=source, run_id=run_id, msg_id=msg_id,
                cwd_override=cwd_override,
                dedupe_by_uid_only=dedupe_by_uid_only,
            )

    def _ingest_impl(
        self,
        root_id: str,
        sid: str,
        event_type: str,
        data: dict,
        *,
        source: str,
        run_id: Optional[str] = None,
        msg_id: Optional[str] = None,
        cwd_override: Optional[str] = None,
        dedupe_by_uid_only: bool = False,
    ) -> int:
        # Resolve cwd BEFORE taking the per-root ingester Lock so that
        # `session_manager.get` (which acquires the session_manager
        # per-root RLock inside `_ref_ctx_for_root`) never runs under the
        # ingester Lock. See `_emit`'s docstring for the cycle this
        # avoids.
        #
        # `cwd_override` short-circuits the session_manager lookup —
        # required by callers that run BEFORE the session is in the
        # session_manager cache (e.g. `_v7_to_v8_migrate`, which runs
        # from `session_store._migrate_session` before
        # `session_manager._load_root` populates `_roots[rid]`).
        # Without the override the `session_manager.get` lookup would
        # recursively re-enter `_load_root` for the same root_id and
        # blow the stack. An empty string override is treated as "skip
        # file-ref rewrite" (same as a missing session cwd).
        if cwd_override is not None:
            # Override path can't consult session_manager (re-entrancy)
            # so remote-ness is unknowable here — keep the local check.
            cwd, assume_exists = cwd_override or None, False
        else:
            cwd, assume_exists = _ref_ctx_for_root(root_id)
        lock = self._locks.setdefault(root_id, threading.Lock())
        lock_wait_started = time.perf_counter()
        lock.acquire()
        lock_acquired_at = time.perf_counter()
        import hydration_index_store
        journal_guard = hydration_index_store.journal_guard(
            root_id, self._events_path(root_id),
        )
        journal_guard.__enter__()
        try:
            # _ensure_open MUST run before we touch `_seen_uuids` — it
            # seeds the set from disk on first call and OVERWRITES the
            # in-memory entry, so any uid we added beforehand would be
            # silently wiped (leading to a duplicate on restart).
            path, fh = self._ensure_open(root_id)
            # Dedup: orchestrator and session_watcher can both emit the
            # same claude event. Skip if we've already seen it ONLY if
            # the data is identical. If data changed, it's an
            # update/delta (e.g. Gemini streaming).
            #
            # Primary key: UUID from the event data. Fallback when no
            # UUID (e.g. pr-link, future metadata events): hash the
            # entire data payload — identical events from dual writers
            # (SDK callback + jsonl tailer) collapse to one row.
            canonical_data = self._canonical_data_for_storage(
                event_type, data, cwd, assume_exists,
            )
            uid = self._extract_uuid(canonical_data)
            seen = self._seen_uuids.setdefault(root_id, set())
            uids_only = self._seen_uids_only.setdefault(root_id, set())
            # `dedupe_by_uid_only=True` path: skip when the uid is
            # already on any row regardless of data shape. Used by
            # `_v7_to_v8_migrate` to avoid re-ingesting events the
            # live `apply_event` already wrote — the snapshot's
            # normalized inner agent_message shape differs from the
            # live outer manager_event wrapper, so the default
            # `uid:sha256(data)` dedup misses and produces duplicate
            # rows (measured: 4346 dup rows on session 4ddbd4d7
            # before this gate).
            if dedupe_by_uid_only and uid and uid in uids_only:
                return -1
            dedup_data = self._dedup_data_for_hash(canonical_data)
            try:
                payload = json.dumps(dedup_data, sort_keys=True).encode()
                raw_hash = hashlib.sha256(payload).hexdigest()
            except (TypeError, ValueError):
                raw_hash = str(hash(str(dedup_data)))
            data_hash = f"{uid}:{raw_hash}" if uid else f":{raw_hash}"

            if self._is_duplicate_event_owner(root_id, data_hash, msg_id):
                return -1
            seen.add(data_hash)
            if uid:
                uids_only.add(uid)

            seq = self._seq[root_id] + 1
            self._seq[root_id] = seq
            search_entry = self._emit(
                fh, root_id, seq, sid, event_type, canonical_data, source,
                run_id, msg_id,
            )
            # Kernel fence: `flush()` makes the line visible in the
            # kernel page cache so cross-process tailers / in-process
            # readers observe it immediately, and raises on write
            # failure so the tailer's "don't advance cursor on dispatch
            # failure" rule kicks in. Stable-storage `fsync` (OS/power-
            # crash durability, beyond the convergence invariant) is
            # batched on the background flusher — see `_mark_fsync_dirty`.
            fh.flush()
            self._chain_meta_identity[root_id] = self._chain_identity(
                os.fstat(fh.fileno()),
            )
            self._mark_fsync_dirty(root_id)
        finally:
            lock_released_at = time.perf_counter()
            journal_guard.__exit__(None, None, None)
            lock.release()
            perf.record(
                "ingest.live.root_lock_wait",
                (lock_acquired_at - lock_wait_started) * 1000.0,
            )
            perf.record(
                "ingest.live.root_lock_held",
                (lock_released_at - lock_acquired_at) * 1000.0,
            )
        self._enqueue_search_projection(root_id, search_entry)
        if seq > 0:
            byte_range = self._seq_byte_range(root_id, seq)
            if byte_range is not None:
                try:
                    import historical_children_projection
                    historical_children_projection.note_event(
                        root_id, search_entry, byte_range[0], byte_range[1],
                    )
                except Exception:
                    logger.exception("historical projection append failed")

        # Orphan-event signal: a `msg_id=None` line for a sid whose
        # latest assistant msg is already finalized arrives AFTER the
        # orchestrator stopped stamping events to it (see
        # `OwnedClaudeJsonlTailer._on_line` jsonl_tailer.py:697). Without
        # this signal, reconcile (which seq-brackets the orphan to the
        # right preceding msg) would never run on read paths — they
        # consult `consume_reconcile_dirty` and only spawn the async
        # reconcile when dirty.
        #
        # INVARIANT — lock order: this call MUST happen OUTSIDE the
        # per-root ingester `lock`. `apply_event` (orchs/base.py) enters
        # this method while holding the session_manager per-root RLock
        # (acquired by `with session_manager.batch(...)` in orchestrator
        # / jsonl_tailer). If a concurrent ingest on a different thread
        # held the ingester Lock here and then tried to acquire the
        # session_manager RLock via `latest_assistant_finalized`, the
        # two threads would form a cycle (A: RLock→Lock, B: Lock→RLock)
        # and the asyncio loop would wedge. Releasing the ingester Lock
        # before any session_manager call breaks the cycle — fsync
        # already provides the happens-before for any reader; the dirty
        # flag is idempotent and the consume/re-arm protocol in
        # `session_manager.consume_reconcile_dirty` tolerates the late
        # set (a flag arming after a clear shows up on the NEXT consume,
        # which the next read path will trigger).
        if msg_id is None and self._affects_render_projection({
            "type": event_type,
            "data": data,
        }):
            try:
                if session_manager.latest_assistant_finalized(sid):
                    session_manager.mark_reconcile_dirty(root_id)
            except Exception:
                logger.debug(
                    "orphan-event dirty-mark failed for sid=%s",
                    sid, exc_info=True,
                )
        return seq

    def ingest_batch(
        self,
        root_id: str,
        events: list[tuple[str, str, dict, str, Optional[str], Optional[str]]],
    ) -> list[int]:
        """Each tuple: (sid, event_type, data, source, run_id, msg_id)."""
        if not events:
            return []
        with perf.timed("ingest.batch"):
            return self._ingest_batch_impl(root_id, events)

    def _ingest_batch_impl(
        self,
        root_id: str,
        events: list[tuple[str, str, dict, str, Optional[str], Optional[str]]],
    ) -> list[int]:
        # Resolve cwd BEFORE the ingester Lock — same reason as in
        # `_ingest_impl`: avoid an ingester-Lock→session_manager-RLock
        # path under the per-root Lock. cwd is per-root, not per-event,
        # so one lookup serves the whole batch.
        cwd, assume_exists = _ref_ctx_for_root(root_id)
        lock = self._locks.setdefault(root_id, threading.Lock())
        lock_wait_started = time.perf_counter()
        lock.acquire()
        lock_acquired_at = time.perf_counter()
        search_entries: list[dict] = []
        import hydration_index_store
        journal_guard = hydration_index_store.journal_guard(
            root_id, self._events_path(root_id),
        )
        journal_guard.__enter__()
        try:
            path, fh = self._ensure_open(root_id)
            seqs: list[int] = []
            uids_only = self._seen_uids_only.setdefault(root_id, set())
            for sid, event_type, data, source, run_id, msg_id in events:
                canonical_data = self._canonical_data_for_storage(
                    event_type, data, cwd, assume_exists,
                )
                uid = self._extract_uuid(canonical_data)
                seen = self._seen_uuids.setdefault(root_id, set())
                dedup_data = self._dedup_data_for_hash(canonical_data)
                try:
                    payload = json.dumps(dedup_data, sort_keys=True).encode()
                    raw_hash = hashlib.sha256(payload).hexdigest()
                except (TypeError, ValueError):
                    raw_hash = str(hash(str(dedup_data)))
                data_hash = f"{uid}:{raw_hash}" if uid else f":{raw_hash}"

                if self._is_duplicate_event_owner(root_id, data_hash, msg_id):
                    seqs.append(-1)
                    continue
                seen.add(data_hash)
                # Keep `_seen_uids_only` in sync so future
                # `dedupe_by_uid_only=True` callers see batch-written
                # rows. Single-ingest does this at the same site.
                if uid:
                    uids_only.add(uid)

                seq = self._seq[root_id] + 1
                self._seq[root_id] = seq
                seqs.append(seq)
                search_entries.append(self._emit(
                    fh, root_id, seq, sid, event_type, canonical_data, source,
                    run_id, msg_id,
                ))
            if search_entries:
                fh.flush()
                self._chain_meta_identity[root_id] = self._chain_identity(
                    os.fstat(fh.fileno()),
                )
                self._mark_fsync_dirty(root_id)
        finally:
            lock_released_at = time.perf_counter()
            journal_guard.__exit__(None, None, None)
            lock.release()
            perf.record(
                "ingest.batch.root_lock_wait",
                (lock_acquired_at - lock_wait_started) * 1000.0,
            )
            perf.record(
                "ingest.batch.root_lock_held",
                (lock_released_at - lock_acquired_at) * 1000.0,
            )
        for entry in search_entries:
            self._enqueue_search_projection(root_id, entry)
            seq = entry.get("seq")
            byte_range = self._seq_byte_range(root_id, seq) if isinstance(seq, int) else None
            if byte_range is not None:
                try:
                    import historical_children_projection
                    historical_children_projection.note_event(
                        root_id, entry, byte_range[0], byte_range[1],
                    )
                except Exception:
                    logger.exception("historical projection batch append failed")
        return seqs

    def cursor(self, root_id: str) -> int:
        if root_id in self._seq:
            return self._seq[root_id]
        path = self._events_path(root_id)
        if not path.exists():
            return 0
        count = 0
        with open(path, encoding="utf-8") as f:
            for _ in f:
                count += 1
        self._seq[root_id] = count
        return count

    def max_seq_by_sid(self, root_id: str) -> dict[str, int]:
        """Per-sid raw head: highest seq observed in events.jsonl for
        each sid present, counting EVERY event (incl. metadata and
        not-yet-resolved orphans).

        NOT the frontend watermark — use `render_seq_by_sid` for that.
        The raw head can exceed what was materialized into a rendered
        message, so handing it to the WS resume cursor skips the
        renderable tail. `render_seq_by_sid` is the render-projection
        head and stays consistent with the rendered snapshot.

        O(1) after the first call: the cache is populated either by
        `_ensure_open` (during ingest setup) or by the fallback scan
        below (cold REST hit before any ingest). `_emit` keeps it fresh.
        INVARIANT: cache lookup AND fallback scan + populate both run
        under `_locks[root_id]` so concurrent ingest can't slip writes
        in between the scan and the assignment.
        """
        lock = self._locks.setdefault(root_id, threading.Lock())
        with lock:
            cached = self._max_seq_by_sid.get(root_id)
            if cached is not None:
                return dict(cached)
            return self._scan_max_seq(root_id)

    def render_seq_by_sid(self, root_id: str) -> dict[str, int]:
        """Per-sid render-projection watermark: highest seq among events
        that affect the rendered transcript (`_affects_render_projection`)
        for each sid present.

        This is the watermark the REST snapshot hands the frontend: it
        equals the highest seq materialized into the rendered snapshot
        (the same projection drives snapshot cache invalidation via
        `render_seq_for_sid`), so the WS resume cursor neither skips the
        renderable tail nor redelivers events already on screen.

        Shares the cache/lock discipline of `max_seq_by_sid`.
        """
        lock = self._locks.setdefault(root_id, threading.Lock())
        with lock:
            cached = self._render_seq_by_sid.get(root_id)
            if cached is not None:
                return dict(cached)
            self._scan_max_seq(root_id)
            cached = self._render_seq_by_sid.get(root_id)
            return dict(cached) if cached else {}

    def session_event_meta(self, root_id: str) -> tuple[bool, int, dict[str, int]]:
        """Return has-events, root cursor, and render watermarks in one pass."""
        lock = self._locks.setdefault(root_id, threading.Lock())
        with lock:
            max_by_sid = self._max_seq_by_sid.get(root_id)
            render_by_sid = self._render_seq_by_sid.get(root_id)
            if max_by_sid is None or render_by_sid is None:
                max_by_sid = self._scan_max_seq(root_id)
                render_by_sid = self._render_seq_by_sid.get(root_id) or {}
            cursor = self._seq.get(root_id)
            if cursor is None:
                path = self._events_path(root_id)
                if not path.exists():
                    cursor = 0
                else:
                    with open(path, encoding="utf-8") as f:
                        cursor = sum(1 for _ in f)
                self._seq[root_id] = cursor
            return bool(max_by_sid), int(cursor), dict(render_by_sid)

    def max_seq_for_sid(self, root_id: str, sid: str) -> int:
        """Scalar variant: max seq for a single sid. Returns 0 if unknown.

        Avoids dict copy on the hot path (snapshot cache key check).
        """
        lock = self._locks.setdefault(root_id, threading.Lock())
        with lock:
            cached = self._max_seq_by_sid.get(root_id)
            if cached is not None:
                return cached.get(sid, 0)
            self._scan_max_seq(root_id)
            cached = self._max_seq_by_sid.get(root_id)
            return cached.get(sid, 0) if cached else 0

    def render_seq_for_sid(self, root_id: str, sid: str) -> int:
        lock = self._locks.setdefault(root_id, threading.Lock())
        with lock:
            cached = self._render_seq_by_sid.get(root_id)
            if cached is not None:
                return cached.get(sid, 0)
            self._scan_max_seq(root_id)
            cached = self._render_seq_by_sid.get(root_id)
            return cached.get(sid, 0) if cached else 0

    @staticmethod
    def _parse_events_range(
        path: Path,
        start_offset: int,
        end_offset: Optional[int],
        entries: list[dict],
        seq_offsets: list[int],
        line_ends: Optional[list[int]] = None,
    ) -> int:
        """Parse JSONL lines from `start_offset` up to `end_offset` (EOF
        when None), appending parsed entries + their start byte offsets
        (and, if `line_ends` is given, their end byte offsets) in place.
        Returns the new clean byte high-water. Pure parsing: touches no
        ingester state, requires no lock -- callers that need to install
        the result into shared caches take `self._locks[root_id]`
        themselves around that step, not around this parse."""
        cur_offset = start_offset
        with open(path, "rb") as f:
            f.seek(start_offset)
            while end_offset is None or cur_offset < end_offset:
                line_start = cur_offset
                raw = f.readline()
                if not raw:
                    break
                cur_offset += len(raw)
                line = raw.decode("utf-8", errors="replace").rstrip("\n")
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                seq_offsets.append(line_start)
                entries.append(entry)
                if line_ends is not None:
                    line_ends.append(cur_offset)
        return cur_offset

    def _scan_max_seq(self, root_id: str) -> dict[str, int]:
        """Full scan fallback for max_seq_by_sid. Caller holds the lock.

        The expensive read+parse of events.jsonl runs with the lock
        RELEASED: events.jsonl is single-writer-process append-only (see
        class docstring), so any byte range already on disk when we
        snapshot the file size is immutable -- only new bytes can land
        while we're unlocked. We reacquire before touching any shared
        cache/sidecar, catch up on anything appended while parsing
        (cheap: only the new delta, never the whole file again), and
        only then install results -- so from the outside this still
        behaves like an atomic "caller holds the lock" operation; it
        just no longer blocks concurrent `_ingest_impl` appends on the
        same root for the scan's full duration.
        """
        path = self._events_path(root_id)
        if not path.exists():
            self._max_seq_by_sid[root_id] = {}
            self._render_seq_by_sid[root_id] = {}
            self._root_events_version[root_id] = 0
            self._root_events_candidate_version[root_id] = 0
            return {}
        sidecar = self._load_event_meta_sidecar_locked(root_id, path)
        if sidecar is not None:
            return sidecar

        lock = self._locks[root_id]
        snapshot_size = path.stat().st_size
        entries: list[dict] = []
        seq_offsets: list[int] = []
        line_ends: list[int] = []
        lock.release()
        try:
            cur_offset = self._parse_events_range(
                path, 0, snapshot_size, entries, seq_offsets, line_ends,
            )
        finally:
            lock.acquire()

        already = self._max_seq_by_sid.get(root_id)
        if already is not None:
            # Another thread (a concurrent cold scan, or an
            # `_ensure_open`/`_emit` bootstrap) already installed a
            # result while we were parsing unlocked. Its snapshot is
            # >= ours (the file only grows) -- trust it rather than
            # risk clobbering fresher state with ours.
            return dict(already)

        # Catch up on anything appended while we were unlocked. Cheap:
        # a `stat()` first avoids even opening the file in the (common)
        # case nothing landed; otherwise only the new delta is read,
        # never the whole file again.
        if path.stat().st_size > cur_offset:
            cur_offset = self._parse_events_range(
                path, cur_offset, None, entries, seq_offsets, line_ends,
            )

        out: dict[str, int] = {}
        render_out: dict[str, int] = {}
        render_projection_version = 0
        root_event_candidate_seqs: set[int] = set()
        resolved_root_event_seqs: set[int] = set()
        summaries: dict[str, dict] = {}
        resolutions: dict[int, str] = {}
        parsed_lines = len(entries)
        for idx, entry in enumerate(entries):
            self._update_summary_line(
                summaries, resolutions, root_id,
                entry, seq_offsets[idx], line_ends[idx], 25,
            )
            sid = entry.get("sid")
            seq = entry.get("seq")
            if not isinstance(sid, str) or not isinstance(seq, int):
                continue
            if seq > out.get(sid, 0):
                out[sid] = seq
            if self._affects_render_projection(entry):
                if seq > render_out.get(sid, 0):
                    render_out[sid] = seq
            if self._affects_root_events_projection(entry):
                render_projection_version += 1
                if self._affects_root_events_candidate(entry):
                    root_event_candidate_seqs.add(seq)
                elif entry.get("type") == "event_ownership_resolved":
                    data = entry.get("data") or {}
                    event_seq = data.get("event_seq")
                    if isinstance(event_seq, int):
                        resolved_root_event_seqs.add(event_seq)
        self._max_seq_by_sid[root_id] = out
        self._render_seq_by_sid[root_id] = render_out
        self._root_events_version[root_id] = render_projection_version
        root_events_candidate_version = len(
            root_event_candidate_seqs - resolved_root_event_seqs
        )
        self._root_events_candidate_version[root_id] = root_events_candidate_version
        self._seq[root_id] = parsed_lines
        self._seq_offsets[root_id] = seq_offsets
        self._next_offset[root_id] = cur_offset
        self._remember_full_scan_cache_locked(root_id, cur_offset, entries)
        self._fold_resolutions(root_id, summaries, resolutions)
        self._summaries_cache[root_id] = (cur_offset, summaries, resolutions)
        self._write_event_summaries_sidecar_locked(
            root_id,
            path,
            tail=25,
            summaries=summaries,
            resolutions=resolutions,
        )
        root_events_by_sid = (
            self._build_root_events_projection(entries)
            if root_events_candidate_version > 0 else {}
        )
        self._root_events_cache[root_id] = (
            render_projection_version,
            root_events_by_sid,
        )
        self._write_event_meta_sidecar_locked(
            root_id,
            path,
            max_by_sid=out,
            render_by_sid=render_out,
            root_events_version=render_projection_version,
            root_events_candidate_version=root_events_candidate_version,
            seq=parsed_lines,
            root_events_by_sid=root_events_by_sid,
        )
        return dict(out)

    @staticmethod
    def _affects_render_projection(entry: dict) -> bool:
        event_type = entry.get("type")
        if event_type not in {
            "agent_message",
            "manager_event",
            "steer_prompt",
            "worker_event",
            "event_ownership_resolved",
        }:
            return False
        from event_shape import is_metadata_event
        return not is_metadata_event(entry)

    @staticmethod
    def _affects_root_events_projection(entry: dict) -> bool:
        event_type = entry.get("type")
        if event_type == "event_ownership_resolved":
            return True
        if event_type not in {"agent_message", "manager_event"}:
            return False
        from event_shape import is_metadata_event
        return not is_metadata_event(entry)

    @staticmethod
    def _affects_root_events_candidate(entry: dict) -> bool:
        event_type = entry.get("type")
        if event_type not in {"agent_message", "manager_event"}:
            return False
        if entry.get("msg_id") is not None:
            return False
        from event_shape import is_metadata_event
        return not is_metadata_event(entry)

    @perf.timed_fn("ingest.read_events")
    def read_events(
        self,
        root_id: str,
        after_seq: int = 0,
        limit: int = 500,
        sid_filter: Optional[str] = None,
        msg_id_filter: Optional[str] = None,
    ) -> tuple[list[dict], int, bool]:
        """Read events from the JSONL file.

        Returns (events, total_count, has_more).

        Fast path: when `after_seq > 0` AND the seq-offset index is
        populated, seeks past the first `after_seq` lines instead of
        parsing-then-discarding them. Falls back to a full scan
        (which ALSO populates the index as a side effect) when the
        cache is cold or `after_seq == 0`.

        Full-scan cache: when after_seq==0, the result is cached by
        file size so multiple callers (hydrate, todos, reconcile)
        share one scan per file version. Filtered in memory.
        """
        path = self._events_path(root_id)
        if not path.exists():
            return [], 0, False
        lock = self._locks.setdefault(root_id, threading.Lock())
        lock_wait_started = time.perf_counter()
        lock.acquire()
        lock_acquired_at = time.perf_counter()
        try:
            # Byte-offset fast path for incremental reads.
            offsets = self._seq_offsets.get(root_id)
            if offsets is not None and after_seq > 0:
                if after_seq >= len(offsets):
                    return [], 0, False
                start_offset = offsets[after_seq]
                return self._scan_from(
                    path, root_id, start_offset, after_seq,
                    limit, sid_filter, msg_id_filter,
                    populate_cache=False,
                )
            if offsets is None and after_seq > 0:
                meta = self._load_chain_meta_locked(root_id, path)
                if meta is not None:
                    point = max(
                        (p for p in self._chain_digests.get(root_id, [])
                         if int(p.get("seq") or 0) <= after_seq),
                        key=lambda p: int(p["seq"]), default=None,
                    )
                    sparse_seq = int(point["seq"]) if point else 0
                    sparse_offset = int(point["size"]) if point else 0
                    scan_started = time.perf_counter()
                    result = self._scan_from(
                        path, root_id, sparse_offset, after_seq,
                        limit, sid_filter, msg_id_filter,
                        populate_cache=False,
                    )
                    perf.record_count(
                        "ingest.read_events.sparse_prefix_rows",
                        max(0, after_seq - sparse_seq),
                    )
                    perf.record(
                        "ingest.read_events.sparse_tail_bytes",
                        max(0, path.stat().st_size - sparse_offset),
                    )
                    perf.record(
                        "ingest.read_events.sparse_tail_ms",
                        (time.perf_counter() - scan_started) * 1000.0,
                    )
                    return result
            # Full scan path (after_seq==0 or cold cache). The cache is
            # keyed on the parsed byte high-water, not just file size, so
            # an append extends it from the tail instead of re-parsing the
            # whole (growing) file — the O(N^2)-over-a-live-turn trap that
            # bit every bulk reader (hydrate, reconcile, todos). Safe to
            # seek from the cached end: reads and writes share the per-root
            # lock, so the file always ends on a clean line boundary.
            file_size = path.stat().st_size
            cached = self._full_scan_cache.get(root_id)
            if cached is not None and cached[0] == file_size:
                self._full_scan_cache.move_to_end(root_id)
                all_entries = cached[1]
            elif cached is not None and cached[0] < file_size:
                new_end, all_entries = self._extend_full_scan(
                    root_id, path, cached[0], cached[1],
                )
                self._remember_full_scan_cache_locked(root_id, new_end, all_entries)
            else:
                populate = offsets is None
                all_entries, _, _ = self._scan_from(
                    path, root_id, 0, 0,
                    limit=999_999, sid_filter=None, msg_id_filter=None,
                    populate_cache=populate,
                )
                self._remember_full_scan_cache_locked(root_id, file_size, all_entries)
            out: list[dict] = []
            total = 0
            page_limit = max(limit, 0)
            for entry in all_entries:
                if after_seq > 0 and entry.get("seq", 0) <= after_seq:
                    continue
                if sid_filter and entry.get("sid") != sid_filter:
                    continue
                if msg_id_filter and entry.get("msg_id") != msg_id_filter:
                    continue
                total += 1
                if len(out) < page_limit:
                    out.append(entry)
            has_more = total > page_limit
            return out, total, has_more
        finally:
            lock_released_at = time.perf_counter()
            lock.release()
            perf.record(
                "ingest.read_events.root_lock_wait",
                (lock_acquired_at - lock_wait_started) * 1000.0,
            )
            perf.record(
                "ingest.read_events.root_lock_held",
                (lock_released_at - lock_acquired_at) * 1000.0,
            )

    def ownership_checkpoint_token(self, root_id: str) -> dict | None:
        path = self._events_path(root_id)
        lock = self._locks.setdefault(root_id, threading.Lock())
        with lock:
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch(mode=0o600)
            _, _, meta = self._ensure_chain_head_locked(root_id, path)
            if meta is None:
                return None
            return {
                "seq": int(meta["seq"]),
                "generation": int(meta["generation"]),
            }

    @staticmethod
    def _write_atomic_payload(path: Path, encoded: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            from windows_handle_marker import WindowsNativeOps, write_atomic_file
            write_atomic_file(WindowsNativeOps(), path.parent, path.name, encoded)
            return
        fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temp = Path(name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
            dir_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        finally:
            temp.unlink(missing_ok=True)

    def commit_ownership_snapshot(
        self,
        root_id: str,
        *,
        token: dict,
        covered_seq: int,
        checkpoint_path: Path,
        payload: dict,
    ) -> dict | None:
        path = self._events_path(root_id)
        lock = self._locks.setdefault(root_id, threading.Lock())
        with lock:
            if not path.exists():
                return None
            _, _, meta = self._ensure_chain_head_locked(root_id, path)
            if meta is None or (
                int(token.get("seq") or -1) != int(meta["seq"])
                or int(token.get("generation") or -1) != int(meta["generation"])
            ):
                perf.record_count("ingest.ownership_checkpoint.cas_conflict")
                return None
            head_seq = int(meta["seq"])
            if covered_seq < 0 or covered_seq > head_seq:
                return None
            if covered_seq == head_seq:
                covered_size = int(meta["size"])
                digest = str(meta["digest"])
            else:
                point = max(
                    (p for p in self._chain_digests.get(root_id, [])
                     if int(p["seq"]) <= covered_seq),
                    key=lambda p: int(p["seq"]), default=None,
                )
                start_seq = int(point["seq"]) if point else 0
                covered_size = int(point["size"]) if point else 0
                rolling = bytes.fromhex(str(point["digest"])) if point else _CHAIN_ZERO
                with open(path, "rb") as source:
                    source.seek(covered_size)
                    for _ in range(start_seq, covered_seq):
                        raw_line = source.readline()
                        if not raw_line or not raw_line.endswith(b"\n"):
                            return None
                        rolling = self._chain_next(rolling, raw_line)
                        covered_size += len(raw_line)
                digest = rolling.hex()
            fence = {
                "covered_seq": int(covered_seq),
                "covered_size": int(covered_size),
                "digest": digest,
                "generation": int(meta["generation"]),
                "head_seq": head_seq,
                "head_size": int(meta["size"]),
                "identity": list(meta["identity"]),
            }
            self._write_chain_meta_locked(
                root_id,
                path,
                seq=head_seq,
                byte_end=int(meta["size"]),
                digest=str(meta["digest"]),
                generation=int(meta["generation"]),
                checkpoint=fence,
            )
            complete = dict(payload)
            complete["journal"] = fence
            self._write_atomic_payload(
                checkpoint_path,
                json.dumps(complete, separators=(",", ":")).encode("utf-8"),
            )
            return fence

    def validate_ownership_checkpoint(self, root_id: str, fence: dict) -> bool:
        path = self._events_path(root_id)
        lock = self._locks.setdefault(root_id, threading.Lock())
        with lock:
            if not path.exists():
                return False
            _, _, meta = self._ensure_chain_head_locked(root_id, path)
            checkpoint = (meta or {}).get("checkpoint")
            if not isinstance(checkpoint, dict) or checkpoint != fence:
                return False
            covered_seq = int(fence.get("covered_seq") or 0)
            point = max(
                (p for p in self._chain_digests.get(root_id, [])
                 if int(p["seq"]) < covered_seq),
                key=lambda p: int(p["seq"]), default=None,
            )
            start_seq = int(point["seq"]) if point else 0
            offset = int(point["size"]) if point else 0
            digest = bytes.fromhex(str(point["digest"])) if point else _CHAIN_ZERO
            with open(path, "rb") as source:
                source.seek(offset)
                for _ in range(start_seq, covered_seq):
                    raw = source.readline()
                    if not raw or not raw.endswith(b"\n"):
                        return False
                    digest = self._chain_next(digest, raw)
                    offset += len(raw)
            perf.record_count(
                "ingest.ownership_checkpoint.validation_rows",
                covered_seq - start_seq,
            )
            return (
                offset == int(fence.get("covered_size") or -1)
                and digest.hex() == fence.get("digest")
            )

    def _extend_full_scan(
        self, root_id: str, path: Path, start_byte: int, base_entries: list[dict],
    ) -> tuple[int, list[dict]]:
        """Parse lines appended since `start_byte` and return
        `(new_byte_high_water, base_entries + new_entries)` as a FRESH
        list — never mutates `base_entries` in place, since it may be
        the same list object aliased into `self._full_scan_cache[root_id]`
        and touching it while unlocked (below) could race a concurrent
        reader/mutator of that cache entry.

        The read itself runs with `self._locks[root_id]` released:
        events.jsonl is single-writer-process append-only (see class
        docstring), so bytes already on disk past `start_byte` when we
        start never change — only new bytes can land while we're
        unlocked. This keeps a large catch-up (e.g. after a long-idle
        full-scan cache) from blocking concurrent `_ingest_impl`
        appends on the same root. `_remember_full_scan_cache_locked`
        is safe to call with our result regardless of what any
        concurrently-racing scan installs — it never regresses the
        shared cache to a smaller byte high-water.
        """
        lock = self._locks[root_id]
        new_entries: list[dict] = []
        end = start_byte
        lock.release()
        try:
            with open(path, "rb") as f:
                f.seek(start_byte)
                while True:
                    raw = f.readline()
                    if not raw:
                        break
                    end += len(raw)
                    line = raw.decode("utf-8", errors="replace").rstrip("\n")
                    if not line.strip():
                        continue
                    try:
                        new_entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        finally:
            lock.acquire()
        combined = list(base_entries)
        combined.extend(new_entries)
        return end, combined

    def read_orphan_events(
        self,
        root_id: str,
        after_seq: int = 0,
    ) -> list[dict]:
        """Read events with msg_id=None (orphan events) after a given seq.
        Uses the byte-offset fast path — only scans the tail of the file,
        not the whole thing. Used by reconcile to find orphan events
        without re-reading everything."""
        path = self._events_path(root_id)
        if not path.exists():
            return []
        lock = self._locks.setdefault(root_id, threading.Lock())
        with lock:
            offsets = self._seq_offsets.get(root_id)
            if after_seq > 0 and offsets is not None:
                if after_seq >= len(offsets):
                    return []
                start_offset = offsets[after_seq]
            elif after_seq > 0:
                # Cold offset cache — fall back to full scan with
                # filter. `_scan_from` requires the per-root lock held
                # on entry (it releases/reacquires internally around
                # its own expensive read), so this call must stay
                # inside this `with lock:` block.
                all_raw, _, _ = self._scan_from(
                    path, root_id, 0, after_seq,
                    limit=10_000, sid_filter=None, msg_id_filter=None,
                    populate_cache=True,
                )
                return [e for e in all_raw if not e.get("msg_id")]
            else:
                start_offset = 0
        # Scan from the offset, filter to orphans only. Returns only a
        # local list (no shared cache write), and events.jsonl is
        # single-writer-process append-only (class docstring), so this
        # plain read needs no lock -- matches `cursor()`'s unlocked read
        # elsewhere in this file. Keeps a long tail scan (or a full
        # scan, when `after_seq == 0`) from blocking concurrent
        # `_ingest_impl` appends on the same root.
        matched: list[dict] = []
        with open(path, "rb") as f:
            f.seek(start_offset)
            while True:
                raw = f.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").rstrip("\n")
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("seq", 0) <= after_seq:
                    continue
                if not entry.get("msg_id"):
                    matched.append(entry)
        return matched

    def cached_rows_for_byte_range(
        self, root_id: str, byte_start: int, byte_end: int,
    ) -> Optional[list[dict]]:
        path = self._events_path(root_id)
        if not path.exists() or byte_end <= byte_start:
            return []
        try:
            file_size = path.stat().st_size
        except OSError:
            return None
        lock = self._locks.setdefault(root_id, threading.Lock())
        with lock:
            cached = self._full_scan_cache.get(root_id)
            offsets = self._seq_offsets.get(root_id)
            if (
                cached is None
                or cached[0] != file_size
                or offsets is None
                or len(offsets) < len(cached[1])
            ):
                return None
            self._full_scan_cache.move_to_end(root_id)
            rows: list[dict] = []
            start_index = bisect.bisect_left(offsets, byte_start)
            for index, entry in enumerate(cached[1][start_index:], start_index):
                line_start = offsets[index]
                if line_start >= byte_end:
                    break
                rows.append(entry)
            return rows

    def root_events_by_sid(self, root_id: str) -> dict[str, list[dict]]:
        path = self._events_path(root_id)
        if not path.exists():
            return {}
        lock = self._locks.setdefault(root_id, threading.Lock())
        with lock:
            version = self._root_events_version.get(root_id)
            if version is None:
                self._scan_max_seq(root_id)
                version = self._root_events_version.get(root_id, 0)
            candidate_version = self._root_events_candidate_version.get(root_id, 0)
            cached = self._root_events_cache.get(root_id)
            if cached is not None and cached[0] == version:
                return copy.deepcopy(cached[1])
            if version == 0 or candidate_version == 0:
                self._root_events_cache[root_id] = (version, {})
                return {}
            file_size = path.stat().st_size
            rows = self._read_all_events_locked(path, root_id, file_size)
            projection = self._build_root_events_projection(rows)
            self._root_events_cache[root_id] = (version, projection)
            return copy.deepcopy(projection)

    def root_events_version(self, root_id: str) -> int:
        path = self._events_path(root_id)
        if not path.exists():
            return 0
        lock = self._locks.setdefault(root_id, threading.Lock())
        with lock:
            version = self._root_events_version.get(root_id)
            if version is None:
                self._scan_max_seq(root_id)
                version = self._root_events_version.get(root_id, 0)
            return int(version or 0)

    def _read_all_events_locked(
        self, path: Path, root_id: str, file_size: int,
    ) -> list[dict]:
        cached = self._full_scan_cache.get(root_id)
        if cached is not None and cached[0] == file_size:
            self._full_scan_cache.move_to_end(root_id)
            return cached[1]
        if cached is not None and cached[0] < file_size:
            new_end, all_entries = self._extend_full_scan(
                root_id, path, cached[0], cached[1],
            )
            self._remember_full_scan_cache_locked(root_id, new_end, all_entries)
            return all_entries
        populate = self._seq_offsets.get(root_id) is None
        all_entries, _, _ = self._scan_from(
            path, root_id, 0, 0,
            limit=999_999, sid_filter=None, msg_id_filter=None,
            populate_cache=populate,
        )
        self._remember_full_scan_cache_locked(root_id, file_size, all_entries)
        return all_entries

    def _build_root_events_projection(
        self, rows: list[dict],
    ) -> dict[str, list[dict]]:
        from event_shape import is_metadata_event

        stamped: dict[str, set[str]] = {}
        orphans: dict[str, list[dict]] = {}
        resolved_seqs: set[int] = set()
        render_types = ("agent_message", "manager_event")
        for entry in rows:
            if entry.get("type") == "event_ownership_resolved":
                data = entry.get("data") or {}
                ev_seq = data.get("event_seq")
                if isinstance(ev_seq, int):
                    resolved_seqs.add(ev_seq)
                continue
            sid = entry.get("sid")
            if not isinstance(sid, str):
                continue
            if entry.get("type") not in render_types:
                continue
            if is_metadata_event(entry):
                continue
            uid = self._extract_uuid(entry.get("data") or {})
            if entry.get("msg_id") is not None:
                if uid:
                    stamped.setdefault(sid, set()).add(uid)
                continue
            orphans.setdefault(sid, []).append(entry)

        out: dict[str, list[dict]] = {}
        for sid, sid_orphans in orphans.items():
            seen: set[str] = set()
            stamped_for_sid = stamped.get(sid, set())
            rendered: list[dict] = []
            for entry in sid_orphans:
                seq = entry.get("seq")
                if isinstance(seq, int) and seq in resolved_seqs:
                    continue
                uid = self._extract_uuid(entry.get("data") or {})
                if uid and (uid in stamped_for_sid or uid in seen):
                    continue
                if uid:
                    seen.add(uid)
                rendered.append(self._root_event_frontend_shape(entry))
            if rendered:
                out[sid] = rendered
        return out

    @staticmethod
    def _root_event_frontend_shape(entry: dict) -> dict:
        data = entry.get("data") or {}
        if entry.get("type") == "manager_event":
            inner = data.get("event") if isinstance(data, dict) else None
            if isinstance(inner, dict):
                return copy.deepcopy(inner)
        return {"type": "agent_message", "data": copy.deepcopy(data)}

    def _update_root_events_cache_for_entry(self, root_id: str, entry: dict) -> None:
        cached = self._root_events_cache.get(root_id)
        if cached is None:
            return
        version = self._root_events_version.get(root_id, 0)
        projection = cached[1]
        sid = entry.get("sid")
        if not isinstance(sid, str):
            self._root_events_cache[root_id] = (version, projection)
            return
        if entry.get("type") == "event_ownership_resolved":
            self._root_events_cache.pop(root_id, None)
            return
        uid = self._extract_uuid(entry.get("data") or {})
        if entry.get("msg_id") is not None:
            if uid:
                self._remove_root_event_projection(
                    projection, sid, uid=uid,
                )
            self._root_events_cache[root_id] = (version, projection)
            return
        existing = projection.setdefault(sid, [])
        if uid and any(
            self._extract_uuid(event.get("data") or {}) == uid
            for event in existing
            if isinstance(event, dict)
        ):
            self._root_events_cache[root_id] = (version, projection)
            return
        existing.append(self._root_event_frontend_shape(entry))
        self._root_events_cache[root_id] = (version, projection)

    def _remove_root_event_projection(
        self,
        projection: dict[str, list[dict]],
        sid: str,
        *,
        uid: Optional[str] = None,
    ) -> None:
        current = projection.get(sid)
        if not current:
            return
        filtered = []
        for event in current:
            if not isinstance(event, dict):
                filtered.append(event)
                continue
            event_data = event.get("data") or {}
            if uid and self._extract_uuid(event_data) == uid:
                continue
            filtered.append(event)
        if filtered:
            projection[sid] = filtered
        else:
            projection.pop(sid, None)

    def has_uid(self, root_id: str, uid: str) -> bool:
        """Check whether a UID is already tracked for this root.

        Lightweight O(1) check against `_seen_uids_only` — no disk I/O,
        no lock. Used by callers that want to skip a write they know the
        ingester would dedup anyway, without paying the executor round-trip.
        May return False negatives after close+reopen if the new seed scan
        hasn't run yet, but that's safe: the ingester's own dedup inside
        `_ingest_impl` is the authoritative guard.
        """
        return uid in self._seen_uids_only.get(root_id, set())

    def current_seq(self, root_id: str) -> Optional[int]:
        """Return the current seq counter for a root (highest assigned seq).
        None if root is unknown."""
        seq = self._seq.get(root_id)
        return seq if seq is not None else None

    def _scan_from(
        self,
        path: Path,
        root_id: str,
        start_offset: int,
        after_seq: int,
        limit: int,
        sid_filter: Optional[str],
        msg_id_filter: Optional[str],
        *,
        populate_cache: bool,
    ) -> tuple[list[dict], int, bool]:
        """Shared body for `read_events`. Opens the file in BINARY mode
        so manually-tracked byte offsets are valid seek targets — text
        mode's `tell()`/`seek()` use opaque cookies (CPython docs).
        Decodes each line as utf-8 with `errors="replace"` matching
        `_ensure_open`'s torn-tail scan.

        When `populate_cache=True`, fills `_seq_offsets[root_id]` and
        `_next_offset[root_id]` as a side effect so subsequent fast-
        path reads can hit the cache. Caller holds
        `self._locks[root_id]`, but the bulk of the read runs with it
        RELEASED: events.jsonl is single-writer-process append-only
        (class docstring), so bytes already on disk at the snapshot
        size we read up to never change. We reacquire before touching
        any shared cache and, when `populate_cache=True`, run a cheap
        catch-up pass over whatever landed while unlocked (never the
        whole range again) before installing anything — so this still
        behaves atomically from the caller's point of view.
        """
        matched: list[dict] = []
        parsed_entries: list[dict] = []
        seq_offsets: list[int] = []
        trailing_invalid = False

        def _consume(range_start: int, range_end: Optional[int]) -> int:
            nonlocal trailing_invalid
            cur = range_start
            with open(path, "rb") as f:
                f.seek(range_start)
                while range_end is None or cur < range_end:
                    line_start = cur
                    raw = f.readline()
                    if not raw:
                        break
                    cur += len(raw)
                    line = raw.decode("utf-8", errors="replace").rstrip("\n")
                    if not line.strip():
                        trailing_invalid = False
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        trailing_invalid = True
                        continue
                    trailing_invalid = False
                    if populate_cache:
                        # INVARIANT: append only when the line parsed as
                        # a dict — mirrors `_ensure_open`'s "increment
                        # only for successfully-parsed lines" convention.
                        seq_offsets.append(line_start)
                        parsed_entries.append(entry)
                    # Defensive: shouldn't trigger when start_offset came
                    # from the index (offsets[after_seq] lands at
                    # seq=after_seq+1). Catches index corruption + the
                    # full-scan path where lines with seq<=after_seq
                    # need filtering.
                    if entry.get("seq", 0) <= after_seq:
                        continue
                    if sid_filter and entry.get("sid") != sid_filter:
                        continue
                    if msg_id_filter and entry.get("msg_id") != msg_id_filter:
                        continue
                    matched.append(entry)
                    # Early-exit: stop once we know `has_more=True`.
                    # Production callers all discard `total`/`has_more`
                    # (verified) so an inexact `total` capped at
                    # `limit+1` is acceptable. CRITICAL INVARIANT: must
                    # NOT early-exit when populating the cache —
                    # `_seq_offsets` must cover EVERY parsed line in the
                    # file or future fast-path lookups corrupt.
                    if not populate_cache and len(matched) > limit:
                        break
            return cur

        lock = self._locks[root_id]
        snapshot_size = path.stat().st_size
        lock.release()
        try:
            cur_offset = _consume(start_offset, snapshot_size)
        finally:
            lock.acquire()
        if populate_cache:
            # Catch up on anything appended while unlocked. A `stat()`
            # first avoids even opening the file in the (common) case
            # nothing landed; otherwise only the new delta is read,
            # never the whole range again.
            if path.stat().st_size > cur_offset:
                cur_offset = _consume(cur_offset, None)
            self._seq_offsets[root_id] = seq_offsets
            self._next_offset[root_id] = cur_offset
            if (
                start_offset == 0
                and after_seq == 0
                and sid_filter is None
                and msg_id_filter is None
                and not trailing_invalid
            ):
                identity = self._event_file_identity(path)
                if identity is not None and identity[3] == cur_offset:
                    self._seed_write_caches_locked(
                        root_id,
                        parsed_entries,
                        seq_offsets,
                        cur_offset,
                        identity,
                    )
        total = len(matched)
        has_more = total > limit
        return matched[:limit], total, has_more

    @perf.timed_fn("ingest.read_ws_events")
    def read_ws_events(
        self,
        root_id: str,
        sid_filter: Optional[str] = None,
        msg_id_filter: Optional[str] = None,
    ) -> list[dict]:
        """Read events and unwrap them into WS event shapes.

        Returns events in the same shape the frontend expects:
        {"type": "agent_message", "data": <enriched_line>, ...}
        """
        raw, _, _ = self.read_events(
            root_id,
            limit=10_000,
            sid_filter=sid_filter,
            msg_id_filter=msg_id_filter,
        )
        ws_events: list[dict] = []
        for entry in raw:
            etype = entry.get("type")
            data = entry.get("data", {})
            if etype == "manager_event" and isinstance(data, dict):
                inner = data.get("event")
                if isinstance(inner, dict):
                    ws_events.append(inner)
            else:
                ws_events.append({"type": etype, "data": data})
        return ws_events

    @perf.timed_fn("ingest.read_ws_events_range")
    def read_ws_events_range(
        self,
        root_id: str,
        byte_start: int,
        byte_end: int,
    ) -> list[dict]:
        """Read events for a byte range of events.jsonl and unwrap into
        WS event shapes. Avoids a full-file scan — seeks directly to
        byte_start and reads until byte_end."""
        path = self._events_path(root_id)
        if not path.exists():
            return []
        ws_events: list[dict] = []
        with open(path, "rb") as f:
            f.seek(byte_start)
            while f.tell() < byte_end:
                raw = f.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").rstrip("\n")
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                etype = entry.get("type")
                data = entry.get("data", {})
                if etype == "manager_event" and isinstance(data, dict):
                    inner = data.get("event")
                    if isinstance(inner, dict):
                        ws_events.append(inner)
                else:
                    ws_events.append({"type": etype, "data": data})
        return ws_events

    @perf.timed_fn("ingest.message_event_summaries")
    def message_event_summaries(
        self,
        root_id: str,
        *,
        sid_filter: Optional[str] = None,
        msg_ids: Optional[set[str]] = None,
        tail: int = 25,
    ) -> dict[str, dict]:
        """Return per-message event refs + collapsed-preview data.

        Incrementally cached: first call does a full scan. Subsequent
        calls only scan new events appended since the last call (using
        the cached byte_end as the seek offset). Updates existing
        summaries in-place. Filtered by sid_filter/msg_ids in memory.
        """
        path = self._events_path(root_id)
        if not path.exists():
            return {}
        with self._summaries_state(
            root_id,
            path,
            tail,
            sid_filter=sid_filter,
            msg_ids=msg_ids,
        ) as (all_summaries, _):
            if not sid_filter and msg_ids is None:
                return self._public_message_summaries(all_summaries)
            return {
                k: self._public_message_summary(v)
                for k, v in all_summaries.items()
                if self._summary_matches_filter(k, v, sid_filter=sid_filter, msg_ids=msg_ids)
            }

    @staticmethod
    def _public_message_summary(summary: dict) -> dict:
        return {
            key: value for key, value in summary.items()
            if not str(key).startswith("_")
        }

    @classmethod
    def _public_message_summaries(cls, summaries: dict[str, dict]) -> dict[str, dict]:
        return {
            msg_id: cls._public_message_summary(summary)
            for msg_id, summary in summaries.items()
        }

    @staticmethod
    def _summary_matches_filter(
        msg_id: str,
        summary: dict,
        *,
        sid_filter: Optional[str],
        msg_ids: Optional[set[str]],
    ) -> bool:
        if sid_filter and summary.get("sid") != sid_filter:
            return False
        return msg_ids is None or msg_id in msg_ids

    def latest_render_event_uid(
        self,
        root_id: str,
        *,
        sid_filter: Optional[str] = None,
    ) -> Optional[str]:
        if sid_filter:
            latest_by_sid = self._latest_render_uid_by_sid.get(root_id)
            latest = latest_by_sid.get(sid_filter) if latest_by_sid else None
            if latest is not None:
                return latest[1]
        latest: Optional[tuple[int, str]] = None
        for summary in self.message_event_summaries(
            root_id, sid_filter=sid_filter, tail=25,
        ).values():
            for event in reversed(summary.get("last_events") or []):
                if not isinstance(event, dict):
                    continue
                seq = event.get("seq")
                if not isinstance(seq, int):
                    continue
                uid = self._extract_uuid(event.get("data") or {})
                if uid:
                    if latest is None or seq > latest[0]:
                        latest = (seq, uid)
                    break
        if latest and sid_filter:
            self._latest_render_uid_by_sid.setdefault(root_id, {})[sid_filter] = latest
        return latest[1] if latest else None

    def ownership_resolutions(self, root_id: str) -> dict[int, str]:
        """Return the {orphan journal seq -> resolved msg_id} map.

        Built incrementally from `event_ownership_resolved` facts during
        the summaries scan. Append-only, so this never triggers a full
        rescan once the cache is warm. Replaces the former full-file
        scan that rediscovered write-time facts on every read.
        """
        path = self._events_path(root_id)
        if not path.exists():
            return {}
        with self._summaries_state(root_id, path) as (_, resolutions):
            return dict(resolutions)

    def ownership_resolutions_range(
        self,
        root_id: str,
        *,
        seq_start: int,
        seq_end: int,
    ) -> dict[int, str]:
        path = self._events_path(root_id)
        if not path.exists() or seq_end < seq_start:
            return {}
        with self._summaries_state(root_id, path) as (_, resolutions):
            return {
                seq: msg_id
                for seq, msg_id in resolutions.items()
                if seq_start <= seq <= seq_end
            }

    @contextmanager
    def _summaries_state(
        self,
        root_id: str,
        path: Path,
        tail: int = 25,
        *,
        sid_filter: Optional[str] = None,
        msg_ids: Optional[set[str]] = None,
    ):
        """Yield (summaries, resolutions) for `root_id`, refreshing the
        cache from disk under the per-root lock. Both reflect effective
        ownership (resolutions folded into summary bounds)."""
        file_size = path.stat().st_size
        lock = self._locks.setdefault(root_id, threading.Lock())
        with lock:
            cached = self._summaries_cache.get(root_id)
            offsets = self._seq_offsets.get(root_id)
            cached_index_current = (
                offsets is not None
                and self._next_offset.get(root_id) == file_size
                and self._seq.get(root_id) == len(offsets)
            )
            if cached is not None and cached[0] == file_size and cached_index_current:
                _, summaries, resolutions = cached
            elif cached is not None and cached[0] < file_size and cached_index_current:
                _, summaries, resolutions = cached
                self._append_summaries(
                    path, root_id, tail, summaries, resolutions,
                )
                self._fold_resolutions(root_id, summaries, resolutions)
                self._summaries_cache[root_id] = (
                    file_size, summaries, resolutions,
                )
                self._write_event_summaries_sidecar_locked(
                    root_id,
                    path,
                    tail=tail,
                    summaries=summaries,
                    resolutions=resolutions,
                )
            else:
                loaded = self._load_event_summaries_sidecar_locked(root_id, path, tail)
                if loaded is not None:
                    summaries, resolutions = loaded
                    self._summaries_cache[root_id] = (
                        file_size, summaries, resolutions,
                    )
                else:
                    summaries, resolutions = self._scan_summaries(
                        path, root_id, tail,
                    )
                    self._fold_resolutions(root_id, summaries, resolutions)
                    self._summaries_cache[root_id] = (
                        file_size, summaries, resolutions,
                    )
                    self._write_event_summaries_sidecar_locked(
                        root_id,
                        path,
                        tail=tail,
                        summaries=summaries,
                        resolutions=resolutions,
                    )
            yield summaries, resolutions

    def _seq_byte_range(
        self, root_id: str, seq: int,
    ) -> Optional[tuple[int, int]]:
        """Byte range [start, end) of the JSONL line at journal `seq`,
        from the write-maintained seq->offset index. None if the index
        does not cover `seq`."""
        offsets = self._seq_offsets.get(root_id)
        if not offsets or seq < 1 or seq > len(offsets):
            return None
        start = offsets[seq - 1]
        if seq < len(offsets):
            end = offsets[seq]
        else:
            end = self._next_offset.get(root_id)
        if end is None or end <= start:
            return None
        return (start, end)

    def _fold_resolutions(
        self,
        root_id: str,
        summaries: dict[str, dict],
        resolutions: dict[int, str],
    ) -> None:
        """Expand each message's byte/seq bounds to cover the orphan rows
        a write-time fact reassigned to it. A message's effective span =
        its own contiguous run UNION every resolved-in orphan's range.
        Idempotent: min/max folding re-applies cleanly each refresh.

        Bounds only — `event_count`/`last_events` (the collapsed-stub
        preview) are NOT bumped for resolved-in orphans; doing so would
        require reading each orphan row's content here. The render tree
        (built via apply_event) still gets the orphan, so expand shows
        it; only a collapsed stub's preview count may under-count by the
        number of resolved-in orphans. Cosmetic, render-correctness
        unaffected (hydrate counts raw journal rows, not this field)."""
        for ev_seq, msg_id in resolutions.items():
            rng = self._seq_byte_range(root_id, ev_seq)
            if rng is None:
                continue
            bs, be = rng
            rec = summaries.get(msg_id)
            if rec is None:
                rec = summaries.setdefault(msg_id, {
                    "root_id": root_id,
                    "sid": None,
                    "msg_id": msg_id,
                    "seq_start": ev_seq,
                    "seq_end": ev_seq,
                    "byte_start": bs,
                    "byte_end": be,
                    "event_count": 0,
                    "direct_event_count": 0,
                    "last_events": [],
                })
            rec["byte_start"] = min(rec["byte_start"], bs)
            rec["byte_end"] = max(rec["byte_end"], be)
            if isinstance(rec.get("seq_start"), int):
                rec["seq_start"] = min(rec["seq_start"], ev_seq)
            else:
                rec["seq_start"] = ev_seq
            rec["seq_end"] = max(rec.get("seq_end") or ev_seq, ev_seq)

    def _update_summary_line(
        self, out: dict[str, dict], resolutions: dict[int, str],
        root_id: str, entry: dict, line_start: int, line_end: int, tail: int,
    ) -> None:
        """Fold one parsed JSONL line into the on-disk-msg_id summaries
        and the resolution map. Shared by full scan and incremental
        append so the two paths can never drift."""
        etype = entry.get("type")
        seq = entry.get("seq")
        if etype == "event_ownership_resolved":
            data = entry.get("data") or {}
            ev_seq = data.get("event_seq")
            target = data.get("message_id") or entry.get("msg_id")
            if (
                isinstance(ev_seq, int) and ev_seq > 0
                and isinstance(target, str) and target
            ):
                resolutions[ev_seq] = target
            return
        msg_id = entry.get("msg_id")
        if not isinstance(msg_id, str) or not msg_id:
            return
        rec = out.setdefault(msg_id, {
            "root_id": root_id,
            "sid": entry.get("sid"),
            "msg_id": msg_id,
            "seq_start": seq,
            "seq_end": seq,
            "byte_start": line_start,
            "byte_end": line_end,
            "event_count": 0,
            "direct_event_count": 0,
            "last_events": [],
            "_render_uuid_idx": {},
        })
        if isinstance(seq, int):
            if not isinstance(rec.get("seq_start"), int):
                rec["seq_start"] = seq
            rec["seq_end"] = seq
        rec["byte_end"] = line_end
        summary_event = self._summary_render_event(entry)
        if summary_event is None:
            return
        uid = event_uuid(summary_event)
        if not uid:
            return
        uuid_idx = rec.setdefault("_render_uuid_idx", {})
        if not isinstance(uuid_idx, dict):
            uuid_idx = {}
            rec["_render_uuid_idx"] = uuid_idx
        existing_idx = uuid_idx.get(uid)
        if isinstance(existing_idx, int):
            for idx, event in enumerate(rec["last_events"]):
                if event_uuid(event) == uid:
                    rec["last_events"][idx] = summary_event
                    rec["last_events"] = self._summary_preview_events(rec["last_events"], tail)
                    break
            return
        uuid_idx[uid] = rec["event_count"]
        rec["event_count"] += 1
        if etype != "worker_event":
            rec["direct_event_count"] = rec.get("direct_event_count", 0) + 1
        rec["last_events"].append(summary_event)
        rec["last_events"] = self._summary_preview_events(rec["last_events"], tail)

    @staticmethod
    def _summary_render_event(entry: dict) -> Optional[dict]:
        return frontend_event_from_journal_row(entry, include_seq=True)

    @staticmethod
    def _summary_preview_events(events: list, tail: int) -> list:
        import render_stub
        return render_stub.stub_preview_events(events, tail)

    def _append_summaries(
        self, path: Path, root_id: str, tail: int,
        summaries: dict[str, dict], resolutions: dict[int, str],
    ) -> None:
        """Scan events appended since the last cached high-water byte,
        updating summaries + resolutions in place. Caller holds lock.

        Seeks from the max raw on-disk byte_end (NOT the effective
        byte_end, which resolution folding may have pushed past real
        rows) so no appended line is skipped or re-read."""
        start_byte = self._summaries_cache.get(root_id, (0, {}, {}))[0]
        with open(path, "rb") as f:
            f.seek(start_byte)
            while True:
                line_start = f.tell()
                raw = f.readline()
                if not raw:
                    break
                line_end = f.tell()
                line = raw.decode("utf-8", errors="replace").rstrip("\n")
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self._update_summary_line(
                    summaries, resolutions, root_id,
                    entry, line_start, line_end, tail,
                )

    def _rebuild_seq_offsets_locked(self, path: Path, root_id: str) -> None:
        seq_offsets: list[int] = []
        with open(path, "rb") as f:
            while True:
                line_start = f.tell()
                raw = f.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").rstrip("\n")
                if not line.strip():
                    continue
                try:
                    json.loads(line)
                except json.JSONDecodeError:
                    continue
                seq_offsets.append(line_start)
        self._seq_offsets[root_id] = seq_offsets
        self._seq[root_id] = len(seq_offsets)
        self._next_offset[root_id] = path.stat().st_size

    def _scan_summaries(
        self, path: Path, root_id: str, tail: int,
    ) -> tuple[dict[str, dict], dict[int, str]]:
        """Full JSONL scan to build per-message summaries + the
        resolution map. Also (re)populates `_seq_offsets` so the
        resolution fold has byte ranges even on a read-only cold load
        where `_ensure_open` has not run. Caller holds lock."""
        out: dict[str, dict] = {}
        resolutions: dict[int, str] = {}
        file_size = path.stat().st_size
        cached = self._full_scan_cache.get(root_id)
        offsets = self._seq_offsets.get(root_id)
        if (
            cached is not None
            and cached[0] == file_size
            and offsets is not None
            and len(offsets) == len(cached[1])
        ):
            self._full_scan_cache.move_to_end(root_id)
            entries = cached[1]
            for index, entry in enumerate(entries):
                line_start = offsets[index]
                if index + 1 < len(offsets):
                    line_end = offsets[index + 1]
                else:
                    line_end = self._next_offset.get(root_id, file_size)
                self._update_summary_line(
                    out, resolutions, root_id,
                    entry, line_start, line_end, tail,
                )
            return out, resolutions
        seq_offsets: list[int] = []
        with open(path, "rb") as f:
            while True:
                line_start = f.tell()
                raw = f.readline()
                if not raw:
                    break
                line_end = f.tell()
                line = raw.decode("utf-8", errors="replace").rstrip("\n")
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                seq_offsets.append(line_start)
                self._update_summary_line(
                    out, resolutions, root_id,
                    entry, line_start, line_end, tail,
                )
        # A full scan under the per-root lock is authoritative for the
        # file as it exists now: it reads every committed line to EOF
        # using the same parsed-line offset convention as `_ensure_open`.
        # So it supersedes any prior index/EOF unconditionally — a
        # correct rescan can be legitimately SHORTER than a stale
        # pre-truncation index, so a length guard would wrongly keep
        # garbage offsets that `_seq_byte_range` would fold past EOF.
        self._seq_offsets[root_id] = seq_offsets
        self._seq[root_id] = len(seq_offsets)
        self._next_offset[root_id] = path.stat().st_size
        return out, resolutions

    def close(self, root_id: str) -> None:
        lock = self._locks.get(root_id)
        if lock:
            with lock:
                self._close_handle_locked(root_id)
                self._seq.pop(root_id, None)
                self._locks.pop(root_id, None)
                self._seen_uuids.pop(root_id, None)
                self._seen_event_owners.pop(root_id, None)
                self._seen_uids_only.pop(root_id, None)
                self._max_seq_by_sid.pop(root_id, None)
                self._render_seq_by_sid.pop(root_id, None)
                self._seq_offsets.pop(root_id, None)
                self._next_offset.pop(root_id, None)
                self._summaries_cache.pop(root_id, None)
                self._drop_full_scan_cache_locked(root_id)
                self._root_events_cache.pop(root_id, None)
                self._root_events_version.pop(root_id, None)
                self._root_events_candidate_version.pop(root_id, None)
                self._latest_render_uid_by_sid.pop(root_id, None)
                self._write_seed_signatures.pop(root_id, None)
                self._chain_digests.pop(root_id, None)
                self._chain_head_digest.pop(root_id, None)
                self._chain_generation.pop(root_id, None)
                self._chain_meta_identity.pop(root_id, None)
                self._chain_checkpoint.pop(root_id, None)
                self._durable_chain_head.pop(root_id, None)

    def close_all(self) -> None:
        # Drain pending background durability before closing handles so
        # shutdown can't lose not-yet-fsync'd events.
        self._fsync_dirty_now()
        root_ids = set(self._handles) | set(self._seq)
        for root_id in list(root_ids):
            self.close(root_id)

    def shutdown(self) -> None:
        self.close_all()
        with self._fsync_cond:
            thread = self._fsync_thread
            self._fsync_stop.set()
            self._fsync_cond.notify_all()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)
            if thread.is_alive():
                raise RuntimeError("event ingester fsync thread did not stop")
        with self._fsync_cond:
            self._fsync_thread = None


event_ingester = EventIngester()
