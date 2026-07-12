"""Global worker registry + per-pair fork mapping.

A "worker" is a Better Agent session that has been registered as
delegate-able. Multiple Better Agent sessions across projects share this registry.
The registry holds:

  1. `workers` — which Better Agent sessions are delegate-able, what
     their orchestration mode is, and which agent_sid the manager forks
     off. Each record carries the worker session's cwd for filtering and
     execution. Provisioned workers also store their stable worker `name`
     and `role_key`; the Better Agent session title is user/provider-owned
     display state and can change independently.

  2. `forks` — the per-(caller Better Agent session, target Better Agent session)
     fork session id. Each delegate fork is now a full Better Agent session (kind
     `kind="delegate_fork"`) embedded in the target's session tree —
     `forks[a_agent_session_id][b_agent_session_id].fork_agent_session_id` resolves through
     `session_manager.get(...)` to a record carrying its own
     `agent_sid`, `orchestration_mode`, `forked_from_agent_sid`
     (= target's agent_sid at fork time, used for invalidation), and
     `parent_line_count_at_fork`. Subsequent delegations from A to B
     load that Better Agent session, validate the snapshots, and resume its
     agent_sid; if invalid, the fork Better Agent session is deleted and a
     fresh one minted.

Storage: one JSON file at ~/.better-claude/workers/global.json with shape:

    {
        "version": 7,
        "workers": [
            {
                "agent_session_id": str,
                "name": str | None,
                "role_key": str | None,
                "cwd": str,
                "orchestration_mode": "manager" | "native",
                "agent_sid": str,           # what we fork off
                "created_at": iso,
                "last_active": iso,
                "delegation_count": int,
                "token_usage": {...},
            },
            ...
        ],
        "forks": {
            "<caller_agent_session_id>": {
                "<worker_agent_session_id>": {
                    "fork_agent_session_id": str,
                    "created_at": iso,
                    "last_used": iso,
                },
                ...
            },
            ...
        }
    }

There is no migration from prior schemas — any file that doesn't match
the v7 shape is treated as a hard error. Wipe ~/.better-claude/workers/
manually if you have stale state.
"""

import json
import hashlib
import logging
import os
import threading
import time
from dataclasses import dataclass
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Optional
from uuid import uuid4

from json_store import write_json, write_json_durable
from session_manager import manager as _sm
import perf

logger = logging.getLogger(__name__)

from paths import ba_home

_lock = threading.RLock()
_worker_count_cache: dict[tuple[str, tuple[int, int]], int] = {}
_worker_count_cache_until = 0.0
_WORKER_COUNT_HOT_TTL_SECONDS = 1.0
_registry_cache_signature: tuple[int, int] | None = None
_registry_cache: dict | None = None
_workers_dir_cache: Path | None = None
_registry_revision = 0
_registry_worker_ids: set[str] = set()
_activity_lock = threading.RLock()
_activity_loaded = False
_activity_epoch = ""
_activity_seq = 0
_activity_by_worker: dict[str, dict] = {}
_activity_compacting = False
_ACTIVITY_COMPACT_EVERY = 1024


def _activity_compaction_boundary(_stage: str) -> None:
    return


@dataclass(frozen=True)
class WorkerActivityCommit:
    authority_epoch: str
    seq: int
    worker: dict

    def event_data(self) -> dict:
        return {
            "authority_epoch": self.authority_epoch,
            "revision": self.seq,
            "worker": deepcopy(self.worker),
        }


def _lock_for(_cwd: str = "") -> threading.Lock:
    return _lock


def _workers_dir() -> Path:
    global _workers_dir_cache
    cached = _workers_dir_cache
    if cached is None:
        cached = ba_home() / "workers"
        _workers_dir_cache = cached
    return cached


SCHEMA_VERSION = 8


def _now() -> str:
    return datetime.now().isoformat()


def _path() -> Path:
    return _workers_dir() / "global.json"


def _activity_checkpoint_path() -> Path:
    return _workers_dir() / "activity.json"


def _activity_journal_path() -> Path:
    return _workers_dir() / "activity.jsonl"


def _activity_digest(epoch: str, seq: int, workers: dict[str, dict]) -> str:
    encoded = json.dumps(
        {"authority_epoch": epoch, "seq": seq, "workers": workers},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _activity_fields(worker: dict) -> dict:
    return {
        "last_active": worker.get("last_active"),
        "delegation_count": int(worker.get("delegation_count", 0)),
        "token_usage": deepcopy(worker.get("token_usage") or {}),
    }


def _load_activity_locked(seed_workers: list[dict] | None = None) -> None:
    global _activity_loaded, _activity_epoch, _activity_seq, _activity_by_worker
    if _activity_loaded:
        return
    epoch = uuid4().hex
    seq = 0
    workers = {
        str(worker.get("agent_session_id")): _activity_fields(worker)
        for worker in (seed_workers or [])
        if worker.get("agent_session_id")
    }
    checkpoint = _activity_checkpoint_path()
    checkpoint_exists = checkpoint.exists()
    try:
        raw = json.loads(checkpoint.read_text(encoding="utf-8"))
        candidate_epoch = str(raw.get("authority_epoch") or "")
        candidate_seq = int(raw.get("seq", -1))
        candidate_workers = raw.get("workers")
        if (
            candidate_epoch
            and candidate_seq >= 0
            and isinstance(candidate_workers, dict)
            and raw.get("checksum")
            == _activity_digest(candidate_epoch, candidate_seq, candidate_workers)
        ):
            epoch = candidate_epoch
            seq = candidate_seq
            workers = candidate_workers
        else:
            raise ValueError(f"worker activity checkpoint failed validation: {checkpoint}")
    except FileNotFoundError:
        checkpoint_exists = False
        pass
    except Exception:
        logger.exception("worker activity checkpoint load failed: %s", checkpoint)
        raise
    journal = _activity_journal_path()
    try:
        with journal.open("r", encoding="utf-8") as handle:
            for line in handle:
                event = json.loads(line)
                event_seq = int(event.get("seq", -1))
                event_epoch = str(event.get("authority_epoch") or "")
                if not checkpoint_exists and seq == 0 and event_epoch:
                    epoch = event_epoch
                if event_epoch != epoch or event_seq <= seq:
                    continue
                if event_seq != seq + 1:
                    raise ValueError(f"worker activity sequence gap {seq}->{event_seq}")
                worker_id = str(event.get("worker_id") or "")
                if not worker_id:
                    raise ValueError("worker activity event missing worker_id")
                if event.get("tombstone"):
                    workers.pop(worker_id, None)
                else:
                    activity = event.get("activity")
                    if not isinstance(activity, dict):
                        raise ValueError("worker activity event missing activity")
                    workers[worker_id] = activity
                seq = event_seq
    except FileNotFoundError:
        pass
    except Exception:
        logger.exception("worker activity journal replay failed closed: %s", journal)
        raise
    for worker in seed_workers or []:
        worker_id = str(worker.get("agent_session_id") or "")
        if worker_id and worker_id not in workers:
            workers[worker_id] = _activity_fields(worker)
    _activity_epoch = epoch
    _activity_seq = seq
    _activity_by_worker = workers
    _activity_loaded = True


def _ensure_activity(seed_workers: list[dict] | None = None) -> None:
    with _activity_lock:
        _load_activity_locked(seed_workers)


def _append_activity_locked(event: dict) -> None:
    path = _activity_journal_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    started = time.perf_counter()
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    perf.record("store.worker.activity.append", (time.perf_counter() - started) * 1000)


def _compact_activity() -> None:
    global _activity_compacting
    try:
        with _activity_lock:
            epoch = _activity_epoch
            seq = _activity_seq
            workers = deepcopy(_activity_by_worker)
        checkpoint = {
            "version": 1,
            "authority_epoch": epoch,
            "seq": seq,
            "workers": workers,
            "checksum": _activity_digest(epoch, seq, workers),
        }
        started = time.perf_counter()
        write_json_durable(_activity_checkpoint_path(), checkpoint)
        _activity_compaction_boundary("checkpoint_committed")
        with _activity_lock:
            journal = _activity_journal_path()
            retained: list[str] = []
            if journal.exists():
                for line in journal.read_text(encoding="utf-8").splitlines():
                    event = json.loads(line)
                    if int(event.get("seq", -1)) > seq:
                        retained.append(line)
            tmp_payload = "".join(line + "\n" for line in retained)
            tmp = journal.with_suffix(".jsonl.compact")
            with tmp.open("w", encoding="utf-8") as handle:
                handle.write(tmp_payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, journal)
            _activity_compaction_boundary("journal_replaced")
            directory_fd = os.open(journal.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            _activity_compaction_boundary("directory_fsynced")
        perf.record("store.worker.activity.compact", (time.perf_counter() - started) * 1000)
    except Exception:
        logger.exception("worker activity compaction failed")
    finally:
        with _activity_lock:
            _activity_compacting = False


def _schedule_activity_compaction_locked() -> None:
    global _activity_compacting
    if _activity_compacting or _activity_seq == 0 or _activity_seq % _ACTIVITY_COMPACT_EVERY:
        return
    _activity_compacting = True
    threading.Thread(target=_compact_activity, name="worker-activity-compact", daemon=True).start()


def activity_authority() -> tuple[str, int]:
    _ensure_activity()
    with _activity_lock:
        return _activity_epoch, _activity_seq


def _merge_activity(registry: dict) -> dict:
    _ensure_activity(registry.get("workers", []))
    with _activity_lock:
        activity = deepcopy(_activity_by_worker)
    for worker in registry.get("workers", []):
        current = activity.get(str(worker.get("agent_session_id") or ""))
        if current:
            worker.update(current)
    return registry


def _sync_activity_membership(registry: dict) -> None:
    global _activity_seq
    _ensure_activity(registry.get("workers", []))
    desired = {
        str(worker.get("agent_session_id")): worker
        for worker in registry.get("workers", [])
        if worker.get("agent_session_id")
    }
    with _activity_lock:
        for worker_id, worker in desired.items():
            if worker_id in _activity_by_worker:
                continue
            _activity_by_worker[worker_id] = _activity_fields(worker)
        for worker_id in tuple(_activity_by_worker):
            if worker_id in desired:
                continue
            next_seq = _activity_seq + 1
            _append_activity_locked({
                "authority_epoch": _activity_epoch,
                "seq": next_seq,
                "worker_id": worker_id,
                "tombstone": True,
            })
            _activity_seq = next_seq
            _activity_by_worker.pop(worker_id, None)
        _schedule_activity_compaction_locked()


def _file_fingerprint() -> tuple[int, int]:
    try:
        stat = _path().stat()
    except FileNotFoundError:
        return (0, 0)
    return (stat.st_mtime_ns, stat.st_size)


def _empty() -> dict:
    return {"version": SCHEMA_VERSION, "workers": [], "forks": {}, "pool_queues": {}, "pool_failed_tasks": {}}


def normalize_tags(value) -> list[str]:
    if value in (None, ""):
        return []
    raw = value if isinstance(value, list) else [value]
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        tag = str(item or "").strip()
        if not tag or tag in seen:
            continue
        seen.add(tag)
        out.append(tag)
    return out


def _read(cwd: str = "") -> dict:
    """Load the global registry. Returns an empty registry on
    malformed/legacy/missing files (after a loud log) so a single
    corrupt file doesn't break callers like list_sessions that walk
    every cwd."""
    global _registry_cache_signature, _registry_cache, _registry_worker_ids
    path = _path()
    try:
        stat = path.stat()
    except FileNotFoundError:
        _registry_cache_signature = None
        _registry_cache = None
        return _empty()
    except OSError as e:
        _registry_cache_signature = None
        _registry_cache = None
        logger.error(
            "worker_store: failed to read %s (%s) — returning empty registry. "
            "Delete the file to start fresh.",
            path, e,
        )
        return _empty()
    signature = (stat.st_mtime_ns, stat.st_size)
    if _registry_cache_signature == signature and _registry_cache is not None:
        return _merge_activity(deepcopy(_registry_cache))
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.error(
            "worker_store: failed to read %s (%s) — returning empty registry. "
            "Delete the file to start fresh.",
            path, e,
        )
        return _empty()
    if not isinstance(raw, dict):
        logger.error(
            "worker_store: unexpected shape at %s (got %r) — returning "
            "empty registry. Delete the file to start fresh.",
            path, type(raw).__name__,
        )
        return _empty()

    if raw.get("version") != SCHEMA_VERSION:
        logger.error(
            "worker_store: unexpected version at %s (expected %s, got %r) "
            "— returning empty registry. Delete the file to start fresh.",
            path, SCHEMA_VERSION, raw.get("version"),
        )
        return _empty()
    raw.setdefault("workers", [])
    raw.setdefault("forks", {})
    raw.setdefault("pool_queues", {})
    _ensure_activity(raw.get("workers", []))
    _registry_cache_signature = signature
    _registry_cache = deepcopy(raw)
    _registry_worker_ids = {
        str(worker.get("agent_session_id"))
        for worker in raw.get("workers", [])
        if worker.get("agent_session_id")
    }
    return _merge_activity(deepcopy(raw))


def _write(
    _cwd: str,
    registry: dict,
    *,
    refresh_worker_summaries: bool = True,
) -> None:
    global _worker_count_cache_until, _registry_cache_signature, _registry_cache
    global _registry_worker_ids
    global _registry_revision
    path = _path()
    structural = deepcopy(registry)
    write_json(path, structural)
    try:
        stat = path.stat()
    except OSError:
        _registry_cache_signature = None
        _registry_cache = None
    else:
        _registry_cache_signature = (stat.st_mtime_ns, stat.st_size)
        _registry_cache = structural
        _registry_worker_ids = {
            str(worker.get("agent_session_id"))
            for worker in structural.get("workers", [])
            if worker.get("agent_session_id")
        }
    _sync_activity_membership(registry)
    _registry_revision += 1
    with _lock_for():
        _worker_count_cache.clear()
        _worker_count_cache_until = 0.0
    if refresh_worker_summaries:
        from session_store import _refresh_all_worker_summaries
        _refresh_all_worker_summaries()


def revision() -> int:
    with _lock_for():
        return _registry_revision


# ============================================================================
# Worker records
# ============================================================================

def list_workers(cwd: str) -> list[dict]:
    """Worker records, sorted by last_active desc.

    Returns the raw on-disk records — does NOT inject Better Agent session names
    (callers that need names should resolve via session_store).
    """
    with _lock_for():
        workers = list(_read().get("workers", []))
    if cwd:
        workers = [w for w in workers if w.get("cwd") == cwd]
    workers.sort(key=lambda w: w.get("last_active", ""), reverse=True)
    return workers


def worker_count(cwd: str = "") -> int:
    global _worker_count_cache_until
    now = time.monotonic()
    with _lock_for():
        if now < _worker_count_cache_until:
            for (cached_cwd, _fingerprint), cached in _worker_count_cache.items():
                if cached_cwd == cwd:
                    return cached
        fingerprint = _file_fingerprint()
        key = (cwd, fingerprint)
        cached = _worker_count_cache.get(key)
        if cached is not None:
            _worker_count_cache_until = now + _WORKER_COUNT_HOT_TTL_SECONDS
            return cached
        workers = _read().get("workers", [])
        if cwd:
            count = sum(1 for w in workers if w.get("cwd") == cwd)
        else:
            count = len(workers)
        _worker_count_cache.clear()
        _worker_count_cache[key] = count
        _worker_count_cache_until = now + _WORKER_COUNT_HOT_TTL_SECONDS
        return count


def list_pools(cwd: str = "") -> list[dict]:
    by_tag: dict[str, list[dict]] = {}
    for worker in list_workers(cwd):
        for tag in normalize_tags(worker.get("tags")):
            by_tag.setdefault(tag, []).append(worker)
    queues = _read().get("pool_queues") or {}
    pools = []
    for tag, workers in sorted(by_tag.items()):
        queue = queues.get(tag) if isinstance(queues.get(tag), list) else []
        pools.append({
            "tag": tag,
            "workers": workers,
            "queued_count": len(queue),
        })
    return pools


def get_worker(cwd: str, agent_session_id: str) -> Optional[dict]:
    with _lock_for():
        for w in _read().get("workers", []):
            if w.get("agent_session_id") == agent_session_id:
                return w
    return None


def list_worker_projection(cwd: str, limit: int = 20) -> list[dict]:
    """Compact projection for `<known_workers>` prompt injection.

    Resolves each worker's `description` from the Better Agent session's `name`
    A worker whose Better Agent session was deleted out from under us is skipped
    so the manager doesn't see references to dead sessions.
    """
    out: list[dict] = []
    workers = list_workers("")
    chunk_size = max(limit * 2, 20)
    for start in range(0, len(workers), chunk_size):
        chunk = workers[start:start + chunk_size]
        fields_by_sid = _sm.get_fields_many(
            [str(w.get("agent_session_id") or "") for w in chunk],
            ("cwd", "name"),
        )
        for w in chunk:
            agent_session_id = w.get("agent_session_id")
            if not agent_session_id:
                continue
            bc = fields_by_sid.get(agent_session_id)
            if not bc:
                continue
            out.append({
                "agent_session_id": agent_session_id,
                "registry_cwd": w.get("cwd") or bc.get("cwd") or cwd,
                "cwd": w.get("cwd") or bc.get("cwd") or "",
                "description": bc.get("name") or "(untitled)",
                "orchestration_mode": w.get("orchestration_mode"),
                "node_id": w.get("node_id") or "primary",
                "last_active": w.get("last_active", ""),
                "delegation_count": w.get("delegation_count", 0),
            })
            if len(out) >= limit:
                return out
    return out


@perf.timed_fn("store.worker.upsert")
def upsert_worker(
    cwd: str,
    agent_session_id: str,
    orchestration_mode: str,
    agent_sid: Optional[str],
    node_id: str = "primary",
    name: Optional[str] = None,
    role_key: Optional[str] = None,
    tags: Optional[list[str]] = None,
) -> dict:
    if orchestration_mode == "manager":
        orchestration_mode = "team"
    if orchestration_mode not in ("team", "native"):
        raise ValueError(f"invalid orchestration_mode: {orchestration_mode!r}")
    if not agent_session_id:
        raise ValueError("agent_session_id is required")

    with _lock_for():
        registry = _read()
        now = _now()
        for w in registry["workers"]:
            if w.get("agent_session_id") == agent_session_id:
                w["cwd"] = cwd
                w["orchestration_mode"] = orchestration_mode
                w["agent_sid"] = agent_sid
                w["node_id"] = node_id
                if name:
                    w["name"] = name
                if role_key:
                    w["role_key"] = role_key
                if tags is not None:
                    w["tags"] = normalize_tags(tags)
                _write(cwd, registry, refresh_worker_summaries=False)
                return w
        record = {
            "agent_session_id": agent_session_id,
            "name": name,
            "role_key": role_key,
            "cwd": cwd,
            "orchestration_mode": orchestration_mode,
            "agent_sid": agent_sid,
            "node_id": node_id,
            "created_at": now,
            "last_active": now,
            "delegation_count": 0,
            "token_usage": {},
            "tags": normalize_tags(tags),
        }
        registry["workers"].append(record)
        _write(cwd, registry)
        return record


def enqueue_pool_task(tag: str, item: dict) -> dict:
    clean = str(tag or "").strip()
    if not clean:
        raise ValueError("pool tag is required")
    if not isinstance(item, dict) or not item.get("id"):
        raise ValueError("pool queue item id is required")
    with _lock_for():
        registry = _read()
        queue = registry.setdefault("pool_queues", {}).setdefault(clean, [])
        insert_at = next(
            (
                index
                for index, queued in enumerate(queue)
                if int(queued.get("attempts") or 0) > 0
            ),
            len(queue),
        )
        queue.insert(insert_at, item)
        _write("", registry, refresh_worker_summaries=False)
        return {"tag": clean, "queued_count": len(queue), "item": item}


def peek_pool_task(tag: str) -> Optional[dict]:
    clean = str(tag or "").strip()
    if not clean:
        return None
    with _lock_for():
        queue = (_read().get("pool_queues") or {}).get(clean)
        if isinstance(queue, list) and queue:
            return queue[0]
    return None


def pop_pool_task(tag: str, item_id: str) -> bool:
    clean = str(tag or "").strip()
    iid = str(item_id or "").strip()
    if not clean or not iid:
        return False
    with _lock_for():
        registry = _read()
        queues = registry.get("pool_queues") or {}
        queue = queues.get(clean)
        if not isinstance(queue, list):
            return False
        before = len(queue)
        queues[clean] = [item for item in queue if item.get("id") != iid]
        if not queues[clean]:
            queues.pop(clean, None)
        registry["pool_queues"] = queues
        if len(queues.get(clean, [])) == before:
            return False
        _write("", registry, refresh_worker_summaries=False)
        return True


def record_pool_task_failure(
    tag: str,
    item_id: str,
    error: str,
    *,
    max_attempts: int = 3,
) -> dict:
    clean = str(tag or "").strip()
    iid = str(item_id or "").strip()
    if not clean or not iid:
        return {"action": "missing"}
    with _lock_for():
        registry = _read()
        queues = registry.get("pool_queues") or {}
        queue = queues.get(clean)
        if not isinstance(queue, list):
            return {"action": "missing"}
        for index, item in enumerate(queue):
            if item.get("id") != iid:
                continue
            failed = dict(item)
            failed["attempts"] = int(failed.get("attempts") or 0) + 1
            failed["last_error"] = str(error or "")
            failed["last_failed_at"] = _now()
            queue.pop(index)
            if failed["attempts"] >= max_attempts:
                failures = registry.setdefault("pool_failed_tasks", {}).setdefault(clean, [])
                failures.append(failed)
                registry["pool_failed_tasks"][clean] = failures[-50:]
                action = "failed"
            else:
                queue.append(failed)
                action = "requeued"
            if queue:
                queues[clean] = queue
            else:
                queues.pop(clean, None)
            registry["pool_queues"] = queues
            _write("", registry, refresh_worker_summaries=False)
            return {"action": action, "item": failed, "queued_count": len(queue)}
    return {"action": "missing"}


@perf.timed_fn("store.worker.touch")
def touch_worker(
    cwd: str,
    agent_session_id: str,
    token_usage: Optional[dict] = None,
) -> Optional[WorkerActivityCommit]:
    lock_started = time.perf_counter()
    with _lock_for():
        perf.record("store.worker.touch.lock_wait", (time.perf_counter() - lock_started) * 1000)
        lookup_started = time.perf_counter()
        if _registry_cache is None:
            _read()
        if agent_session_id not in _registry_worker_ids:
            return None
        perf.record("store.worker.touch.lookup", (time.perf_counter() - lookup_started) * 1000)
        with _activity_lock:
            _load_activity_locked()
            previous = _activity_by_worker.get(agent_session_id) or {
                "last_active": None,
                "delegation_count": 0,
                "token_usage": {},
            }
            activity = {
                "last_active": _now(),
                "delegation_count": int(previous.get("delegation_count", 0)) + 1,
                "token_usage": deepcopy(previous.get("token_usage") or {}),
            }
            if token_usage:
                for key, value in token_usage.items():
                    if isinstance(value, (int, float)):
                        activity["token_usage"][key] = int(activity["token_usage"].get(key, 0)) + int(value)
            global _activity_seq
            next_seq = _activity_seq + 1
            event = {
                "authority_epoch": _activity_epoch,
                "seq": next_seq,
                "worker_id": agent_session_id,
                "activity": activity,
            }
            _append_activity_locked(event)
            _activity_seq = next_seq
            _activity_by_worker[agent_session_id] = activity
            _schedule_activity_compaction_locked()
            worker = {"agent_session_id": agent_session_id, **activity}
            return WorkerActivityCommit(_activity_epoch, next_seq, worker)


def remove_worker(cwd: str, agent_session_id: str) -> bool:
    with _lock_for():
        registry = _read()
        before = len(registry["workers"])
        registry["workers"] = [
            w for w in registry["workers"] if w.get("agent_session_id") != agent_session_id
        ]
        if len(registry["workers"]) == before:
            return False
        forks = registry.get("forks") or {}
        for caller_sid, by_worker in list(forks.items()):
            by_worker.pop(agent_session_id, None)
            if not by_worker:
                forks.pop(caller_sid, None)
        registry["forks"] = forks
        _write(cwd, registry)
        return True


def remove_worker_everywhere(agent_session_id: str) -> int:
    """Drop `agent_session_id` from the global registry.

    Used when a Better Agent session is deleted by the user. Also clears any
    forks pointing at it (as caller OR as worker). Returns count of
    records touched.
    """
    with _lock_for():
        raw = _read()
        changed = False
        before = len(raw.get("workers", []))
        raw["workers"] = [
            w for w in raw.get("workers", [])
            if w.get("agent_session_id") != agent_session_id
        ]
        removed_worker = len(raw["workers"]) != before
        if removed_worker:
            changed = True
        forks = raw.get("forks") or {}
        if agent_session_id in forks:
            forks.pop(agent_session_id, None)
            changed = True
        for caller_sid, by_worker in list(forks.items()):
            if agent_session_id in by_worker:
                by_worker.pop(agent_session_id, None)
                changed = True
                if not by_worker:
                    forks.pop(caller_sid, None)
        raw["forks"] = forks
        if changed:
            _write("", raw, refresh_worker_summaries=removed_worker)
            return 1
    return 0


# ============================================================================
# Per-pair fork mapping
# ============================================================================

def get_fork_record(
    cwd: str,
    caller_agent_session_id: str,
    worker_agent_session_id: str,
) -> Optional[dict]:
    with _lock_for():
        rec = (
            _read()
            .get("forks", {})
            .get(caller_agent_session_id, {})
            .get(worker_agent_session_id)
        )
        return rec if isinstance(rec, dict) else None


def get_fork(
    cwd: str,
    caller_agent_session_id: str,
    worker_agent_session_id: str,
) -> Optional[str]:
    """Return just the fork_agent_session_id for this pair (convenience)."""
    rec = get_fork_record(cwd, caller_agent_session_id, worker_agent_session_id)
    return rec.get("fork_agent_session_id") if rec else None


def set_fork(
    cwd: str,
    caller_agent_session_id: str,
    worker_agent_session_id: str,
    fork_agent_session_id: str,
) -> None:
    if not (caller_agent_session_id and worker_agent_session_id and fork_agent_session_id):
        raise ValueError("set_fork: caller/worker/fork_bc ids all required")
    with _lock_for():
        registry = _read()
        forks = registry.setdefault("forks", {})
        by_worker = forks.setdefault(caller_agent_session_id, {})
        now = _now()
        by_worker[worker_agent_session_id] = {
            "fork_agent_session_id": fork_agent_session_id,
            "created_at": now,
            "last_used": now,
        }
        _write(cwd, registry, refresh_worker_summaries=False)


def touch_fork(
    cwd: str,
    caller_agent_session_id: str,
    worker_agent_session_id: str,
) -> None:
    with _lock_for():
        registry = _read()
        rec = (
            registry.get("forks", {})
            .get(caller_agent_session_id, {})
            .get(worker_agent_session_id)
        )
        if isinstance(rec, dict):
            rec["last_used"] = _now()
            _write(cwd, registry, refresh_worker_summaries=False)


def clear_fork(
    cwd: str,
    caller_agent_session_id: str,
    worker_agent_session_id: str,
) -> bool:
    with _lock_for():
        registry = _read()
        forks = registry.get("forks") or {}
        by_worker = forks.get(caller_agent_session_id)
        if not by_worker or worker_agent_session_id not in by_worker:
            return False
        by_worker.pop(worker_agent_session_id, None)
        if not by_worker:
            forks.pop(caller_agent_session_id, None)
        registry["forks"] = forks
        _write(cwd, registry, refresh_worker_summaries=False)
        return True


def clear_forks_for_worker_everywhere(worker_agent_session_id: str) -> list[str]:
    """Drop every fork pointing at `worker_agent_session_id`.

    Used when the worker Better Agent session is rewound (its agent_sid lineage
    moves under the fork) or deleted. Returns the list of cleared
    `fork_agent_session_id`s — the caller is responsible for deleting
    those Better Agent sessions via session_manager.delete (kept here as
    storage-only to avoid a circular import with session_manager).
    """
    cleared: list[str] = []
    with _lock_for():
        raw = _read()
        forks = raw.get("forks") or {}
        changed = False
        for caller_sid, by_worker in list(forks.items()):
            if worker_agent_session_id in by_worker:
                rec = by_worker.pop(worker_agent_session_id, None)
                if isinstance(rec, dict):
                    fbsid = rec.get("fork_agent_session_id")
                    if fbsid:
                        cleared.append(fbsid)
                changed = True
                if not by_worker:
                    forks.pop(caller_sid, None)
        if changed:
            raw["forks"] = forks
            _write("", raw, refresh_worker_summaries=False)
    return cleared


def clear_forks_for_caller_everywhere(caller_agent_session_id: str) -> list[str]:
    """Drop every fork made by `caller_agent_session_id`. Used when the
    caller Better Agent session is deleted. Returns the list of cleared
    `fork_agent_session_id`s — the caller deletes those Better Agent sessions."""
    cleared: list[str] = []
    with _lock_for():
        raw = _read()
        forks = raw.get("forks") or {}
        by_worker = forks.get(caller_agent_session_id)
        if by_worker:
            for rec in by_worker.values():
                if isinstance(rec, dict):
                    fbsid = rec.get("fork_agent_session_id")
                    if fbsid:
                        cleared.append(fbsid)
            forks.pop(caller_agent_session_id, None)
            raw["forks"] = forks
            _write("", raw, refresh_worker_summaries=False)
    return cleared
