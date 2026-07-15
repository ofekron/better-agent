"""Persistent session storage — saves conversation history as JSON files.

**Tree-shape storage (schema v2).** A *root* session is a top-level
record in `~/.better-claude/sessions/<root_id>.json`. A *fork* session
is embedded inside its parent's `forks: [Session, ...]` array (the
parent may itself be a fork — nested forks are valid). Every session
record (root or fork) has a unique id; root files are the only files
on disk. A reverse index `_fork_index: {fork_id: root_id}` lets every
read/write resolve any session id back to its root file in O(1).

Why embedded: the user-facing UX is "fork panes belong to their parent
session." Embedding makes the sidebar list naturally exclude forks
(they aren't separate files), gives atomic root-tree writes, and lets
delete-root recurse to all forks for free. See CLAUDE.md state-
ownership rule.

Workers are NOT stored on session records — they live in the global
`worker_store.py` roster.

Schema migrations are NOT supported (per CLAUDE.md): legacy on-disk
fork files (a top-level file with `parent_session_id` set) raise on
read with "wipe ~/.better-claude/sessions/."

`provider_id` backfill (one-shot, persisted): legacy sessions without
`provider_id` get one inferred from disk — for each configured
provider, we check whether that provider's `<config_dir>/projects/
<encoded-cwd>/<claude_sid>.jsonl` exists. The provider whose dir
holds the session's actual claude jsonl wins; if zero or more than
one match, we fall back to the currently-active provider. The
inferred id is written back to disk on first detection so subsequent
loads stay deterministic regardless of what's active later.
"""

import copy
import collections
import hashlib
import json
import logging
import os
import queue
import tempfile
import threading
import time
import uuid
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional

import config_store
import perf
import messages_delta_compaction
import runtime_ownership
from grouped_durability_writer import DurabilityReceipt, GroupedDurabilityWriter
from root_change_wal import LocalMutation, RootChange, RootChangeOwner, RootChangeWal
from i18n import t
from reasoning_effort import normalize_reasoning_effort
from permission import normalize_permission, default_permission_for_kind
# `worker_store` is imported lazily inside `list_sessions` — the
# single call site. Keeping it lazy here lets `worker_store` and
# `orchs/base` import `session_manager` at top level without a
# session_manager → session_store → orchs.manager.* → session_manager
# cycle.

from paths import ba_home

_logger = logging.getLogger(__name__)

SCHEMA_VERSION = 12


# ── User-initiation taxonomy ──────────────────────────────────────────
#
# `user_initiated` distinguishes sessions the user is AWARE of having
# created (so they expect to see / own them) from sessions the system or
# an agent spun up on its own. It is ORTHOGONAL to the free-form `source`
# label: `source` is coerced to ("web","cli","import") and an agent's
# standalone session reuses source="cli", so `source` alone can never
# tell a human CLI session from an agent-spawned one. Each creation
# pathway stamps `user_initiated` explicitly.
#
# User-initiated (True) — the user explicitly asked for this session, or
#   approved a popup to create it:
#     • UI / CLI `POST /api/sessions`
#     • native import
#     • file-editor / prompt-engineer sessions (user clicked)
#     • user-made forks
#     • fresh-worker creation the user APPROVED via the "ask" popup
#
# Non-user-initiated (False) — created by the system or an agent without
#   the user being aware:
#     • agent tools: create_session / create_sub_session / delegate_task
#     • worker creation under "approve"/"deny" policy (no popup shown)
#     • provisioned helper sessions (search / board workers, source=internal)
#     • extension-created sessions
#     • internal forks: delegate_fork / adv_sync_fork / supervisor_worker
#       / sub_session
#
# Backfill heuristic for legacy records lives in `_migrate_session`.

# Session origin labels we preserve on disk. `user_initiated` is the
# authoritative user-awareness bit; `source` remains a coarse origin label
# for filters/badges/debugging.
_VALID_SESSION_SOURCES = frozenset({"web", "cli", "import", "extension", "internal"})

# `source` values that are unambiguously NOT user-aware. Used only by the
# legacy backfill heuristic — live creation paths pass `user_initiated`
# explicitly.
_NON_USER_INITIATED_SOURCES = frozenset({
    "internal", "extension", "subprocess_agent", "provisioning",
})

# `kind` values that are never the session the user thinks they created.
_NON_USER_INITIATED_KINDS = frozenset({
    "delegate_fork", "supervisor_worker", "sub_session", "adv_sync_fork",
})


def _infer_user_initiated(session: dict) -> bool:
    """Best-effort backfill for records written before `user_initiated`
    existed. Errs toward the safe signals we DO have (kind + source);
    cannot perfectly recover agent-created standalone sessions that
    historically reused source="cli", so those default to True."""
    if bool(session.get("is_delegate_fork", False)):
        return False
    if session.get("kind", "user") in _NON_USER_INITIATED_KINDS:
        return False
    if (session.get("source") or "web") in _NON_USER_INITIATED_SOURCES:
        return False
    if session.get("working_mode") in (
        "search_worker", "ask_singleton", "assistant_board",
    ):
        return False
    return True


_SESSIONS_DIR: Path | None = None
_SESSIONS_DIR_READY = False
_SESSIONS_DIR_READY_LOCK = threading.Lock()
_ROUTINE_SESSIONS_DIR_NAME = "routine-sessions"
_STORAGE_SEGMENT_MAX = 128
_root_file_dirs: dict[str, Path] = {}
_root_file_dirs_lock = threading.Lock()


def _sessions_dir() -> Path:
    global _SESSIONS_DIR, _SESSIONS_DIR_READY
    resolved = ba_home() / "sessions"
    if _SESSIONS_DIR == resolved:
        return resolved
    with _SESSIONS_DIR_READY_LOCK:
        if _SESSIONS_DIR == resolved:
            return resolved
        _SESSIONS_DIR = resolved
        _SESSIONS_DIR_READY = False
        _reset_home_scoped_caches()
        return resolved


def _ensure_dir():
    global _SESSIONS_DIR_READY
    sessions_dir = _sessions_dir()
    if _SESSIONS_DIR_READY:
        return
    with _SESSIONS_DIR_READY_LOCK:
        if _SESSIONS_DIR_READY:
            return
        sessions_dir.mkdir(parents=True, exist_ok=True)
        _SESSIONS_DIR_READY = True


def _routine_sessions_dir() -> Path:
    return ba_home() / _ROUTINE_SESSIONS_DIR_NAME


def _validate_storage_segment(value: object, field: str) -> str:
    segment = str(value or "").strip()
    if not segment:
        raise ValueError(f"{field} is required")
    if len(segment) > _STORAGE_SEGMENT_MAX:
        raise ValueError(f"{field} is too long")
    if segment in (".", "..") or "/" in segment or "\\" in segment:
        raise ValueError(f"{field} must be a single path segment")
    return segment


def _normalize_storage_scope(storage_scope: Optional[dict]) -> Optional[dict]:
    if storage_scope is None:
        return None
    if not isinstance(storage_scope, dict):
        raise ValueError("storage_scope must be an object")
    kind = str(storage_scope.get("kind") or "").strip()
    if not kind:
        return None
    if kind != "routine":
        raise ValueError(f"unsupported storage_scope kind: {kind}")
    routine_id = _validate_storage_segment(storage_scope.get("routine_id"), "routine_id")
    return {"kind": "routine", "routine_id": routine_id}


def _storage_dir_for_scope(storage_scope: Optional[dict]) -> Path:
    scope = _normalize_storage_scope(storage_scope)
    if scope is None:
        return _sessions_dir()
    if scope["kind"] == "routine":
        return _routine_sessions_dir() / scope["routine_id"]
    raise ValueError(f"unsupported storage_scope kind: {scope['kind']}")


def _remember_root_file_dir(root_id: str, directory: Path) -> None:
    if not root_id:
        return
    with _root_file_dirs_lock:
        _root_file_dirs[root_id] = directory


def _root_file_path(root_id: str) -> Path:
    if not root_id:
        return _sessions_dir() / ".missing.json"
    with _root_file_dirs_lock:
        directory = _root_file_dirs.get(root_id)
    if directory is not None:
        cached = directory / f"{root_id}.json"
        if cached.exists():
            return cached
        with _root_file_dirs_lock:
            if _root_file_dirs.get(root_id) == directory:
                _root_file_dirs.pop(root_id, None)
    path = _sessions_dir() / f"{root_id}.json"
    if path.exists():
        _remember_root_file_dir(root_id, path.parent)
        return path
    routine_root = _routine_sessions_dir()
    try:
        routine_dirs = list(routine_root.iterdir())
    except OSError:
        routine_dirs = []
    for directory in routine_dirs:
        if not directory.is_dir():
            continue
        candidate = directory / f"{root_id}.json"
        if candidate.exists():
            _remember_root_file_dir(root_id, directory)
            return candidate
    return path


# ── Fork index ────────────────────────────────────────────────────────
#
# In-memory map of fork_id → root_id. Roots are NOT in this map (a sid
# absent from the index resolves to itself). Loaded lazily on first
# resolve, mutated by every fork_session / delete that touches a fork.

_fork_index: dict[str, str] = {}
_root_forks: dict[str, set[str]] = {}
FileSignature = tuple[int, int, int, int, int]
DirFingerprint = tuple[int, int, int, int, int]
_FORK_INDEX_SIDECAR_SCHEMA_VERSION = 3
_root_index_signatures: dict[str, FileSignature] = {}
_index_loaded = False
_index_lock = threading.Lock()
# Stat-only signature of the sessions dir at the last full scan:
# (file_count, 0, 0, total_size, identity_mix). `_refresh_index` compares
# the live signature against this and skips the expensive parse-every-
# file rescan when the dir is byte-for-byte unchanged.
_index_fingerprint: Optional[DirFingerprint] = None
_index_generation = 0
_index_build_lock = threading.Lock()
_negative_root_resolve_cache: dict[str, DirFingerprint] = {}
_negative_root_resolve_until: dict[str, float] = {}
_negative_root_resolve_global_until = 0.0
_index_refresh_attempt_until: dict[DirFingerprint, float] = {}
_index_refresh_global_attempt_until = 0.0
_NEGATIVE_ROOT_RESOLVE_TTL_SECONDS = 0.75
_DIR_FINGERPRINT_CACHE_TTL_SECONDS = 0.10
_INDEX_INCREMENTAL_REFRESH_MAX_CHANGED = 32
_dir_fingerprint_cache: tuple[float, DirFingerprint] | None = None
_dir_fingerprint_cache_lock = threading.Lock()


def _fingerprint_after_root_write_locked(
    previous_signature: FileSignature | None,
    file_signature: FileSignature,
    root_id: str,
) -> DirFingerprint | None:
    if _index_fingerprint is None:
        return None
    count, _, _, total_size, identity_mix = _index_fingerprint
    if previous_signature is None:
        count += 1
        total_size += file_signature[4]
    else:
        total_size += file_signature[4] - previous_signature[4]
        identity_mix ^= _file_signature_mix(previous_signature, root_id)
    identity_mix ^= _file_signature_mix(file_signature, root_id)
    return count, 0, 0, total_size, identity_mix


def _fingerprint_after_root_delete_locked(
    file_signature: FileSignature,
    root_id: str,
) -> DirFingerprint | None:
    if _index_fingerprint is None:
        return None
    count, _, _, total_size, identity_mix = _index_fingerprint
    return (
        max(0, count - 1),
        0,
        0,
        max(0, total_size - file_signature[4]),
        identity_mix ^ _file_signature_mix(file_signature, root_id),
    )


def _file_signature_mix(signature: FileSignature, root_id: str = "") -> int:
    raw = (root_id + ":" + ":".join(str(part) for part in signature)).encode("utf-8")
    return int.from_bytes(hashlib.blake2b(raw, digest_size=8).digest(), "big")


def _bump_index_generation_locked() -> int:
    global _index_generation
    _index_generation += 1
    return _index_generation


def _publish_dir_fingerprint_cache(
    fingerprint: DirFingerprint,
    generation: int,
) -> bool:
    global _dir_fingerprint_cache
    wait_started = time.perf_counter()
    with _dir_fingerprint_cache_lock:
        acquired_at = time.perf_counter()
        perf.record(
            "store.session.dir_fingerprint.publish.lock_wait",
            (acquired_at - wait_started) * 1000.0,
        )
        with _index_lock:
            if (
                generation != _index_generation
                or fingerprint != _index_fingerprint
            ):
                perf.record_count("store.session.dir_fingerprint.publish.stale")
                perf.record(
                    "store.session.dir_fingerprint.publish.lock_hold",
                    (time.perf_counter() - acquired_at) * 1000.0,
                )
                return False
            _dir_fingerprint_cache = (time.monotonic(), fingerprint)
        perf.record(
            "store.session.dir_fingerprint.publish.lock_hold",
            (time.perf_counter() - acquired_at) * 1000.0,
        )
    return True

# ── Summary index ─────────────────────────────────────────────────────
#
# In-memory dict of root_session_id → summary_dict. Writers update their
# own entry directly (O(1)) instead of bumping a generation counter that
# forces an O(n) full-disk-rebuild. Replaces the former generation-counter
# cache that had ~87% miss rate under write load because every
# `write_session_full` invalidated it.
#
# INVARIANT: every writer that changes session-summary-visible state MUST
# call `_upsert_summary(root)` or `_remove_summary(root_id)` — not a
# generation bump. The index is always authoritative; `list_sessions()`
# reads directly from it (sorted copy) with zero disk I/O.
#
# INVARIANT: summary dicts are SHARED references — callers must not mutate
# them. `list_sessions()` returns a shallow copy of the sorted list.
_summary_index: dict[str, dict] = {}
_summary_index_lock = threading.Lock()
_summary_index_loaded = False
_summary_index_version = 0
_summary_order_version = 0
_summary_visibility_version = 0
_summary_metadata_version = 0
_summary_sorted_cache_version = -1
_summary_sorted_id_cache: list[str] = []
_summary_sorted_id_caches: dict[tuple[str, bool], tuple[int, list[str]]] = {}
_sidebar_page_projections: collections.OrderedDict[
    tuple[str, str | None, bool, int, int], tuple[str, ...]
] = collections.OrderedDict()
_SIDEBAR_PAGE_PROJECTIONS_MAX = 16
_requirement_tags_by_session: dict[str, list[dict]] = {}
_requirement_tags_lock = threading.Lock()
# Per-session extension attention markers: sid -> {extension_id -> marker}.
# Durable: owned via session_manager mutators, persisted atomically to
# `attention_markers.json` on every mutation and lazily loaded on first
# access so markers survive backend restarts.
_markers_by_session: dict[str, dict[str, dict]] = {}
_markers_lock = threading.RLock()
_markers_loaded = False
_summary_projection_repair_lock = threading.Lock()
_summary_projection_repair_running = False
_metadata_trigram_index_version = -1
_metadata_trigram_index: dict[str, dict[str, set[str]]] = {}
_metadata_trigram_index_warm_running = False
_metadata_trigram_index_warm_lock = threading.Lock()
_migrated_root_cache: dict[tuple[str, FileSignature], dict] = {}
_migrated_root_cache_lock = threading.Lock()
_MIGRATED_ROOT_CACHE_MAX = 32
# Injected by `session_manager` at singleton construction (no
# session_store → session_manager import — see the circular-import note
# near the top of this file). Serializes any write this module makes to
# a root ID that `session_manager` doesn't already hold locked, and
# skips the write outright when the root is currently resident in
# `session_manager`'s in-memory cache — a resident root is the live
# authority; overwriting its file from an unlocked, possibly-stale
# snapshot silently clobbers concurrent in-memory mutations (e.g. a
# live turn's just-appended assistant message). See `_migrate_and_persist`.
_root_writer_guard: Optional[Callable[[str, Callable[[], None]], None]] = None


def register_root_writer_guard(
    fn: Callable[[str, Callable[[], None]], None],
) -> None:
    global _root_writer_guard
    _root_writer_guard = fn
_index_sidecar_write_queue: queue.Queue[
    tuple[
        DirFingerprint,
        dict[str, str],
        dict[str, set[str]],
        dict[str, FileSignature],
    ] | None
] = queue.Queue(maxsize=1)
_index_sidecar_write_started = False
_index_sidecar_write_lock = threading.Lock()
_durability_writer: GroupedDurabilityWriter | None = None
_durability_writer_lock = threading.Lock()
_root_change_owner: RootChangeOwner | None = None
_root_change_owner_lock = threading.Lock()
_summary_sidecar_write_queue: queue.Queue[
    tuple[str, dict, int | None, FileSignature | None] | None
] = queue.Queue(maxsize=256)
_summary_sidecar_write_started = False
_summary_sidecar_write_lock = threading.Lock()
_opened_cache_lock = threading.Lock()
_opened_cache: dict[
    str,
    tuple[tuple[int, int, int, int] | None, dict[str, str]],
] = {}


def _get_durability_writer() -> GroupedDurabilityWriter:
    global _durability_writer
    if _durability_writer is not None:
        return _durability_writer
    with _durability_writer_lock:
        if _durability_writer is None:
            _durability_writer = GroupedDurabilityWriter(
                max_batch_age_s=0,
                signature_resolver=_session_file_signature,
                thread_name="session-store-durability",
            )
        return _durability_writer


def _wait_durability(receipt: DurabilityReceipt):
    started = time.perf_counter()
    acknowledged = receipt.wait()
    perf.record(
        "store.session.durability_ack_wait",
        (time.perf_counter() - started) * 1000.0,
    )
    if acknowledged < receipt.generation:
        raise RuntimeError("session-store durability acknowledgement regressed")
    return receipt.signature


def shutdown_durability_writer() -> None:
    global _durability_writer, _index_sidecar_write_started
    if _index_sidecar_write_started:
        _index_sidecar_write_queue.join()
        _index_sidecar_write_queue.put(None)
        _index_sidecar_write_queue.join()
        with _index_sidecar_write_lock:
            _index_sidecar_write_started = False
    with _durability_writer_lock:
        writer = _durability_writer
        _durability_writer = None
    if writer is not None:
        writer.close()


def _apply_root_change(change: RootChange) -> bool | None:
    # Global sidecar files (e.g. attention_markers.json) are not session roots but
    # can appear in the root-change WAL from older disk scans. Rejecting them would
    # poison the owner startup (wait_ready raises on any rejected projection); ignore
    # them instead so the checkpoint advances past the entry without indexing it.
    if _is_sidecar_json(change.path.name):
        return None
    path = change.path
    try:
        parent = path.parent.resolve(strict=True)
        allowed = {
            directory.resolve(strict=True)
            for directory in _session_storage_dirs()
        }
    except OSError:
        return False
    if parent not in allowed or path.name != f"{change.root_id}.json":
        perf.record_count("store.session.root_change_wal.rejected_path")
        return False
    _remember_root_file_dir(change.root_id, path.parent)
    if change.kind == "upsert":
        applied = project_external_root_change(change.root_id)
        if applied is False and _session_file_signature(path) is None:
            applied = True
    else:
        project_external_root_delete(change.root_id)
        applied = True
    _index_sidecar_write_queue.join()
    return applied


def start_root_change_owner() -> None:
    global _root_change_owner
    with _root_change_owner_lock:
        if _root_change_owner is not None:
            return
        owner = RootChangeOwner(
            wal=RootChangeWal(ba_home() / "indexes" / "root-changes.sqlite3"),
            roots=lambda: tuple(_session_storage_dirs()),
            apply=_apply_root_change,
            accept_path=lambda path: path.suffix == ".json" and not _is_sidecar_json(path.name),
        )
        owner.start()
        _root_change_owner = owner


def shutdown_root_change_owner() -> None:
    global _root_change_owner
    with _root_change_owner_lock:
        owner, _root_change_owner = _root_change_owner, None
    if owner is not None:
        owner.stop()


def _begin_root_change(kind: str, root_id: str, path: Path) -> LocalMutation | None:
    owner = _root_change_owner
    if owner is None:
        return None
    if kind == "upsert":
        return owner.begin_local_upsert(root_id, path)
    return owner.begin_local_delete(root_id, path)


def _durable_root_change(
    mutation: LocalMutation | None,
    signature: FileSignature | None,
) -> RootChange | None:
    if mutation is None:
        return None
    assert _root_change_owner is not None
    return _root_change_owner.durable_local(mutation, signature)


def _complete_root_change(change: RootChange | None) -> None:
    if change is not None:
        assert _root_change_owner is not None
        _root_change_owner.complete_local(change)


def _abandon_root_change(mutation: LocalMutation | None) -> None:
    if mutation is not None:
        assert _root_change_owner is not None
        _root_change_owner.abandon_local(mutation)


def _wait_root_change_owner_ready() -> None:
    owner = _root_change_owner
    if owner is not None:
        owner.wait_ready()


def _wait_root_change_observation(generation: int, timeout: float = 0.05) -> bool:
    owner = _root_change_owner
    if owner is None:
        return False
    started = time.perf_counter()
    observed = owner.wait_for_observation(generation, timeout)
    perf.record(
        "store.session.root_change_watcher.resolve_observation_wait",
        (time.perf_counter() - started) * 1000.0,
    )
    perf.record_count(
        "store.session.root_change_watcher.resolve_observation_observed"
        if observed else "store.session.root_change_watcher.resolve_observation_timeout"
    )
    return observed
_OPENED_CACHE_MAX = 256
_summary_roots_fingerprint: tuple[str, ...] = ()


def _reset_home_scoped_caches() -> None:
    global _index_loaded, _index_fingerprint, _dir_fingerprint_cache
    global _summary_index_loaded, _summary_index_version, _summary_order_version
    global _summary_visibility_version
    global _summary_metadata_version, _summary_sorted_cache_version
    global _metadata_trigram_index_version, _summary_roots_fingerprint

    with _index_lock:
        _fork_index.clear()
        _root_forks.clear()
        _root_index_signatures.clear()
        _index_loaded = False
        _index_fingerprint = None
        _clear_negative_root_resolve_cache()
        _bump_index_generation_locked()
    with _root_file_dirs_lock:
        _root_file_dirs.clear()
    with _dir_fingerprint_cache_lock:
        _dir_fingerprint_cache = None
    with _summary_index_lock:
        _summary_index.clear()
        _summary_index_loaded = False
        _summary_index_version += 1
        _summary_order_version += 1
        _summary_visibility_version += 1
        _summary_metadata_version += 1
        _summary_sorted_cache_version = -1
        _summary_sorted_id_cache.clear()
        _summary_sorted_id_caches.clear()
        _sidebar_page_projections.clear()
    with _requirement_tags_lock:
        _requirement_tags_by_session.clear()
    global _markers_loaded
    with _markers_lock:
        _markers_by_session.clear()
        _markers_loaded = False
    _metadata_trigram_index_version = -1
    _metadata_trigram_index.clear()
    with _migrated_root_cache_lock:
        _migrated_root_cache.clear()
    with _opened_cache_lock:
        _opened_cache.clear()
    _summary_roots_fingerprint = ()


def _clear_negative_root_resolve_cache() -> None:
    global _negative_root_resolve_global_until, _index_refresh_global_attempt_until
    _negative_root_resolve_cache.clear()
    _negative_root_resolve_until.clear()
    _index_refresh_attempt_until.clear()
    _negative_root_resolve_global_until = 0.0
    _index_refresh_global_attempt_until = 0.0


def _copy_jsonish(value):
    if isinstance(value, dict):
        return {k: _copy_jsonish(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_copy_jsonish(v) for v in value]
    return value
_SUMMARY_INDEX_CACHE_VERSION = 2
# Single-flights the one-time summary-index build. Held ONLY by
# `_ensure_summary_index` and acquired by nothing else, so it can never be
# the inner lock of a cycle. The build runs under THIS lock — never under
# `_summary_index_lock` — so the build can freely call `list_workers`
# (which takes `worker_store._lock_for()`) and `write_session_full`
# without forming the `_summary_index_lock <-> _lock_for(cwd)` ABBA or the
# `_summary_index_lock` self-re-entry that `_upsert_summary` would cause.
_summary_build_lock = threading.Lock()


def wait_for_summary_index(
    timeout_seconds: float,
    *,
    min_published: int | None = None,
) -> bool:
    if _summary_index_loaded:
        return True
    _ensure_summary_index(blocking=False)
    if _summary_index_loaded:
        return True
    if min_published is not None:
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        target = max(1, int(min_published))
        while time.monotonic() < deadline:
            with _summary_index_lock:
                if len(_summary_index) >= target:
                    return False
            time.sleep(0.005)
        return False
    acquired = _summary_build_lock.acquire(timeout=max(0.0, timeout_seconds))
    if not acquired:
        return False
    _summary_build_lock.release()
    return _summary_index_loaded


def summary_index_snapshot_complete() -> bool:
    return _summary_index_loaded


def summary_index_has_roots_on_disk() -> bool:
    try:
        next(_session_json_files())
        return True
    except StopIteration:
        return False


def _replace_summary_projection_field(
    session_id: str,
    field: str,
    value: object,
) -> bool:
    global _summary_index_version
    updated: dict | None = None
    with _summary_index_lock:
        summary = _summary_index.get(session_id)
        if summary is None:
            return False
        if summary.get(field) == value:
            updated = dict(summary)
        else:
            updated = {**summary, field: value}
            _summary_index[session_id] = updated
            _summary_index_version += 1
    if updated is not None:
        try:
            _write_summary_file(session_id, updated)
        except Exception:
            pass
    return True


def _summary_metadata_changed(before: Optional[dict], after: dict) -> bool:
    if before is None:
        return True
    return (
        before.get("name") != after.get("name")
        or before.get("first_prompt") != after.get("first_prompt")
    )


@lru_cache(maxsize=4096)
def _timestamp_sort_value_str(value: str) -> float:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        return datetime.fromisoformat(normalized).timestamp()
    except ValueError:
        return 0.0


def timestamp_sort_value(value: object) -> float:
    if not isinstance(value, str) or not value:
        return 0.0
    return _timestamp_sort_value_str(value)


def _newer_timestamp(left: str, right: str) -> str:
    return left if timestamp_sort_value(left) >= timestamp_sort_value(right) else right


def _walk_forks(node: dict) -> Iterator[dict]:
    """Yield every fork dict reachable from `node` (depth-first, includes
    nested forks). Does NOT yield `node` itself."""
    for child in node.get("forks") or []:
        yield child
        yield from _walk_forks(child)


# ── Summary index helpers ────────────────────────────────────────────


def _projection_snapshot() -> tuple[dict[str, list[dict]], dict[str, dict[str, dict]]]:
    with _requirement_tags_lock:
        requirement_tags = {
            sid: list(tags)
            for sid, tags in _requirement_tags_by_session.items()
        }
    with _markers_lock:
        _ensure_markers_loaded_locked()
        markers = {
            sid: {k: dict(v) for k, v in per.items()}
            for sid, per in _markers_by_session.items()
        }
    return requirement_tags, markers


_SUMMARY_PROJECTION_FIELDS = (
    "pending_eng_session_id",
    "current_todos",
    "current_tasks",
    "requirement_tags",
    "markers",
    "folder_id",
    "session_tags",
)


def current_turn_error(session: dict) -> Optional[str]:
    """Durable "does the latest turn have an unseen error" derivation.

    The latest message (any role) is authoritative:
    - assistant: its `errorText`/`error` fields (set by
      `set_assistant_error` / `set_msg_retrying_until`).
    - user: its `errorText` when `status == "error"` — this is the
      shape left behind when `_finalize_turn_messages` hits its
      exception path and calls `remove_assistant_msg`, so the failed
      turn has no assistant message at all and the user message is
      the only durable record of the failure.

    Only falls back to the `unseen_error` flag when there is no
    message history yet to derive from."""
    for msg in reversed(session.get("messages") or []):
        role = msg.get("role")
        if role == "assistant":
            if not msg.get("error"):
                return None
            text = msg.get("errorText")
            return str(text) if text else "error"
        if role == "user":
            if msg.get("status") != "error":
                return None
            text = msg.get("errorText")
            return str(text) if text else "error"
    error = session.get("unseen_error")
    return str(error) if error else None


def _model_history_for_root(root: dict) -> list[str]:
    seen: set[str] = set()
    models: list[str] = []

    def add_model(value: object) -> None:
        model = str(value or "").strip()
        if not model or model in seen:
            return
        seen.add(model)
        models.append(model)

    for msg in root.get("messages", []):
        if not isinstance(msg, dict):
            continue
        for event in msg.get("events") or []:
            if not isinstance(event, dict) or event.get("type") != "model_switched":
                continue
            data = event.get("data")
            if not isinstance(data, dict):
                continue
            add_model(data.get("previous_model"))
            add_model(data.get("model"))
    add_model(root.get("model", ""))
    return models


def _build_summary_for_root(
    root: dict,
    projection_snapshot: tuple[dict[str, list[dict]], dict[str, dict[str, dict]]] | None = None,
    organization_projection: tuple[dict, dict] | None = None,
) -> dict:
    """Extract sidebar-visible summary fields from a root session dict.
    Mirrors the per-root block in the old `_build_session_list`."""
    cwd = root.get("cwd", "")
    import session_organization_store
    user_fork_ids: list[str] = []
    for fork in _walk_forks(root):
        if fork.get("kind", "user") == "user":
            fork_id = fork.get("id")
            if fork_id:
                user_fork_ids.append(fork_id)
    _msgs = root.get("messages", [])
    _user_msg_count = sum(1 for msg in _msgs if msg.get("role") == "user")
    _last_msg_ts = _msgs[-1].get("timestamp", "") if _msgs else ""
    _stored_updated = root.get("updated_at", "")
    _effective_updated = (
        _newer_timestamp(_stored_updated, _last_msg_ts)
        if _last_msg_ts else _stored_updated
    )
    if projection_snapshot is None:
        requirement_tags = _requirement_tags_for_session(root["id"])
        markers = _markers_for_session(root["id"])
    else:
        tags_by_session, markers_by_session = projection_snapshot
        requirement_tags = list(tags_by_session.get(root["id"], []))
        markers = {
            k: dict(v)
            for k, v in markers_by_session.get(root["id"], {}).items()
        }
    summary = {
        "id": root["id"],
        "name": root.get("name") or t("session.untitled"),
        "model": root.get("model", ""),
        "model_history": _model_history_for_root(root),
        "reasoning_effort": root.get("reasoning_effort", ""),
        "permission": root.get("permission", {}),
        "provider_id": root.get("provider_id"),
        "cwd": cwd,
        "cwd_explicit": root.get("cwd_explicit", True),
        "all_projects": bool(root.get("all_projects", False)),
        "node_id": root.get("node_id") or "primary",
        "created_at": root.get("created_at", ""),
        "updated_at": _effective_updated,
        "last_user_prompt_at": _last_user_prompt_timestamp(root),
        "last_opened_at": root.get("last_opened_at", ""),
        "message_count": _user_msg_count,
        "first_prompt": _first_user_prompt(root),
        "last_seen_event_uid": root.get("last_seen_event_uid"),
        "unseen_error": current_turn_error(root),
        "orchestration_mode": _normalize_orchestration_mode(
            root.get("orchestration_mode")
        ),
        "worker_creation_policy": root.get("worker_creation_policy", "ask"),
        "bare_config": bool(root.get("bare_config", False)),
        "source": root.get("source", "web"),
        "user_initiated": bool(root.get("user_initiated", _infer_user_initiated(root))),
        "kind": root.get("kind", "user"),
        "agent_session_id": root.get("agent_session_id"),
        "supervisor_agent_session_id": root.get("supervisor_agent_session_id"),
        "parent_session_id": None,
        "forked_from_agent_sid": root.get("forked_from_agent_sid"),
        "fork_point_seq": None,
        "fork_closed": False,
        "fork_count": len(user_fork_ids),
        "fork_ids": user_fork_ids,
        "supervisor_enabled": root.get("supervisor_enabled", False),
        "supervisor_custom_prompt": root.get("supervisor_custom_prompt", ""),
        "continuation_chain": root.get("continuation_chain", []),
        "is_prompt_engineering": bool(root.get("working_mode") == "prompt_engineering"),
        "working_mode": root.get("working_mode"),
        "working_mode_meta": root.get("working_mode_meta"),
        "pending_eng_session_id": None,
        "worker_count": _worker_summary_count(),
        "requirement_tags": requirement_tags,
        "markers": markers,
        "current_todos": list(root.get("current_todos") or []),
        "current_tasks": list(root.get("current_tasks") or []),
        "pinned": bool(root.get("pinned", False)),
        "topbar_pinned": bool(root.get("topbar_pinned", False)),
        "topbar_pinned_at": root.get("topbar_pinned_at"),
        "archived": bool(root.get("archived", False)),
        "worker_eligible": bool(root.get("worker_eligible", False)),
        "moved_to_session_id": root.get("moved_to_session_id"),
        "moved_from_session_id": root.get("moved_from_session_id"),
    }
    if organization_projection is None:
        summary = session_organization_store.enrich_session_summary(summary)
    else:
        summary = session_organization_store.enrich_session_summary_from_projection(
            summary,
            organization_projection[0],
            organization_projection[1],
        )
    summary["tag_filter_ids"] = _tag_filter_ids(
        summary.get("session_tags") or [],
        requirement_tags,
    )
    return summary


def _build_summary_for_root_preserving_projections(root: dict, existing: dict) -> dict:
    requirement_tags = existing.get("requirement_tags") or []
    summary = _build_summary_for_root(
        root,
        projection_snapshot=(
            {root["id"]: requirement_tags},
            {root["id"]: existing.get("markers") or {}},
        ),
        organization_projection=(
            {
                root["id"]: {
                    "folder_id": existing.get("folder_id"),
                    "tag_ids": [
                        tag.get("id")
                        for tag in existing.get("session_tags") or []
                        if isinstance(tag, dict) and isinstance(tag.get("id"), str)
                    ],
                }
            },
            {
                tag.get("id"): tag
                for tag in existing.get("session_tags") or []
                if isinstance(tag, dict)
            },
        ),
    )
    for field in _SUMMARY_PROJECTION_FIELDS:
        if field in existing:
            summary[field] = existing[field]
    summary["tag_filter_ids"] = _tag_filter_ids(
        summary.get("session_tags") or [],
        summary.get("requirement_tags") or [],
    )
    return summary


def _tag_filter_ids(session_tags: list[dict], requirement_tags: list[dict]) -> list[str]:
    ids: set[str] = set()
    for tag in session_tags:
        if not isinstance(tag, dict):
            continue
        tag_id = tag.get("id")
        if isinstance(tag_id, str) and tag_id:
            ids.add(tag_id)
    for tag in requirement_tags:
        if not isinstance(tag, dict):
            continue
        kind = tag.get("kind")
        tag_id = tag.get("id")
        if isinstance(kind, str) and isinstance(tag_id, str) and kind and tag_id:
            ids.add(f"req:{kind}:{tag_id}")
    return sorted(ids)


def set_requirement_tags_projection(tags_by_session: dict[str, list[dict]]) -> None:
    global _summary_index_version
    clean: dict[str, list[dict]] = {}
    for sid, tags in tags_by_session.items():
        if isinstance(sid, str) and isinstance(tags, list):
            clean[sid] = [tag for tag in tags if isinstance(tag, dict)]
    with _requirement_tags_lock:
        previous = set(_requirement_tags_by_session)
        _requirement_tags_by_session.clear()
        _requirement_tags_by_session.update(clean)
    changed = previous | set(clean)
    with _summary_index_lock:
        if _summary_index_loaded:
            for sid in changed:
                summary = _summary_index.get(sid)
                if summary is None:
                    continue
                tags = clean.get(sid, [])
                if summary.get("requirement_tags") != tags:
                    _summary_index[sid] = {
                        **summary,
                        "requirement_tags": tags,
                        "tag_filter_ids": _tag_filter_ids(
                            summary.get("session_tags") or [],
                            tags,
                        ),
                    }
                    _summary_index_version += 1
        else:
            _summary_index_version += 1


def _requirement_tags_for_session(session_id: str) -> list[dict]:
    with _requirement_tags_lock:
        return list(_requirement_tags_by_session.get(session_id, []))


def _markers_path() -> Path:
    return _sessions_dir() / "attention_markers.json"


def _ensure_markers_loaded_locked() -> None:
    """Load the persisted marker map on first access. Caller MUST hold
    ``_markers_lock``."""
    global _markers_loaded
    if _markers_loaded:
        return
    _markers_loaded = True
    try:
        raw = json.loads(_markers_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return
    if not isinstance(raw, dict):
        return
    for sid, per in raw.items():
        if not (isinstance(sid, str) and isinstance(per, dict)):
            continue
        clean = {
            ext_id: dict(marker)
            for ext_id, marker in per.items()
            if isinstance(ext_id, str) and isinstance(marker, dict)
        }
        if clean:
            _markers_by_session[sid] = clean


def _write_markers_locked() -> None:
    """Atomically persist the marker map. Caller MUST hold ``_markers_lock``.
    Non-fatal on failure — the in-memory map stays authoritative and the
    next mutation rewrites the file."""
    try:
        _ensure_dir()
        path = _markers_path()
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(_markers_by_session, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(tmp, path)
    except OSError:
        pass


def set_marker_projection(sid: str, extension_id: str, marker: Optional[dict]) -> None:
    """Set or clear one extension's marker on a session. ``marker=None``
    drops the key. Persists the map and bumps the summary version so list
    snapshots refresh."""
    global _summary_index_version
    if not (isinstance(sid, str) and isinstance(extension_id, str)):
        return
    with _markers_lock:
        _ensure_markers_loaded_locked()
        per = _markers_by_session.setdefault(sid, {})
        if marker is None:
            per.pop(extension_id, None)
            if not per:
                _markers_by_session.pop(sid, None)
        else:
            per[extension_id] = dict(marker)
        current = {k: dict(v) for k, v in _markers_by_session.get(sid, {}).items()}
        _write_markers_locked()
    _replace_summary_projection_field(sid, "markers", current)
    with _summary_index_lock:
        if not _summary_index_loaded:
            _summary_index_version += 1


def _markers_for_session(session_id: str) -> dict[str, dict]:
    with _markers_lock:
        _ensure_markers_loaded_locked()
        return {k: dict(v) for k, v in _markers_by_session.get(session_id, {}).items()}


def _start_summary_projection_repair() -> None:
    global _summary_projection_repair_running
    with _summary_projection_repair_lock:
        if _summary_projection_repair_running:
            return
        _summary_projection_repair_running = True

    def _repair() -> None:
        global _summary_index_version, _summary_projection_repair_running
        try:
            while True:
                projection_snapshot = _projection_snapshot()
                tags_by_session, markers_by_session = projection_snapshot
                with _summary_index_lock:
                    pending = list(_summary_index.items())
                originals = dict(pending)
                updates: dict[str, dict] = {}
                retry = False
                for sid, summary in pending:
                    tags = list(tags_by_session.get(sid, []))
                    marker = {
                        k: dict(v)
                        for k, v in markers_by_session.get(sid, {}).items()
                    }
                    tag_filter_ids = _tag_filter_ids(summary.get("session_tags") or [], tags)
                    if (
                        summary.get("requirement_tags") == tags
                        and summary.get("markers") == marker
                        and summary.get("tag_filter_ids") == tag_filter_ids
                    ):
                        continue
                    updates[sid] = {
                        **summary,
                        "requirement_tags": tags,
                        "markers": marker,
                        "tag_filter_ids": tag_filter_ids,
                    }
                if not updates:
                    return
                changed = False
                with _summary_index_lock:
                    for sid, updated in updates.items():
                        current = _summary_index.get(sid)
                        if current is not originals.get(sid):
                            retry = True
                            continue
                        _summary_index[sid] = updated
                        changed = True
                    if changed:
                        _summary_index_version += 1
                if not retry:
                    return
        finally:
            with _summary_projection_repair_lock:
                _summary_projection_repair_running = False

    threading.Thread(
        target=_repair,
        name="summary-projection-repair",
        daemon=True,
    ).start()


def summary_version() -> int:
    with _summary_index_lock:
        return _summary_index_version


def summary_fields_many(
    sids: list[str] | tuple[str, ...],
    fields: set[str] | tuple[str, ...] | list[str],
) -> dict[str, dict]:
    _ensure_summary_index(blocking=False)
    wanted = tuple(fields)
    with _summary_index_lock:
        return {
            sid: {
                field: copy.deepcopy(summary.get(field))
                for field in wanted
            }
            for sid in sids
            if isinstance(sid, str)
            and (summary := _summary_index.get(sid)) is not None
        }


def summary_order_version() -> int:
    with _summary_index_lock:
        return _summary_order_version


def summary_index_version() -> int:
    with _summary_index_lock:
        return _summary_index_version


def _summary_order_key(
    summary: Optional[dict], folder_view: bool = False,
) -> tuple[bool, bool, float]:
    return _summary_sort_key(summary, "updated_at", folder_view)


def _summary_sort_key(
    summary: Optional[dict], sort_by: str, folder_view: bool = False,
) -> tuple[bool, bool, float]:
    if not summary:
        return (False, False, 0.0)
    return (
        bool(summary.get("folder_id")) if folder_view else False,
        bool(summary.get("pinned", False)),
        timestamp_sort_value(summary.get(sort_by)),
    )


def _summary_order_changed(before: Optional[dict], after: dict) -> bool:
    if before is None:
        return True
    sort_fields = ("updated_at", "last_user_prompt_at", "last_opened_at")
    return (
        bool(before.get("pinned", False)) != bool(after.get("pinned", False))
        or bool(before.get("folder_id")) != bool(after.get("folder_id"))
        or any(
            timestamp_sort_value(before.get(field)) != timestamp_sort_value(after.get(field))
            for field in sort_fields
        )
    )


def _summary_visibility_value(summary: Optional[dict]) -> tuple[object, ...]:
    if not summary:
        return ()
    meta = summary.get("working_mode_meta")
    return (
        bool(summary.get("archived")),
        summary.get("working_mode"),
        bool(meta.get("persistent")) if isinstance(meta, dict) else False,
        summary.get("cwd"),
        bool(summary.get("all_projects")),
    )


def _summary_visibility_changed(before: Optional[dict], after: dict) -> bool:
    return _summary_visibility_value(before) != _summary_visibility_value(after)


def _summary_visible_in_sidebar(summary: dict, project_path: str | None) -> bool:
    if summary.get("archived"):
        return False
    working_mode = summary.get("working_mode")
    if working_mode:
        meta = summary.get("working_mode_meta")
        if working_mode != "file_editing" or not (
            isinstance(meta, dict) and meta.get("persistent")
        ):
            return False
    if project_path is None or summary.get("all_projects"):
        return True
    return summary.get("cwd") == project_path


def refresh_organization_projection(session_ids: Iterable[str] | None = None) -> None:
    global _summary_index_version
    import session_organization_store

    organization_projection = session_organization_store.enrichment_projection()
    requested = set(session_ids) if session_ids is not None else None
    updates: dict[str, dict] = {}
    with _summary_index_lock:
        if not _summary_index_loaded:
            return
        items = list(_summary_index.items())
    for sid, summary in items:
        if requested is not None and sid not in requested:
            continue
        updated = session_organization_store.enrich_session_summary_from_projection(
            summary,
            organization_projection[0],
            organization_projection[1],
        )
        updated["tag_filter_ids"] = _tag_filter_ids(
            updated.get("session_tags") or [],
            updated.get("requirement_tags") or [],
        )
        if updated != summary:
            updates[sid] = updated
    if not updates:
        return
    with _summary_index_lock:
        for sid, updated in updates.items():
            if _summary_index.get(sid) != updated:
                _summary_index[sid] = updated
                _summary_index_version += 1
    for sid, summary in updates.items():
        try:
            _write_summary_file(sid, summary)
        except Exception:
            pass


def search_metadata_version() -> int:
    with _summary_index_lock:
        return _summary_metadata_version


def markers_for_extension_purge(extension_id: str) -> list[str]:
    """Drop ``extension_id`` from every session's markers. Returns the
    affected session ids."""
    global _summary_index_version
    affected: list[str] = []
    current_by_sid: dict[str, dict[str, dict]] = {}
    with _markers_lock:
        _ensure_markers_loaded_locked()
        for sid in list(_markers_by_session):
            per = _markers_by_session[sid]
            if extension_id in per:
                per.pop(extension_id, None)
                affected.append(sid)
                if not per:
                    _markers_by_session.pop(sid, None)
                    current_by_sid[sid] = {}
                else:
                    current_by_sid[sid] = {k: dict(v) for k, v in per.items()}
        if affected:
            _write_markers_locked()
    if affected:
        with _summary_index_lock:
            for sid in affected:
                summary = _summary_index.get(sid)
                if summary is None:
                    continue
                marker = current_by_sid.get(sid, {})
                if summary.get("markers") == marker:
                    continue
                _summary_index[sid] = {**summary, "markers": marker}
                _summary_index_version += 1
            if not _summary_index_loaded:
                _summary_index_version += 1
    return affected


def _upsert_summary(
    root: dict,
    *,
    preserve_projection_fields: bool = False,
    root_mtime_ns: int | None = None,
    root_signature: FileSignature | None = None,
    sync_sidecar: bool = False,
) -> None:
    """Update the summary index entry for this root. Called by every writer
    that mutates session-summary-visible state."""
    global _summary_index_version, _summary_order_version, _summary_metadata_version
    global _summary_visibility_version
    if root_signature is None:
        root_signature = _session_file_signature(_root_file_path(root["id"]))
    existing = None
    if preserve_projection_fields:
        with _summary_index_lock:
            existing = _summary_index.get(root["id"])
    with perf.timed("store.session.summary.build"):
        if existing:
            summary = _build_summary_for_root_preserving_projections(root, existing)
        else:
            summary = _build_summary_for_root(root)
    with perf.timed("store.session.summary.index"):
        with _summary_index_lock:
            existing = _summary_index.get(root["id"])
            if preserve_projection_fields and existing:
                for field in _SUMMARY_PROJECTION_FIELDS:
                    if field in existing:
                        summary[field] = existing[field]
                summary["tag_filter_ids"] = _tag_filter_ids(
                    summary.get("session_tags") or [],
                    summary.get("requirement_tags") or [],
                )
            elif existing and existing.get("pending_eng_session_id"):
                summary["pending_eng_session_id"] = existing["pending_eng_session_id"]
            if existing == summary:
                summary_changed = False
            else:
                _summary_index[root["id"]] = summary
                _summary_index_version += 1
                if _summary_order_changed(existing, summary):
                    _summary_order_version += 1
                if _summary_visibility_changed(existing, summary):
                    _summary_visibility_version += 1
                summary_changed = True
                if _summary_metadata_changed(existing, summary):
                    _summary_metadata_version += 1
    # Write lightweight summary file AFTER the in-memory update. Uses
    # atomic write (tmpfile + os.replace) so a crash mid-write leaves the
    # previous file intact. Non-fatal — in-memory index is authoritative.
    try:
        sidecar_current = True
        if not summary_changed:
            with perf.timed("store.session.summary.sidecar_stat"):
                sidecar_current = _touch_summary_file_current(
                    root["id"],
                    summary=summary,
                    root_mtime_ns=root_mtime_ns,
                    root_signature=root_signature,
                )
        if summary_changed or not sidecar_current:
            if sync_sidecar:
                _write_summary_file(
                    root["id"],
                    summary,
                    root_mtime_ns=root_mtime_ns,
                    expected_root_signature=root_signature,
                )
            else:
                _schedule_summary_sidecar_write(
                    root["id"],
                    summary,
                    root_mtime_ns=root_mtime_ns,
                    root_signature=root_signature,
                )
    except Exception:
        # Summary file write failure is non-fatal — in-memory index is
        # authoritative. Next write will overwrite.
        pass


def _seen_cursor_path(root_id: str) -> Path:
    return _root_file_path(root_id).with_name(f"{root_id}.seen.json")


def _opened_path(root_id: str) -> Path:
    return _root_file_path(root_id).with_name(f"{root_id}.opened.json")


def _opened_file_signature(path: Path) -> tuple[int, int, int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _opened_cache_get(root_id: str, signature: tuple[int, int, int, int] | None) -> dict[str, str] | None:
    with _opened_cache_lock:
        cached = _opened_cache.get(root_id)
        if cached is None or cached[0] != signature:
            return None
        return dict(cached[1])


def _opened_cache_put(
    root_id: str,
    signature: tuple[int, int, int, int] | None,
    opened: dict[str, str],
) -> None:
    with _opened_cache_lock:
        _opened_cache[root_id] = (signature, dict(opened))
        while len(_opened_cache) > _OPENED_CACHE_MAX:
            _opened_cache.pop(next(iter(_opened_cache)))


def _opened_cache_invalidate(root_id: str) -> None:
    with _opened_cache_lock:
        _opened_cache.pop(root_id, None)


def read_seen_cursors(root_id: str) -> dict[str, Optional[str]]:
    path = _seen_cursor_path(root_id)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    raw = data.get("seen") if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        return {}
    return {
        sid: (uid if isinstance(uid, str) and uid else None)
        for sid, uid in raw.items()
        if isinstance(sid, str) and sid
    }


def read_last_opened(root_id: str) -> dict[str, str]:
    path = _opened_path(root_id)
    signature = _opened_file_signature(path)
    cached = _opened_cache_get(root_id, signature)
    if cached is not None:
        return cached
    if signature is None:
        _opened_cache_put(root_id, None, {})
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        _opened_cache_put(root_id, signature, {})
        return {}
    raw = data.get("opened") if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        _opened_cache_put(root_id, signature, {})
        return {}
    opened = {
        sid: at
        for sid, at in raw.items()
        if isinstance(sid, str) and sid and isinstance(at, str) and at
    }
    _opened_cache_put(root_id, signature, opened)
    return dict(opened)


def write_last_opened(root_id: str, sid: str, at: str) -> None:
    if not (root_id and sid and isinstance(at, str) and at):
        return
    opened = read_last_opened(root_id)
    if opened.get(sid) == at:
        return
    opened[sid] = at
    path = _opened_path(root_id)
    tmp_fd, tmp_path = tempfile.mkstemp(
        prefix=f".{root_id}.opened.", suffix=".tmp", dir=path.parent,
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "opened": opened}, f)
        os.replace(tmp_path, path)
        signature = _opened_file_signature(path)
        if signature is None:
            _opened_cache_invalidate(root_id)
        else:
            _opened_cache_put(root_id, signature, opened)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def write_seen_cursor(root_id: str, sid: str, uid: Optional[str]) -> None:
    if not (root_id and sid):
        return
    normalized = uid if isinstance(uid, str) and uid else None
    cursors = read_seen_cursors(root_id)
    if cursors.get(sid) == normalized and (sid in cursors or normalized is None):
        return
    cursors[sid] = normalized
    path = _seen_cursor_path(root_id)
    tmp_fd, tmp_path = tempfile.mkstemp(
        prefix=f".{root_id}.seen.", suffix=".tmp", dir=path.parent,
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "seen": cursors}, f)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def update_seen_cursor_projection(sid: str, uid: Optional[str]) -> None:
    global _summary_index_version
    updated: Optional[dict] = None
    with _summary_index_lock:
        if not _summary_index_loaded:
            return
        summary = _summary_index.get(sid)
        if summary is None or summary.get("last_seen_event_uid") == uid:
            return
        updated = {**summary, "last_seen_event_uid": uid}
        _summary_index[sid] = updated
        _summary_index_version += 1
    try:
        _write_summary_file(sid, updated)
    except Exception:
        pass


def update_last_opened_projection(sid: str, at: str) -> None:
    global _summary_index_version, _summary_order_version
    updated: Optional[dict] = None
    with _summary_index_lock:
        if not _summary_index_loaded:
            return
        summary = _summary_index.get(sid)
        if summary is None or summary.get("last_opened_at") == at:
            return
        updated = {**summary, "last_opened_at": at}
        _summary_index[sid] = updated
        _summary_index_version += 1
        _summary_order_version += 1
    try:
        _write_summary_file(sid, updated)
    except Exception:
        pass


def _overlay_seen_cursors(root: dict, root_id: str) -> None:
    cursors = read_seen_cursors(root_id)
    if not cursors:
        return
    for node in [root, *_walk_forks(root)]:
        sid = node.get("id")
        if sid in cursors:
            node["last_seen_event_uid"] = cursors[sid]


def _overlay_last_opened(root: dict, root_id: str) -> None:
    opened = read_last_opened(root_id)
    if not opened:
        return
    for node in [root, *_walk_forks(root)]:
        sid = node.get("id")
        if sid in opened:
            node["last_opened_at"] = opened[sid]


def _remove_summary(root_id: str) -> None:
    """Remove a root's summary entry and file (on delete)."""
    global _summary_index_version, _summary_order_version, _summary_metadata_version
    global _summary_visibility_version
    with _summary_index_lock:
        if _summary_index.pop(root_id, None) is not None:
            _summary_index_version += 1
            _summary_order_version += 1
            _summary_visibility_version += 1
            _summary_metadata_version += 1
    try:
        sp = _root_file_path(root_id).with_name(f"{root_id}.summary.json")
        sp.unlink(missing_ok=True)
    except OSError:
        pass


def _write_summary_file(
    root_id: str,
    summary: dict,
    *,
    root_mtime_ns: int | None = None,
    expected_root_signature: FileSignature | None = None,
) -> None:
    root_path = _root_file_path(root_id)
    if not root_path.exists():
        try:
            root_path.with_name(f"{root_id}.summary.json").unlink(missing_ok=True)
        except OSError:
            pass
        return
    current_root_signature = _session_file_signature(root_path)
    if (
        expected_root_signature is not None
        and current_root_signature != expected_root_signature
    ):
        return
    sp = root_path.with_name(f"{root_id}.summary.json")
    tmp_fd, tmp_path = tempfile.mkstemp(
        prefix=f".{root_id}.summary.",
        suffix=".tmp",
        dir=root_path.parent,
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            payload = dict(summary)
            if current_root_signature is not None:
                payload["_root_file_signature"] = list(current_root_signature)
            json.dump(payload, f)
        os.replace(tmp_path, sp)
        target_mtime_ns = time.time_ns()
        if root_mtime_ns is None:
            try:
                root_mtime_ns = root_path.stat().st_mtime_ns
            except OSError:
                try:
                    sp.unlink(missing_ok=True)
                except OSError:
                    pass
                return
        if root_mtime_ns is not None:
            target_mtime_ns = max(target_mtime_ns, root_mtime_ns)
        os.utime(sp, ns=(target_mtime_ns, target_mtime_ns))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _ensure_summary_sidecar_writer() -> None:
    global _summary_sidecar_write_started
    if _summary_sidecar_write_started:
        return
    with _summary_sidecar_write_lock:
        if _summary_sidecar_write_started:
            return
        threading.Thread(
            target=_summary_sidecar_writer_loop,
            name="summary-sidecar-writer",
            daemon=True,
        ).start()
        _summary_sidecar_write_started = True


def _summary_sidecar_writer_loop() -> None:
    while True:
        item = _summary_sidecar_write_queue.get()
        if item is None:
            _summary_sidecar_write_queue.task_done()
            return
        should_stop = _process_summary_sidecar_batch(item)
        if should_stop:
            return


def _process_summary_sidecar_batch(
    first_item: tuple[str, dict, int | None, FileSignature | None],
) -> bool:
    pending: dict[
        str, tuple[str, dict, int | None, FileSignature | None]
    ] = {first_item[0]: first_item}
    consumed = 1
    should_stop = False
    while True:
        try:
            next_item = _summary_sidecar_write_queue.get_nowait()
        except queue.Empty:
            break
        consumed += 1
        if next_item is None:
            should_stop = True
            break
        pending[next_item[0]] = next_item
    try:
        for root_id, summary, root_mtime_ns, root_signature in pending.values():
            if _summary_sidecar_write_item_stale(root_id, root_signature):
                continue
            try:
                with perf.timed("store.session.summary.sidecar_write"):
                    _write_summary_file(
                        root_id,
                        summary,
                        root_mtime_ns=root_mtime_ns,
                        expected_root_signature=root_signature,
                    )
            except Exception:
                _logger.debug("summary sidecar write failed for %s", root_id, exc_info=True)
    finally:
        for _ in range(consumed):
            _summary_sidecar_write_queue.task_done()
    return should_stop


def _summary_sidecar_write_item_stale(
    root_id: str,
    root_signature: FileSignature | None,
) -> bool:
    if root_signature is None:
        return False
    return _session_file_signature(_root_file_path(root_id)) != root_signature


def _schedule_summary_sidecar_write(
    root_id: str,
    summary: dict,
    *,
    root_mtime_ns: int | None = None,
    root_signature: FileSignature | None = None,
) -> None:
    _ensure_summary_sidecar_writer()
    item = (root_id, _copy_jsonish(summary), root_mtime_ns, root_signature)
    try:
        _summary_sidecar_write_queue.put_nowait(item)
    except queue.Full:
        try:
            _summary_sidecar_write_queue.get_nowait()
            _summary_sidecar_write_queue.task_done()
        except queue.Empty:
            pass
        try:
            _summary_sidecar_write_queue.put_nowait(item)
        except queue.Full:
            pass


def _root_summary_ids_on_disk() -> tuple[str, ...]:
    try:
        return tuple(sorted(p.stem for p in _session_json_files()))
    except OSError:
        return ()


def _cleanup_orphan_summary_sidecars(root_ids: set[str]) -> None:
    entries: list[Path] = []
    for storage_dir in _session_storage_dirs():
        try:
            entries.extend(storage_dir.iterdir())
        except OSError:
            pass
    suffixes = (".summary.json", ".opened.json")
    for path in entries:
        name = path.name
        suffix = next((s for s in suffixes if name.endswith(s)), None)
        if suffix is None:
            continue
        sid = name.removesuffix(suffix)
        if sid in root_ids:
            continue
        try:
            path.unlink(missing_ok=True)
            if suffix == ".opened.json":
                _opened_cache_invalidate(sid)
        except OSError:
            pass


def _purge_missing_summary_roots_locked(root_ids: set[str]) -> bool:
    global _summary_index_version, _summary_order_version, _summary_metadata_version
    global _summary_visibility_version
    removed = [
        sid for sid in list(_summary_index)
        if sid not in root_ids
        and not _root_file_path(sid).exists()
    ]
    if not removed:
        return False
    for sid in removed:
        _summary_index.pop(sid, None)
        parent = _root_file_path(sid).parent
        try:
            (parent / f"{sid}.summary.json").unlink(missing_ok=True)
        except OSError:
            pass
        try:
            (parent / f"{sid}.opened.json").unlink(missing_ok=True)
            _opened_cache_invalidate(sid)
        except OSError:
            pass
    _summary_index_version += 1
    _summary_order_version += 1
    _summary_visibility_version += 1
    _summary_metadata_version += 1
    return True


def _reconcile_summary_index_roots() -> None:
    global _summary_roots_fingerprint
    root_ids_tuple = _root_summary_ids_on_disk()
    root_ids = set(root_ids_tuple)
    with _summary_index_lock:
        if _summary_index_loaded and root_ids_tuple == _summary_roots_fingerprint:
            return
        _purge_missing_summary_roots_locked(root_ids)
        if _summary_index_loaded:
            _summary_roots_fingerprint = root_ids_tuple
            _summary_sorted_id_caches.clear()
    _cleanup_orphan_summary_sidecars(root_ids)


def _summary_index_cache_path() -> Path:
    return _sessions_dir() / ".summary-index.json"


def _summary_index_cache_fingerprint(
    full_files: dict[str, Path],
    summary_files: dict[str, Path],
    seen_cursor_ids: set[str],
) -> dict[str, dict[str, list[int]]]:
    def _signature_map(paths: dict[str, Path]) -> dict[str, list[int]]:
        out: dict[str, list[int]] = {}
        for sid, path in paths.items():
            signature = _session_file_signature(path)
            if signature is not None:
                out[sid] = list(signature)
        return out

    seen_paths = {
        sid: _seen_cursor_path(sid)
        for sid in seen_cursor_ids
    }
    return {
        "roots": _signature_map(full_files),
        "summaries": _signature_map(summary_files),
        "seen": _signature_map(seen_paths),
    }


def _load_summary_index_cache(
    fingerprint: dict[str, dict[str, list[int]]],
) -> Optional[dict[str, dict]]:
    try:
        raw = json.loads(_summary_index_cache_path().read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(raw, dict):
        return None
    if raw.get("version") != _SUMMARY_INDEX_CACHE_VERSION:
        return None
    if raw.get("fingerprint") != fingerprint:
        return None
    summaries = raw.get("summaries")
    if not isinstance(summaries, dict):
        return None
    skipped_root_ids = {
        sid for sid in raw.get("skipped_root_ids") or []
        if isinstance(sid, str)
    }
    clean = {
        sid: summary
        for sid, summary in summaries.items()
        if isinstance(sid, str)
        and isinstance(summary, dict)
        and summary.get("id") == sid
        and "last_seen_event_uid" in summary
        and _summary_has_current_projections(summary)
    }
    root_ids = set(fingerprint.get("roots") or {})
    if set(clean) | skipped_root_ids != root_ids:
        return None
    if set(clean) & skipped_root_ids:
        return None
    return clean


def _write_summary_index_cache(
    fingerprint: dict[str, dict[str, list[int]]],
    summaries: dict[str, dict],
) -> None:
    skipped_root_ids = sorted(set(fingerprint.get("roots") or {}) - set(summaries))
    payload = {
        "version": _SUMMARY_INDEX_CACHE_VERSION,
        "fingerprint": fingerprint,
        "skipped_root_ids": skipped_root_ids,
        "summaries": summaries,
    }
    tmp_fd, tmp_path = tempfile.mkstemp(
        prefix=".summary-index.",
        suffix=".json.tmp",
        dir=_sessions_dir(),
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, separators=(",", ":"))
        os.replace(tmp_path, _summary_index_cache_path())
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _touch_summary_file_current(
    root_id: str,
    *,
    summary: dict,
    root_mtime_ns: int | None = None,
    root_signature: FileSignature | None = None,
) -> bool:
    _schedule_summary_sidecar_write(
        root_id,
        summary,
        root_mtime_ns=root_mtime_ns,
        root_signature=root_signature,
    )
    return True


def _summary_has_current_projections(summary: dict) -> bool:
    return "current_todos" in summary and "current_tasks" in summary


def _sanitize_summary(summary: dict) -> tuple[dict, bool]:
    cleaned = dict(summary)
    changed = False
    if "_root_file_signature" in cleaned:
        cleaned.pop("_root_file_signature", None)
        changed = True
    if "workers" in cleaned:
        cleaned.pop("workers", None)
        changed = True
    if "fork_ids" not in cleaned and int(cleaned.get("fork_count") or 0) == 0:
        cleaned["fork_ids"] = []
        changed = True
    return cleaned, changed


def _ensure_summary_index(blocking: bool = True) -> None:
    """Populate the summary index on first access.

    Prefers lightweight .summary.json files (Option C) for fast startup.
    Falls back to parsing full session files when summaries are missing or
    stale. Subsequent calls are a no-op.

    `blocking=True` (default) waits for any concurrent build to finish
    or builds it ourselves. Used by the eager-warm startup task.

    `blocking=False` never performs the build on the caller thread. It
    starts a background builder if needed, then returns immediately so
    `list_sessions` sees whatever has already been published.
    """
    if _summary_index_loaded:
        return
    if not blocking:
        _start_summary_index_warm()
        return
    if not _summary_build_lock.acquire(blocking=True):
        return
    try:
        if _summary_index_loaded:
            return
        _do_build_summary_index_unsafe()
    finally:
        _summary_build_lock.release()


def _start_summary_index_warm() -> None:
    if _summary_index_loaded:
        return
    if not _summary_build_lock.acquire(blocking=False):
        return

    def _build() -> None:
        try:
            if not _summary_index_loaded:
                _do_build_summary_index_unsafe()
        finally:
            _summary_build_lock.release()

    thread = threading.Thread(
        target=_build,
        name="summary-index-warm",
        daemon=True,
    )
    thread.start()


def _do_build_summary_index_unsafe() -> None:
    """Actual build logic. Caller MUST hold `_summary_build_lock`.

    INVARIANT (incremental publish): each summary is upserted into
    `_summary_index` immediately under a brief `_summary_index_lock` —
    not buffered into a local dict and published once at the end.
    Concurrent readers (`list_sessions` with `blocking=False`) see the
    index GROW during the build instead of returning empty until the
    full scan completes. Trade-off: a /api/sessions hit mid-build
    returns a partial list (ordered by `_summary_index` insertion order
    + the sort step). The eager-warm startup task ensures the FE rarely
    sees a partial state — the warm fires before the FE bootstraps in
    practice.

    Lock-order invariants (carried from the prior implementation):
      - `_summary_build_lock` is held by ONE thread during the whole
        build. `_summary_index_lock` is acquired briefly inside this
        function (per-summary upsert + the final eng-pointer pass).
      - `_summary_index_lock` is never held across a `list_workers` or
        `write_session_full` call (both can take other locks and would
        re-enter `_summary_index_lock` themselves).
      - `_migrate_session` below can reach `event_ingester.ingest`,
        which takes the session_manager per-root RLock IFF the session
        is already cached. Cold build → not cached → no
        `_summary_build_lock → session_manager-RLock` edge forms.
    """
    global _summary_index_loaded, _summary_index_version, _summary_order_version, _summary_metadata_version, _summary_roots_fingerprint
    _ensure_dir()
    full_files: dict[str, Path] = {}
    summary_files: dict[str, Path] = {}
    seen_cursor_ids: set[str] = set()
    for storage_dir in _session_storage_dirs():
        try:
            entries = list(storage_dir.iterdir())
        except OSError:
            continue
        for p in entries:
            name = p.name
            if name.endswith(".summary.json"):
                summary_files[name.removesuffix(".summary.json")] = p
            elif name.endswith(".seen.json"):
                seen_cursor_ids.add(name.removesuffix(".seen.json"))
            elif name.endswith(".json") and not _is_sidecar_json(name):
                full_files[p.stem] = p
                _remember_root_file_dir(p.stem, p.parent)
    _cleanup_orphan_summary_sidecars(set(full_files))
    summary_files = {
        sid: path for sid, path in summary_files.items()
        if sid in full_files
    }
    summary_cache_fingerprint = _summary_index_cache_fingerprint(
        full_files,
        summary_files,
        seen_cursor_ids,
    )
    cached_summaries = _load_summary_index_cache(summary_cache_fingerprint)
    if cached_summaries is not None:
        with _summary_index_lock:
            _summary_index.clear()
            _summary_index.update(cached_summaries)
            _summary_index_version += 1
            _summary_order_version += 1
            _summary_metadata_version += 1
            _summary_index_loaded = True
            _summary_roots_fingerprint = tuple(sorted(full_files))
        _start_summary_projection_repair()
        _start_metadata_search_index_warm()
        return

    # Trees migrated in Pass 2 that need a persist — written AFTER the
    # locks release so the next start hits the Pass-1 fast path.
    dirty_trees: list[dict] = []
    stale_summaries: list[tuple[str, dict]] = []
    eng_by_parent: dict[str, str] = {}

    # Pass 1: load from summary files where available + fresh
    # (summary mtime must be >= session file mtime — a crash between
    # write_session_full and summary file write leaves a stale summary).
    missing_ids: list[str] = []
    import session_organization_store
    projection_snapshot = _projection_snapshot()
    organization_projection = session_organization_store.enrichment_projection()
    for sid in full_files:
        sp = summary_files.get(sid)
        published = False
        if sp and sp.exists():
            try:
                session_mtime = full_files[sid].stat().st_mtime_ns
                summary_mtime = sp.stat().st_mtime_ns
                if summary_mtime >= session_mtime:
                    summary = json.loads(sp.read_text(encoding="utf-8"))
                    if summary.get("id") == sid and "last_seen_event_uid" in summary:
                        if not _summary_has_current_projections(summary):
                            continue
                        summary, cleaned = _sanitize_summary(summary)
                        seen_cursors = read_seen_cursors(sid) if sid in seen_cursor_ids else {}
                        if sid in seen_cursors:
                            summary = {
                                **summary,
                                "last_seen_event_uid": seen_cursors[sid],
                            }
                            cleaned = True
                        if summary.get("working_mode"):
                            meta = summary.get("working_mode_meta") or {}
                            pid = meta.get("parent_session_id")
                            if pid:
                                eng_by_parent[pid] = sid
                        needs_fork_backfill = (
                            "fork_ids" not in summary
                            and int(summary.get("fork_count") or 0) > 0
                        )
                        if not needs_fork_backfill:
                            with _summary_index_lock:
                                existing = _summary_index.get(sid)
                                _summary_index[sid] = summary
                                _summary_index_version += 1
                                if _summary_order_changed(existing, summary):
                                    _summary_order_version += 1
                                if _summary_metadata_changed(existing, summary):
                                    _summary_metadata_version += 1
                            if cleaned:
                                stale_summaries.append((sid, summary))
                            published = True
            except (json.JSONDecodeError, KeyError, ValueError, OSError):
                pass
        if not published:
            missing_ids.append(sid)

    # Pass 2: build from full files for missing summaries. Each
    # parse+build publishes immediately so `/api/sessions` callers
    # observe the index growing.
    provider_ctx: Optional[dict] = None
    for sid in missing_ids:
        fpath = full_files[sid]
        try:
            raw = json.loads(fpath.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or "id" not in raw:
                continue
            if provider_ctx is None:
                provider_ctx = _provider_backfill_context()
            data = _migrate_session(raw, provider_ctx)
            _overlay_seen_cursors(data, data["id"])
            _overlay_last_opened(data, data["id"])
            summary = _build_summary_for_root(
                data,
                projection_snapshot,
                organization_projection,
            )
            with _summary_index_lock:
                existing = _summary_index.get(data["id"])
                _summary_index[data["id"]] = summary
                _summary_index_version += 1
                if _summary_order_changed(existing, summary):
                    _summary_order_version += 1
                if _summary_metadata_changed(existing, summary):
                    _summary_metadata_version += 1
            stale_summaries.append((data["id"], summary))
        except (json.JSONDecodeError, KeyError, ValueError, OSError):
            continue
        if provider_ctx["dirty"][0]:
            dirty_trees.append(data)
        if data.get("working_mode"):
            meta = data.get("working_mode_meta") or {}
            pid = meta.get("parent_session_id")
            if pid:
                eng_by_parent[pid] = data["id"]

    # Final unified pass for eng pointers across the WHOLE index
    # (Pass 1 + Pass 2). Keep only the mutation phase under the index
    # lock so `/api/sessions` does not wait behind per-summary projection
    # comparison work during warm completion.
    with _summary_index_lock:
        for pid, eng_sid in eng_by_parent.items():
            if pid in _summary_index:
                _summary_index[pid] = {
                    **_summary_index[pid],
                    "pending_eng_session_id": eng_sid,
                }
                _summary_index_version += 1
        _summary_index_loaded = True
        _summary_roots_fingerprint = tuple(sorted(full_files))

    _start_summary_projection_repair()
    _start_metadata_search_index_warm()

    # Phase 3: persist migrated trees outside both locks (write_session_full
    # → _upsert_summary takes _summary_index_lock cleanly here). Best-effort.
    for tree in dirty_trees:
        try:
            write_session_full(tree, bump_updated_at=False)
        except Exception:
            pass
    for sid, summary in stale_summaries:
        try:
            _write_summary_file(sid, summary)
        except Exception:
            pass
    if not dirty_trees and not stale_summaries:
        with _summary_index_lock:
            summaries = dict(_summary_index)
        try:
            _write_summary_index_cache(summary_cache_fingerprint, summaries)
        except Exception:
            pass


def _refresh_summaries_for_cwd(cwd: str) -> None:
    """Refresh worker-dependent summary fields for all sessions."""
    _refresh_summaries_for_cwds([cwd])


def _worker_summary_count() -> int:
    from stores import worker_store
    return worker_store.worker_count("")


def _refresh_summaries_for_cwd_from(cwd: str, workers: list[dict]) -> None:
    """Refresh worker-dependent summary fields using pre-loaded worker data."""
    global _summary_index_version
    if not _summary_index_loaded:
        return
    with _summary_index_lock:
        for sid, s in _summary_index.items():
            _summary_index[sid] = {
                **s,
                "worker_count": len(workers),
            }
        _summary_index_version += 1


def _refresh_summaries_for_cwds(cwds: list[str]) -> None:
    """Refresh worker-dependent summary fields for all sessions."""
    global _summary_index_version
    if not _summary_index_loaded:
        return
    from stores import worker_store
    workers = worker_store.list_workers("")
    with _summary_index_lock:
        for sid, s in _summary_index.items():
            _summary_index[sid] = {
                **s,
                "worker_count": len(workers),
            }
        _summary_index_version += 1


def _refresh_all_worker_summaries() -> None:
    """Refresh worker fields for all summaries. Called by worker_store
    everywhere-walkers that can't recover the raw cwd from encoded paths."""
    if not _summary_index_loaded:
        return
    _refresh_summaries_for_cwds([""])


def _session_file_signature(path: Path) -> Optional[FileSignature]:
    try:
        st = path.stat()
    except OSError:
        return None
    try:
        change_identity = _file_change_identity(path, st)
    except OSError:
        return None
    return (
        st.st_dev,
        st.st_ino,
        change_identity,
        st.st_mtime_ns,
        st.st_size,
    )


def _content_change_identity(path: Path, expected: os.stat_result) -> int:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino):
            raise OSError("session file identity changed before hashing")
        digest = hashlib.sha256()
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        finished = os.fstat(fd)
        if (
            (finished.st_dev, finished.st_ino) != (opened.st_dev, opened.st_ino)
            or finished.st_mtime_ns != opened.st_mtime_ns
            or finished.st_size != opened.st_size
        ):
            raise OSError("session file changed while hashing")
        return -int.from_bytes(digest.digest()[:16], "big") - 1
    finally:
        os.close(fd)


def _windows_change_time(path: Path, expected: os.stat_result) -> int:
    import ctypes
    import msvcrt

    class FILE_BASIC_INFO(ctypes.Structure):
        _fields_ = [
            ("CreationTime", ctypes.c_longlong),
            ("LastAccessTime", ctypes.c_longlong),
            ("LastWriteTime", ctypes.c_longlong),
            ("ChangeTime", ctypes.c_longlong),
            ("FileAttributes", ctypes.c_ulong),
        ]

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino):
            raise OSError("session file identity changed before ChangeTime query")
        handle = msvcrt.get_osfhandle(fd)
        get_info = ctypes.windll.kernel32.GetFileInformationByHandleEx
        get_info.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_ulong,
        ]
        get_info.restype = ctypes.c_int
        def query_change_time() -> int:
            info = FILE_BASIC_INFO()
            if not get_info(
                ctypes.c_void_p(handle),
                0,
                ctypes.byref(info),
                ctypes.sizeof(info),
            ):
                raise ctypes.WinError()
            return int(info.ChangeTime)

        change_time = query_change_time()
        finished = os.fstat(fd)
        if (
            (finished.st_dev, finished.st_ino) != (opened.st_dev, opened.st_ino)
            or finished.st_mtime_ns != opened.st_mtime_ns
            or finished.st_size != opened.st_size
        ):
            raise OSError("session file identity changed during ChangeTime query")
        if query_change_time() != change_time:
            raise OSError("session file ChangeTime changed during query")
        if change_time <= 0:
            raise OSError("filesystem returned no reliable ChangeTime")
        return change_time
    finally:
        os.close(fd)


def _file_change_identity(path: Path, stat_result: os.stat_result) -> int:
    if os.name == "nt":
        try:
            return _windows_change_time(path, stat_result)
        except (ImportError, OSError, AttributeError):
            return _content_change_identity(path, stat_result)
    ctime_ns = getattr(stat_result, "st_ctime_ns", None)
    if isinstance(ctime_ns, int) and ctime_ns > 0:
        return ctime_ns
    return _content_change_identity(path, stat_result)


def _index_tree(
    root: dict,
    *,
    file_signature: Optional[FileSignature] = None,
    force: bool = False,
) -> bool:
    """Populate `_fork_index` for every fork in `root`. Holds
    `_index_lock` so concurrent forks/deletes can't race a reader's
    `_fork_index` walk (CPython dict ops are not all GIL-atomic — a
    `for k in d:` interleaved with `d[k] = v` raises RuntimeError)."""
    rid = root["id"]
    with _index_lock:
        if (
            not force
            and
            file_signature is not None
            and _root_index_signatures.get(rid) == file_signature
        ):
            return False
        stale = _root_forks.get(rid, set())
        current: set[str] = {
            fork["id"]
            for fork in _walk_forks(root)
            if isinstance(fork, dict) and isinstance(fork.get("id"), str)
        }
        topology_changed = stale != current
        for fid in stale:
            _fork_index.pop(fid, None)
        for fork_id in current:
            _fork_index[fork_id] = rid
        _root_forks[rid] = current
        if file_signature is not None:
            _root_index_signatures[rid] = file_signature
        if topology_changed or force:
            _clear_negative_root_resolve_cache()
        if topology_changed or force or file_signature is not None:
            _bump_index_generation_locked()
        return topology_changed


def _index_set(fork_id: str, root_id: str) -> None:
    global _index_loaded
    with _index_lock:
        _fork_index[fork_id] = root_id
        _root_forks.setdefault(root_id, set()).add(fork_id)
        _index_loaded = True
        _clear_negative_root_resolve_cache()
        _bump_index_generation_locked()


def _index_pop(sid: str) -> None:
    with _index_lock:
        root_id = _fork_index.pop(sid, None)
        if root_id is not None:
            forks = _root_forks.get(root_id)
            if forks is not None:
                forks.discard(sid)
                if not forks:
                    _root_forks.pop(root_id, None)
                    _root_index_signatures.pop(root_id, None)
        _clear_negative_root_resolve_cache()
        _bump_index_generation_locked()


# Sidecar files share the sessions dir and the `.json` extension but are
# NOT session root trees — they must be excluded from every root-file
# glob (else they get parsed as sessions → KeyError 'id').
_SIDECAR_JSON_SUFFIXES = (
    ".summary.json",
    ".drafts.json",
    ".seen.json",
    ".opened.json",
    ".fork-index.json",
    ".summary-index.json",
    "attention_markers.json",
    ".missing.json",
)


def _is_sidecar_json(name: str) -> bool:
    return name.endswith(_SIDECAR_JSON_SUFFIXES)


def _session_storage_dirs() -> Iterator[Path]:
    yield _sessions_dir()
    routine_root = _routine_sessions_dir()
    try:
        children = sorted(routine_root.iterdir(), key=lambda p: p.name)
    except OSError:
        return
    for child in children:
        if child.is_dir():
            yield child


def _session_json_files() -> Iterator[Path]:
    """Yield session root JSON files, excluding sidecars."""
    for storage_dir in _session_storage_dirs():
        try:
            with os.scandir(storage_dir) as it:
                for entry in it:
                    name = entry.name
                    if not name.endswith(".json") or _is_sidecar_json(name):
                        continue
                    path = Path(entry.path)
                    _remember_root_file_dir(path.stem, path.parent)
                    yield path
        except OSError:
            continue


def _fork_index_path() -> Path:
    return _sessions_dir() / ".fork-index.json"


def _summary_matches_root_identity(summary: dict, path: Path) -> bool:
    signature = _session_file_signature(path)
    raw = summary.get("_root_file_signature")
    return (
        signature is not None
        and isinstance(raw, list)
        and len(raw) == 5
        and tuple(raw) == signature
    )


def _session_json_files_requiring_fork_scan() -> Iterator[Path]:
    for p in _session_json_files():
        sp = p.with_name(f"{p.stem}.summary.json")
        try:
            if sp.stat().st_mtime_ns >= p.stat().st_mtime_ns:
                summary = json.loads(sp.read_text(encoding="utf-8"))
                if (
                    summary.get("id") != p.stem
                    or not _summary_matches_root_identity(summary, p)
                ):
                    yield p
                    continue
                fork_ids = summary.get("fork_ids")
                if isinstance(fork_ids, list):
                    continue
                if int(summary.get("fork_count") or 0) == 0:
                    continue
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            pass
        yield p


def _dir_fingerprint() -> DirFingerprint:
    """Stat-only signature of every root file's durable identity."""
    count = 0
    total_size = 0
    identity_mix = 0
    for storage_dir in _session_storage_dirs():
        try:
            it = os.scandir(storage_dir)
        except OSError:
            continue
        with it:
            for entry in it:
                if not entry.name.endswith(".json") or _is_sidecar_json(entry.name):
                    continue
                try:
                    st = entry.stat()
                    change_identity = _file_change_identity(Path(entry.path), st)
                except OSError:
                    continue
                count += 1
                total_size += st.st_size
                identity_mix ^= _file_signature_mix((
                    st.st_dev,
                    st.st_ino,
                    change_identity,
                    st.st_mtime_ns,
                    st.st_size,
                ), entry.name)
    return (count, 0, 0, total_size, identity_mix)


def _dir_fingerprint_for(source: str) -> DirFingerprint:
    perf.record_count(f"store.session.dir_fingerprint.scan_source.{source}")
    return _dir_fingerprint()


def _dir_fingerprint_cached() -> DirFingerprint:
    global _dir_fingerprint_cache
    now = time.monotonic()
    cached = _dir_fingerprint_cache
    if cached is not None and now - cached[0] <= _DIR_FINGERPRINT_CACHE_TTL_SECONDS:
        return cached[1]
    wait_started = time.perf_counter()
    with _dir_fingerprint_cache_lock:
        acquired_at = time.perf_counter()
        perf.record(
            "store.session.dir_fingerprint.cache.lock_wait",
            (acquired_at - wait_started) * 1000.0,
        )
        now = time.monotonic()
        cached = _dir_fingerprint_cache
        if cached is not None and now - cached[0] <= _DIR_FINGERPRINT_CACHE_TTL_SECONDS:
            perf.record(
                "store.session.dir_fingerprint.cache.lock_hold",
                (time.perf_counter() - acquired_at) * 1000.0,
            )
            return cached[1]
        scan_started = time.perf_counter()
        fingerprint = _dir_fingerprint_for("resolve_cache_miss")
        perf.record(
            "store.session.dir_fingerprint.cache.scan",
            (time.perf_counter() - scan_started) * 1000.0,
        )
        _dir_fingerprint_cache = (now, fingerprint)
        perf.record(
            "store.session.dir_fingerprint.cache.lock_hold",
            (time.perf_counter() - acquired_at) * 1000.0,
        )
        return fingerprint


def _build_index_snapshot(
    fp: Optional[DirFingerprint] = None,
) -> tuple[DirFingerprint, dict[str, str], dict[str, set[str]], dict[str, FileSignature]]:
    """Build a fork-index snapshot without holding `_index_lock`."""
    if fp is None:
        fp = _dir_fingerprint_for("index_snapshot")
    cached = _load_index_sidecar(fp)
    if cached is not None:
        return cached
    refreshed = _refresh_stale_index_sidecar(fp)
    if refreshed is not None:
        return refreshed
    fork_index: dict[str, str] = {}
    root_forks: dict[str, set[str]] = {}
    root_signatures: dict[str, FileSignature] = {}
    remaining_scan_paths: list[Path] = []
    for path in _session_json_files():
        sp = path.with_name(f"{path.stem}.summary.json")
        file_signature = _session_file_signature(path)
        if file_signature is not None:
            root_signatures[path.stem] = file_signature
        try:
            if sp.stat().st_mtime_ns < path.stat().st_mtime_ns:
                remaining_scan_paths.append(path)
                continue
            summary = json.loads(sp.read_text(encoding="utf-8"))
            if (
                summary.get("id") != path.stem
                or not _summary_matches_root_identity(summary, path)
            ):
                remaining_scan_paths.append(path)
                continue
            fork_ids = summary.get("fork_ids")
            if isinstance(fork_ids, list):
                current = {
                    fork_id
                    for fork_id in fork_ids
                    if isinstance(fork_id, str) and fork_id
                }
                for fork_id in current:
                    fork_index[fork_id] = path.stem
                root_forks[path.stem] = current
                continue
            if int(summary.get("fork_count") or 0) == 0:
                root_forks[path.stem] = set()
                continue
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            pass
        remaining_scan_paths.append(path)
    for path in remaining_scan_paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            root = _migrate_session(data)
            rid = root["id"]
        except (json.JSONDecodeError, KeyError, ValueError):
            continue
        current: set[str] = set()
        for fork in _walk_forks(root):
            fork_id = fork.get("id")
            if fork_id:
                fork_index[fork_id] = rid
                current.add(fork_id)
        root_forks[rid] = current
        file_signature = _session_file_signature(path)
        if file_signature is not None:
            root_signatures[rid] = file_signature
    return fp, fork_index, root_forks, root_signatures


def _fork_index_entry_from_summary_or_root(
    path: Path,
) -> Optional[tuple[str, set[str], FileSignature]]:
    file_signature = _session_file_signature(path)
    if file_signature is None:
        return None
    sp = path.with_name(f"{path.stem}.summary.json")
    try:
        if sp.stat().st_mtime_ns >= path.stat().st_mtime_ns:
            summary = json.loads(sp.read_text(encoding="utf-8"))
            if (
                summary.get("id") == path.stem
                and _summary_matches_root_identity(summary, path)
            ):
                fork_ids = summary.get("fork_ids")
                if isinstance(fork_ids, list):
                    return (
                        path.stem,
                        {
                            fork_id
                            for fork_id in fork_ids
                            if isinstance(fork_id, str) and fork_id
                        },
                        file_signature,
                    )
                if int(summary.get("fork_count") or 0) == 0:
                    return path.stem, set(), file_signature
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        pass
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        root = _migrate_session(data)
        rid = root["id"]
    except (json.JSONDecodeError, KeyError, ValueError):
        return None
    return (
        rid,
        {
            fork["id"]
            for fork in _walk_forks(root)
            if isinstance(fork, dict) and isinstance(fork.get("id"), str)
        },
        file_signature,
    )


def _refresh_index_incremental(
    live_fp: DirFingerprint,
) -> Optional[tuple[DirFingerprint, dict[str, str], dict[str, set[str]], dict[str, FileSignature]]]:
    with _index_lock:
        if not _index_loaded or _index_fingerprint is None:
            return None
        fork_index = dict(_fork_index)
        root_forks = {
            root_id: set(forks)
            for root_id, forks in _root_forks.items()
        }
        old_signatures = dict(_root_index_signatures)

    current_paths: dict[str, Path] = {}
    current_signatures: dict[str, FileSignature] = {}
    for path in _session_json_files():
        signature = _session_file_signature(path)
        if signature is None:
            continue
        current_paths[path.stem] = path
        current_signatures[path.stem] = signature

    changed_roots = {
        root_id
        for root_id, signature in current_signatures.items()
        if old_signatures.get(root_id) != signature
    }
    deleted_roots = set(old_signatures) - set(current_signatures)
    touched_roots = changed_roots | deleted_roots
    if len(touched_roots) > _INDEX_INCREMENTAL_REFRESH_MAX_CHANGED:
        return None

    for root_id in touched_roots:
        for fork_id in root_forks.pop(root_id, set()):
            fork_index.pop(fork_id, None)
        old_signatures.pop(root_id, None)

    for root_id in changed_roots:
        parsed = _fork_index_entry_from_summary_or_root(current_paths[root_id])
        if parsed is None:
            return None
        parsed_root_id, forks, signature = parsed
        if parsed_root_id != root_id:
            return None
        root_forks[root_id] = forks
        old_signatures[root_id] = signature
        for fork_id in forks:
            fork_index[fork_id] = root_id

    return live_fp, fork_index, root_forks, old_signatures


def _load_index_sidecar(
    expected_fp: DirFingerprint,
) -> Optional[tuple[DirFingerprint, dict[str, str], dict[str, set[str]], dict[str, FileSignature]]]:
    raw = _read_index_sidecar_payload()
    if raw is None:
        return None
    if tuple(raw.get("fingerprint") or ()) != expected_fp:
        return None
    parsed = _parse_index_sidecar(raw)
    if parsed is None:
        return None
    fork_index, root_forks, root_signatures = parsed
    return expected_fp, fork_index, root_forks, root_signatures


def _read_index_sidecar_payload() -> Optional[dict]:
    try:
        raw = json.loads(_fork_index_path().read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else None
    except (json.JSONDecodeError, OSError):
        return None


def _parse_index_sidecar(
    raw: dict,
) -> Optional[tuple[dict[str, str], dict[str, set[str]], dict[str, FileSignature]]]:
    try:
        if raw.get("schema_version") != _FORK_INDEX_SIDECAR_SCHEMA_VERSION:
            return None
        raw_root_signatures = raw.get("root_signatures") or {}
        if not isinstance(raw_root_signatures, dict):
            return None
        fork_index = {
            str(k): str(v)
            for k, v in (raw.get("fork_index") or {}).items()
            if isinstance(k, str) and isinstance(v, str)
        }
        root_forks = {
            str(k): {str(item) for item in v if isinstance(item, str)}
            for k, v in (raw.get("root_forks") or {}).items()
            if isinstance(k, str) and isinstance(v, list)
        }
        root_signatures = {
            str(k): tuple(int(part) for part in v)
            for k, v in raw_root_signatures.items()
            if (
                isinstance(k, str)
                and isinstance(v, list)
                and len(v) == 5
            )
        }
        if len(root_signatures) != len(raw_root_signatures):
            return None
        return fork_index, root_forks, root_signatures
    except (TypeError, ValueError):
        return None


def _root_signatures_from_disk() -> Optional[dict[str, FileSignature]]:
    signatures: dict[str, FileSignature] = {}
    for path in _session_json_files():
        signature = _session_file_signature(path)
        if signature is None:
            return None
        signatures[path.stem] = signature
    return signatures


def _fork_ids_for_root(root: dict) -> set[str]:
    return {
        fork_id
        for fork in _walk_forks(root)
        if isinstance((fork_id := fork.get("id")), str) and fork_id
    }


def _refresh_stale_index_sidecar(
    fp: DirFingerprint,
) -> Optional[tuple[DirFingerprint, dict[str, str], dict[str, set[str]], dict[str, FileSignature]]]:
    raw = _read_index_sidecar_payload()
    if raw is None:
        return None
    parsed = _parse_index_sidecar(raw)
    if parsed is None:
        return None
    fork_index, root_forks, root_signatures = parsed
    disk_signatures = _root_signatures_from_disk()
    if disk_signatures is None:
        return None
    removed_roots = set(root_signatures) - set(disk_signatures)
    for root_id in removed_roots:
        for fork_id in root_forks.pop(root_id, set()):
            fork_index.pop(fork_id, None)
        root_signatures.pop(root_id, None)
    changed_roots = [
        root_id
        for root_id, signature in disk_signatures.items()
        if root_signatures.get(root_id) != signature
    ]
    for root_id in changed_roots:
        path = _root_file_path(root_id)
        fork_ids = _fork_ids_from_fresh_summary(path)
        if fork_ids is None:
            root = _read_root_for_fork_ids(path)
            if root is None:
                return None
            fork_ids = _fork_ids_for_root(root)
        for fork_id in root_forks.get(root_id, set()):
            fork_index.pop(fork_id, None)
        for fork_id in fork_ids:
            fork_index[fork_id] = root_id
        if fork_ids:
            root_forks[root_id] = fork_ids
        else:
            root_forks.pop(root_id, None)
        root_signatures[root_id] = disk_signatures[root_id]
    try:
        _write_index_sidecar(fp, fork_index, root_forks, root_signatures)
    except OSError:
        pass
    return fp, fork_index, root_forks, root_signatures


def _fork_ids_from_fresh_summary(path: Path) -> Optional[set[str]]:
    sp = path.with_name(f"{path.stem}.summary.json")
    try:
        if sp.stat().st_mtime_ns < path.stat().st_mtime_ns:
            return None
        summary = json.loads(sp.read_text(encoding="utf-8"))
        if (
            summary.get("id") != path.stem
            or not _summary_matches_root_identity(summary, path)
        ):
            return None
        fork_ids = summary.get("fork_ids")
        if isinstance(fork_ids, list):
            return {
                fork_id
                for fork_id in fork_ids
                if isinstance(fork_id, str) and fork_id
            }
        if int(summary.get("fork_count") or 0) == 0:
            return set()
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        return None
    return None


def _read_root_for_fork_ids(path: Path) -> Optional[dict]:
    try:
        root = _migrate_session(json.loads(path.read_text(encoding="utf-8")))
        if root.get("id") == path.stem:
            return root
    except (json.JSONDecodeError, KeyError, OSError, ValueError):
        return None
    return None


def _write_index_sidecar(
    fp: DirFingerprint,
    fork_index: dict[str, str],
    root_forks: dict[str, set[str]],
    root_signatures: dict[str, FileSignature],
) -> None:
    payload = {
        "schema_version": _FORK_INDEX_SIDECAR_SCHEMA_VERSION,
        "fingerprint": list(fp),
        "fork_index": fork_index,
        "root_forks": {
            root_id: sorted(forks)
            for root_id, forks in root_forks.items()
            if forks
        },
        "root_signatures": {
            root_id: list(sig)
            for root_id, sig in root_signatures.items()
        },
    }
    encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    receipt = _get_durability_writer().replace(_fork_index_path(), encoded)
    _wait_durability(receipt)


def _write_index_sidecar_best_effort(
    fp: DirFingerprint,
    fork_index: dict[str, str],
    root_forks: dict[str, set[str]],
    root_signatures: dict[str, FileSignature],
) -> None:
    try:
        _write_index_sidecar(fp, fork_index, root_forks, root_signatures)
    except OSError:
        pass


def _ensure_index_sidecar_writer() -> None:
    global _index_sidecar_write_started
    if _index_sidecar_write_started:
        return
    with _index_sidecar_write_lock:
        if _index_sidecar_write_started:
            return
        thread = threading.Thread(
            target=_index_sidecar_writer_loop,
            name="session-fork-index-sidecar",
            daemon=True,
        )
        thread.start()
        _index_sidecar_write_started = True


def _index_sidecar_writer_loop() -> None:
    while True:
        item = _index_sidecar_write_queue.get()
        if item is None:
            _index_sidecar_write_queue.task_done()
            return
        try:
            fp, fork_index, root_forks, root_signatures = item
            _write_index_sidecar_best_effort(
                fp,
                fork_index,
                root_forks,
                root_signatures,
            )
        finally:
            _index_sidecar_write_queue.task_done()


def _schedule_index_sidecar_write(
    fp: DirFingerprint,
    fork_index: dict[str, str],
    root_forks: dict[str, set[str]],
    root_signatures: dict[str, FileSignature],
) -> None:
    _ensure_index_sidecar_writer()
    item = (
        fp,
        dict(fork_index),
        {root_id: set(forks) for root_id, forks in root_forks.items()},
        dict(root_signatures),
    )
    try:
        _index_sidecar_write_queue.put_nowait(item)
        return
    except queue.Full:
        pass
    try:
        _index_sidecar_write_queue.get_nowait()
        _index_sidecar_write_queue.task_done()
    except queue.Empty:
        pass
    try:
        _index_sidecar_write_queue.put_nowait(item)
    except queue.Full:
        pass


def _persist_index_sidecar_if_loaded(
    fingerprint: DirFingerprint | None = None,
    expected_generation: int | None = None,
) -> None:
    with _index_lock:
        if not _index_loaded or _index_fingerprint is None:
            return
        if expected_generation is not None and expected_generation != _index_generation:
            perf.record_count("store.session.index.sidecar.coalesced")
            return
        fp = _index_fingerprint
        if fingerprint is not None and fingerprint != fp:
            perf.record_count("store.session.index.sidecar.coalesced")
            return
        fork_index = dict(_fork_index)
        root_forks = {
            root_id: set(forks)
            for root_id, forks in _root_forks.items()
        }
        root_signatures = dict(_root_index_signatures)
    _schedule_index_sidecar_write(fp, fork_index, root_forks, root_signatures)


def _refresh_index_sidecar_for_written_root(
    root: dict,
    file_signature: FileSignature | None,
) -> None:
    if file_signature is None:
        return
    raw = _read_index_sidecar_payload()
    if raw is None:
        return
    parsed = _parse_index_sidecar(raw)
    if parsed is None:
        return
    fork_index, root_forks, root_signatures = parsed
    root_id = str(root.get("id") or "")
    if not root_id:
        return
    for fork_id in root_forks.get(root_id, set()):
        fork_index.pop(fork_id, None)
    fork_ids = _fork_ids_for_root(root)
    for fork_id in fork_ids:
        fork_index[fork_id] = root_id
    if fork_ids:
        root_forks[root_id] = fork_ids
    else:
        root_forks.pop(root_id, None)
    root_signatures[root_id] = file_signature
    _write_index_sidecar(
        _dir_fingerprint_for("written_root_sidecar"),
        fork_index,
        root_forks,
        root_signatures,
    )


def _install_index_snapshot(
    fp: DirFingerprint,
    fork_index: dict[str, str],
    root_forks: dict[str, set[str]],
    root_signatures: dict[str, FileSignature],
) -> None:
    global _index_fingerprint
    _fork_index.clear()
    _fork_index.update(fork_index)
    _root_forks.clear()
    _root_forks.update(root_forks)
    _root_index_signatures.clear()
    _root_index_signatures.update(root_signatures)
    _index_fingerprint = fp
    _clear_negative_root_resolve_cache()
    _bump_index_generation_locked()


def _refresh_index(
    live_fp: Optional[DirFingerprint] = None,
) -> DirFingerprint:
    """Re-scan the sessions directory and rebuild the fork index.
    Used as a fallback when `_resolve_root_id` misses on a sid that
    might exist on disk (created by another process — CLI vs.
    backend, multiple uvicorn workers, etc.).

    Gated by a stat-only directory fingerprint: when the sessions dir
    is unchanged since the last scan the rescan is skipped entirely.
    INVARIANT: this matters because `_resolve_root_id` calls here on
    EVERY miss — and a miss on a genuinely-absent sid (e.g. startup
    run-recovery integrating runs whose sessions were deleted) would
    otherwise re-parse the whole multi-hundred-MB sessions dir once
    per miss."""
    global _index_fingerprint, _index_refresh_global_attempt_until
    _ensure_dir()
    if live_fp is None:
        live_fp = _dir_fingerprint_for("refresh_initial")
    with _index_lock:
        if _index_fingerprint is not None and live_fp == _index_fingerprint:
            return live_fp
    with _index_build_lock:
        with _index_lock:
            if _index_fingerprint is not None and live_fp == _index_fingerprint:
                return live_fp
            if _index_refresh_global_attempt_until > time.monotonic():
                return live_fp
            if _index_refresh_attempt_until.get(live_fp, 0.0) > time.monotonic():
                return live_fp
        incremental = _refresh_index_incremental(live_fp)
        if incremental is not None:
            fp, fork_index, root_forks, root_signatures = incremental
            with _index_lock:
                if _index_fingerprint is not None and _index_fingerprint == fp:
                    return fp
                _install_index_snapshot(fp, fork_index, root_forks, root_signatures)
                _index_refresh_attempt_until.pop(fp, None)
                _index_refresh_global_attempt_until = 0.0
            _schedule_index_sidecar_write(fp, fork_index, root_forks, root_signatures)
            return fp
        with perf.timed("store.session.index.refresh.build"):
            fp, fork_index, root_forks, root_signatures = _build_index_snapshot(live_fp)
        latest_fp = _dir_fingerprint_for("refresh_validate")
        if latest_fp != fp:
            with _index_lock:
                _install_index_snapshot(fp, fork_index, root_forks, root_signatures)
            incremental = _refresh_index_incremental(latest_fp)
            if incremental is None:
                with perf.timed("store.session.index.refresh.rebuild_after_dirty"):
                    fp, fork_index, root_forks, root_signatures = (
                        _build_index_snapshot(latest_fp)
                    )
                latest_fp = _dir_fingerprint_for("refresh_rebuild_validate")
                if latest_fp != fp:
                    with _index_lock:
                        _install_index_snapshot(fp, fork_index, root_forks, root_signatures)
                        _index_fingerprint = None
                        _clear_negative_root_resolve_cache()
                    return fp
            else:
                fp, fork_index, root_forks, root_signatures = incremental
        with _index_lock:
            if _index_fingerprint is not None and _index_fingerprint == fp:
                return fp
            _install_index_snapshot(fp, fork_index, root_forks, root_signatures)
            _index_refresh_attempt_until.pop(fp, None)
            _index_refresh_global_attempt_until = 0.0
        _schedule_index_sidecar_write(fp, fork_index, root_forks, root_signatures)
        return fp


def _ensure_index() -> None:
    """Lazy first-time load of the fork index. After the first load,
    `_resolve_root_id` does its own per-miss refresh to pick up forks
    created by other processes since startup."""
    global _index_loaded
    if _index_loaded:
        return
    _ensure_dir()
    with _index_build_lock:
        with _index_lock:
            if _index_loaded:
                return
        with perf.timed("store.session.index.ensure.build"):
            fp, fork_index, root_forks, root_signatures = _build_index_snapshot()
        with _index_lock:
            if _index_loaded:
                return
            _install_index_snapshot(fp, fork_index, root_forks, root_signatures)
            _index_loaded = True
        _schedule_index_sidecar_write(fp, fork_index, root_forks, root_signatures)


def _loaded_root_id_for(sid: str) -> Optional[str]:
    wait_started = time.perf_counter()
    with _index_lock:
        acquired_at = time.perf_counter()
        perf.record(
            "store.session.index.lookup.lock_wait",
            (acquired_at - wait_started) * 1000.0,
        )
        if not _index_loaded:
            perf.record(
                "store.session.index.lookup.lock_hold",
                (time.perf_counter() - acquired_at) * 1000.0,
            )
            return None
        if sid in _root_index_signatures:
            perf.record(
                "store.session.index.lookup.lock_hold",
                (time.perf_counter() - acquired_at) * 1000.0,
            )
            return sid
        root_id = _fork_index.get(sid)
        perf.record(
            "store.session.index.lookup.lock_hold",
            (time.perf_counter() - acquired_at) * 1000.0,
        )
        return root_id


def _resolve_root_id(sid: str) -> Optional[str]:
    """Return the root id for any session id (root or fork). None if
    the id is unknown.

    Cross-process changes are projected by the root-change owner. An unknown
    id waits briefly for one already-in-progress observation cycle, then
    returns None under the truthful eventual-consistency contract."""
    global _negative_root_resolve_global_until
    loaded_root_id = _loaded_root_id_for(sid)
    if loaded_root_id is not None:
        return loaded_root_id
    if _root_file_path(sid).exists():
        return sid
    _wait_root_change_owner_ready()
    _ensure_index()
    with _index_lock:
        if sid in _root_index_signatures:
            return sid
        if sid in _fork_index:
            return _fork_index[sid]
    owner = _root_change_owner
    if owner is not None:
        generation = owner.observation_generation
        loaded_root_id = _loaded_root_id_for(sid)
        if loaded_root_id is not None:
            return loaded_root_id
        if _root_file_path(sid).exists():
            return sid
        _wait_root_change_observation(generation)
        loaded_root_id = _loaded_root_id_for(sid)
        if loaded_root_id is not None:
            return loaded_root_id
        if _root_file_path(sid).exists():
            return sid
    # The ready root-change owner has completed a fenced disk observation.
    # Subsequent external changes are projected by that single owner; misses
    # never re-scan the sessions directory on the request path.
    return None


def _session_path(sid: str) -> Path:
    """Return the on-disk path for the root file that contains `sid`.
    For a fork id, returns the root's file (the fork is embedded
    inside it). For a root id, returns its own file. For an unknown
    id, returns `<sid>.json` — caller is creating a new root."""
    root_id = _resolve_root_id(sid)
    if root_id is None:
        root_id = sid
    return _root_file_path(root_id)


def session_file_path(sid: str) -> str:
    return str(_session_path(sid))


def root_session_file_path(root_id: str) -> Path:
    """Return a known root's storage path without fork resolution."""
    return _root_file_path(root_id)


def project_external_root_change(root_id: str) -> bool:
    """Fold an explicitly announced external root write into the projection."""
    path = _root_file_path(root_id)
    file_signature = _session_file_signature(path)
    if file_signature is None:
        return False
    try:
        root = _migrate_session(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, KeyError, OSError, ValueError):
        return False
    if root.get("id") != root_id:
        return False
    with _index_lock:
        previous_signature = _root_index_signatures.get(root_id)
    _index_tree(root, force=True, file_signature=file_signature)
    _upsert_summary(
        root,
        preserve_projection_fields=True,
        root_mtime_ns=file_signature[3],
        root_signature=file_signature,
    )
    with _index_lock:
        updated = _fingerprint_after_root_write_locked(
            previous_signature, file_signature, root_id,
        )
        if updated is not None:
            global _index_fingerprint
            _index_fingerprint = updated
        generation = _index_generation
    _persist_index_sidecar_if_loaded(updated, expected_generation=generation)
    return True


def project_external_root_delete(root_id: str) -> bool:
    """Fold an explicitly announced external root delete into the projection."""
    global _index_fingerprint
    with _index_lock:
        file_signature = _root_index_signatures.pop(root_id, None)
        forks = _root_forks.pop(root_id, set())
        for fork_id in forks:
            _fork_index.pop(fork_id, None)
        updated = (
            _fingerprint_after_root_delete_locked(file_signature, root_id)
            if file_signature is not None
            else None
        )
        if updated is not None:
            _index_fingerprint = updated
        _clear_negative_root_resolve_cache()
        generation = _bump_index_generation_locked()
    _remove_summary(root_id)
    if updated is not None:
        _publish_dir_fingerprint_cache(updated, generation)
        _persist_index_sidecar_if_loaded(updated, expected_generation=generation)
    return file_signature is not None or bool(forks)


def session_file_fingerprint(
    root_id: str,
) -> Optional[tuple[int, int, int, int, int]]:
    path = _root_file_path(root_id)
    try:
        st = path.stat()
    except OSError:
        return None
    return (
        st.st_dev,
        st.st_ino,
        st.st_ctime_ns,
        st.st_mtime_ns,
        st.st_size,
    )


def _find_in_tree(root: dict, sid: str) -> Optional[dict]:
    if root.get("id") == sid:
        return root
    for fork in _walk_forks(root):
        if fork.get("id") == sid:
            return fork
    return None


def _find_parent_of(root: dict, sid: str) -> Optional[dict]:
    """Return the dict whose `forks` array contains the session `sid`,
    or None if `sid` is the root or not found."""
    for fork in [root, *_walk_forks(root)]:
        for child in fork.get("forks") or []:
            if child.get("id") == sid:
                return fork
    return None


# ── Schema ────────────────────────────────────────────────────────────


def _event_uuid_from_stored(event: dict) -> Optional[str]:
    """Pull the durable UUID from one stored event entry on a msg's
    events array. Mirrors `orchs.base._event_uuid` / the local helper in
    `session_manager._event_uuid_safe` (kept duplicated to avoid an
    import cycle from session_store → orchs).
    """
    if not isinstance(event, dict):
        return None
    u = event.get("uuid")
    if isinstance(u, str) and u:
        return u
    data = event.get("data")
    if isinstance(data, dict):
        u = data.get("uuid")
        if isinstance(u, str) and u:
            return u
        inner = data.get("event")
        if isinstance(inner, dict):
            inner_data = inner.get("data")
            if isinstance(inner_data, dict):
                u = inner_data.get("uuid")
                if isinstance(u, str) and u:
                    return u
    return None


# Keyed by the "agent-sid SLOT" namespace, NOT the orchestration_mode
# namespace. The word "mode" is overloaded across the codebase into three
# distinct namespaces — keep them straight:
#   1. orchestration_mode (session field): only "native"|"team". No
#      longer selects state shape — both store their primary CLI sid in
#      the single `agent_session_id` field.
#   2. sid slot (THIS map + orchestrator's `discovery_mode`):
#      "native"|"manager"|"supervisor" — manager and native share the
#      primary `agent_session_id` slot; supervisor owns a real separate
#      sid slot (its sidecar session).
#   3. turn source (`run_turn`'s `source` param):
#      "user"|"supervisor"|"adv_sync"|"schedule"|None.
_CLAUDE_SID_FIELD_BY_MODE = {
    "team": "agent_session_id",
    "manager": "agent_session_id",
    "native": "agent_session_id",
    "supervisor": "supervisor_agent_session_id",
}


def _agent_sid_field_for_mode(slot: str) -> str:
    """Resolve which session field stores the CLI jsonl sid for the given
    sid SLOT (namespace #2 above — "native"|"manager"|"supervisor"):
    manager/native → `agent_session_id`, supervisor →
    `supervisor_agent_session_id`."""
    field = _CLAUDE_SID_FIELD_BY_MODE.get(slot)
    if not field:
        raise ValueError(f"unknown agent-sid slot: {slot!r}")
    return field


def _normalize_orchestration_mode(mode: Optional[str]) -> str:
    if mode == "manager":
        return "team"
    if mode in ("team", "native"):
        return mode
    return "team"


def _claude_jsonl_dir_for(provider_record: dict) -> Optional[Path]:
    """Resolve the absolute path to a provider's claude `projects` dir.
    Honors `config_dir` with `$VAR` and `~` expansion, mirroring
    `ClaudeProvider.build_env`. Returns None when the dir doesn't
    exist on disk."""
    raw = (provider_record.get("config_dir") or "").strip()
    if raw:
        base = Path(os.path.expanduser(os.path.expandvars(raw)))
    else:
        base = Path.home() / ".claude"
    proj = base / "projects"
    return proj if proj.exists() else None


def _detect_provider_for_session(
    session: dict, providers: list[dict]
) -> Optional[str]:
    """Best-effort: which provider's `projects/*/<claude_sid>.jsonl`
    actually exists on disk?

    Globs the sid filename across every project dir under each
    provider's CLAUDE_CONFIG_DIR — claude CLI's directory naming
    encodes more than `/`+`_` (e.g. `.` is also flattened, and the
    encoding has changed across CLI versions), so reproducing it in
    Python is a moving target. The sid alone is unique (UUIDv4), so
    glob-by-sid is both simpler and version-proof. Mirrors the same
    glob fallback pattern already used in `orchestrator._compute_jsonl_path`.

    Returns the matching provider_id, or None if zero / multiple
    providers' dirs hold the file. Used as the deterministic backfill
    for legacy sessions.
    """
    sids = [
        sid for sid in (
            session.get("agent_session_id"),
            session.get("supervisor_agent_session_id"),
        ) if sid
    ]
    if not sids:
        return None
    matches: set[str] = set()
    for prov in providers:
        proj_dir = _claude_jsonl_dir_for(prov)
        if proj_dir is None:
            continue
        for sid in sids:
            # `*/sid.jsonl` — match the sid file under any project
            # subdir of this provider's config_dir, regardless of how
            # claude CLI encoded the project's cwd.
            try:
                hit = next(proj_dir.glob(f"*/{sid}.jsonl"), None)
            except OSError:
                hit = None
            if hit is not None:
                matches.add(prov["id"])
                break
    # Exactly-one match wins. Multiple matches means the same sid
    # exists under two providers' config dirs — almost certainly the
    # user copied/symlinked, can't disambiguate, refuse to guess.
    if len(matches) == 1:
        return next(iter(matches))
    return None


def _default_reasoning_effort_for_provider(provider_id: Optional[str]) -> str:
    if not provider_id:
        return ""
    record = config_store.get_provider(provider_id)
    effort = normalize_reasoning_effort(
        (record or {}).get("default_reasoning_effort")
    )
    options = (record or {}).get("reasoning_effort_options") or []
    return effort if effort and effort in options else ""


def _session_reasoning_effort(
    value: Optional[str], provider_id: Optional[str],
) -> str:
    effort = normalize_reasoning_effort(value)
    if effort:
        record = config_store.get_provider(provider_id) if provider_id else None
        options = (
            config_store.reasoning_effort_options_for_provider(record)
            if record else []
        )
        if not options or effort in options:
            return effort
    return _default_reasoning_effort_for_provider(provider_id)


def _kind_for_provider(provider_id: Optional[str]) -> str:
    if not provider_id:
        return ""
    record = config_store.get_provider(provider_id)
    return (record or {}).get("kind", "") or ""


def _default_permission_for_provider(provider_id: Optional[str]) -> dict:
    record = config_store.get_provider(provider_id) if provider_id else None
    kind = (record or {}).get("kind", "") or ""
    default = (record or {}).get("default_permission")
    norm = normalize_permission(kind, default)
    return norm if norm is not None else default_permission_for_kind(kind)


def _session_permission(value: object, provider_id: Optional[str]) -> dict:
    """Effective permission override to persist on the session. Empty dict =
    inherit the provider default (no per-session override)."""
    kind = _kind_for_provider(provider_id)
    norm = normalize_permission(kind, value)
    if norm is not None:
        return norm
    return {}


def _provider_backfill_context() -> dict:
    """Build a per-load context cached during one migration recursion.

    Reads config_store ONCE per top-level migrate call so the recursive
    walk over embedded forks doesn't fire N config_store reads. The
    context also exposes a `dirty` flag the caller can read to decide
    whether to persist the migrated tree."""
    providers: list[dict] = []
    active_id: Optional[str] = None
    try:
        listed = config_store.list_providers().get("providers", []) or []
        # Each provider record from list_providers is the public view
        # (no api_key — we only need config_dir for jsonl-path matching).
        providers = list(listed)
        active = config_store.get_default_provider()
        if active is not None:
            active_id = active["id"]
    except ImportError:
        # Module not yet available (very early init / standalone tests
        # that import session_store before config_store is reachable).
        # Provider binding is deferred to a later load.
        pass
    except (json.JSONDecodeError, OSError):
        # Corrupt or unreadable `~/.better-claude/config.json` — skip
        # backfill rather than crashing every session read. The user's
        # next provider edit will rewrite a valid config.
        pass
    return {"providers": providers, "active_id": active_id, "dirty": [False]}


# v2 → v3 rename table: every claude_* identifier we changed in the
# v3 schema, in the on-disk JSON. Pure 1:1 renames — no semantic shift —
# so safe to apply in place. Used by `_v2_to_v3_migrate`.
_V2_TO_V3_SESSION_FIELDS = {
    "manager_claude_session_id": "manager_agent_session_id",
    "native_claude_session_id": "native_agent_session_id",
    "supervisor_claude_session_id": "supervisor_agent_session_id",
    "forked_from_claude_sid": "forked_from_agent_sid",
}
_V2_TO_V3_MESSAGE_FIELDS = {
    "claude_message_uuid": "agent_message_uuid",
}
_V2_TO_V3_WORKER_FIELDS = {
    "fork_claude_sid": "fork_agent_sid",
    "live_parent_claude_sid": "live_parent_agent_sid",
}


def _rename_keys_in_place(d: dict, mapping: dict[str, str]) -> bool:
    """Rename keys per `mapping`. Returns True iff any key was renamed."""
    changed = False
    for old, new in mapping.items():
        if old in d:
            d[new] = d.pop(old)
            changed = True
    return changed


def _rewrite_event_type_in_place(event: dict) -> bool:
    """v2 events stored on msg.events / msg.manager.events used the
    legacy `type: "claude_message"` wire-shape. v3 renamed to
    `agent_message`. Rewrite in place; returns True if the type
    changed. Pure: no other fields touched."""
    if isinstance(event, dict) and event.get("type") == "claude_message":
        event["type"] = "agent_message"
        return True
    return False


def _rewrite_events_array(events: Optional[list]) -> bool:
    if not isinstance(events, list):
        return False
    changed = False
    for ev in events:
        if _rewrite_event_type_in_place(ev):
            changed = True
    return changed


def _v2_to_v3_migrate(session: dict, ctx: Optional[dict]) -> None:
    """Pure-rename migration: every v2 claude_* key becomes its v3
    agent_* counterpart. Walks the session record, every fork, every
    message, every worker panel. ALSO rewrites legacy
    `type: "claude_message"` event-envelope strings inside the persisted
    `msg.events` and `msg.manager.events` arrays — without this, the
    frontend's `flattenClaudeMessages` (which only unwraps
    `agent_message`) silently drops the historical events and the
    assistant bubble renders empty. Marks `ctx["dirty"]` if anything
    changed so the caller persists the upgraded form.

    Triggered strictly by `_schema_version == 2` so a v3 record that
    happens to be hand-edited with a stray claude_* key (debugging,
    user mucking with the file) doesn't get re-walked and re-persisted
    on every load. v2 records always have _schema_version=2."""
    if session.get("_schema_version") != 2:
        return

    changed = False
    # Top-level session fields (manager/native/supervisor agent ids,
    # forked_from_agent_sid).
    if _rename_keys_in_place(session, _V2_TO_V3_SESSION_FIELDS):
        changed = True

    # Messages: user_msg's agent_message_uuid + worker panels under
    # assistant messages + the events arrays themselves.
    for msg in session.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        if _rename_keys_in_place(msg, _V2_TO_V3_MESSAGE_FIELDS):
            changed = True
        # Rewrite event-envelope types inside the message's events
        # arrays (both the native `events` and the manager-mode
        # `manager.events`). Frontend filters on type=="agent_message"
        # — without this the rendering drops every historical event.
        if _rewrite_events_array(msg.get("events")):
            changed = True
        mgr = msg.get("manager")
        if isinstance(mgr, dict) and _rewrite_events_array(mgr.get("events")):
            changed = True
        # Workers live on assistant msgs as either `msg["workers"]`
        # (top-level) or `msg["manager"]["workers"]` (delegating modes).
        workers = msg.get("workers")
        if isinstance(workers, list):
            for w in workers:
                if isinstance(w, dict):
                    if _rename_keys_in_place(w, _V2_TO_V3_WORKER_FIELDS):
                        changed = True
                    if _rewrite_events_array(w.get("events")):
                        changed = True
        if isinstance(mgr, dict):
            mgr_workers = mgr.get("workers")
            if isinstance(mgr_workers, list):
                for w in mgr_workers:
                    if isinstance(w, dict):
                        if _rename_keys_in_place(w, _V2_TO_V3_WORKER_FIELDS):
                            changed = True
                        if _rewrite_events_array(w.get("events")):
                            changed = True

    # Forks recurse — they're embedded session shapes.
    for fork in session.get("forks") or []:
        if isinstance(fork, dict):
            _v2_to_v3_migrate(fork, ctx)

    # Stamp the version regardless so the next load doesn't re-walk.
    session["_schema_version"] = SCHEMA_VERSION
    if changed and ctx is not None:
        ctx.setdefault("dirty", [False])[0] = True


def _v3_to_v4_migrate(session: dict, ctx: Optional[dict]) -> None:
    """v3 → v4: rewrite legacy `type: "claude_message"` event-envelope
    strings inside the persisted `msg.events` / `msg.manager.events` /
    worker-panel `events` arrays.

    Why this exists: v2 → v3 renamed dict KEYS (manager_claude_session_id
    → manager_agent_session_id, etc.) but missed the event-type strings
    inside the events arrays themselves. v3 records on disk thus have
    msg.events entries like `{"type": "claude_message", "data": ...}` —
    which the frontend's `flattenClaudeMessages` filter drops because
    it only unwraps `agent_message`. Net effect: assistant bubbles
    render empty even though the message has events.

    This pass is idempotent — a v3 record migrated from v2 by the
    extended _v2_to_v3_migrate (which now rewrites types too) reaches
    here with no remaining claude_message entries, so the walk is
    a no-op."""
    if session.get("_schema_version") != 3:
        return
    changed = False
    for msg in session.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        if _rewrite_events_array(msg.get("events")):
            changed = True
        mgr = msg.get("manager")
        if isinstance(mgr, dict) and _rewrite_events_array(mgr.get("events")):
            changed = True
        workers = msg.get("workers")
        if isinstance(workers, list):
            for w in workers:
                if isinstance(w, dict) and _rewrite_events_array(w.get("events")):
                    changed = True
        if isinstance(mgr, dict):
            mgr_workers = mgr.get("workers")
            if isinstance(mgr_workers, list):
                for w in mgr_workers:
                    if isinstance(w, dict) and _rewrite_events_array(w.get("events")):
                        changed = True
    for fork in session.get("forks") or []:
        if isinstance(fork, dict):
            _v3_to_v4_migrate(fork, ctx)
    session["_schema_version"] = SCHEMA_VERSION
    if changed and ctx is not None:
        ctx.setdefault("dirty", [False])[0] = True


def _v4_to_v5_migrate(session: dict, ctx: Optional[dict]) -> None:
    """v4 → v5: collapse supervisor-mode sessions into native + the new
    `supervisor_enabled` toggle.

    Pre-v5, supervisor was a third orchestration_mode value. The
    user-facing session was the supervisor (judge); a hidden
    `kind="supervisor_worker"` fork held the actual agent that did the
    work. v5 makes supervisor an orthogonal toggle: the user-facing
    session IS the primary, and the supervisor is a sidecar identified
    by `supervisor_agent_session_id` on the same record.

    In-place conversion:
      1. Promote the supervisor_worker fork's native_agent_session_id
         onto the parent (the agent that ACTUALLY did the work).
      2. Flip the parent's orchestration_mode to "native" and set
         supervisor_enabled=True.
      3. Drop the supervisor_worker fork from the parent's forks array
         and unindex its id.

    Detection is by `orchestration_mode == "supervisor"`, not by the
    version number — defensive against v2/v3 records that got bumped
    through the version chain without ever running this pass. Idempotent
    on already-migrated records (no supervisor-mode parent left to
    convert).
    """
    if session.get("orchestration_mode") != "supervisor":
        return
    worker_fork = None
    other_forks = []
    for fork in session.get("forks") or []:
        if isinstance(fork, dict) and fork.get("kind") == "supervisor_worker":
            worker_fork = fork
        else:
            other_forks.append(fork)
    if worker_fork is not None:
        worker_sid = worker_fork.get("native_agent_session_id")
        if worker_sid:
            session["native_agent_session_id"] = worker_sid
        # NOTE: don't call _index_pop here — _v4_to_v5_migrate runs
        # under _ensure_index / _refresh_index, which already hold
        # _index_lock (non-reentrant). The index gets rebuilt from the
        # post-migration tree by the caller's walk, so a stale
        # supervisor_worker id simply won't be re-added.
        _fork_index.pop(worker_fork.get("id") or "", None)
    session["forks"] = other_forks
    session["orchestration_mode"] = "native"
    session["supervisor_enabled"] = True
    session["_schema_version"] = SCHEMA_VERSION
    if ctx is not None:
        ctx.setdefault("dirty", [False])[0] = True


def _v7_to_v8_migrate(session: dict, ctx: Optional[dict]) -> None:
    """v7 → v8: move every event off the snapshot into events.jsonl.

    v8 invariant: the on-disk snapshot omits `msg.events`,
    `msg.manager.events`, `msg.workers[*].events`, and
    `msg.manager.workers[*].events` lists. The authoritative event
    stream lives in `<ba_home>/sessions/<root_id>/events.jsonl`
    (append-only, single-writer-per-process). The in-memory cache
    keeps the live lists; they are rehydrated from events.jsonl on
    cold load via `render_tree_hydrate.hydrate_msg_events_from_jsonl`.

    Migration walks every msg in this root + every embedded fork. For
    each event found:

      - `msg.events` / `msg.manager.events` — ingest as-is (these are
        the same shape live `apply_event` writes to events.jsonl).
      - `msg.workers[panel].events` / `msg.manager.workers[panel].events`
        — wrap each inner event as `{type: "worker_event", data:
        {delegation_id: panel.id, event: inner}}` and ingest. Mirrors
        the live ingest at `backend/orchs/base.py:553-563`. Hydration
        then routes back to the panel via `apply_event(live=False)` →
        `apply_worker_panel_event`.

    Idempotent against an already-populated events.jsonl — the
    ingester's `uid:sha256(data)` dedup makes repeat writes no-ops. So
    re-running the migration on a v8-bumped record with on-disk events
    still present (e.g. crash between ingest and the next write) is
    safe. After the bump, the next `write_session_full` strips the
    on-disk event fields (see `_strip_volatile_from_tree`).

    Only runs on ROOT records (forks are walked here). Detection is by
    `_schema_version == 7`. The chain `setdefault('_schema_version',
    SCHEMA_VERSION)` in `_migrate_session` is what bumps records that
    skipped earlier numbered migrations; this function intentionally
    runs BEFORE that setdefault so v7-on-disk records get the explicit
    event ingest before the bump.
    """
    if session.get("parent_session_id"):
        return
    if session.get("_schema_version") != 7:
        return
    root_id = session.get("id")
    if not isinstance(root_id, str) or not root_id:
        return

    from event_journal import publish_event_sync

    # Pass cwd explicitly so the writer thread does NOT call
    # `session_manager.get(root_id)` to look it up — at this point the
    # session is mid-migration and not yet in the session_manager
    # cache; the lookup would recursively re-enter `_load_root` for
    # the same root and blow the stack. Cwd is best-effort for file-ref
    # rewrite; empty string is fine (the original `apply_event` calls
    # already rewrote refs when these events first landed).
    root_cwd = session.get("cwd") or ""

    def _migrate_node(node: dict) -> None:
        node_sid = node.get("id")
        if not isinstance(node_sid, str) or not node_sid:
            return
        for msg in node.get("messages") or []:
            if not isinstance(msg, dict):
                continue
            msg_id = msg.get("id") if isinstance(msg.get("id"), str) else None

            # msg.events (native mode)
            for ev in msg.get("events") or []:
                if not isinstance(ev, dict):
                    continue
                etype = ev.get("type") or "unknown"
                data = ev.get("data")
                if not isinstance(data, dict):
                    continue
                try:
                    publish_event_sync(
                        session_id=root_id,
                        context_id=node_sid,
                        event_type=etype,
                        data=data,
                        source="v8_migration",
                        run_id=None,
                        message_id=msg_id,
                        cwd_override=root_cwd,
                        dedupe_by_uid_only=True,
                    )
                except Exception:
                    _logger.exception(
                        "v7→v8: ingest failed for msg.events node=%s msg=%s",
                        node_sid, msg_id,
                    )

            # msg.manager.events (manager mode)
            mgr = msg.get("manager")
            if isinstance(mgr, dict):
                for ev in mgr.get("events") or []:
                    if not isinstance(ev, dict):
                        continue
                    etype = ev.get("type") or "unknown"
                    data = ev.get("data")
                    if not isinstance(data, dict):
                        continue
                    try:
                        publish_event_sync(
                            session_id=root_id,
                            context_id=node_sid,
                            event_type=etype,
                            data=data,
                            source="v8_migration",
                            run_id=None,
                            message_id=msg_id,
                            cwd_override=root_cwd,
                            dedupe_by_uid_only=True,
                        )
                    except Exception:
                        _logger.exception(
                            "v7→v8: ingest failed for msg.manager.events "
                            "node=%s msg=%s",
                            node_sid, msg_id,
                        )

            # msg.workers[panel].events — wrap inner events as worker_event.
            for panel in msg.get("workers") or []:
                if not isinstance(panel, dict):
                    continue
                delegation_id = panel.get("delegation_id") or panel.get("id")
                if not isinstance(delegation_id, str) or not delegation_id:
                    continue
                for inner in panel.get("events") or []:
                    if not isinstance(inner, dict):
                        continue
                    try:
                        publish_event_sync(
                            session_id=root_id,
                            context_id=node_sid,
                            event_type="worker_event",
                            data={
                                "delegation_id": delegation_id,
                                "event": inner,
                            },
                            source="v8_migration",
                            run_id=None,
                            message_id=msg_id,
                            cwd_override=root_cwd,
                            dedupe_by_uid_only=True,
                        )
                    except Exception:
                        _logger.exception(
                            "v7→v8: ingest failed for msg.workers panel=%s "
                            "node=%s msg=%s",
                            delegation_id, node_sid, msg_id,
                        )

            # msg.manager.workers[panel].events — legacy/manager-mode
            # panel storage.
            if isinstance(mgr, dict):
                for panel in mgr.get("workers") or []:
                    if not isinstance(panel, dict):
                        continue
                    delegation_id = (
                        panel.get("delegation_id") or panel.get("id")
                    )
                    if not isinstance(delegation_id, str) or not delegation_id:
                        continue
                    for inner in panel.get("events") or []:
                        if not isinstance(inner, dict):
                            continue
                        try:
                            publish_event_sync(
                                session_id=root_id,
                                context_id=node_sid,
                                event_type="worker_event",
                                data={
                                    "delegation_id": delegation_id,
                                    "event": inner,
                                },
                                source="v8_migration",
                                run_id=None,
                                message_id=msg_id,
                                cwd_override=root_cwd,
                                dedupe_by_uid_only=True,
                            )
                        except Exception:
                            _logger.exception(
                                "v7→v8: ingest failed for "
                                "msg.manager.workers panel=%s node=%s msg=%s",
                                delegation_id, node_sid, msg_id,
                            )

    _migrate_node(session)
    for fork in _walk_forks(session):
        _migrate_node(fork)

    session["_schema_version"] = SCHEMA_VERSION
    for fork in _walk_forks(session):
        fork["_schema_version"] = SCHEMA_VERSION

    if ctx is not None:
        ctx.setdefault("dirty", [False])[0] = True


def _v8_to_v9_migrate(session: dict, ctx: Optional[dict]) -> None:
    """v8 → v9: consolidate the manager and native orchestration modes
    into one message/session shape (no wipe — existing sessions survive).

    - Collapse `manager_agent_session_id` / `native_agent_session_id`
      into a single `agent_session_id`. The active CLI thread is chosen
      by `orchestration_mode`; falls back to whichever slot is set.
      `supervisor_agent_session_id` is untouched.
    - Flatten each assistant msg's `manager` scope: `manager.events` →
      `msg.events`, `manager.session_id` → `msg.agent_session_id`,
      `manager.workers` → `msg.workers`; drop the `manager` key.

    Shape-detected (runs whenever an old sid field or a `msg.manager`
    scope is present on the root or any fork) so it is robust to records
    that an earlier numbered migration already stamped to the current
    `_schema_version` without flattening. Walks root + every fork."""
    def _node_needs(node: dict) -> bool:
        if not isinstance(node, dict):
            return False
        if "manager_agent_session_id" in node or "native_agent_session_id" in node:
            return True
        for m in node.get("messages") or []:
            if isinstance(m, dict) and isinstance(m.get("manager"), dict):
                return True
        return False

    nodes = [session, *_walk_forks(session)]
    if not any(_node_needs(n) for n in nodes):
        return

    for node in nodes:
        if not isinstance(node, dict):
            continue
        if "manager_agent_session_id" in node or "native_agent_session_id" in node:
            mode = _normalize_orchestration_mode(node.get("orchestration_mode"))
            mgr_sid = node.pop("manager_agent_session_id", None)
            nat_sid = node.pop("native_agent_session_id", None)
            primary = mgr_sid if mode == "team" else nat_sid
            if primary is None:
                primary = mgr_sid if mgr_sid is not None else nat_sid
            if node.get("agent_session_id") is None:
                node["agent_session_id"] = primary
        for m in node.get("messages") or []:
            if not isinstance(m, dict):
                continue
            mgr = m.get("manager")
            if not isinstance(mgr, dict):
                continue
            mgr_events = mgr.get("events")
            if isinstance(mgr_events, list) and mgr_events:
                m["events"] = (m.get("events") or []) + mgr_events
            else:
                m.setdefault("events", [])
            if m.get("agent_session_id") is None:
                m["agent_session_id"] = mgr.get("session_id")
            mgr_workers = mgr.get("workers")
            if isinstance(mgr_workers, list) and mgr_workers:
                m["workers"] = (m.get("workers") or []) + mgr_workers
            del m["manager"]
        node["_schema_version"] = SCHEMA_VERSION

    if ctx is not None:
        ctx.setdefault("dirty", [False])[0] = True


def _migrate_session(session: dict, ctx: Optional[dict] = None) -> dict:
    """Apply additive defaults so new fields exist on records written by
    an older revision of this same schema. Recursively migrates embedded
    forks. Raises ValueError on the v1 legacy shape (a top-level record
    with `parent_session_id` set — these need to be wiped).

    `ctx` carries cached provider records + a `dirty` flag the caller
    inspects to persist on first detection. When omitted, a fresh
    context is built (single-shot load path)."""
    # v1 legacy detection: a top-level file whose record has a parent
    # set is from before the tree refactor. We can't safely migrate
    # because the parent file may have its own diverged state.
    if session.get("parent_session_id") and "_schema_version" not in session:
        # Only raise if this looks like it was a top-level read (not an
        # embedded fork being normalized). A top-level read is the
        # entry path through get_session; embedded forks come through
        # _migrate_session via the recursive call below where the
        # parent's _schema_version is already set.
        if not session.get("_legacy_ok"):
            raise ValueError(
                "Legacy fork session found at top level. Schema v2 embeds "
                "forks inside their parent. Wipe ~/.better-claude/sessions/ "
                "to start fresh."
            )

    # v2 → v3: claude_* identifiers renamed to agent_*. Pure 1:1 rename.
    _v2_to_v3_migrate(session, ctx)
    # v3 → v4: rewrite legacy `type: "claude_message"` event-envelope
    # strings inside msg.events arrays so the frontend keeps rendering them.
    _v3_to_v4_migrate(session, ctx)
    # v4 → v5: collapse old supervisor-mode sessions into native + the
    # supervisor_enabled toggle.
    _v4_to_v5_migrate(session, ctx)
    # v7 → v8: move msg events into events.jsonl (snapshot → metadata-only).
    _v7_to_v8_migrate(session, ctx)
    # v8 → v9: consolidate manager + native into one shape — collapse the
    # two CLI sid fields to a single `agent_session_id` and flatten
    # `msg.manager.{events,session_id}` onto `msg.events` /
    # `msg.agent_session_id`. Preserves existing sessions (no wipe).
    _v8_to_v9_migrate(session, ctx)

    if ctx is None:
        ctx = _provider_backfill_context()

    if session.get("_schema_version") != SCHEMA_VERSION:
        session["_schema_version"] = SCHEMA_VERSION
        ctx["dirty"][0] = True
    session.setdefault("agent_session_id", None)
    normalized_mode = _normalize_orchestration_mode(
        session.get("orchestration_mode")
    )
    if session.get("orchestration_mode") != normalized_mode:
        session["orchestration_mode"] = normalized_mode
        ctx["dirty"][0] = True
    session.setdefault("supervisor_agent_session_id", None)
    # True once the supervisor sub-session has successfully consumed
    # the full adversarial bootstrap preamble. Subsequent verdicts then
    # send a compact prompt that relies on the supervisor's accumulated
    # claude session context. Persisted so reload/reboot doesn't re-send
    # the full preamble; only flipped in `_verdict.request_verdict` AFTER
    # a successful turn so a mid-turn failure leaves the flag False and
    # the next attempt resends the full preamble.
    session.setdefault("supervisor_bootstrap_received", False)
    # provider_id: which provider record drives this session's claude
    # CLI invocations. Backfill order:
    #   1. Detect by jsonl presence — the ground truth.
    #   2. Fall back to currently-active provider — tagged with
    #      `_provider_id_source: "active_fallback"` so a future load
    #      can re-attempt detection (e.g. user added the right
    #      provider after the first load).
    #   3. Leave None (config_store unavailable) — orchestrator's
    #      `_provider_for_session` falls back to active at call time.
    # Detected/assigned id is persisted by the caller via the `dirty`
    # flag so subsequent loads stay deterministic regardless of what's
    # active later.
    needs_detection = (
        not session.get("provider_id")
        or session.get("_provider_id_source") == "active_fallback"
    )
    if needs_detection:
        detected = _detect_provider_for_session(session, ctx["providers"])
        if detected and detected != session.get("provider_id"):
            session["provider_id"] = detected
            session.pop("_provider_id_source", None)
            ctx["dirty"][0] = True
        elif detected:
            # Same id we already had (detected) — strip stale fallback marker
            if "_provider_id_source" in session:
                session.pop("_provider_id_source", None)
                ctx["dirty"][0] = True
        elif not session.get("provider_id"):
            # Genuinely first-time backfill, no detection match: tag as
            # active_fallback so future loads retry once providers are
            # configured the right way.
            chosen = ctx.get("active_id")
            if chosen:
                session["provider_id"] = chosen
                session["_provider_id_source"] = "active_fallback"
                ctx["dirty"][0] = True
    if "reasoning_effort" not in session:
        session["reasoning_effort"] = _default_reasoning_effort_for_provider(
            session.get("provider_id")
        )
        ctx["dirty"][0] = True
    else:
        normalized_effort = _session_reasoning_effort(
            session.get("reasoning_effort"), session.get("provider_id")
        )
        if normalized_effort != session.get("reasoning_effort"):
            session["reasoning_effort"] = normalized_effort
            ctx["dirty"][0] = True
    stored_permission = session.get("permission")
    normalized_permission = _session_permission(
        stored_permission, session.get("provider_id")
    )
    if normalized_permission != stored_permission:
        session["permission"] = normalized_permission
        ctx["dirty"][0] = True
    session.setdefault("worker_eligible", False)
    if session.get("worker_creation_policy") not in ("ask", "approve", "deny"):
        session["worker_creation_policy"] = "ask"
    session.setdefault("supervisor_enabled", False)
    session.setdefault("supervisor_custom_prompt", "")
    session.setdefault("pending_supervisor_verdict", None)
    # Backfill `user_initiated` BEFORE the source coercion below clobbers
    # non-(web,cli) source values (e.g. "internal"/"extension") to "web" —
    # those source labels are a signal `_infer_user_initiated` relies on.
    if "user_initiated" not in session:
        session["user_initiated"] = _infer_user_initiated(session)
        ctx.setdefault("dirty", [False])[0] = True
    src = session.get("source")
    if src not in _VALID_SESSION_SOURCES:
        session["source"] = "web"
    session.setdefault("processed_line_by_sid", {})
    session.setdefault("parent_session_id", None)
    session.setdefault("forked_from_agent_sid", None)
    # One-shot "next supervisor turn must --fork-session this sid" marker.
    # Cleared after the supervisor's first post-separate verdict succeeds.
    # Independent of forked_from_agent_sid (which is the native/manager
    # marker) so the two sid fields have local, non-overloaded semantics.
    session.setdefault("forked_from_supervisor_agent_sid", None)
    # Delegate-fork discriminator + lineage (see create_delegate_fork).
    # Kind discriminator — "user" (default; root or user-facing fork),
    # "delegate_fork" (per-pair manager-mode delegate fork),
    # "supervisor_worker" (the worker side of a supervisor session),
    # "sub_session" (hidden native child addressable by mssg/ask), or
    # "adv_sync_fork" (one of the two adversarial-sync forks driven by
    # orchs/adv_sync). Frontend hides non-user kinds from sidebar fork count;
    # "adv_sync_fork" remains visible in ForkSplitView so the user can drill in.
    # session_watcher still tails the jsonls of every kind.
    if session.get("kind") not in (
        "user", "delegate_fork", "supervisor_worker", "sub_session",
        "adv_sync_fork",
    ):
        # Backfill from the legacy `is_delegate_fork` boolean if present.
        legacy = bool(session.pop("is_delegate_fork", False))
        session["kind"] = "delegate_fork" if legacy else "user"
    session.setdefault("caller_agent_session_id", None)
    session.setdefault("parent_line_count_at_fork", None)
    session.setdefault("continuation_chain", [])
    session.setdefault("continuation_requested", None)
    # Fork-point seq — the parent's last persisted message seq at fork
    # time. None on roots. Frontend uses this to slice the rendered
    # messages for split-pane rendering.
    session.setdefault("fork_point_seq", None)
    # fork_closed — once true, this fork is locked (no focus, no new
    # prompts). UI dims the pane.
    session.setdefault("fork_closed", False)
    # Embedded children. Roots and forks alike can have nested forks.
    session.setdefault("forks", [])
    session.setdefault("inline_tags", [])
    # Adversarial-sync overlays — per-message text substitutions produced
    # by the orchs.adv_sync ping-pong loop. Same persistence pattern as
    # inline_tags (full list shipped via session_metadata_updated).
    session.setdefault("adv_sync_overlays", [])
    # User-/agent-opened file panels for the tabbed/split right-panel
    # viewer. Additive metadata (like inline_tags) — the LIST + the
    # agent-requested focus/selection is persisted; the user's live
    # scroll/selection within a panel stays frontend-transient.
    session.setdefault("open_file_panels", [])
    session.setdefault("open_config_panels", [])
    session.setdefault("notes", [])
    # Cross-provider TODO/task lists reconstructed by the public Todos
    # extension from provider tool events.
    # `setdefault` only — the AUTHORITATIVE backfill happens in
    # `session_manager._load_root` AFTER `hydrate_msg_events_from_jsonl`
    # by reading events.jsonl directly. That's the only source that
    # sees BOTH named events AND msg_id=None orphan rows, which the
    # hydrate fast path (render_tree_hydrate.py:76-91) skips entirely
    # for pre-v8 sessions whose msg.events is already populated on
    # disk. Walking msg.events here would miss those orphans —
    # verified against real session data
    # (~/.better-claude/sessions/12eb332c... has 4 orphan TodoWrites).
    session.setdefault("current_todos", [])
    session.setdefault("current_tasks", [])
    # Per-session UI state restored when switching sessions.
    session.setdefault("right_panel_open", False)
    session.setdefault("right_panel_active_tab", None)
    session.setdefault("right_panel_width", None)
    session.setdefault("right_panel_mobile_height", None)
    session.setdefault("right_panel_todos_dismissed", False)
    session.setdefault("right_panel_auto_opened_by", [])
    session.setdefault("sidebar_minimized", False)
    for app_field in ("draft_input", "draft_input_seq", "draft_images"):
        if app_field in session:
            session.pop(app_field)
            ctx["dirty"][0] = True
    session.setdefault("capability_contexts", [])
    session.setdefault("working_mode", None)
    session.setdefault("working_mode_meta", None)
    session.setdefault("browser_harness_enabled", True)
    session.setdefault("browser_harness_headless", True)
    session.setdefault("bare_config", False)
    session.setdefault("disallowed_tools", [])
    session.setdefault("pinned", False)
    session.setdefault("topbar_pinned", False)
    session.setdefault("topbar_pinned_at", None)
    session.setdefault("archived", False)
    # Whether the agent itself is allowed to rename this session's title
    # after creation (e.g. via the "ai-title" auto-naming event). Does not
    # gate the user's own manual rename endpoint or `name_locked`.
    session.setdefault("agent_rename_allowed", False)
    # node_id: which machine in topology.yaml hosts this session's
    # workers by default. Defaults to "primary" so single-machine
    # deployments (and pre-v7 records) keep working unchanged.
    # Forks inherit from parent at create time (fork_session /
    # create_delegate_fork); legacy forks lacking the key are stamped
    # by the v10 block in the parent's pass before this default fires.
    session.setdefault("node_id", "primary")
    # last_seen_event_uid — the UUID of the most recent rendered event
    # the user has acknowledged viewing for this session (sent by the
    # frontend on focus change). Drives the per-session unread badge:
    # unread_count = number of distinct event UUIDs appended to this
    # session's render tree after this uid.
    #
    # `_unread_migrated` — one-shot marker that the "stamp every
    # pre-existing event as already-seen" migration has run. Without
    # this, every legacy session would surface its entire history as
    # "unread" the moment the feature rolls forward (a 100-message
    # session shows 99+). On first load, walk msg.events to find the
    # latest UUID, stamp it as the ack head, and
    # mark migrated. Subsequent loads skip — so a user who explicitly
    # clears their ack (mark_seen(None) when no events exist) doesn't
    # get re-migrated on every load.
    session.setdefault("last_seen_event_uid", None)
    if not session.get("_unread_migrated"):
        latest_uid: Optional[str] = None
        for _msg in session.get("messages") or []:
            if _msg.get("role") != "assistant":
                continue
            for _ev in _msg.get("events") or []:
                _u = _event_uuid_from_stored(_ev)
                if _u:
                    latest_uid = _u
        if latest_uid is not None:
            session["last_seen_event_uid"] = latest_uid
        session["_unread_migrated"] = True
        if ctx is not None:
            ctx.setdefault("dirty", [False])[0] = True

    msgs = session.get("messages") or []
    needs_backfill = any(m.get("seq") is None for m in msgs)
    if needs_backfill:
        cursor = 0
        for m in msgs:
            if m.get("seq") is None:
                m["seq"] = cursor
            cursor = max(cursor, int(m["seq"])) + 1
        session["next_seq"] = cursor
    else:
        highest = max((int(m["seq"]) for m in msgs), default=-1)
        session.setdefault("next_seq", highest + 1)
    session["messages"] = msgs

    # v3 migration: move worker-sourced messages from the supervisor root
    # into the paired supervisor_worker child. Before the persist_to split,
    # all messages lived on the supervisor session. After, each panel reads
    # from its own session node.
    if (
        session.get("orchestration_mode") == "supervisor"
        and not session.get("_worker_msg_migrated")
    ):
        sw_fork = None
        for f in session.get("forks") or []:
            if f.get("kind") == "supervisor_worker":
                sw_fork = f
                break
        if sw_fork is not None:
            root_msgs = session.get("messages") or []
            worker_msgs = [
                m for m in root_msgs if m.get("source") == "worker"
            ]
            if worker_msgs:
                remaining = [m for m in root_msgs if m.get("source") != "worker"]
                # Clear seqs so the backfill below re-assigns them.
                for m in remaining:
                    m.pop("seq", None)
                for m in worker_msgs:
                    m.pop("seq", None)
                session["messages"] = remaining
                fork_msgs = sw_fork.get("messages") or []
                fork_msgs.extend(worker_msgs)
                for m in fork_msgs:
                    m.pop("seq", None)
                sw_fork["messages"] = fork_msgs
                ctx["dirty"][0] = True
            session["_worker_msg_migrated"] = True
            ctx["dirty"][0] = True

    # v9 → v10: stamp node_id on forks that lack it on disk (records
    # written before fork_session inherited node_id). Must run in the
    # PARENT's pass — the rule needs the parent's resolved node_id —
    # so it sits just before the fork recursion (parent-before-children).
    # Rule: a never-ran fork (no agent_session_id) inherits the parent's
    # node — its forked_from_agent_sid points at jsonl on the parent's
    # node. A fork WITH turns keeps "primary": provider routing read the
    # in-memory "primary" default for these records, so its own claude
    # jsonl lives on primary's disk and re-pointing would break resume.
    # Forks that already carry node_id on disk (delegate forks, post-v10
    # user forks) are untouched. Shape-detected (key absence), like the
    # rest of the chain.
    for fork in session.get("forks") or []:
        if isinstance(fork, dict) and "node_id" not in fork:
            fork["node_id"] = (
                (session.get("node_id") or "primary")
                if not fork.get("agent_session_id")
                else "primary"
            )
            if ctx is not None:
                ctx.setdefault("dirty", [False])[0] = True

    # Recurse into embedded forks. Mark them with `_legacy_ok` so the
    # parent-set check above doesn't raise on them — embedded forks
    # SHOULD have a parent. Pass the same `ctx` so we don't re-read
    # config_store per fork.
    for fork in session.get("forks") or []:
        fork["_legacy_ok"] = True
        _migrate_session(fork, ctx)
        fork.pop("_legacy_ok", None)

    return session


def assign_message_seq(session: dict, message: dict) -> dict:
    seq = int(session.get("next_seq", 0))
    message["seq"] = seq
    session["next_seq"] = seq + 1
    return message


# ── Lifecycle ─────────────────────────────────────────────────────────


from root_lifecycle import serialized_root_argument


@serialized_root_argument(position=17, keyword="id")
def create_session(
    name: str = "",
    model: Optional[str] = None,
    cwd: str = "",
    orchestration_mode: str = "team",
    source: str = "web",
    provider_id: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
    permission: Optional[dict] = None,
    browser_harness_enabled: bool = True,
    browser_harness_headless: bool = True,
    node_id: str = "primary",
    worker_creation_policy: str = "ask",
    bare_config: bool = False,
    user_initiated: bool = False,
    disallowed_tools: Optional[list[str]] = None,
    disabled_builtin_extensions: Optional[list[str]] = None,
    storage_scope: Optional[dict] = None,
    id: Optional[str] = None,
    created_at: Optional[str] = None,
) -> dict:
    """Create a new ROOT session and persist it.

    `user_initiated` records whether the user is AWARE of having created
    this session (UI/CLI create, import, file-edit, an approved worker
    popup) versus a session the system or an agent spun up on its own
    (provisioning, agent `create_session`, auto-approved workers). It is
    orthogonal to `source` — see the module-level taxonomy. Defaults to
    False so any caller that forgets to pass it is treated as NOT
    user-aware (fail-closed: hidden helper sessions never leak into
    user-facing surfaces just because a caller omitted the flag).

    `provider_id`, `model`, and `reasoning_effort` default to the
    `default_session` internal-LLM assignment (which itself falls back to
    the active provider's values) — see `config_store.resolve_internal_llm`.
    `provider_id` is stamped on the session at create time and is intended
    to be immutable thereafter (the underlying claude jsonl lives under that
    provider's `CLAUDE_CONFIG_DIR`, so swapping it would orphan the
    session's history).

    `id` lets callers stamp a stable, well-known id (e.g. the Ask
    singleton). When omitted, a fresh UUID is generated. Duplicate ids
    raise `ValueError` so the singleton lazy-create path can race-safely
    bail on collision.
    """
    from canonical_runtime_journal import canonical_runtime_journal
    if id is not None:
        canonical_runtime_journal().resolve_pending_deletions(root_id=id)
    _ensure_dir()
    normalized_storage_scope = _normalize_storage_scope(storage_scope)
    storage_dir = _storage_dir_for_scope(normalized_storage_scope)
    storage_dir.mkdir(parents=True, exist_ok=True)
    if provider_id is None:
        provider_id = config_store.default_session_provider_id()
    if not model:
        model = config_store.default_session_model()
    if reasoning_effort is None:
        reasoning_effort = config_store.default_session_reasoning_effort() or None
    sid = id or str(uuid.uuid4())
    if id is not None and _root_file_path(sid).exists():
        raise ValueError(f"session id already exists: {sid}")
    _remember_root_file_dir(sid, storage_dir)
    resolved_reasoning_effort = _session_reasoning_effort(
        reasoning_effort, provider_id,
    )
    session = {
        "id": sid,
        "_owner_incarnation": uuid.uuid4().hex,
        "_schema_version": SCHEMA_VERSION,
        "name": name or t("session.default_name", time=datetime.now().strftime('%H:%M')),
        "model": model,
        "reasoning_effort": resolved_reasoning_effort,
        "permission": _session_permission(permission, provider_id),
        "cwd": cwd or str(Path.home()),
        "cwd_explicit": bool(cwd),
        # `created_at` may be supplied (native import preserves the original
        # conversation time so analytics bucket it under its real date, not now).
        "created_at": created_at or datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
        "orchestration_mode": _normalize_orchestration_mode(
            orchestration_mode
        ),
        "provider_id": provider_id,
        "last_active_provider_id": None,
        "last_active_model": None,
        "last_active_supervisor_provider_id": None,
        "last_active_supervisor_model": None,
        "agent_session_id": None,
        "supervisor_agent_session_id": None,
        "supervisor_bootstrap_received": False,
        "source": source if source in _VALID_SESSION_SOURCES else "web",
        # Whether the user is aware of having created this session. See the
        # module-level user-initiation taxonomy. Orthogonal to `source`.
        "user_initiated": bool(user_initiated),
        "processed_line_by_sid": {},
        "parent_session_id": None,
        "forked_from_agent_sid": None,
        "forked_from_supervisor_agent_sid": None,
        "fork_point_seq": None,
        "fork_closed": False,
        "forks": [],
        # Kind discriminator — see _migrate_session for the values.
        # Defaults to "user" on fresh roots / user-facing forks.
        "kind": "user",
        "caller_agent_session_id": None,
        "parent_line_count_at_fork": None,
        "messages": [],
        "next_seq": 0,
        "inline_tags": [],
        "adv_sync_overlays": [],
        "open_file_panels": [],
        "open_config_panels": [],
        "notes": [],
        "current_todos": [],
        "current_tasks": [],
        "right_panel_open": False,
        "right_panel_active_tab": None,
        "right_panel_width": None,
        "right_panel_mobile_height": None,
        "right_panel_todos_dismissed": False,
        "right_panel_auto_opened_by": [],
        "sidebar_minimized": False,
        "queued_prompts": [],
        "capability_contexts": [],
        "browser_harness_enabled": browser_harness_enabled,
        "browser_harness_headless": browser_harness_headless,
        "worker_creation_policy": (
            worker_creation_policy
            if worker_creation_policy in ("ask", "approve", "deny")
            else "ask"
        ),
        "bare_config": bool(bare_config),
        "disallowed_tools": list(dict.fromkeys(str(tool).strip() for tool in (disallowed_tools or []) if str(tool).strip())),
        "worker_eligible": False,
        # New sessions start UNPINNED. While a session is empty (0 messages)
        # the sort key (`isEmpty, pinned, ...` in `_session_list_sort_key`)
        # already floats it to the top, so it stays visible without a pin.
        # Defaulting to pinned made every session stick to the top forever
        # once it gained messages — not what the user wants.
        "pinned": False,
        "topbar_pinned": False,
        "topbar_pinned_at": None,
        "archived": False,
        "supervisor_enabled": False,
        "supervisor_custom_prompt": "",
        "pending_supervisor_verdict": None,
        "node_id": node_id,
        "last_seen_event_uid": None,
        "_unread_migrated": True,
        "token_usage_total": {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
        "token_usage_last": None,
        "continuation_chain": [],
    }
    if normalized_storage_scope is not None:
        session["storage_scope"] = normalized_storage_scope
    if disabled_builtin_extensions is not None:
        session["disabled_builtin_extensions"] = list(dict.fromkeys(
            str(item).strip()
            for item in disabled_builtin_extensions
            if str(item or "").strip()
        ))
    # Route through `write_session_full` so the single write funnel
    # (and its `_upsert_summary` hook) covers fresh sessions too.
    # `bump_updated_at=False` because the in-memory record already
    # carries the just-set `updated_at` from above.
    write_session_full(session, bump_updated_at=False)
    return session


def _migrate_and_persist(root: dict) -> dict:
    """Run `_migrate_session` and, if the migration produced a real
    field change (e.g. one-shot `provider_id` backfill), write the
    tree back to disk so the next load sees the same value. Persists
    without bumping `updated_at` — backfill isn't user-visible activity.

    Routed through `_root_writer_guard` (registered by `session_manager`)
    when available: the guard serializes the write against
    `session_manager`'s per-root lock and skips it entirely when the
    root is currently resident in memory, so an unlocked bulk walker
    (`iter_all_sessions`, used by session_watcher/run_recovery) can never
    overwrite a live turn's in-memory mutation with a stale disk snapshot.
    Falls back to a direct write when the guard isn't registered yet
    (pre-`session_manager`-init callers, e.g. standalone scripts)."""
    ctx = _provider_backfill_context()
    migrated = _migrate_session(root, ctx)
    if ctx["dirty"][0]:
        try:
            if _root_writer_guard is not None:
                _root_writer_guard(
                    migrated["id"],
                    lambda: write_session_full(migrated, bump_updated_at=False),
                )
            else:
                write_session_full(migrated, bump_updated_at=False)
        except Exception:
            # Persistence failure is non-fatal: in-memory state is
            # correct for this load; next load will retry detection.
            pass
    return migrated


@perf.timed_fn("store.session.get")
def get_session(session_id: str) -> Optional[dict]:
    """Return the session record for `session_id` (root or fork). For a
    fork, returns the embedded dict inside its parent's tree — mutating
    it in memory is safe; persistence still requires writing the root."""
    root_id = _resolve_root_id(session_id)
    if root_id is None:
        return None
    path = _root_file_path(root_id)
    if not path.exists():
        return None
    root = _migrate_and_persist(json.loads(path.read_text(encoding="utf-8")))
    # Re-index in case the file was written by another process and
    # contains forks not yet in our in-memory map.
    _index_tree(root, force=True)
    _overlay_seen_cursors(root, root_id)
    _overlay_last_opened(root, root_id)
    return _find_in_tree(root, session_id)


def _cached_migrated_root(root_id: str, file_signature: FileSignature, root: dict) -> dict:
    cache_key = (root_id, file_signature)
    with _migrated_root_cache_lock:
        cached = _migrated_root_cache.get(cache_key)
    if cached is not None:
        with perf.timed("store.session.get_root_tree.migrate.cache_hit"):
            return _copy_jsonish(cached)
    migrated = _migrate_and_persist(root)
    with _migrated_root_cache_lock:
        if len(_migrated_root_cache) >= _MIGRATED_ROOT_CACHE_MAX:
            _migrated_root_cache.pop(next(iter(_migrated_root_cache)), None)
        _migrated_root_cache[cache_key] = _copy_jsonish(migrated)
    return migrated


def read_node_kind_record(root_id: str, sid: str) -> Optional[dict]:
    """Pure, side-effect-free read of just `{"kind": ...}` for node `sid`
    in root `root_id`. NO migration, NO draft overlay, NO disk write —
    unlike `get_root_tree`, so a hot read-only caller (recompute_state's
    kind gate) never triggers a loop-thread write or a draft seed.
    Returns None when the root file or the node is absent."""
    path = _root_file_path(root_id)
    if not path.exists():
        return None
    try:
        root = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    node = _find_in_tree(root, sid) if isinstance(root, dict) else None
    if node is None:
        return None
    return {"kind": node.get("kind")}


@perf.timed_fn("store.session.get_root_tree")
def get_root_tree(session_id: str) -> Optional[dict]:
    """Return the FULL root tree containing `session_id` (so an embedded
    fork id resolves to its root). Used by the API so a single REST
    call hands the frontend the whole tree."""
    with perf.timed("store.session.get_root_tree.resolve_root"):
        root_id = _resolve_root_id(session_id)
    if root_id is None:
        return None
    with perf.timed("store.session.get_root_tree.read_json"):
        path = _root_file_path(root_id)
        if not path.exists():
            return None
        file_signature = _session_file_signature(path)
        root = json.loads(path.read_text(encoding="utf-8"))
    with perf.timed("store.session.get_root_tree.migrate"):
        root = _cached_migrated_root(root_id, file_signature, root)
    with perf.timed("store.session.get_root_tree.index_tree"):
        if session_id != root_id:
            _index_tree(root, file_signature=file_signature)
    with perf.timed("store.session.get_root_tree.overlay_seen"):
        _overlay_seen_cursors(root, root_id)
    with perf.timed("store.session.get_root_tree.overlay_opened"):
        _overlay_last_opened(root, root_id)
    return root


def _strip_volatile_from_tree(root: dict) -> dict:
    """Pop fields that MUST NOT live on disk (schema v8 invariant) from
    every message in the tree (root + every embedded fork). Returns a
    single popped struct so the caller can restore in-memory state
    in a single `finally`.

    Popped fields:

      - `msg.isStreaming` — derived view of runner registration in
        `orchestrator._run_state`. Persisting it would re-introduce the
        two-source-of-truth drift that motivated removing
        `_reap_zombie_streaming`. (Pre-v8 behavior.)

      - `msg.events`, `msg.workers[*].events` — the
        authoritative event stream lives in
        `<ba_home>/sessions/<root_id>/events.jsonl`. Persisting events
        inside the snapshot is what made `write_session_full` O(tree
        size). The v8 invariant: event-list fields are absent on disk;
        the cache holds the live lists and they are rehydrated from
        events.jsonl on cold load (see `render_tree_hydrate`).

    Single tree walk to avoid the partial-strip risk of running two
    strip passes in sequence. Caller MUST restore in a `finally` so a
    failed `json.dump` doesn't lose the in-memory state."""
    isstreaming: list[tuple[dict, bool]] = []
    events_lists: list[tuple[dict, list]] = []
    uid_idxs: list[tuple[dict, dict]] = []
    omitted_revisions: list[tuple[dict, str]] = []
    panel_anchor_caches: list[tuple[dict, dict]] = []
    opened: list[tuple[dict, str]] = []
    content_dirty: list[tuple[dict, bool]] = []
    def _pop_uid_idx(owner: dict) -> None:
        idx = owner.pop("_uid_idx", None)
        if isinstance(idx, dict):
            uid_idxs.append((owner, idx))
    def _pop_omitted_revision(owner: dict) -> None:
        value = owner.pop(messages_delta_compaction.PRECOMPUTED_REVISION_KEY, None)
        if isinstance(value, str):
            omitted_revisions.append((owner, value))
    def _pop_panel_anchor_cache(owner: dict) -> None:
        cache = owner.pop("_panel_anchor_cache", None)
        if isinstance(cache, dict):
            panel_anchor_caches.append((owner, cache))
    def _pop_opened(node: dict) -> None:
        at = node.pop("last_opened_at", None)
        if isinstance(at, str):
            opened.append((node, at))

    def _pop_events(owner: dict) -> None:
        ev = owner.get("events")
        if isinstance(ev, list):
            events_lists.append((owner, ev))
            del owner["events"]

    stack = [root]
    while stack:
        node = stack.pop()
        _pop_opened(node)
        for m in node.get("messages", []):
            if m.get("role") == "assistant":
                dirty = bool(m.get("_content_dirty")) or not m.get("content")
                if dirty:
                    try:
                        from render_stub import message_output_text
                        content = message_output_text(m)
                    except Exception:
                        content = ""
                    if content:
                        m["content"] = content
                if "_content_dirty" in m:
                    content_dirty.append((m, False))
                    del m["_content_dirty"]
            if "isStreaming" in m:
                isstreaming.append((m, m["isStreaming"]))
                del m["isStreaming"]
            _pop_events(m)
            _pop_uid_idx(m)
            _pop_omitted_revision(m)
            _pop_panel_anchor_cache(m)
            workers = m.get("workers")
            if isinstance(workers, list):
                for w in workers:
                    if not isinstance(w, dict):
                        continue
                    _pop_events(w)
                    _pop_uid_idx(w)
        for f in node.get("forks", []) or []:
            stack.append(f)
    return {
        "isstreaming": isstreaming,
        "events_lists": events_lists,
        "uid_idxs": uid_idxs,
        "omitted_revisions": omitted_revisions,
        "panel_anchor_caches": panel_anchor_caches,
        "opened": opened,
        "content_dirty": content_dirty,
    }


def _restore_volatile_to_tree(popped: dict) -> None:
    """Counterpart to `_strip_volatile_from_tree`. Re-attaches every
    popped value to its owning dict. MUST be called in the write path's
    `finally`."""
    for m, v in popped.get("isstreaming", []):
        m["isStreaming"] = v
    for owner, ev in popped.get("events_lists", []):
        owner["events"] = ev
    for owner, idx in popped.get("uid_idxs", []):
        owner["_uid_idx"] = idx
    for owner, value in popped.get("omitted_revisions", []):
        owner[messages_delta_compaction.PRECOMPUTED_REVISION_KEY] = value
    for owner, cache in popped.get("panel_anchor_caches", []):
        owner["_panel_anchor_cache"] = cache
    for node, at in popped.get("opened", []):
        node["last_opened_at"] = at
    for m, value in popped.get("content_dirty", []):
        m["_content_dirty"] = value


def copy_persistable_tree(root: dict) -> dict:
    with perf.timed("store.session.copy_persistable.strip"):
        popped = _strip_volatile_from_tree(root)
    try:
        with perf.timed("store.session.copy_persistable.deepcopy"):
            return copy.deepcopy(root)
    finally:
        _restore_volatile_to_tree(popped)


def _overlay_queue_projection(root: dict) -> None:
    import session_queue_projection

    stack = [root]
    nodes: list[dict] = []
    sids: list[str] = []
    while stack:
        node = stack.pop()
        nodes.append(node)
        sid = node.get("id")
        if isinstance(sid, str) and sid:
            sids.append(sid)
        for fork in node.get("forks") or []:
            if isinstance(fork, dict):
                stack.append(fork)
    records = session_queue_projection.get_many(sids)
    for node in nodes:
        sid = node.get("id")
        if not isinstance(sid, str):
            continue
        record = records.get(sid)
        if isinstance(record, dict) and "queued_prompts" in record:
            # A projection record older than the node (async upserts land on a
            # background thread) must not overwrite the newer tree state —
            # doing so silently drops a just-admitted prompt from memory and
            # disk. Mirror of _regresses_queue_revision on the upsert side.
            if session_queue_projection._regresses_queue_revision(node, record):
                continue
            node["queued_prompts"] = [
                copy.deepcopy(prompt)
                for prompt in record.get("queued_prompts") or []
                if isinstance(prompt, dict)
            ]
            node["queue_revision"] = max(
                int(node.get("queue_revision") or 0),
                int(record.get("queue_revision") or 0),
            )


@perf.timed_fn("store.session.write_full")
def write_session_full(
    root: dict,
    *,
    bump_updated_at: bool = True,
    preserve_projection_fields: bool = False,
    already_persistable: bool = False,
) -> None:
    """Write the whole ROOT tree to disk. Caller MUST pass a root dict
    (the top-level record), not an embedded fork — embedded fork writes
    happen by mutating the in-memory root and calling this function on
    the root. SessionManager handles the mutate-in-place + write-root
    pattern via its `_persist`.

    SEMANTICS: the per-write step is atomic against torn reads — a
    reader concurrent with a write sees either the pre-write file or
    the post-write file, never a partial one. Last-writer-wins applies
    to concurrent writers; the per-root `_lock_for_root` in
    `session_manager` is the only thing that serializes them. A caller
    that bypasses that lock and calls this function directly (with a
    root read straight off disk) can still clobber a concurrent locked
    writer. `_migrate_and_persist` routes through `_root_writer_guard`
    (registered by `session_manager`) precisely to close that gap for
    its own callers (`iter_all_sessions`, used by session_watcher /
    run_recovery); `adv_sync.recover_running_overlays_on_startup` writes
    directly and is safe only because it runs at startup before any
    root is resident in `session_manager`'s cache and before any turn
    can run. Any NEW unlocked bulk-walk writer must route through
    `_root_writer_guard` the same way `_migrate_and_persist` does.
    """
    global _index_fingerprint
    runtime_ownership.assert_runtime_writer()
    if root.get("parent_session_id"):
        raise ValueError(
            "write_session_full received a fork dict; pass the root tree "
            "(SessionManager._persist resolves to root before calling)."
        )
    if bump_updated_at:
        with perf.timed("store.session.write_full.updated_at"):
            root["updated_at"] = datetime.now().isoformat()
    if preserve_projection_fields:
        with perf.timed("store.session.write_full.queue_projection"):
            _overlay_queue_projection(root)
    with perf.timed("store.session.write_full.index_tree"):
        fork_topology_changed = _index_tree(root)
    with perf.timed("store.session.write_full.path"):
        storage_scope = _normalize_storage_scope(root.get("storage_scope"))
        if storage_scope is not None:
            root["storage_scope"] = storage_scope
            storage_dir = _storage_dir_for_scope(storage_scope)
            _remember_root_file_dir(root["id"], storage_dir)
            path = storage_dir / f"{root['id']}.json"
        else:
            path = _root_file_path(root["id"])
    popped = None
    if not already_persistable:
        with perf.timed("store.session.write_full.strip"):
            popped = _strip_volatile_from_tree(root)
    try:
        with perf.timed("store.session.write_full.dump"):
            encoded = json.dumps(root, separators=(",", ":")).encode("utf-8")
        root_mutation = _begin_root_change("upsert", root["id"], path)
        try:
            with perf.timed("store.session.write_full.durable_replace"):
                receipt = _get_durability_writer().replace(path, encoded)
                committed_signature = _wait_durability(receipt)
                root_change = _durable_root_change(
                    root_mutation, committed_signature,
                )
        except BaseException:
            _abandon_root_change(root_mutation)
            raise
        with perf.timed("store.session.write_full.signature"):
            file_signature = (
                committed_signature
                if committed_signature is not None
                else _session_file_signature(path)
            )
    finally:
        if popped is not None:
            with perf.timed("store.session.write_full.restore"):
                _restore_volatile_to_tree(popped)
    try:
        with perf.timed("store.session.write_full.index_signature"):
            if file_signature is not None:
                with _index_lock:
                    previous_signature = _root_index_signatures.get(root["id"])
                    updated_fingerprint = _fingerprint_after_root_write_locked(
                        previous_signature, file_signature, root["id"],
                    )
                    _root_index_signatures[root["id"]] = file_signature
                    if updated_fingerprint is not None:
                        _index_fingerprint = updated_fingerprint
                    index_generation = _bump_index_generation_locked()
                    index_loaded = _index_loaded
            else:
                index_loaded = False
                updated_fingerprint = None
                index_generation = None
        if updated_fingerprint is not None and index_generation is not None:
            _publish_dir_fingerprint_cache(updated_fingerprint, index_generation)
        if index_loaded and fork_topology_changed:
            with perf.timed("store.session.write_full.index_sidecar"):
                _persist_index_sidecar_if_loaded(
                    updated_fingerprint, expected_generation=index_generation,
                )
        elif root.get("forks") and not index_loaded:
            with perf.timed("store.session.write_full.index_sidecar"):
                _refresh_index_sidecar_for_written_root(root, file_signature)
        with perf.timed("store.session.write_full.summary"):
            _upsert_summary(
                root,
                preserve_projection_fields=preserve_projection_fields,
                root_mtime_ns=file_signature[3] if file_signature is not None else None,
                root_signature=file_signature,
                sync_sidecar=bool(root.get("forks")),
            )
        with perf.timed("store.session.write_full.queue_projection_fact"):
            import session_queue_projection
            session_queue_projection.note_persisted_tree(root)
    except BaseException:
        _abandon_root_change(root_mutation)
        raise
    _complete_root_change(root_change)


def list_sessions() -> list[dict]:
    """Return a summary of every ROOT session (sorted by updated_at desc).
    Forks are NOT included as top-level entries — they're embedded in
    each root's `forks` array. The returned summary intentionally mirrors
    the previous schema's per-session fields plus a `fork_count` for the
    sidebar; the full fork tree is fetched via /api/sessions/{id}.

    INVARIANT: reads directly from the in-memory `_summary_index` — no
    disk I/O on the hot path. Writers keep the index up-to-date via
    `_upsert_summary` / `_remove_summary`.

    INVARIANT: read-side is LOCK-FREE vs. the debounce queue.
    Previously this called `session_manager.flush_pending_persists()`
    to force every queued tail write to disk before reading the
    summary index, guaranteeing freshness. Under heavy write load
    (a turn streaming events) the flush serialized through per-root
    locks and pushed `/api/sessions` peaks to 11 s (5 calls averaged
    2.2 s during a new claude turn spawn). The flush is gone — the
    summary index may now lag in-memory mutations by up to
    `PERSIST_DEBOUNCE_S` (50 ms) during write bursts. Acceptable
    tradeoff: the sidebar's `updated_at` / `message_count` may show
    50 ms stale data during streaming, imperceptible to humans; the
    next bootstrap / WS event closes any gap.

    `blocking=False`: if the eager-warm task (or any other thread) is
    currently mid-build, don't wait on `_summary_build_lock`. Return
    whatever's been published incrementally so far — the build
    publishes per-summary so the index grows during the scan. Closes
    the cold-restart 23 s outlier where the first `/api/sessions`
    waited for the full 400-file walk to complete.

    The returned list is a SHALLOW copy (sorted) so a caller's mutations
    can't affect the index. Summary dicts inside are shared — callers
    must not mutate them, but no current caller does.
    """
    global _summary_sorted_cache_version, _summary_sorted_id_cache
    with _summary_index_lock:
        has_published_summaries = bool(_summary_index)
    _ensure_summary_index(
        blocking=_root_change_owner is None and not has_published_summaries,
    )
    if _root_change_owner is None:
        _reconcile_summary_index_roots()
    with _summary_index_lock:
        if _summary_sorted_cache_version != _summary_order_version:
            _summary_sorted_id_cache = [
                str(summary.get("id"))
                for summary in sorted(
                    _summary_index.values(),
                    key=_summary_order_key,
                    reverse=True,
                )
                if summary.get("id")
            ]
            _summary_sorted_cache_version = _summary_order_version
        return [
            _summary_index[sid]
            for sid in _summary_sorted_id_cache
            if sid in _summary_index
        ]


def ordered_session_summary_ids(sort_by: str, folder_view: bool = False) -> list[str]:
    _ensure_summary_index(blocking=False)
    cache_key = (sort_by, folder_view)
    with _summary_index_lock:
        cached = _summary_sorted_id_caches.get(cache_key)
        if cached is None or cached[0] != _summary_order_version:
            cached = (
                _summary_order_version,
                [
                    str(summary.get("id"))
                    for summary in sorted(
                        _summary_index.values(),
                        key=lambda summary: _summary_sort_key(summary, sort_by, folder_view),
                        reverse=True,
                    )
                    if summary.get("id")
                ],
            )
            _summary_sorted_id_caches[cache_key] = cached
        return list(cached[1])


def sidebar_session_summary_page(
    sort_by: str,
    project_path: str | None,
    offset: int,
    limit: int,
    folder_view: bool = False,
) -> tuple[list[dict], int, int, int]:
    """Return one complete, generation-consistent default sidebar page."""
    _ensure_summary_index(blocking=True)
    wait_started = time.perf_counter()
    _summary_index_lock.acquire()
    perf.record(
        "store.session.sidebar_page.lock_wait",
        (time.perf_counter() - wait_started) * 1000.0,
    )
    try:
        key = (
            sort_by,
            project_path,
            folder_view,
            _summary_order_version,
            _summary_visibility_version,
        )
        visible_ids = _sidebar_page_projections.get(key)
        if visible_ids is None:
            build_started = time.perf_counter()
            sorted_cache_key = (sort_by, folder_view)
            ordered_ids = _summary_sorted_id_caches.get(sorted_cache_key)
            if ordered_ids is None or ordered_ids[0] != _summary_order_version:
                ordered_ids = (
                    _summary_order_version,
                    [
                        str(summary.get("id"))
                        for summary in sorted(
                            _summary_index.values(),
                            key=lambda item: _summary_sort_key(item, sort_by, folder_view),
                            reverse=True,
                        )
                        if summary.get("id")
                    ],
                )
                _summary_sorted_id_caches[sorted_cache_key] = ordered_ids
            visible_ids = tuple(
                sid for sid in ordered_ids[1]
                if sid in _summary_index
                and _summary_visible_in_sidebar(_summary_index[sid], project_path)
            )
            _sidebar_page_projections[key] = visible_ids
            _sidebar_page_projections.move_to_end(key)
            while len(_sidebar_page_projections) > _SIDEBAR_PAGE_PROJECTIONS_MAX:
                _sidebar_page_projections.popitem(last=False)
            perf.record_count("store.session.sidebar_page.projection_miss")
            perf.record(
                "store.session.sidebar_page.projection_build",
                (time.perf_counter() - build_started) * 1000.0,
            )
        else:
            _sidebar_page_projections.move_to_end(key)
            perf.record_count("store.session.sidebar_page.projection_hit")
        page_refs = [
            _summary_index[sid]
            for sid in visible_ids[offset:offset + limit]
            if sid in _summary_index
        ]
        total = len(visible_ids)
        order_version = _summary_order_version
        visibility_version = _summary_visibility_version
    finally:
        _summary_index_lock.release()
    perf.record("store.session.sidebar_page.order_generation", float(order_version))
    perf.record("store.session.sidebar_page.visibility_generation", float(visibility_version))
    return (
        [_copy_jsonish(summary) for summary in page_refs],
        total,
        order_version,
        visibility_version,
    )


def get_session_summaries_by_ids(session_ids: Iterable[str]) -> list[dict]:
    ids = [sid for sid in session_ids if sid]
    if not ids:
        return []
    _ensure_summary_index(blocking=False)
    with _summary_index_lock:
        found = {
            sid: _summary_index[sid]
            for sid in ids
            if sid in _summary_index
        }
    missing = [sid for sid in ids if sid not in found]
    for sid in missing:
        summary = _load_summary_for_requested_id(sid)
        if summary is not None:
            found[sid] = summary
    return [found[sid] for sid in ids if sid in found]


def get_indexed_session_summaries_by_ids_if_current(
    session_ids: Iterable[str],
    expected_summary_index_version: int,
) -> Optional[list[dict]]:
    ids = [sid for sid in session_ids if sid]
    with _summary_index_lock:
        if _summary_index_version != expected_summary_index_version:
            return None
        summaries = [
            _summary_index[sid]
            for sid in ids
            if sid in _summary_index
        ]
    if len(summaries) != len(ids):
        return None
    return summaries


def get_indexed_session_summary_if_current(
    session_id: str,
    expected_summary_index_version: int,
) -> Optional[dict]:
    if not session_id:
        return None
    with _summary_index_lock:
        if _summary_index_version != expected_summary_index_version:
            return None
        return _summary_index.get(session_id)


def get_indexed_session_summary(session_id: str) -> Optional[dict]:
    if not session_id:
        return None
    _ensure_summary_index(blocking=False)
    with _summary_index_lock:
        return _summary_index.get(session_id)


def get_indexed_session_summaries_by_ids(session_ids: Iterable[str]) -> list[dict]:
    ids = [sid for sid in session_ids if sid]
    if not ids:
        return []
    _ensure_summary_index(blocking=False)
    with _summary_index_lock:
        return [
            _summary_index[sid]
            for sid in ids
            if sid in _summary_index
        ]


def _load_summary_for_requested_id(sid: str) -> Optional[dict]:
    path = _root_file_path(sid)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("id") != sid:
            return None
        summary = _build_summary_for_root(_migrate_session(raw))
        _publish_requested_summary(sid, summary)
        _write_summary_file(sid, summary)
        return summary
    except (json.JSONDecodeError, KeyError, ValueError, OSError):
        return None


def _publish_requested_summary(sid: str, summary: dict) -> None:
    global _summary_index_version, _summary_order_version, _summary_metadata_version
    global _summary_visibility_version
    with _summary_index_lock:
        existing = _summary_index.get(sid)
        if existing == summary:
            return
        _summary_index[sid] = summary
        _summary_index_version += 1
        if _summary_order_changed(existing, summary):
            _summary_order_version += 1
        if _summary_visibility_changed(existing, summary):
            _summary_visibility_version += 1
        if _summary_metadata_changed(existing, summary):
            _summary_metadata_version += 1


def iter_all_sessions() -> Iterator[dict]:
    """Yield every session record across all roots — root, then each of
    its forks (depth-first). Used by session_watcher / run_recovery to
    walk the full session universe regardless of nesting.

    Persists any first-time provider_id backfill so SessionWatcher
    ticks don't re-run detection on the same record across restarts.

    Non-session JSON files that leak into the sessions dir (e.g. a
    stray ``git-last.json``) are skipped: only dicts carrying an
    ``id`` are yielded, so one malformed file can't abort the walk and
    crash callers like the startup adv-sync overlay recovery task."""
    _ensure_dir()
    for path in _session_json_files():
        try:
            root = _migrate_and_persist(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, KeyError, ValueError):
            continue
        if not isinstance(root, dict) or "id" not in root:
            continue
        _index_tree(root)
        yield root
        yield from _walk_forks(root)


# ── Fork ──────────────────────────────────────────────────────────────


def _derive_fork_current_todos(copied_messages: list) -> list:
    """Walk the FORK's copied messages through the todos extractor and
    return the resulting `current_todos`.

    Imported inline (not at module top) because `session_store` is a
    leaf-ish module the extractor module doesn't pull in, and we want
    to keep that one-way dependency direction. Failures are swallowed
    to a fresh empty list so a malformed message stream never blocks
    fork creation — the field will refill on the next live TodoWrite.
    """
    try:
        import session_local_projection
        current: list = []
        for msg in copied_messages or []:
            for event in msg.get("events") or []:
                fields = session_local_projection.project_event_fields(
                    event,
                    current_todos=current,
                    current_tasks=[],
                )
                if "current_todos" in fields:
                    current = list(fields.get("current_todos") or [])
        return current
    except Exception:
        return []


def _derive_fork_current_tasks(copied_messages: list) -> list:
    """Walk the FORK's copied messages through the tasks extractor and
    return the resulting `current_tasks`.
    """
    try:
        import session_local_projection
        current: list = []
        for msg in copied_messages or []:
            for event in msg.get("events") or []:
                fields = session_local_projection.project_event_fields(
                    event,
                    current_todos=[],
                    current_tasks=current,
                )
                if "current_tasks" in fields:
                    current = list(fields.get("current_tasks") or [])
        return current
    except Exception:
        return []


def fork_session(root: dict, parent_id: str, name: Optional[str] = None) -> dict:
    """Fork `parent_id` (root or embedded fork) off its current claude
    head, appending a new fork record to the parent's `forks` array
    within the given `root` tree. Returns the new fork dict. The caller
    (session_manager) owns persisting the mutated `root` — this function
    does not touch disk."""
    parent = _find_in_tree(root, parent_id)
    if parent is None:
        raise KeyError(parent_id)
    mode = _normalize_orchestration_mode(parent.get("orchestration_mode"))
    sid_field = _agent_sid_field_for_mode(mode)
    parent_claude_sid = parent.get(sid_field)
    if not parent_claude_sid:
        raise ValueError(
            t("prompt_engineer.parent_no_claude_session")
        )

    # Default fork name. Avoid stacking " (fork) (fork) (fork)" — if
    # the parent already ends in a "(fork[ N])" suffix, increment the
    # counter; otherwise append "(fork)". Cosmetic but keeps long
    # nested-fork chains readable.
    if name is None:
        parent_name = parent.get("name") or t("session.untitled")
        import re as _re
        m = _re.search(r"\s*\(fork(?:\s+(\d+))?\)\s*$", parent_name)
        if m:
            n = int(m.group(1) or 1) + 1
            base = _re.sub(r"\s*\(fork(?:\s+\d+)?\)\s*$", "", parent_name)
            name = base + t("session.fork_suffix_n", n=n)
        else:
            name = parent_name + t("session.fork_suffix")

    copied_messages = copy.deepcopy(parent.get("messages", []))
    for m in copied_messages:
        m["id"] = str(uuid.uuid4())
    fork_next_seq = max(
        (int(m.get("seq", 0)) for m in copied_messages), default=-1
    ) + 1
    now = datetime.now().isoformat()

    child = {
        "id": str(uuid.uuid4()),
        "_owner_incarnation": uuid.uuid4().hex,
        "_schema_version": SCHEMA_VERSION,
        "name": name,
        "model": parent.get("model") or config_store.default_session_model(),
        "reasoning_effort": parent.get("reasoning_effort") or "",
        "cwd": parent.get("cwd") or str(Path.home()),
        "created_at": now,
        "updated_at": now,
        "orchestration_mode": mode,
        # Forks inherit the parent's provider — same CLAUDE_CONFIG_DIR,
        # same auth — because the fork's claude jsonl branches off the
        # parent's, which lives under that provider.
        "provider_id": parent.get("provider_id"),
        "last_active_provider_id": parent.get("last_active_provider_id"),
        "last_active_model": parent.get("last_active_model"),
        "last_active_supervisor_provider_id": parent.get("last_active_supervisor_provider_id"),
        "last_active_supervisor_model": parent.get("last_active_supervisor_model"),
        # Forks inherit the parent's node: the fork's claude jsonl
        # branches off the parent's, which lives on that node's disk.
        "node_id": parent.get("node_id") or "primary",
        "agent_session_id": None,
        "supervisor_agent_session_id": None,
        "supervisor_bootstrap_received": False,
        "source": parent.get("source") if parent.get("source") in _VALID_SESSION_SOURCES else "web",
        # A plain fork inherits the parent's user-awareness. The
        # session_manager.fork wrapper forces this False when a non-user
        # `kind` (e.g. adv_sync_fork) is stamped on the new fork.
        "user_initiated": bool(parent.get("user_initiated", _infer_user_initiated(parent))),
        "processed_line_by_sid": {},
        "parent_session_id": parent_id,
        "forked_from_agent_sid": parent_claude_sid,
        "forked_from_supervisor_agent_sid": None,
        "fork_point_seq": fork_next_seq - 1 if fork_next_seq > 0 else None,
        "fork_closed": False,
        "forks": [],
        "messages": copied_messages,
        "next_seq": fork_next_seq,
        "inline_tags": [],
        "open_file_panels": [],
        "open_config_panels": [],
        "notes": [],
        # Re-derive current_todos from the COPIED messages' events
        # rather than inheriting the parent's running state — the
        # parent's `current_todos` may reflect events PAST the fork
        # point (parent kept going while user was setting up the fork).
        # Walking `copied_messages` produces exactly the state matching
        # the fork's event subset. Empty if no TodoWrite events were
        # copied.
        "current_todos": _derive_fork_current_todos(copied_messages),
        "current_tasks": _derive_fork_current_tasks(copied_messages),
        "right_panel_open": False,
        "right_panel_active_tab": None,
        "right_panel_width": None,
        "right_panel_mobile_height": None,
        "right_panel_todos_dismissed": False,
        "right_panel_auto_opened_by": [],
        "sidebar_minimized": False,
        "token_usage_total": {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
        "token_usage_last": None,
    }
    parent.setdefault("forks", []).append(child)
    _index_set(child["id"], root["id"])
    return child


def create_sub_session(
    root: dict,
    *,
    parent_session_id: str,
    name: str,
    model: Optional[str] = None,
    provider_id: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
    permission: Optional[dict] = None,
    cwd: str = "",
    node_id: Optional[str] = None,
    disallowed_tools: Optional[list[str]] = None,
    disabled_builtin_extensions: Optional[list[str]] = None,
) -> dict:
    parent = _find_in_tree(root, parent_session_id)
    if parent is None:
        raise KeyError(parent_session_id)

    resolved_provider_id = provider_id or parent.get("provider_id")
    resolved_model = (
        model
        or parent.get("model")
        or config_store.default_session_model()
    )
    if reasoning_effort is None:
        reasoning_effort = parent.get("reasoning_effort")
    resolved_effort = _session_reasoning_effort(
        reasoning_effort, resolved_provider_id,
    )
    if permission is None:
        permission = parent.get("permission")
    resolved_permission = _session_permission(
        permission, resolved_provider_id,
    )
    now = datetime.now().isoformat()
    child = {
        "id": str(uuid.uuid4()),
        "_owner_incarnation": uuid.uuid4().hex,
        "_schema_version": SCHEMA_VERSION,
        "name": name or "sub-session",
        "model": resolved_model,
        "reasoning_effort": resolved_effort,
        "permission": resolved_permission,
        "cwd": cwd or parent.get("cwd") or str(Path.home()),
        "created_at": now,
        "updated_at": now,
        "orchestration_mode": "native",
        "provider_id": resolved_provider_id,
        "last_active_provider_id": None,
        "last_active_model": None,
        "last_active_supervisor_provider_id": None,
        "last_active_supervisor_model": None,
        "node_id": node_id or parent.get("node_id") or "primary",
        "agent_session_id": None,
        "supervisor_agent_session_id": None,
        "supervisor_bootstrap_received": False,
        "source": "cli",
        # Hidden native child spun up by an agent (mssg/ask/delegate_task);
        # the user is not aware of it.
        "user_initiated": False,
        "processed_line_by_sid": {},
        "parent_session_id": parent_session_id,
        "forked_from_agent_sid": None,
        "forked_from_supervisor_agent_sid": None,
        "fork_point_seq": None,
        "fork_closed": False,
        "forks": [],
        "messages": [],
        "next_seq": 0,
        "inline_tags": [],
        "open_file_panels": [],
        "open_config_panels": [],
        "notes": [],
        "current_todos": [],
        "current_tasks": [],
        "right_panel_open": False,
        "right_panel_active_tab": None,
        "right_panel_width": None,
        "right_panel_mobile_height": None,
        "right_panel_todos_dismissed": False,
        "right_panel_auto_opened_by": [],
        "sidebar_minimized": False,
        "queued_prompts": [],
        "capability_contexts": [],
        "token_usage_total": {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
        "token_usage_last": None,
        "kind": "sub_session",
        "caller_agent_session_id": parent_session_id,
        "parent_line_count_at_fork": None,
        "continuation_chain": [],
        "worker_eligible": False,
        "pinned": False,
        "topbar_pinned": False,
        "topbar_pinned_at": None,
        "archived": False,
        "supervisor_enabled": False,
        "supervisor_custom_prompt": "",
        "pending_supervisor_verdict": None,
        "disallowed_tools": list(
            dict.fromkeys(str(tool).strip() for tool in (disallowed_tools or []) if str(tool).strip())
        ),
        "disabled_builtin_extensions": list(
            dict.fromkeys(
                str(item).strip()
                for item in (disabled_builtin_extensions or [])
                if str(item).strip()
            )
        ),
    }
    parent.setdefault("forks", []).append(child)
    _index_set(child["id"], root["id"])
    return child


def create_delegate_fork(
    root: dict,
    *,
    parent_agent_session_id: str,
    caller_agent_session_id: str,
    parent_agent_sid_at_fork: str,
    parent_line_count_at_fork: int,
    orchestration_mode: str,
) -> dict:
    """Create an internal-only delegate fork Better Agent session embedded in the
    target session's tree.

    Unlike `fork_session` (user-facing branch with copied history), the
    delegate fork starts EMPTY — it's the per-(caller, target-session)
    fork used by ask(run_mode="fork"). Fields it carries:

      - `kind="delegate_fork"` — UI filters anything `kind != "user"`
        out of sidebar/fork-split.
      - `parent_session_id=parent_agent_session_id` (the target Better Agent session).
      - `caller_agent_session_id` — the Better Agent session that requested this fork.
      - `forked_from_agent_sid=parent_agent_sid_at_fork` — snapshot of
        the target's provider sid at fork mint time, for invalidation.
      - `parent_line_count_at_fork` — snapshot of target's jsonl line
        count, for invalidation.
      - `orchestration_mode` — usually inherits target's mode but the
        caller may pass a different value to mint a fork that runs
        in another mode.

    `claude_sid` (manager_/native_) is None initially; the caller sets
    it via session_manager.set_agent_sid once `session_discovered`
    arrives from the runner.
    """
    if orchestration_mode not in _CLAUDE_SID_FIELD_BY_MODE:
        raise ValueError(f"invalid orchestration_mode: {orchestration_mode!r}")
    parent = _find_in_tree(root, parent_agent_session_id)
    if parent is None:
        raise KeyError(parent_agent_session_id)

    now = datetime.now().isoformat()
    child = {
        "id": str(uuid.uuid4()),
        "_owner_incarnation": uuid.uuid4().hex,
        "_schema_version": SCHEMA_VERSION,
        "name": f"delegate-fork:{caller_agent_session_id[:8]}→{parent_agent_session_id[:8]}",
        "model": parent.get("model") or config_store.default_session_model(),
        "reasoning_effort": parent.get("reasoning_effort") or "",
        "cwd": parent.get("cwd") or str(Path.home()),
        "created_at": now,
        "updated_at": now,
        "orchestration_mode": orchestration_mode,
        "provider_id": parent.get("provider_id"),
        "agent_session_id": None,
        "supervisor_agent_session_id": None,
        "supervisor_bootstrap_received": False,
        "source": "web",
        # Internal per-(caller,target) fork for ask(run_mode="fork");
        # never user-facing.
        "user_initiated": False,
        "processed_line_by_sid": {},
        "parent_session_id": parent_agent_session_id,
        "forked_from_agent_sid": parent_agent_sid_at_fork,
        "forked_from_supervisor_agent_sid": None,
        "fork_point_seq": None,
        "fork_closed": False,
        "forks": [],
        "kind": "delegate_fork",
        "caller_agent_session_id": caller_agent_session_id,
        "parent_line_count_at_fork": int(parent_line_count_at_fork),
        # Forks inherit the target session's node binding (shared worktree).
        "node_id": parent.get("node_id") or "primary",
        "messages": [],
        "next_seq": 0,
        "inline_tags": [],
        "adv_sync_overlays": [],
        "open_file_panels": [],
        "open_config_panels": [],
        "notes": [],
        "current_todos": [],
        "current_tasks": [],
        "right_panel_open": False,
        "right_panel_active_tab": None,
        "right_panel_width": None,
        "right_panel_mobile_height": None,
        "right_panel_todos_dismissed": False,
        "right_panel_auto_opened_by": [],
        "sidebar_minimized": False,
        "token_usage_total": {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
        "token_usage_last": None,
    }
    parent.setdefault("forks", []).append(child)
    _index_set(child["id"], root["id"])
    return child


def splice_fork(root: dict, fork_id: str) -> bool:
    """Splice `fork_id` (and its descendants) out of `root`'s fork tree
    and drop them from the fork index. Mutates `root` in place; the
    caller (session_manager) owns persisting it. Returns False if
    `fork_id` is not a fork under `root`."""
    parent_node = _find_parent_of(root, fork_id)
    if parent_node is None:
        return False
    target = next(
        (f for f in parent_node.get("forks") or [] if f.get("id") == fork_id),
        None,
    )
    if target is None:
        return False
    _index_pop(fork_id)
    for descendant in _walk_forks(target):
        _index_pop(descendant["id"])
    parent_node["forks"] = [
        f for f in parent_node["forks"] if f.get("id") != fork_id
    ]
    return True


def delete_session(root_id: str) -> bool:
    """Delete a ROOT session: unlink its file and unindex the root plus
    every embedded fork. Fork deletes go through `splice_fork` +
    session_manager's single persist, not here. Returns False if the
    root file is already gone."""
    _ensure_index()
    path = _root_file_path(root_id)
    if not path.exists():
        return False
    seen_cursor_path = _seen_cursor_path(root_id)
    opened_path = _opened_path(root_id)
    root = _migrate_session(json.loads(path.read_text(encoding="utf-8")))
    root_mutation = _begin_root_change("delete", root_id, path)
    try:
        receipt = _get_durability_writer().unlink(path)
        _wait_durability(receipt)
        root_change = _durable_root_change(root_mutation, None)
    except BaseException:
        _abandon_root_change(root_mutation)
        raise
    try:
        _remove_summary(root_id)
        with _index_lock:
            file_signature = _root_index_signatures.pop(root_id, None)
            for fork_id in _root_forks.pop(root_id, set()):
                _fork_index.pop(fork_id, None)
            if file_signature is not None:
                updated_fingerprint = _fingerprint_after_root_delete_locked(
                    file_signature, root_id,
                )
                if updated_fingerprint is not None:
                    global _index_fingerprint
                    _index_fingerprint = updated_fingerprint
            else:
                updated_fingerprint = None
            _clear_negative_root_resolve_cache()
            generation = _bump_index_generation_locked()
        if updated_fingerprint is not None:
            _publish_dir_fingerprint_cache(updated_fingerprint, generation)
            _persist_index_sidecar_if_loaded(
                updated_fingerprint, expected_generation=generation,
            )
        try:
            import session_search_index
            session_search_index.delete_session(root_id)
        except Exception:
            _logger.debug("session search index delete failed", exc_info=True)
    except BaseException:
        _abandon_root_change(root_mutation)
        raise
    _complete_root_change(root_change)
    try:
        seen_cursor_path.unlink(missing_ok=True)
    except OSError:
        pass
    try:
        opened_path.unlink(missing_ok=True)
        _opened_cache_invalidate(root_id)
    except OSError:
        pass
    return True


SEARCH_FIELD_CONTENT = "content"
SEARCH_FIELD_TITLE = "title"
SEARCH_FIELD_FIRST_PROMPT = "first_prompt"
SEARCH_FIELDS = frozenset({
    SEARCH_FIELD_CONTENT,
    SEARCH_FIELD_TITLE,
    SEARCH_FIELD_FIRST_PROMPT,
})
DEFAULT_SEARCH_FIELDS = frozenset({
    SEARCH_FIELD_TITLE,
    SEARCH_FIELD_FIRST_PROMPT,
})
_METADATA_SEARCH_CACHE_MAX = 128
_metadata_search_cache: dict[tuple[str, tuple[str, ...], int], dict[str, int]] = {}
_metadata_text_cache_version = -1
_metadata_text_cache: tuple[tuple[str, str, str], ...] = ()
_metadata_text_by_id_cache_version = -1
_metadata_text_by_id_cache: dict[str, tuple[str, str]] = {}
_METADATA_NGRAM_MAX_SIZE = 3


def _normalize_search_fields(fields: Iterable[str] | None) -> set[str]:
    if fields is None:
        return set(DEFAULT_SEARCH_FIELDS)
    return {field for field in fields if field in SEARCH_FIELDS}


def _message_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    if isinstance(value, dict):
        content = value.get("content")
        if content is not value:
            return _message_text(content)
    return ""


def _last_user_prompt_timestamp(root: dict) -> str:
    """Timestamp of the most recent user-role message in the root.
    Distinct from `updated_at`, which any write (fork, rename, agent
    activity) bumps; this only moves when the user sends a prompt."""
    for message in reversed(root.get("messages") or []):
        if isinstance(message, dict) and message.get("role") == "user":
            return message.get("timestamp", "") or ""
    return ""


def _first_user_prompt(root: dict) -> str:
    for message in root.get("messages") or []:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        text = _message_text(message.get("content"))
        if text:
            return text
    return ""


def _metadata_search_rows() -> tuple[tuple[str, str, str], ...]:
    global _metadata_text_cache_version, _metadata_text_cache
    global _metadata_text_by_id_cache_version, _metadata_text_by_id_cache
    _ensure_summary_index(blocking=False)
    with _summary_index_lock:
        if _metadata_text_cache_version == _summary_metadata_version:
            return _metadata_text_cache
        rows = tuple(
            (
                str(summary.get("id") or ""),
                str(summary.get("name") or "").lower(),
                str(summary.get("first_prompt") or "").lower(),
            )
            for summary in _summary_index.values()
            if summary.get("id")
        )
        _metadata_text_cache = rows
        _metadata_text_cache_version = _summary_metadata_version
        _metadata_text_by_id_cache = {
            sid: (title, first_prompt)
            for sid, title, first_prompt in rows
        }
        _metadata_text_by_id_cache_version = _summary_metadata_version
        return rows


def _metadata_search_row_map() -> dict[str, tuple[str, str]]:
    global _metadata_text_by_id_cache_version, _metadata_text_by_id_cache
    while True:
        _ensure_summary_index(blocking=False)
        with _summary_index_lock:
            version = _summary_metadata_version
            if _metadata_text_by_id_cache_version == version:
                return _metadata_text_by_id_cache
        rows = _metadata_search_rows()
        row_map = {sid: (title, first_prompt) for sid, title, first_prompt in rows}
        with _summary_index_lock:
            if _summary_metadata_version != version:
                continue
            if _metadata_text_by_id_cache_version == version:
                return _metadata_text_by_id_cache
            _metadata_text_by_id_cache = row_map
            _metadata_text_by_id_cache_version = version
            return _metadata_text_by_id_cache


def _metadata_ngrams(value: str, size: int) -> set[str]:
    if len(value) < size:
        return set()
    return {
        value[index:index + size]
        for index in range(len(value) - size + 1)
    }


def _metadata_query_grams(query_lower: str) -> set[str]:
    size = min(len(query_lower), _METADATA_NGRAM_MAX_SIZE)
    if size <= 0:
        return set()
    return {
        query_lower[index:index + size]
        for index in range(len(query_lower) - size + 1)
    }


def _build_metadata_trigram_index(
    rows: tuple[tuple[str, str, str], ...],
) -> dict[str, dict[str, set[str]]]:
    index = {
        SEARCH_FIELD_TITLE: {},
        SEARCH_FIELD_FIRST_PROMPT: {},
    }
    for sid, title, first_prompt in rows:
        for size in range(1, _METADATA_NGRAM_MAX_SIZE + 1):
            for gram in _metadata_ngrams(title, size):
                index[SEARCH_FIELD_TITLE].setdefault(gram, set()).add(sid)
            for gram in _metadata_ngrams(first_prompt, size):
                index[SEARCH_FIELD_FIRST_PROMPT].setdefault(gram, set()).add(sid)
    return index


def _start_metadata_search_index_warm() -> None:
    global _metadata_trigram_index_warm_running
    if not _summary_index_loaded:
        return
    with _metadata_trigram_index_warm_lock:
        if _metadata_trigram_index_warm_running:
            return
        _metadata_trigram_index_warm_running = True

    def _warm() -> None:
        global _metadata_trigram_index_warm_running
        try:
            _metadata_search_index_for_current_version()
        finally:
            with _metadata_trigram_index_warm_lock:
                _metadata_trigram_index_warm_running = False

    threading.Thread(
        target=_warm,
        name="metadata-search-index-warm",
        daemon=True,
    ).start()


def _metadata_search_index_for_current_version() -> tuple[int, dict[str, dict[str, set[str]]]]:
    global _metadata_trigram_index_version, _metadata_trigram_index
    while True:
        with _summary_index_lock:
            version = _summary_metadata_version
            if _metadata_trigram_index_version == version:
                return version, _metadata_trigram_index
        rows = _metadata_search_rows()
        built = _build_metadata_trigram_index(rows)
        with _summary_index_lock:
            if _summary_metadata_version != version:
                continue
            if _metadata_trigram_index_version == version:
                return version, _metadata_trigram_index
            _metadata_trigram_index = built
            _metadata_trigram_index_version = version
            return version, _metadata_trigram_index


def _metadata_candidate_ids(query_lower: str, metadata_fields: tuple[str, ...]) -> set[str] | None:
    grams = _metadata_query_grams(query_lower)
    if not grams:
        return None
    with _summary_index_lock:
        if _metadata_trigram_index_version != _summary_metadata_version:
            _start_metadata_search_index_warm()
            return None
        index = _metadata_trigram_index
    candidates: set[str] | None = None
    for field in metadata_fields:
        field_index = index.get(field) or {}
        field_candidates: set[str] | None = None
        for gram in grams:
            ids = field_index.get(gram)
            if not ids:
                field_candidates = set()
                break
            if field_candidates is None:
                field_candidates = set(ids)
            else:
                field_candidates.intersection_update(ids)
            if not field_candidates:
                break
        if not field_candidates:
            continue
        if candidates is None:
            candidates = field_candidates
        else:
            candidates.update(field_candidates)
    return candidates or set()


def _metadata_search_scores(query: str, fields: set[str]) -> dict[str, int]:
    query_lower = query.lower()
    metadata_fields = tuple(
        field for field in (SEARCH_FIELD_TITLE, SEARCH_FIELD_FIRST_PROMPT)
        if field in fields
    )
    if not metadata_fields:
        return {}
    with _summary_index_lock:
        cache_key = (query_lower, metadata_fields, _summary_metadata_version)
        cached = _metadata_search_cache.get(cache_key)
        if cached is not None:
            return dict(cached)
    candidate_ids = _metadata_candidate_ids(query_lower, metadata_fields)
    if candidate_ids is None:
        rows = _metadata_search_rows()
    elif not candidate_ids:
        rows = ()
    else:
        row_map = _metadata_search_row_map()
        rows = (
            (sid, row[0], row[1])
            for sid in candidate_ids
            if (row := row_map.get(sid)) is not None
        )
    scores: dict[str, int] = {}
    search_title = SEARCH_FIELD_TITLE in metadata_fields
    search_first_prompt = SEARCH_FIELD_FIRST_PROMPT in metadata_fields
    for sid, title, first_prompt in rows:
        if search_title:
            score = title.count(query_lower)
            if score > 0:
                scores[sid] = scores.get(sid, 0) + score
        if search_first_prompt:
            score = first_prompt.count(query_lower)
            if score > 0:
                scores[sid] = scores.get(sid, 0) + score
    with _summary_index_lock:
        _metadata_search_cache[cache_key] = dict(scores)
        if len(_metadata_search_cache) > _METADATA_SEARCH_CACHE_MAX:
            _metadata_search_cache.pop(next(iter(_metadata_search_cache)))
    return scores


def _rank_search_score_items(
    scores: dict[str, int],
    *,
    offset: int,
    limit: int,
    sort_by: str,
    folder_view: bool,
) -> tuple[list[tuple[str, int]], int]:
    if not scores:
        return [], 0
    import heapq

    def visible(summary: dict) -> bool:
        wm = summary.get("working_mode")
        if not wm:
            return True
        meta = summary.get("working_mode_meta") or {}
        return wm == "file_editing" and bool(meta.get("persistent"))

    _ensure_summary_index(blocking=False)
    with _summary_index_lock:
        sort_keys = {
            sid: (
                bool(summary.get("folder_id")) if folder_view else False,
                bool(summary.get("pinned", False)),
                score,
                timestamp_sort_value(summary.get(sort_by)),
            )
            for sid, score in scores.items()
            if (summary := _summary_index.get(sid)) is not None
            and visible(summary)
        }
    end = max(offset + limit, 0)
    items = ((sid, scores[sid]) for sid in sort_keys)
    if 0 < end < len(sort_keys):
        ranked = heapq.nlargest(end, items, key=lambda item: sort_keys[item[0]])
    else:
        ranked = sorted(items, key=lambda item: sort_keys[item[0]], reverse=True)
    return ranked[offset:end], len(sort_keys)


def grep_session_scores(
    query: str,
    fields: Iterable[str] | None = None,
    *,
    content_limit: int = 10_000,
    content_max_wait_seconds: float | None = None,
) -> dict[str, int]:
    selected_fields = _normalize_search_fields(fields)
    if not selected_fields:
        return {}
    scores = _metadata_search_scores(query, selected_fields)
    if SEARCH_FIELD_CONTENT in selected_fields:
        import session_search_index
        effective_content_wait = content_max_wait_seconds
        if (
            effective_content_wait is not None
            and len(scores) >= max(content_limit, 1)
        ):
            effective_content_wait = 0.0
        for item in session_search_index.search(
            query,
            limit=content_limit,
            max_wait_seconds=effective_content_wait,
        ):
            sid = item.get("session_id")
            if sid:
                sid_str = str(sid)
                scores[sid_str] = scores.get(sid_str, 0) + int(item.get("score") or 0)
    return scores


def grep_session_score_page(
    query: str,
    fields: Iterable[str] | None = None,
    *,
    offset: int = 0,
    limit: int = 50,
    sort_by: str = "updated_at",
    folder_view: bool = False,
    content_limit: int = 10_000,
    content_max_wait_seconds: float | None = None,
) -> tuple[list[tuple[str, int]], int]:
    scores = grep_session_scores(
        query,
        fields,
        content_limit=content_limit,
        content_max_wait_seconds=content_max_wait_seconds,
    )
    return _rank_search_score_items(
        scores,
        offset=offset,
        limit=limit,
        sort_by=sort_by,
        folder_view=folder_view,
    )


def grep_sessions(query: str, limit: int = 50, fields: Iterable[str] | None = None) -> list[dict]:
    ranked, _total = grep_session_score_page(
        query,
        fields,
        offset=0,
        limit=limit,
        content_limit=max(limit, 1),
    )
    return [{"session_id": sid, "score": score} for sid, score in ranked]
