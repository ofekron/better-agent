"""Layer 1: Single writer for Better Agent session events JSONL.

All event sources (the orchestrator's per-event save callback and the
on-API-call native-jsonl migration in `main._migrate_native_jsonl`)
feed into this ingester, which appends enriched events to a per-root-session
JSONL file. This file is the single source of truth for all session events.

File location: <ba_home>/sessions/<root_id>/events.jsonl
State location: <ba_home>/sessions/<root_id>/ingester_state.json
"""

import hashlib
import bisect
import copy
import json
import logging
import os
import re
import threading
from collections import OrderedDict
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from paths import bc_home
from file_ref_resolver import rewrite_event_data
from event_shape import event_uuid, frontend_event_from_journal_row
from session_manager import manager as session_manager
import perf
import session_store

logger = logging.getLogger(__name__)

_UUID_KEY = "uuid"
_EVENT_SUMMARIES_VERSION = 5
_MAX_OPEN_APPEND_HANDLES = 64
# Stable-storage fsync cadence for the background flusher. `fh.flush()`
# (kernel page-cache visibility — what cross-process tailers and readers
# actually need) stays synchronous on the ingest path; only `os.fsync()`
# (OS/power-crash durability, beyond the clean-restart convergence
# invariant) is deferred and batched here. See `_mark_fsync_dirty`.
_FSYNC_INTERVAL = 0.25
_BCFILE_LINK_RE = re.compile(r"`?\[([^\]\n]+)\]\(bcfile:[^)\s]+\)`?")


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
        # daemon thread fsyncs each still-open handle every `_FSYNC_INTERVAL`.
        # `_fsync_thread` is started lazily on first dirty mark so tests
        # that never ingest don't spawn it. The thread runs for the
        # process lifetime; `_fsync_dirty_now` drains synchronously
        # (e.g. on shutdown) without killing it — the module-level
        # singleton is reused after `close_all`, so a permanent stop
        # flag would silently disable durability for the rest of life.
        self._fsync_dirty: set[str] = set()
        self._fsync_cond = threading.Condition()
        self._fsync_thread: Optional[threading.Thread] = None
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
        # per root; ~160 KB for a 20K-event session. No LRU — `close`
        # is the eviction path; TODO revisit if lifetime root count
        # × per-root events exceeds ~200 MB in a long-running backend.
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
        # reconcile) share one cached scan.
        self._full_scan_cache: dict[str, tuple[int, list[dict]]] = {}
        self._root_events_cache: dict[str, tuple[int, dict[str, list[dict]]]] = {}
        self._root_events_version: dict[str, int] = {}
        self._root_events_candidate_version: dict[str, int] = {}
        self._latest_render_uid_by_sid: dict[str, dict[str, tuple[int, str]]] = {}

    def _root_dir(self, root_id: str) -> Path:
        return bc_home() / "sessions" / root_id

    def _events_path(self, root_id: str) -> Path:
        return self._root_dir(root_id) / "events.jsonl"

    def _event_meta_path(self, root_id: str) -> Path:
        return self._root_dir(root_id) / "event_meta.json"

    def _event_summaries_path(self, root_id: str) -> Path:
        return self._root_dir(root_id) / "event_summaries.json"

    @staticmethod
    def _event_file_signature(path: Path) -> Optional[tuple[int, int]]:
        try:
            st = path.stat()
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

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
        with self._fsync_cond:
            self._fsync_dirty.discard(root_id)
        if not pair:
            return
        _, fh = pair
        # Drain durability for this handle synchronously — once closed
        # the background flusher can no longer reach it.
        try:
            fh.flush()
            os.fsync(fh.fileno())
        except OSError:
            logger.debug("close fsync failed for %s", root_id, exc_info=True)
        fh.close()

    def _prune_append_handles(self, *, exclude_root_id: str) -> None:
        while True:
            with self._guard:
                if len(self._handles) <= _MAX_OPEN_APPEND_HANDLES:
                    return
                victim_id = next(
                    (rid for rid in self._handles if rid != exclude_root_id),
                    None,
                )
                if victim_id is None:
                    return
            victim_lock = self._locks.get(victim_id)
            if victim_lock is None or not victim_lock.acquire(blocking=False):
                return
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
            self._fsync_dirty.add(root_id)
            self._fsync_cond.notify_all()

    def _start_fsync_thread_locked(self) -> None:
        if self._fsync_thread is None:
            t = threading.Thread(
                target=self._fsync_loop, name="event-ingester-fsync",
                daemon=True,
            )
            self._fsync_thread = t
            t.start()

    def _fsync_loop(self) -> None:
        while True:
            with self._fsync_cond:
                if not self._fsync_dirty:
                    self._fsync_cond.wait(timeout=_FSYNC_INTERVAL)
                dirty = sorted(self._fsync_dirty)
            # Fsync outside `_fsync_cond` so a slow disk can't block
            # dirty-marking. Re-fetch the CURRENT handle under `_guard`
            # per root: an evicted/closed root's data was fsync'd in the
            # close path, so skipping it is correct; this also never
            # touches a recycled fd.
            for root_id in dirty:
                with self._guard:
                    pair = self._handles.get(root_id)
                    fh = pair[1] if pair is not None else None
                if fh is None:
                    continue
                try:
                    os.fsync(fh.fileno())
                except OSError:
                    # Stable-storage failure (EIO etc.): the line IS
                    # kernel-visible, so the convergence invariant holds,
                    # but the operator must see it — escalate, don't hide
                    # it at debug.
                    logger.error(
                        "background fsync failed for %s; durability at risk",
                        root_id, exc_info=True,
                    )
                with self._fsync_cond:
                    self._fsync_dirty.discard(root_id)

    def _fsync_dirty_now(self) -> None:
        """Synchronous fsync of every currently-dirty root. Used by
        `close_all` so pending background durability isn't lost. Does
        NOT stop the flusher — the singleton is reused after `close_all`."""
        with self._fsync_cond:
            dirty = sorted(self._fsync_dirty)
            self._fsync_dirty.clear()
            self._fsync_cond.notify_all()
        for root_id in dirty:
            with self._guard:
                pair = self._handles.get(root_id)
                fh = pair[1] if pair is not None else None
            if fh is None:
                continue
            try:
                fh.flush()
                os.fsync(fh.fileno())
            except OSError:
                logger.error("shutdown fsync failed for %s; durability at risk",
                             root_id, exc_info=True)

    def _ensure_open(self, root_id: str) -> tuple[Path, Any]:
        with self._guard:
            cached = self._handles.get(root_id)
            if cached is not None:
                self._handles.move_to_end(root_id)
                return cached
        root_dir = self._root_dir(root_id)
        root_dir.mkdir(parents=True, exist_ok=True)
        path = self._events_path(root_id)
        # Gate on the dedup set being seeded, NOT just `_seq`: `cursor()`
        # caches `_seq[root_id]` from a cheap line-count scan WITHOUT
        # seeding `_seen_event_owners`/`_seen_uuids`. If we early-returned
        # on `_seq` alone, the first ingest after a `cursor()` call (every
        # subscribed session — `add_subscriber` runs `cursor()`) would
        # skip the disk seed, leaving the dedup sets empty. The dual
        # writers (SDK callback `apply_event` + jsonl tailer
        # `ingest_orphan`) would then both write the same event with no
        # dedup → duplicate rows → duplicate rendered content.
        if root_id in self._seq and root_id in self._seen_event_owners:
            return path, self._open_append_handle(root_id, path)
        existing_lines = 0
        # Seed `_seen_uuids` from disk so a backend restart doesn't
        # silently re-ingest the entire claude jsonl as duplicate seqs
        # in events.jsonl. Without this seed, the in-memory dedup set
        # was empty after restart and every claude tailer-driven
        # re-read would append a duplicate row.
        seen: set[str] = set()
        seen_owners: dict[str, set[Optional[str]]] = {}
        # Same scan also seeds the seq watermarks so REST snapshot
        # callers don't re-scan the file later.
        sid_max: dict[str, int] = {}
        render_sid_max: dict[str, int] = {}
        render_projection_version = 0
        root_event_candidate_seqs: set[int] = set()
        resolved_root_event_seqs: set[int] = set()
        # Same scan also seeds the seq → byte-offset index so read_events
        # can fast-path-skip the after_seq prefix. INVARIANT: append
        # ONLY at the same site as `existing_lines += 1` (parsed,
        # non-torn lines) so `len(seq_offsets) == _seq[root_id]` after
        # bootstrap.
        seq_offsets: list[int] = []
        # Torn-tail recovery: if a crash interrupted ingest between
        # fsync and the trailing newline, the last line is partial JSON.
        # Truncate it so the file stays parseable and the next append
        # doesn't bury garbage in the middle. Only the trailing run of
        # un-parseable lines is truncated; mid-file decode errors stay
        # (truncating them would lose subsequent good data).
        torn_offset: Optional[int] = None
        if path.exists():
            with open(path, "rb") as f:
                while True:
                    line_start = f.tell()
                    raw = f.readline()
                    if not raw:
                        break
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
                    existing_lines += 1
                    # INVARIANT: seq_offsets append MUST stay at this
                    # site (parsed, non-torn line, immediately after the
                    # `existing_lines += 1` companion) so the offsets
                    # list length tracks _seq[root_id] exactly.
                    seq_offsets.append(line_start)
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
                        self._seen_uids_only.setdefault(root_id, set()).add(uid)
                    sid_val = entry.get("sid")
                    seq_val = entry.get("seq")
                    if isinstance(sid_val, str) and isinstance(seq_val, int):
                        if seq_val > sid_max.get(sid_val, 0):
                            sid_max[sid_val] = seq_val
                        if self._affects_render_projection(entry):
                            if seq_val > render_sid_max.get(sid_val, 0):
                                render_sid_max[sid_val] = seq_val
                        if self._affects_root_events_projection(entry):
                            render_projection_version += 1
                            if self._affects_root_events_candidate(entry):
                                root_event_candidate_seqs.add(seq_val)
                            elif entry.get("type") == "event_ownership_resolved":
                                data = entry.get("data") or {}
                                event_seq = data.get("event_seq")
                                if isinstance(event_seq, int):
                                    resolved_root_event_seqs.add(event_seq)
            if torn_offset is not None:
                logger.warning(
                    "event_ingester: truncating torn trailing line at "
                    "offset %d in %s", torn_offset, path,
                )
                with open(path, "r+b") as f:
                    f.truncate(torn_offset)
        self._seq[root_id] = existing_lines
        self._max_seq_by_sid[root_id] = sid_max
        self._render_seq_by_sid[root_id] = render_sid_max
        self._root_events_version[root_id] = render_projection_version
        self._root_events_candidate_version[root_id] = len(
            root_event_candidate_seqs - resolved_root_event_seqs
        )
        self._seq_offsets[root_id] = seq_offsets
        # File size AFTER any torn-tail truncation = next write offset.
        self._next_offset[root_id] = path.stat().st_size if path.exists() else 0
        self._locks.setdefault(root_id, threading.Lock())
        self._seen_uuids[root_id] = seen
        self._seen_event_owners[root_id] = seen_owners
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
        canonical = copy.deepcopy(data)
        try:
            rewrite_event_data(
                event_type, canonical, cwd, assume_exists=assume_exists,
            )
        except Exception:
            logger.debug("file_ref_resolver rewrite failed", exc_info=True)
        return canonical

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
    ) -> None:
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
        # INVARIANT: ingest/ingest_batch hold `_locks[root_id]` across
        # this method, so all 3 cache updates below are serialized
        # against the fallback scans in `max_seq_by_sid` and
        # `_scan_from`. All updates reflect the line that was just
        # written — pre-flush is fine because the file-handle is still
        # owned by this process.
        offset_for_this_line = self._next_offset.get(root_id, 0)
        fh.write(line)
        # `_next_offset` and `_seq_offsets` MUST update together; if
        # either is dropped a future read_events would seek to the wrong
        # offset OR future _emit would record a stale offset.
        self._next_offset[root_id] = (
            offset_for_this_line + len(line.encode("utf-8"))
        )
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
        with lock:
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
            self._emit(
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
            self._mark_fsync_dirty(root_id)

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
        with lock:
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
                self._emit(
                    fh, root_id, seq, sid, event_type, canonical_data, source,
                    run_id, msg_id,
                )
            fh.flush()
            self._mark_fsync_dirty(root_id)
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

    def _scan_max_seq(self, root_id: str) -> dict[str, int]:
        """Full scan fallback for max_seq_by_sid. Caller holds the lock."""
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
        out: dict[str, int] = {}
        render_out: dict[str, int] = {}
        render_projection_version = 0
        root_event_candidate_seqs: set[int] = set()
        resolved_root_event_seqs: set[int] = set()
        summaries: dict[str, dict] = {}
        resolutions: dict[int, str] = {}
        parsed_lines = 0
        seq_offsets: list[int] = []
        all_entries: list[dict] = []
        cur_offset = 0
        with open(path, "rb") as f:
            while True:
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
                parsed_lines += 1
                seq_offsets.append(line_start)
                all_entries.append(entry)
                self._update_summary_line(
                    summaries, resolutions, root_id,
                    entry, line_start, cur_offset, 25,
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
        self._full_scan_cache[root_id] = (cur_offset, all_entries)
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
            self._build_root_events_projection(all_entries)
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
        with lock:
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
                all_entries = cached[1]
            elif cached is not None and cached[0] < file_size:
                all_entries = cached[1]
                new_end = self._extend_full_scan(path, cached[0], all_entries)
                self._full_scan_cache[root_id] = (new_end, all_entries)
            else:
                populate = offsets is None
                all_entries, _, _ = self._scan_from(
                    path, root_id, 0, 0,
                    limit=999_999, sid_filter=None, msg_id_filter=None,
                    populate_cache=populate,
                )
                self._full_scan_cache[root_id] = (file_size, all_entries)
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

    def _extend_full_scan(
        self, path: Path, start_byte: int, all_entries: list[dict],
    ) -> int:
        """Parse lines appended since `start_byte`, append them to
        `all_entries` in place, and return the new clean byte high-water.
        Caller holds the per-root lock; `start_byte` is a line boundary
        by the read/write lock invariant."""
        end = start_byte
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
                    all_entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return end

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
                # Cold offset cache — fall back to full scan with filter.
                all_raw, _, _ = self._scan_from(
                    path, root_id, 0, after_seq,
                    limit=10_000, sid_filter=None, msg_id_filter=None,
                    populate_cache=True,
                )
                return [e for e in all_raw if not e.get("msg_id")]
            else:
                start_offset = 0
            # Scan from the offset, filter to orphans only.
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
            return cached[1]
        if cached is not None and cached[0] < file_size:
            all_entries = cached[1]
            new_end = self._extend_full_scan(path, cached[0], all_entries)
            self._full_scan_cache[root_id] = (new_end, all_entries)
            return all_entries
        populate = self._seq_offsets.get(root_id) is None
        all_entries, _, _ = self._scan_from(
            path, root_id, 0, 0,
            limit=999_999, sid_filter=None, msg_id_filter=None,
            populate_cache=populate,
        )
        self._full_scan_cache[root_id] = (file_size, all_entries)
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
        path reads can hit the cache. Caller MUST hold the per-root
        lock.
        """
        matched: list[dict] = []
        seq_offsets: list[int] = []
        cur_offset = start_offset
        with open(path, "rb") as f:
            f.seek(start_offset)
            while True:
                line_start = cur_offset
                raw = f.readline()
                if not raw:
                    break
                cur_offset += len(raw)
                line = raw.decode("utf-8", errors="replace").rstrip("\n")
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if populate_cache:
                    # INVARIANT: append only when the line parsed as a
                    # dict — mirrors `_ensure_open`'s "increment only
                    # for successfully-parsed lines" convention.
                    seq_offsets.append(line_start)
                # Defensive: shouldn't trigger when start_offset came
                # from the index (offsets[after_seq] lands at
                # seq=after_seq+1). Catches index corruption + the
                # full-scan path where lines with seq<=after_seq need
                # filtering.
                if entry.get("seq", 0) <= after_seq:
                    continue
                if sid_filter and entry.get("sid") != sid_filter:
                    continue
                if msg_id_filter and entry.get("msg_id") != msg_id_filter:
                    continue
                matched.append(entry)
                # Early-exit: stop once we know `has_more=True`. Production
                # callers all discard `total`/`has_more` (verified) so an
                # inexact `total` capped at `limit+1` is acceptable.
                # CRITICAL INVARIANT: must NOT early-exit when populating
                # the cache — `_seq_offsets` must cover EVERY parsed line
                # in the file or future fast-path lookups corrupt.
                if not populate_cache and len(matched) > limit:
                    break
        if populate_cache:
            self._seq_offsets[root_id] = seq_offsets
            self._next_offset[root_id] = cur_offset
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
                self._full_scan_cache.pop(root_id, None)
                self._root_events_cache.pop(root_id, None)
                self._root_events_version.pop(root_id, None)
                self._root_events_candidate_version.pop(root_id, None)
                self._latest_render_uid_by_sid.pop(root_id, None)

    def close_all(self) -> None:
        # Drain pending background durability before closing handles so
        # shutdown can't lose not-yet-fsync'd events.
        self._fsync_dirty_now()
        root_ids = set(self._handles) | set(self._seq)
        for root_id in list(root_ids):
            self.close(root_id)


event_ingester = EventIngester()
