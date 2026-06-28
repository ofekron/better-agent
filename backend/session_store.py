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
import json
import logging
import os
import tempfile
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Iterable, Iterator, Optional

import config_store
import perf
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

SCHEMA_VERSION = 11


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
    if session.get("kind", "user") in _NON_USER_INITIATED_KINDS:
        return False
    if (session.get("source") or "web") in _NON_USER_INITIATED_SOURCES:
        return False
    if session.get("working_mode") in (
        "search_worker", "ask_singleton", "assistant_board",
    ):
        return False
    return True


def _sessions_dir() -> Path:
    return ba_home() / "sessions"


def _ensure_dir():
    _sessions_dir().mkdir(parents=True, exist_ok=True)


# ── Fork index ────────────────────────────────────────────────────────
#
# In-memory map of fork_id → root_id. Roots are NOT in this map (a sid
# absent from the index resolves to itself). Loaded lazily on first
# resolve, mutated by every fork_session / delete that touches a fork.

_fork_index: dict[str, str] = {}
_root_forks: dict[str, set[str]] = {}
_root_index_signatures: dict[str, tuple[int, int]] = {}
_index_loaded = False
_index_lock = threading.Lock()
# Stat-only signature of the sessions dir at the last full scan:
# (file_count, newest_mtime_ns, total_size). `_refresh_index` compares
# the live signature against this and skips the expensive parse-every-
# file rescan when the dir is byte-for-byte unchanged.
_index_fingerprint: Optional[tuple[int, int, int]] = None
_index_build_lock = threading.Lock()

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
_summary_sorted_cache_version = -1
_summary_sorted_cache: list[dict] = []
_requirement_tags_by_session: dict[str, list[dict]] = {}
_requirement_tags_lock = threading.Lock()
# Per-session extension attention markers: sid -> {extension_id -> marker}.
# Disposable projection mirroring requirement_tags; owned via
# session_manager mutators, rebuilt on demand.
_markers_by_session: dict[str, dict[str, dict]] = {}
_markers_lock = threading.Lock()
# Single-flights the one-time summary-index build. Held ONLY by
# `_ensure_summary_index` and acquired by nothing else, so it can never be
# the inner lock of a cycle. The build runs under THIS lock — never under
# `_summary_index_lock` — so the build can freely call `list_workers`
# (which takes `worker_store._lock_for()`) and `write_session_full`
# without forming the `_summary_index_lock <-> _lock_for(cwd)` ABBA or the
# `_summary_index_lock` self-re-entry that `_upsert_summary` would cause.
_summary_build_lock = threading.Lock()


def timestamp_sort_value(value: object) -> float:
    if not isinstance(value, str) or not value:
        return 0.0
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        return datetime.fromisoformat(normalized).timestamp()
    except ValueError:
        return 0.0


def _newer_timestamp(left: str, right: str) -> str:
    return left if timestamp_sort_value(left) >= timestamp_sort_value(right) else right


def _walk_forks(node: dict) -> Iterator[dict]:
    """Yield every fork dict reachable from `node` (depth-first, includes
    nested forks). Does NOT yield `node` itself."""
    for child in node.get("forks") or []:
        yield child
        yield from _walk_forks(child)


# ── Summary index helpers ────────────────────────────────────────────


def _build_summary_for_root(root: dict) -> dict:
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
    _last_msg_ts = _msgs[-1].get("timestamp", "") if _msgs else ""
    _stored_updated = root.get("updated_at", "")
    _effective_updated = (
        _newer_timestamp(_stored_updated, _last_msg_ts)
        if _last_msg_ts else _stored_updated
    )
    summary = {
        "id": root["id"],
        "name": root.get("name") or t("session.untitled"),
        "model": root.get("model", ""),
        "reasoning_effort": root.get("reasoning_effort", ""),
        "permission": root.get("permission", {}),
        "provider_id": root.get("provider_id"),
        "cwd": cwd,
        "node_id": root.get("node_id") or "primary",
        "created_at": root.get("created_at", ""),
        "updated_at": _effective_updated,
        "last_user_prompt_at": _last_user_prompt_timestamp(root),
        "last_opened_at": root.get("last_opened_at", ""),
        "message_count": len(_msgs),
        "first_prompt": _first_user_prompt(root),
        "token_usage_total": root.get("token_usage_total"),
        "token_usage_last": root.get("token_usage_last"),
        "last_seen_event_uid": root.get("last_seen_event_uid"),
        "unseen_error": root.get("unseen_error"),
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
        "rearranger_enabled": root.get("rearranger_enabled", False),
        "supervisor_enabled": root.get("supervisor_enabled", False),
        "supervisor_custom_prompt": root.get("supervisor_custom_prompt", ""),
        "rearranger_stats": root.get("rearranger_stats"),
        "continuation_chain": root.get("continuation_chain", []),
        "is_prompt_engineering": bool(root.get("working_mode") == "prompt_engineering"),
        "working_mode": root.get("working_mode"),
        "working_mode_meta": root.get("working_mode_meta"),
        "pending_eng_session_id": None,
        "worker_count": _worker_summary_count(),
        "requirement_tags": _requirement_tags_for_session(root["id"]),
        "markers": _markers_for_session(root["id"]),
        "pinned": bool(root.get("pinned", False)),
        "archived": bool(root.get("archived", False)),
        "worker_eligible": bool(root.get("worker_eligible", False)),
    }
    return session_organization_store.enrich_session_summary(summary)


def set_requirement_tags_projection(tags_by_session: dict[str, list[dict]]) -> None:
    global _summary_index_version
    clean: dict[str, list[dict]] = {}
    for sid, tags in tags_by_session.items():
        if isinstance(sid, str) and isinstance(tags, list):
            clean[sid] = [tag for tag in tags if isinstance(tag, dict)]
    with _requirement_tags_lock:
        _requirement_tags_by_session.clear()
        _requirement_tags_by_session.update(clean)
    with _summary_index_lock:
        _summary_index_version += 1


def _requirement_tags_for_session(session_id: str) -> list[dict]:
    with _requirement_tags_lock:
        return list(_requirement_tags_by_session.get(session_id, []))


def _requirement_tags_snapshot() -> dict[str, list[dict]]:
    with _requirement_tags_lock:
        return {
            sid: list(tags)
            for sid, tags in _requirement_tags_by_session.items()
        }


def set_marker_projection(sid: str, extension_id: str, marker: Optional[dict]) -> None:
    """Set or clear one extension's marker on a session. ``marker=None``
    drops the key. Bumps the summary version so list snapshots refresh."""
    global _summary_index_version
    if not (isinstance(sid, str) and isinstance(extension_id, str)):
        return
    with _markers_lock:
        per = _markers_by_session.setdefault(sid, {})
        if marker is None:
            per.pop(extension_id, None)
            if not per:
                _markers_by_session.pop(sid, None)
        else:
            per[extension_id] = dict(marker)
    with _summary_index_lock:
        _summary_index_version += 1


def _markers_for_session(session_id: str) -> dict[str, dict]:
    with _markers_lock:
        return {k: dict(v) for k, v in _markers_by_session.get(session_id, {}).items()}


def _markers_snapshot() -> dict[str, dict[str, dict]]:
    with _markers_lock:
        return {
            sid: {k: dict(v) for k, v in per.items()}
            for sid, per in _markers_by_session.items()
        }


def markers_for_extension_purge(extension_id: str) -> list[str]:
    """Drop ``extension_id`` from every session's markers. Returns the
    affected session ids."""
    global _summary_index_version
    affected: list[str] = []
    with _markers_lock:
        for sid in list(_markers_by_session):
            per = _markers_by_session[sid]
            if extension_id in per:
                per.pop(extension_id, None)
                affected.append(sid)
                if not per:
                    _markers_by_session.pop(sid, None)
    if affected:
        with _summary_index_lock:
            _summary_index_version += 1
    return affected


def _upsert_summary(root: dict) -> None:
    """Update the summary index entry for this root. Called by every writer
    that mutates session-summary-visible state."""
    global _summary_index_version
    summary = _build_summary_for_root(root)
    # Preserve pending_eng_session_id from the existing index entry —
    # _build_summary_for_root can't compute it (it requires cross-session
    # lookup) and it must survive across writes to the parent session.
    with _summary_index_lock:
        existing = _summary_index.get(root["id"])
        if existing and existing.get("pending_eng_session_id"):
            summary["pending_eng_session_id"] = existing["pending_eng_session_id"]
        _summary_index[root["id"]] = summary
        _summary_index_version += 1
    # Write lightweight summary file AFTER the in-memory update. Uses
    # atomic write (tmpfile + os.replace) so a crash mid-write leaves the
    # previous file intact. Non-fatal — in-memory index is authoritative.
    try:
        _write_summary_file(root["id"], summary)
    except Exception:
        # Summary file write failure is non-fatal — in-memory index is
        # authoritative. Next write will overwrite.
        pass


def _drafts_path(root_id: str) -> Path:
    return _sessions_dir() / f"{root_id}.drafts.json"


def write_drafts(root_id: str, drafts: dict[str, dict]) -> None:
    """Atomically persist the per-node draft sidecar. `drafts` maps a
    node sid -> {draft_input, draft_input_seq, draft_images}; nodes with
    an empty draft are omitted by the caller.

    Draft state lives ONLY here — it is stripped from the session tree by
    `_strip_volatile_from_tree`, so this file is its single on-disk home.
    Keeping it out of the tree is what makes a per-keystroke draft flush
    O(one small file) instead of O(whole session tree)."""
    path = _drafts_path(root_id)
    if not drafts:
        # Nothing to persist → remove a stale sidecar so cleared drafts
        # can't resurrect on the next load.
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return
    tmp_fd, tmp_path = tempfile.mkstemp(
        prefix=f".{root_id}.drafts.", suffix=".tmp", dir=_sessions_dir(),
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(drafts, f)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def read_drafts(root_id: str) -> dict[str, dict]:
    """Load the per-node draft sidecar. Returns {} when absent or
    unreadable (a missing/torn sidecar just means empty drafts)."""
    path = _drafts_path(root_id)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def collect_tree_drafts(root: dict) -> dict[str, dict]:
    """Snapshot every node's (root + forks) non-empty draft into the
    sidecar shape {sid: {draft_input, draft_input_seq, draft_images}}.
    Single source of truth for both the seed-on-load and the
    session_manager persist paths."""
    out: dict[str, dict] = {}
    for node in [root, *_walk_forks(root)]:
        sid = node.get("id")
        if not sid:
            continue
        text = node.get("draft_input") or ""
        images = node.get("draft_images") or []
        if not text and not images:
            continue
        out[sid] = {
            "draft_input": text,
            "draft_input_seq": node.get("draft_input_seq") or 0,
            "draft_images": images,
        }
    return out


def _overlay_drafts(root: dict, root_id: str) -> None:
    """Stamp sidecar drafts back onto a freshly-loaded tree. Drafts are
    stripped from the persisted tree, so every load funnel must overlay
    them — otherwise a read returns the migration default (empty).

    Legacy seed: a pre-sidecar session still carries `draft_input` baked
    into its tree json. The FIRST tree write would strip it with no
    sidecar to fall back to → silent draft loss. So when no sidecar
    exists yet, seed it from whatever draft the loaded tree carries
    (the in-memory tree always has it here — `write_session_full`
    strips then restores in a `finally`). One-time, idempotent: once the
    sidecar exists, the overlay branch handles every later load."""
    drafts = read_drafts(root_id)
    if not drafts:
        seed = collect_tree_drafts(root)
        if seed:
            write_drafts(root_id, seed)
        return
    for node in [root, *_walk_forks(root)]:
        sid = node.get("id")
        d = drafts.get(sid) if sid else None
        if not isinstance(d, dict):
            continue
        node["draft_input"] = d.get("draft_input") or ""
        node["draft_input_seq"] = d.get("draft_input_seq") or 0
        node["draft_images"] = d.get("draft_images") or []


def _remove_summary(root_id: str) -> None:
    """Remove a root's summary entry and file (on delete)."""
    global _summary_index_version
    with _summary_index_lock:
        if _summary_index.pop(root_id, None) is not None:
            _summary_index_version += 1
    try:
        sp = _sessions_dir() / f"{root_id}.summary.json"
        sp.unlink(missing_ok=True)
    except OSError:
        pass


def _write_summary_file(root_id: str, summary: dict) -> None:
    sp = _sessions_dir() / f"{root_id}.summary.json"
    tmp_fd, tmp_path = tempfile.mkstemp(
        prefix=f".{root_id}.summary.",
        suffix=".tmp",
        dir=_sessions_dir(),
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(summary, f)
        os.replace(tmp_path, sp)
        root_path = _sessions_dir() / f"{root_id}.json"
        target_mtime_ns = time.time_ns()
        try:
            target_mtime_ns = max(target_mtime_ns, root_path.stat().st_mtime_ns)
        except OSError:
            pass
        os.utime(sp, ns=(target_mtime_ns, target_mtime_ns))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _sanitize_summary(summary: dict) -> tuple[dict, bool]:
    cleaned = dict(summary)
    changed = False
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
    global _summary_index_loaded, _summary_index_version
    _ensure_dir()
    # {session_id}.json → path
    full_files: dict[str, Path] = {}
    for p in _sessions_dir().glob("*.json"):
        if not _is_sidecar_json(p.name):
            full_files[p.stem] = p
    # {session_id}.summary.json → path
    summary_files: dict[str, Path] = {}
    for p in _sessions_dir().glob("*.summary.json"):
        sid = p.name.removesuffix(".summary.json")
        summary_files[sid] = p

    # Trees migrated in Pass 2 that need a persist — written AFTER the
    # locks release so the next start hits the Pass-1 fast path.
    dirty_trees: list[dict] = []
    stale_summaries: list[tuple[str, dict]] = []

    # Pass 1: load from summary files where available + fresh
    # (summary mtime must be >= session file mtime — a crash between
    # write_session_full and summary file write leaves a stale summary).
    missing_ids: list[str] = []
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
                        summary, cleaned = _sanitize_summary(summary)
                        needs_fork_backfill = (
                            "fork_ids" not in summary
                            and int(summary.get("fork_count") or 0) > 0
                        )
                        if not needs_fork_backfill:
                            with _summary_index_lock:
                                _summary_index[sid] = summary
                                _summary_index_version += 1
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
    eng_by_parent: dict[str, str] = {}
    for sid in missing_ids:
        fpath = full_files[sid]
        try:
            ctx = _provider_backfill_context()
            data = _migrate_session(json.loads(fpath.read_text(encoding="utf-8")), ctx)
            summary = _build_summary_for_root(data)
            with _summary_index_lock:
                _summary_index[data["id"]] = summary
                _summary_index_version += 1
            stale_summaries.append((data["id"], summary))
        except (json.JSONDecodeError, KeyError, ValueError, OSError):
            continue
        if ctx["dirty"][0]:
            dirty_trees.append(data)
        if data.get("working_mode"):
            meta = data.get("working_mode_meta") or {}
            pid = meta.get("parent_session_id")
            if pid:
                eng_by_parent[pid] = data["id"]

    # Final unified pass for eng pointers across the WHOLE index
    # (Pass 1 + Pass 2). Acquired under the index lock so all updates
    # land atomically with the `_summary_index_loaded = True` flip.
    with _summary_index_lock:
        # Collect Pass 1 eng pointers we couldn't gather during the
        # incremental publish loop above (Pass 1 didn't track them).
        for sid, summary in list(_summary_index.items()):
            if summary.get("working_mode"):
                meta = summary.get("working_mode_meta") or {}
                pid = meta.get("parent_session_id")
                if pid:
                    eng_by_parent[pid] = sid
        for pid, eng_sid in eng_by_parent.items():
            if pid in _summary_index:
                _summary_index[pid] = {
                    **_summary_index[pid],
                    "pending_eng_session_id": eng_sid,
                }
                _summary_index_version += 1
        _summary_index_loaded = True

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


def _refresh_summaries_for_cwd(cwd: str) -> None:
    """Refresh worker-dependent summary fields for all sessions."""
    _refresh_summaries_for_cwds([cwd])


def _worker_summary_count() -> int:
    from stores import worker_store
    return len(worker_store.list_workers(""))


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


def _session_file_signature(path: Path) -> Optional[tuple[int, int]]:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _index_tree(
    root: dict,
    *,
    file_signature: Optional[tuple[int, int]] = None,
    force: bool = False,
) -> None:
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
            return
        stale = _root_forks.get(rid, set())
        for fid in stale:
            _fork_index.pop(fid, None)
        current: set[str] = set()
        for fork in _walk_forks(root):
            fork_id = fork["id"]
            _fork_index[fork_id] = rid
            current.add(fork_id)
        _root_forks[rid] = current
        if file_signature is not None:
            _root_index_signatures[rid] = file_signature


def _index_set(fork_id: str, root_id: str) -> None:
    global _index_loaded, _index_fingerprint
    fp = _dir_fingerprint()
    with _index_lock:
        _fork_index[fork_id] = root_id
        _root_forks.setdefault(root_id, set()).add(fork_id)
        _index_loaded = True
        _index_fingerprint = fp


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


# Sidecar files share the sessions dir and the `.json` extension but are
# NOT session root trees — they must be excluded from every root-file
# glob (else they get parsed as sessions → KeyError 'id').
_SIDECAR_JSON_SUFFIXES = (".summary.json", ".drafts.json", ".fork-index.json")


def _is_sidecar_json(name: str) -> bool:
    return name.endswith(_SIDECAR_JSON_SUFFIXES)


def _session_json_files() -> Iterator[Path]:
    """Yield session root JSON files, excluding sidecars."""
    for p in _sessions_dir().glob("*.json"):
        if not _is_sidecar_json(p.name):
            yield p


def _fork_index_path() -> Path:
    return _sessions_dir() / ".fork-index.json"


def _session_json_files_requiring_fork_scan() -> Iterator[Path]:
    for p in _session_json_files():
        sp = p.with_name(f"{p.stem}.summary.json")
        try:
            if sp.stat().st_mtime_ns >= p.stat().st_mtime_ns:
                summary = json.loads(sp.read_text(encoding="utf-8"))
                if summary.get("id") != p.stem:
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


def _dir_fingerprint() -> tuple[int, int, int]:
    """Cheap stat-only signature of the sessions dir: (file_count,
    newest_mtime_ns, total_size). No file contents are read. Bumps on
    any add/remove/modify — including a fork written into an existing
    root file, which changes that file's mtime and size."""
    count = 0
    max_mtime = 0
    total_size = 0
    try:
        with os.scandir(_sessions_dir()) as it:
            for entry in it:
                if not entry.name.endswith(".json") or _is_sidecar_json(entry.name):
                    continue
                try:
                    st = entry.stat()
                except OSError:
                    continue
                count += 1
                if st.st_mtime_ns > max_mtime:
                    max_mtime = st.st_mtime_ns
                total_size += st.st_size
    except OSError:
        pass
    return (count, max_mtime, total_size)


def _build_index_snapshot() -> tuple[tuple[int, int, int], dict[str, str], dict[str, set[str]], dict[str, tuple[int, int]]]:
    """Build a fork-index snapshot without holding `_index_lock`."""
    fp = _dir_fingerprint()
    cached = _load_index_sidecar(fp)
    if cached is not None:
        return cached
    refreshed = _refresh_stale_index_sidecar(fp)
    if refreshed is not None:
        return refreshed
    fork_index: dict[str, str] = {}
    root_forks: dict[str, set[str]] = {}
    root_signatures: dict[str, tuple[int, int]] = {}
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
            if summary.get("id") != path.stem:
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


def _load_index_sidecar(
    expected_fp: tuple[int, int, int],
) -> Optional[tuple[tuple[int, int, int], dict[str, str], dict[str, set[str]], dict[str, tuple[int, int]]]]:
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
) -> Optional[tuple[dict[str, str], dict[str, set[str]], dict[str, tuple[int, int]]]]:
    try:
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
            str(k): (int(v[0]), int(v[1]))
            for k, v in (raw.get("root_signatures") or {}).items()
            if (
                isinstance(k, str)
                and isinstance(v, list)
                and len(v) == 2
            )
        }
        return fork_index, root_forks, root_signatures
    except (TypeError, ValueError):
        return None


def _root_signatures_from_disk() -> Optional[dict[str, tuple[int, int]]]:
    signatures: dict[str, tuple[int, int]] = {}
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
    fp: tuple[int, int, int],
) -> Optional[tuple[tuple[int, int, int], dict[str, str], dict[str, set[str]], dict[str, tuple[int, int]]]]:
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
        path = _sessions_dir() / f"{root_id}.json"
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
        if summary.get("id") != path.stem:
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
    fp: tuple[int, int, int],
    fork_index: dict[str, str],
    root_forks: dict[str, set[str]],
    root_signatures: dict[str, tuple[int, int]],
) -> None:
    payload = {
        "fingerprint": list(fp),
        "fork_index": fork_index,
        "root_forks": {
            root_id: sorted(forks)
            for root_id, forks in root_forks.items()
            if forks
        },
        "root_signatures": {
            root_id: [sig[0], sig[1]]
            for root_id, sig in root_signatures.items()
        },
    }
    path = _fork_index_path()
    tmp_fd, tmp_path = tempfile.mkstemp(
        prefix=".fork-index.",
        suffix=".json.tmp",
        dir=_sessions_dir(),
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _persist_index_sidecar_if_loaded() -> None:
    global _index_fingerprint
    with _index_lock:
        if not _index_loaded:
            return
        fp = _dir_fingerprint()
        _index_fingerprint = fp
        fork_index = dict(_fork_index)
        root_forks = {
            root_id: set(forks)
            for root_id, forks in _root_forks.items()
        }
        root_signatures = dict(_root_index_signatures)
    try:
        _write_index_sidecar(fp, fork_index, root_forks, root_signatures)
    except OSError:
        pass


def _install_index_snapshot(
    fp: tuple[int, int, int],
    fork_index: dict[str, str],
    root_forks: dict[str, set[str]],
    root_signatures: dict[str, tuple[int, int]],
) -> None:
    global _index_fingerprint
    _fork_index.clear()
    _fork_index.update(fork_index)
    _root_forks.clear()
    _root_forks.update(root_forks)
    _root_index_signatures.clear()
    _root_index_signatures.update(root_signatures)
    _index_fingerprint = fp
    try:
        _write_index_sidecar(fp, fork_index, root_forks, root_signatures)
    except OSError:
        pass


def _refresh_index() -> None:
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
    _ensure_dir()
    live_fp = _dir_fingerprint()
    with _index_lock:
        if _index_fingerprint is not None and live_fp == _index_fingerprint:
            return
    with _index_build_lock:
        with _index_lock:
            if _index_fingerprint is not None and live_fp == _index_fingerprint:
                return
        for _ in range(2):
            with perf.timed("store.session.index.refresh.build"):
                fp, fork_index, root_forks, root_signatures = _build_index_snapshot()
            if _dir_fingerprint() != fp:
                continue
            with _index_lock:
                if _index_fingerprint is not None and _index_fingerprint == fp:
                    return
                _install_index_snapshot(fp, fork_index, root_forks, root_signatures)
            return


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


def _resolve_root_id(sid: str) -> Optional[str]:
    """Return the root id for any session id (root or fork). None if
    the id is unknown.

    Cross-process safe: on a miss, re-scans the sessions directory
    once before giving up — covers the case where another process
    (CLI, second backend) created a fork after this process started."""
    if (_sessions_dir() / f"{sid}.json").exists():
        return sid
    _ensure_index()
    if sid in _fork_index:
        return _fork_index[sid]
    # Miss: another process may have minted this sid. Refresh and
    # retry once. Idempotent — no-op if our cache was already current.
    _refresh_index()
    if sid in _fork_index:
        return _fork_index[sid]
    if (_sessions_dir() / f"{sid}.json").exists():
        return sid
    return None


def _session_path(sid: str) -> Path:
    """Return the on-disk path for the root file that contains `sid`.
    For a fork id, returns the root's file (the fork is embedded
    inside it). For a root id, returns its own file. For an unknown
    id, returns `<sid>.json` — caller is creating a new root."""
    root_id = _resolve_root_id(sid)
    if root_id is None:
        root_id = sid
    return _sessions_dir() / f"{root_id}.json"


def session_file_fingerprint(root_id: str) -> Optional[tuple[int, int]]:
    path = _sessions_dir() / f"{root_id}.json"
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


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
    session.setdefault("rearranger_enabled", False)
    session.setdefault("supervisor_enabled", False)
    session.setdefault("supervisor_custom_prompt", "")
    session.setdefault("pending_supervisor_verdict", None)
    session.setdefault("rearranger_tree", None)
    session.setdefault("rearranger_session_id", None)
    session.setdefault("rearranger_last_message_count", 0)
    session.setdefault("rearranger_stats", {
        "call_count": 0,
        "total_cost_usd": 0.0,
        "token_usage": {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
    })
    # Backfill `user_initiated` BEFORE the source coercion below clobbers
    # non-(web,cli) source values (e.g. "internal"/"extension") to "web" —
    # those source labels are a signal `_infer_user_initiated` relies on.
    if "user_initiated" not in session:
        session["user_initiated"] = _infer_user_initiated(session)
        ctx.setdefault("dirty", [False])[0] = True
    src = session.get("source")
    if src not in ("web", "cli"):
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
    # Right-panel UI state — persisted per-session, mutated via
    # PATCH /api/sessions/{sid}/right-panel and broadcast via
    # session_metadata_updated (kind: right_panel_set). Default-on-
    # read: open=True (new sessions show the panel by default),
    # active_tab=None (render-time fallback picks the first tab
    # with content; user clicks persist an explicit choice).
    session.setdefault("right_panel_open", True)
    session.setdefault("right_panel_active_tab", None)
    session.setdefault("draft_input", "")
    session.setdefault("draft_input_seq", 0)
    session.setdefault("draft_images", [])
    session.setdefault("capability_contexts", [])
    session.setdefault("working_mode", None)
    session.setdefault("working_mode_meta", None)
    session.setdefault("browser_harness_enabled", True)
    session.setdefault("browser_harness_headless", True)
    session.setdefault("bare_config", False)
    session.setdefault("pinned", False)
    session.setdefault("archived", False)
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


def should_auto_register_project(session: dict) -> bool:
    """Whether a session's cwd should be auto-registered as a user
    project. `bare_config` sessions are internal/isolated (provisioned
    machine-completion workers, TestApe-isolated runs); their cwd is an
    implementation detail and must never surface in the user's project
    list."""
    return bool(session.get("cwd")) and not session.get("bare_config")


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
    id: Optional[str] = None,
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
    _ensure_dir()
    if provider_id is None:
        provider_id = config_store.default_session_provider_id()
    if not model:
        model = config_store.default_session_model()
    if reasoning_effort is None:
        reasoning_effort = config_store.default_session_reasoning_effort() or None
    sid = id or str(uuid.uuid4())
    if id is not None and (_sessions_dir() / f"{sid}.json").exists():
        raise ValueError(f"session id already exists: {sid}")
    resolved_reasoning_effort = _session_reasoning_effort(
        reasoning_effort, provider_id,
    )
    session = {
        "id": sid,
        "_schema_version": SCHEMA_VERSION,
        "name": name or t("session.default_name", time=datetime.now().strftime('%H:%M')),
        "model": model,
        "reasoning_effort": resolved_reasoning_effort,
        "permission": _session_permission(permission, provider_id),
        "cwd": cwd or str(Path.home()),
        "created_at": datetime.now().isoformat(),
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
        "source": source if source in ("web", "cli", "import") else "web",
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
        "queued_prompts": [],
        "capability_contexts": [],
        "draft_input": "",
        "draft_input_seq": 0,
        "draft_images": [],
        "browser_harness_enabled": browser_harness_enabled,
        "browser_harness_headless": browser_harness_headless,
        "worker_creation_policy": (
            worker_creation_policy
            if worker_creation_policy in ("ask", "approve", "deny")
            else "ask"
        ),
        "bare_config": bool(bare_config),
        "worker_eligible": False,
        "pinned": True,
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
    without bumping `updated_at` — backfill isn't user-visible activity."""
    ctx = _provider_backfill_context()
    migrated = _migrate_session(root, ctx)
    if ctx["dirty"][0]:
        try:
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
    path = _sessions_dir() / f"{root_id}.json"
    if not path.exists():
        return None
    root = _migrate_and_persist(json.loads(path.read_text(encoding="utf-8")))
    # Re-index in case the file was written by another process and
    # contains forks not yet in our in-memory map.
    _index_tree(root, force=True)
    _overlay_drafts(root, root_id)
    return _find_in_tree(root, session_id)


def read_node_kind_record(root_id: str, sid: str) -> Optional[dict]:
    """Pure, side-effect-free read of just `{"kind": ...}` for node `sid`
    in root `root_id`. NO migration, NO draft overlay, NO disk write —
    unlike `get_root_tree`, so a hot read-only caller (recompute_state's
    kind gate) never triggers a loop-thread write or a draft seed.
    Returns None when the root file or the node is absent."""
    path = _sessions_dir() / f"{root_id}.json"
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
        path = _sessions_dir() / f"{root_id}.json"
        if not path.exists():
            return None
        file_signature = _session_file_signature(path)
        root = json.loads(path.read_text(encoding="utf-8"))
    with perf.timed("store.session.get_root_tree.migrate"):
        root = _migrate_and_persist(root)
    with perf.timed("store.session.get_root_tree.index_tree"):
        if session_id != root_id:
            _index_tree(root, file_signature=file_signature)
    with perf.timed("store.session.get_root_tree.overlay_drafts"):
        _overlay_drafts(root, root_id)
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
    drafts: list[tuple[dict, dict]] = []
    _DRAFT_KEYS = ("draft_input", "draft_input_seq", "draft_images")

    def _pop_drafts(node: dict) -> None:
        # Per-node draft (root + every fork) lives ONLY in the drafts
        # sidecar (`write_drafts`). Stripping it here is what removes the
        # whole-tree rewrite on every keystroke.
        popped = {k: node.pop(k) for k in _DRAFT_KEYS if k in node}
        if popped:
            drafts.append((node, popped))
    def _pop_uid_idx(owner: dict) -> None:
        idx = owner.pop("_uid_idx", None)
        if isinstance(idx, dict):
            uid_idxs.append((owner, idx))

    def _pop_events(owner: dict) -> None:
        ev = owner.get("events")
        if isinstance(ev, list):
            events_lists.append((owner, ev))
            del owner["events"]

    stack = [root]
    while stack:
        node = stack.pop()
        _pop_drafts(node)
        for m in node.get("messages", []):
            if m.get("role") == "assistant":
                try:
                    from render_stub import message_output_text
                    content = message_output_text(m)
                except Exception:
                    content = ""
                if content:
                    m["content"] = content
            if "isStreaming" in m:
                isstreaming.append((m, m["isStreaming"]))
                del m["isStreaming"]
            _pop_events(m)
            _pop_uid_idx(m)
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
        "drafts": drafts,
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
    for node, fields in popped.get("drafts", []):
        node.update(fields)


@perf.timed_fn("store.session.write_full")
def write_session_full(root: dict, *, bump_updated_at: bool = True) -> None:
    """Write the whole ROOT tree to disk. Caller MUST pass a root dict
    (the top-level record), not an embedded fork — embedded fork writes
    happen by mutating the in-memory root and calling this function on
    the root. SessionManager handles the mutate-in-place + write-root
    pattern via its `_persist`.

    SEMANTICS: the per-write step is atomic against torn reads — a
    reader concurrent with a write sees either the pre-write file or
    the post-write file, never a partial one. Last-writer-wins applies
    to concurrent writers; the per-root `_lock_for_root` in
    `session_manager` is the only thing that serializes them. Callers
    that bypass that lock (e.g. `_migrate_and_persist`,
    `adv_sync.recover_running_overlays_on_startup`) can clobber each other on
    the same root id — pre-existing behavior, unchanged by this
    function.
    """
    if root.get("parent_session_id"):
        raise ValueError(
            "write_session_full received a fork dict; pass the root tree "
            "(SessionManager._persist resolves to root before calling)."
        )
    if bump_updated_at:
        root["updated_at"] = datetime.now().isoformat()
    _index_tree(root)
    path = _sessions_dir() / f"{root['id']}.json"
    # Bootstrap writer: often the first write into a fresh home (new session,
    # no prior index warm-up), so the sessions/ dir may not exist yet.
    # mkstemp(dir=path.parent) below requires the parent to already exist.
    _ensure_dir()
    # INVARIANT: atomic write — serialize to a temp file in the same
    # directory then `os.replace` into place. A crash between the
    # `write` and the `replace` leaves the canonical file unchanged.
    #
    # Tmpfile leak: `mkstemp` mints a random suffix, so an orphan from
    # a crashed write does NOT get reclaimed by any later write — it
    # lingers until manual cleanup or a startup sweep. The naming
    # (leading `.` + `.json.tmp` suffix) ensures the orphan is invisible
    # to `list_sessions`' `*.json` glob and to both directory
    # fingerprints (`endswith(".json")` is False for `.json.tmp`).
    #
    # `indent` dropped — session JSON is not human-edited and `indent=2`
    # is ~30-40% slower for large trees.
    tmp_fd, tmp_path = tempfile.mkstemp(
        prefix=f".{root['id']}.",
        suffix=".json.tmp",
        dir=path.parent,
    )
    with perf.timed("store.session.write_full.strip"):
        popped = _strip_volatile_from_tree(root)
    try:
        with perf.timed("store.session.write_full.dump"):
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                json.dump(root, f, separators=(",", ":"))
        with perf.timed("store.session.write_full.replace"):
            os.replace(tmp_path, path)
        with perf.timed("store.session.write_full.signature"):
            file_signature = _session_file_signature(path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    finally:
        _restore_volatile_to_tree(popped)
    if file_signature is not None:
        with _index_lock:
            _root_index_signatures[root["id"]] = file_signature
            index_loaded = _index_loaded
    else:
        index_loaded = False
    if index_loaded:
        _persist_index_sidecar_if_loaded()
    # INVARIANT: update summary index AFTER the durable write (post
    # `os.replace`). A summary update before the replace would let a
    # concurrent `list_sessions` observe the new summary while the
    # on-disk file is still the old one.
    with perf.timed("store.session.write_full.summary"):
        _upsert_summary(root)


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
    global _summary_sorted_cache_version, _summary_sorted_cache
    _ensure_summary_index(blocking=False)
    requirement_tags = _requirement_tags_snapshot()
    markers = _markers_snapshot()
    with _summary_index_lock:
        if _summary_sorted_cache_version != _summary_index_version:
            _summary_sorted_cache = sorted(
                _summary_index.values(),
                key=lambda s: (
                    s.get("pinned", False),
                    timestamp_sort_value(s.get("updated_at")),
                ),
                reverse=True,
            )
            _summary_sorted_cache_version = _summary_index_version
        items = list(_summary_sorted_cache)
    return [
        {
            **summary,
            "requirement_tags": requirement_tags.get(summary.get("id", ""), []),
            "markers": markers.get(summary.get("id", ""), {}),
        }
        for summary in items
    ]


def get_session_summaries_by_ids(session_ids: Iterable[str]) -> list[dict]:
    ids = [sid for sid in session_ids if sid]
    if not ids:
        return []
    _ensure_summary_index(blocking=False)
    requirement_tags = _requirement_tags_snapshot()
    markers = _markers_snapshot()
    with _summary_index_lock:
        summaries = [
            _summary_index[sid]
            for sid in ids
            if sid in _summary_index
        ]
    return [
        {
            **summary,
            "requirement_tags": requirement_tags.get(summary.get("id", ""), []),
            "markers": markers.get(summary.get("id", ""), {}),
        }
        for summary in summaries
    ]


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
        "source": parent.get("source") if parent.get("source") in ("web", "cli", "import") else "web",
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
        "draft_input": "",
        "draft_input_seq": 0,
        "draft_images": [],
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
        "draft_input": "",
        "draft_input_seq": 0,
        "draft_images": [],
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
        "archived": False,
        "supervisor_enabled": False,
        "supervisor_custom_prompt": "",
        "pending_supervisor_verdict": None,
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
        "draft_input": "",
        "draft_input_seq": 0,
        "draft_images": [],
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
    path = _sessions_dir() / f"{root_id}.json"
    if not path.exists():
        return False
    root = _migrate_session(json.loads(path.read_text(encoding="utf-8")))
    _index_pop(root_id)
    _root_forks.pop(root_id, None)
    _root_index_signatures.pop(root_id, None)
    for fork in _walk_forks(root):
        _index_pop(fork["id"])
    path.unlink()
    # Delete has no write funnel — remove from the summary index directly.
    _remove_summary(root_id)
    try:
        import session_search_index
        session_search_index.delete_session(root_id)
    except Exception:
        _logger.debug("session search index delete failed", exc_info=True)
    try:
        _drafts_path(root_id).unlink(missing_ok=True)
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


def _normalize_search_fields(fields: Iterable[str] | None) -> set[str]:
    if fields is None:
        return set(DEFAULT_SEARCH_FIELDS)
    return {field for field in fields if field in SEARCH_FIELDS}


def _match_count(value: object, query_lower: str) -> int:
    if not isinstance(value, str) or not query_lower:
        return 0
    return value.lower().count(query_lower)


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


def _metadata_search_scores(query: str, fields: set[str]) -> dict[str, int]:
    query_lower = query.lower()
    scores: dict[str, int] = {}
    if SEARCH_FIELD_TITLE not in fields and SEARCH_FIELD_FIRST_PROMPT not in fields:
        return scores
    _ensure_summary_index(blocking=False)
    with _summary_index_lock:
        summaries = list(_summary_index.values())
    if SEARCH_FIELD_TITLE in fields:
        for summary in summaries:
            sid = summary.get("id")
            score = _match_count(summary.get("name"), query_lower)
            if sid and score > 0:
                scores[str(sid)] = scores.get(str(sid), 0) + score
    if SEARCH_FIELD_FIRST_PROMPT not in fields:
        return scores
    for summary in summaries:
        sid = summary.get("id")
        if not sid:
            continue
        score = _match_count(summary.get("first_prompt"), query_lower)
        if score > 0:
            scores[str(sid)] = scores.get(str(sid), 0) + score
    return scores


def grep_session_scores(
    query: str,
    fields: Iterable[str] | None = None,
    *,
    content_limit: int = 10_000,
) -> dict[str, int]:
    selected_fields = _normalize_search_fields(fields)
    if not selected_fields:
        return {}
    scores: dict[str, int] = {}
    if SEARCH_FIELD_CONTENT in selected_fields:
        import session_search_index
        scores.update({
            str(item.get("session_id")): int(item.get("score") or 0)
            for item in session_search_index.search(query, limit=content_limit)
            if item.get("session_id")
        })
    for sid, score in _metadata_search_scores(query, selected_fields).items():
        scores[sid] = scores.get(sid, 0) + score
    return scores


def grep_sessions(query: str, limit: int = 50, fields: Iterable[str] | None = None) -> list[dict]:
    scores = grep_session_scores(query, fields, content_limit=max(limit, 1))
    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)[:limit]
    return [{"session_id": sid, "score": score} for sid, score in ranked]
