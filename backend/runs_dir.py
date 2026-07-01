"""Per-run directory + atomic JSON + pid-liveness helpers.

The "runs" directory holds per-run backend_state.json files written by
each provider's runner-supervision layer. Helpers used to live on
`provider_claude.py` (and a duplicate set on `provider_gemini.py`)
which forced lazy cross-imports and a circular dependency between
the abstract `provider` and concrete `provider_claude`.

INVARIANT: do NOT cache `runs_root()` as a module-level constant —
`ba_home()` is computed per-call so tests/scripts can flip
`BETTER_CLAUDE_HOME` after import without writing to the developer's
real `~/.better-claude/runs`.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
import heapq
from pathlib import Path
from typing import Optional

from json_store import write_json
from paths import ba_home

logger = logging.getLogger(__name__)
_RUN_STATE_LEDGER_NAME = "run_state_index.jsonl"
_RUN_STATE_LEDGER_BACKFILL_MARKER_NAME = "run_state_index.backfilled.json"
_RUN_STATE_LEDGER_BACKFILL_VERSION = 1
_RECONCILED_MARKER_INDEX_NAME = "reconciled_marker_index.jsonl"
_RECONCILED_MARKER_BACKFILL_MARKER_NAME = "reconciled_marker_index.backfilled.json"
_RECONCILED_MARKER_BACKFILL_VERSION = 1
_RUN_STATE_LEDGER_SEEN: set[tuple[str, str, str]] = set()
_RUN_STATE_LEDGER_BACKFILL_LOCK = threading.Lock()
_RUN_STATE_RECENT_SCAN_LIMIT = 256
_RUN_STATE_RECENT_INDEX_TTL_S = 1.0
_RUN_STATE_RECENT_INDEX_MAX_AGE_S = 30.0
_RUN_STATE_LOOKUP_CACHE_LOCK = threading.Lock()
_RunStateLedgerSignature = tuple[int, int, int, int]
_RUN_STATE_LEDGER_CACHE: dict[str, tuple[_RunStateLedgerSignature, dict[str, list[tuple[float, Path]]]]] = {}
_RUN_STATE_RECENT_INDEX_CACHE: dict[str, tuple[float, tuple[tuple[int, int, str], ...], dict[str, list[Path]]]] = {}
_RUN_STATE_RECENT_INDEX_INFLIGHT: dict[str, threading.Event] = {}
_RECONCILED_MARKER_INDEX_SEEN: set[tuple[str, str, int, int, int, int]] = set()
_RECONCILED_MARKER_BACKFILL_LOCK = threading.Lock()


def runs_root() -> Path:
    return ba_home() / "runs"


def run_state_ledger_path(root: Optional[Path] = None) -> Path:
    return (root or runs_root()) / _RUN_STATE_LEDGER_NAME


def run_state_ledger_backfill_marker_path(root: Optional[Path] = None) -> Path:
    return (root or runs_root()) / _RUN_STATE_LEDGER_BACKFILL_MARKER_NAME


def reconciled_marker_index_path(root: Optional[Path] = None) -> Path:
    return (root or runs_root()) / _RECONCILED_MARKER_INDEX_NAME


def reconciled_marker_index_backfill_marker_path(root: Optional[Path] = None) -> Path:
    return (root or runs_root()) / _RECONCILED_MARKER_BACKFILL_MARKER_NAME


def _run_state_ledger_backfill_current(root: Path) -> bool:
    marker = run_state_ledger_backfill_marker_path(root)
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return data.get("version") == _RUN_STATE_LEDGER_BACKFILL_VERSION


def _reconciled_marker_backfill_current(root: Path) -> bool:
    marker = reconciled_marker_index_backfill_marker_path(root)
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return data.get("version") == _RECONCILED_MARKER_BACKFILL_VERSION


def _append_run_state_ledger(path: Path, data: dict) -> None:
    if path.name != "state.json":
        return
    session_id = data.get("session_id")
    jsonl_path = data.get("jsonl_path")
    if not session_id or not jsonl_path:
        return
    try:
        key = (str(path), str(session_id), str(jsonl_path))
        if key in _RUN_STATE_LEDGER_SEEN:
            return
        row = {
            "session_id": str(session_id),
            "jsonl_path": str(jsonl_path),
            "state_path": str(path),
            "written_at": time.time(),
        }
        ledger = run_state_ledger_path(path.parent.parent)
        ledger.parent.mkdir(parents=True, exist_ok=True)
        if _run_state_ledger_has_key(ledger, key):
            _RUN_STATE_LEDGER_SEEN.add(key)
            return
        with ledger.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, separators=(",", ":")) + "\n")
        _RUN_STATE_LEDGER_SEEN.add(key)
    except Exception:
        logger.exception("runs_dir: failed to append run-state ledger")


def _run_state_path_under_root(path: Path, root_resolved: Path) -> bool:
    if path.name != "state.json":
        return False
    try:
        path.resolve().relative_to(root_resolved)
        return True
    except (OSError, ValueError):
        return False


def ledger_state_files_for_sid(root: Path, agent_sid: str) -> list[Path]:
    try:
        ledger = run_state_ledger_path(root)
        st = ledger.stat()
    except OSError:
        return []
    signature = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
    root_key = str(root)
    try:
        root_resolved = root.resolve()
    except OSError:
        return []
    with _RUN_STATE_LOOKUP_CACHE_LOCK:
        cached = _RUN_STATE_LEDGER_CACHE.get(root_key)
        if cached is not None:
            cached_signature, index = cached
            if cached_signature == signature:
                return [
                    path for _, path in index.get(agent_sid, [])
                    if _run_state_path_under_root(path, root_resolved)
                ]
    latest_by_key: dict[tuple[str, str], tuple[float, Path]] = {}
    try:
        with ledger.open(encoding="utf-8") as f:
            for raw in f:
                try:
                    row = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                sid = str(row.get("session_id") or "")
                state_path = row.get("state_path")
                if not sid or not state_path:
                    continue
                path = Path(str(state_path))
                if not _run_state_path_under_root(path, root_resolved):
                    continue
                try:
                    written_at = float(row.get("written_at"))
                except (TypeError, ValueError):
                    written_at = 0.0
                key = (sid, str(path))
                current = latest_by_key.get(key)
                if current is None or written_at >= current[0]:
                    latest_by_key[key] = (written_at, path)
    except OSError:
        return []
    index: dict[str, list[tuple[float, Path]]] = {}
    for (sid, _), value in latest_by_key.items():
        index.setdefault(sid, []).append(value)
    with _RUN_STATE_LOOKUP_CACHE_LOCK:
        _RUN_STATE_LEDGER_CACHE[root_key] = (signature, index)
    return [path for _, path in index.get(agent_sid, [])]


def state_files_for_sid(root: Path, agent_sid: str) -> list[Path]:
    ledger_paths = ledger_state_files_for_sid(root, agent_sid)
    if ledger_paths:
        return ledger_paths
    did_backfill = ensure_run_state_ledger_backfilled(root)
    if did_backfill:
        with _RUN_STATE_LOOKUP_CACHE_LOCK:
            _RUN_STATE_LEDGER_CACHE.pop(str(root), None)
        ledger_paths = ledger_state_files_for_sid(root, agent_sid)
        if ledger_paths:
            return ledger_paths
    return recent_state_index_for_root(root).get(agent_sid, [])


def recent_state_index_for_root(root: Path) -> dict[str, list[Path]]:
    now = time.monotonic()
    root_key = str(root)
    try:
        root_resolved = root.resolve()
    except OSError:
        return {}
    with _RUN_STATE_LOOKUP_CACHE_LOCK:
        cached = _RUN_STATE_RECENT_INDEX_CACHE.get(root_key)
        if cached is not None:
            ts, _fingerprint, index = cached
            if now - ts < _RUN_STATE_RECENT_INDEX_TTL_S:
                return _filter_recent_state_index(index, root_resolved)
        event = _RUN_STATE_RECENT_INDEX_INFLIGHT.get(root_key)
        if event is None:
            event = threading.Event()
            _RUN_STATE_RECENT_INDEX_INFLIGHT[root_key] = event
            owner = True
        else:
            owner = False
    if not owner:
        event.wait(1.0)
        with _RUN_STATE_LOOKUP_CACHE_LOCK:
            cached = _RUN_STATE_RECENT_INDEX_CACHE.get(root_key)
            return _filter_recent_state_index(cached[2], root_resolved) if cached is not None else {}
    try:
        candidates = _recent_state_candidates(root, root_resolved)
        if not candidates:
            return {}
        with _RUN_STATE_LOOKUP_CACHE_LOCK:
            cached = _RUN_STATE_RECENT_INDEX_CACHE.get(root_key)
            if cached is not None:
                ts, fingerprint, index = cached
                if (
                    fingerprint == candidates
                    and now - ts < _RUN_STATE_RECENT_INDEX_MAX_AGE_S
                ):
                    _RUN_STATE_RECENT_INDEX_CACHE[root_key] = (now, fingerprint, index)
                    return _filter_recent_state_index(index, root_resolved)
        index = _build_recent_state_index(candidates, root_resolved)
        _backfill_run_state_ledger(root, index)
        with _RUN_STATE_LOOKUP_CACHE_LOCK:
            _RUN_STATE_RECENT_INDEX_CACHE[root_key] = (now, candidates, index)
        return index
    finally:
        with _RUN_STATE_LOOKUP_CACHE_LOCK:
            done = _RUN_STATE_RECENT_INDEX_INFLIGHT.pop(root_key, None)
        if done is not None:
            done.set()


def _recent_state_candidates(
    root: Path,
    root_resolved: Path | None = None,
) -> tuple[tuple[int, int, str], ...]:
    candidates: list[tuple[int, int, str]] = []
    if root_resolved is None:
        try:
            root_resolved = root.resolve()
        except OSError:
            return ()
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                if not entry.is_dir(follow_symlinks=False):
                    continue
                state_path = Path(entry.path) / "state.json"
                if not _run_state_path_under_root(state_path, root_resolved):
                    continue
                try:
                    st = state_path.stat()
                except OSError:
                    continue
                candidates.append((st.st_mtime_ns, st.st_size, str(state_path)))
    except OSError:
        return ()
    return tuple(heapq.nlargest(_RUN_STATE_RECENT_SCAN_LIMIT, candidates))


def _filter_recent_state_index(
    index: dict[str, list[Path]],
    root_resolved: Path,
) -> dict[str, list[Path]]:
    filtered: dict[str, list[Path]] = {}
    for sid, paths in index.items():
        safe_paths = [
            path for path in paths
            if _run_state_path_under_root(path, root_resolved)
        ]
        if safe_paths:
            filtered[sid] = safe_paths
    return filtered


def _build_recent_state_index(
    candidates: tuple[tuple[int, int, str], ...],
    root_resolved: Path | None = None,
) -> dict[str, list[Path]]:
    index: dict[str, list[Path]] = {}
    for _, _, state_path in candidates:
        path = Path(state_path)
        if root_resolved is not None and not _run_state_path_under_root(path, root_resolved):
            continue
        try:
            st = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        agent_sid = str(st.get("session_id") or "")
        if not agent_sid:
            continue
        index.setdefault(agent_sid, []).append(path)
    return index


def _backfill_run_state_ledger(root: Path, index: dict[str, list[Path]]) -> None:
    try:
        root_resolved = root.resolve()
    except Exception:
        return
    for paths in index.values():
        for path in paths:
            try:
                if not _run_state_path_under_root(path, root_resolved):
                    continue
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(data, dict):
                _append_run_state_ledger(path, data)


def ensure_run_state_ledger_backfilled(root: Optional[Path] = None) -> bool:
    root = root or runs_root()
    if _run_state_ledger_backfill_current(root):
        return False
    with _RUN_STATE_LEDGER_BACKFILL_LOCK:
        if _run_state_ledger_backfill_current(root):
            return False
        try:
            root_resolved = root.resolve()
            ledger = run_state_ledger_path(root)
            existing = _run_state_ledger_keys(ledger)
            rows: list[dict] = []
            now = time.time()
            with os.scandir(root) as entries:
                for entry in entries:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    state_path = Path(entry.path) / "state.json"
                    if state_path.name != "state.json":
                        continue
                    try:
                        state_path.resolve().relative_to(root_resolved)
                        data = json.loads(state_path.read_text(encoding="utf-8"))
                    except Exception:
                        continue
                    session_id = data.get("session_id") if isinstance(data, dict) else None
                    jsonl_path = data.get("jsonl_path") if isinstance(data, dict) else None
                    if not session_id or not jsonl_path:
                        continue
                    key = (str(state_path), str(session_id), str(jsonl_path))
                    if key in existing:
                        continue
                    existing.add(key)
                    rows.append({
                        "session_id": str(session_id),
                        "jsonl_path": str(jsonl_path),
                        "state_path": str(state_path),
                        "written_at": now,
                    })
            ledger.parent.mkdir(parents=True, exist_ok=True)
            if rows:
                with ledger.open("a", encoding="utf-8") as f:
                    for row in rows:
                        f.write(json.dumps(row, separators=(",", ":")) + "\n")
                        _RUN_STATE_LEDGER_SEEN.add((
                            row["state_path"],
                            row["session_id"],
                            row["jsonl_path"],
                        ))
            write_json(
                run_state_ledger_backfill_marker_path(root),
                {
                    "version": _RUN_STATE_LEDGER_BACKFILL_VERSION,
                    "backfilled_at": now,
                    "appended": len(rows),
                },
            )
            return True
        except Exception:
            logger.exception("runs_dir: failed to backfill run-state ledger")
            return False


def append_reconciled_marker_index(
    marker_path: Path,
    provider_kind: str | None,
    ingestion_version: int,
    *,
    root: Optional[Path] = None,
) -> None:
    row = _reconciled_marker_index_row(
        marker_path,
        provider_kind,
        ingestion_version,
        root=root,
    )
    if row is None:
        return
    key = _reconciled_marker_index_key(row)
    if key in _RECONCILED_MARKER_INDEX_SEEN:
        return
    try:
        index = reconciled_marker_index_path(root or runs_root())
        index.parent.mkdir(parents=True, exist_ok=True)
        if _reconciled_marker_index_has_key(index, key):
            _RECONCILED_MARKER_INDEX_SEEN.add(key)
            return
        with index.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, separators=(",", ":")) + "\n")
        _RECONCILED_MARKER_INDEX_SEEN.add(key)
    except Exception:
        logger.exception("runs_dir: failed to append reconciled-marker index")


def ensure_reconciled_marker_index_backfilled(root: Optional[Path] = None) -> bool:
    root = root or runs_root()
    if _reconciled_marker_backfill_current(root):
        return False
    with _RECONCILED_MARKER_BACKFILL_LOCK:
        if _reconciled_marker_backfill_current(root):
            return False
        try:
            index = reconciled_marker_index_path(root)
            existing = _reconciled_marker_index_keys(index)
            rows: list[dict] = []
            with os.scandir(root) as entries:
                for entry in entries:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    marker_path = Path(entry.path) / "reconciled.marker"
                    try:
                        if marker_path.is_symlink():
                            continue
                        data = json.loads(marker_path.read_text(encoding="utf-8"))
                    except Exception:
                        continue
                    provider_kind = data.get("provider_kind") if isinstance(data, dict) else None
                    ingestion_version = data.get("ingestion_version") if isinstance(data, dict) else None
                    if not isinstance(provider_kind, str) or not isinstance(ingestion_version, int):
                        continue
                    row = _reconciled_marker_index_row(
                        marker_path,
                        provider_kind,
                        ingestion_version,
                        root=root,
                    )
                    if row is None:
                        continue
                    key = _reconciled_marker_index_key(row)
                    if key in existing:
                        continue
                    existing.add(key)
                    rows.append(row)
            index.parent.mkdir(parents=True, exist_ok=True)
            if rows:
                with index.open("a", encoding="utf-8") as f:
                    for row in rows:
                        f.write(json.dumps(row, separators=(",", ":")) + "\n")
                        _RECONCILED_MARKER_INDEX_SEEN.add(
                            _reconciled_marker_index_key(row)
                        )
            write_json(
                reconciled_marker_index_backfill_marker_path(root),
                {
                    "version": _RECONCILED_MARKER_BACKFILL_VERSION,
                    "backfilled_at": time.time(),
                    "appended": len(rows),
                },
            )
            return True
        except Exception:
            logger.exception("runs_dir: failed to backfill reconciled-marker index")
            return False


def load_reconciled_marker_index(root: Optional[Path] = None) -> dict[str, dict]:
    root = root or runs_root()
    latest: dict[str, dict] = {}
    try:
        with reconciled_marker_index_path(root).open(encoding="utf-8") as f:
            for raw in f:
                try:
                    row = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                run_id = row.get("run_id")
                if isinstance(run_id, str) and run_id:
                    latest[run_id] = row
    except OSError:
        pass
    return latest


def reconciled_marker_index_row_matches(run_dir: Path, row: dict) -> bool:
    run_id = row.get("run_id")
    marker_path_raw = row.get("marker_path")
    if not isinstance(run_id, str) or run_id != run_dir.name:
        return False
    marker_path = run_dir / "reconciled.marker"
    if not isinstance(marker_path_raw, str) or marker_path_raw != str(marker_path):
        return False
    try:
        if run_dir.is_symlink() or marker_path.is_symlink():
            return False
        st = marker_path.lstat()
    except OSError:
        return False
    try:
        return (
            int(row.get("marker_size")) == int(st.st_size)
            and int(row.get("marker_mtime_ns")) == int(st.st_mtime_ns)
            and int(row.get("marker_inode") or 0) == int(getattr(st, "st_ino", 0) or 0)
        )
    except (TypeError, ValueError):
        return False


def _reconciled_marker_index_row(
    marker_path: Path,
    provider_kind: str | None,
    ingestion_version: int,
    *,
    root: Optional[Path] = None,
) -> dict | None:
    if marker_path.name != "reconciled.marker" or provider_kind is None:
        return None
    root = root or runs_root()
    try:
        root_resolved = root.resolve()
        marker_resolved = marker_path.resolve()
        marker_resolved.relative_to(root_resolved)
        run_dir = marker_path.parent
        run_dir.resolve().relative_to(root_resolved)
        if run_dir.parent.resolve() != root_resolved:
            return None
        if run_dir.is_symlink() or marker_path.is_symlink():
            return None
        st = marker_path.lstat()
    except (OSError, ValueError):
        return None
    return {
        "run_id": run_dir.name,
        "marker_path": str(marker_path),
        "provider_kind": str(provider_kind),
        "ingestion_version": int(ingestion_version),
        "marker_size": int(st.st_size),
        "marker_mtime_ns": int(st.st_mtime_ns),
        "marker_inode": int(getattr(st, "st_ino", 0) or 0),
        "written_at": time.time(),
    }


def _reconciled_marker_index_key(row: dict) -> tuple[str, str, int, int, int, int]:
    return (
        str(row.get("marker_path") or ""),
        str(row.get("provider_kind") or ""),
        int(row.get("ingestion_version") or 0),
        int(row.get("marker_size") or 0),
        int(row.get("marker_mtime_ns") or 0),
        int(row.get("marker_inode") or 0),
    )


def _reconciled_marker_index_keys(index: Path) -> set[tuple[str, str, int, int, int, int]]:
    keys: set[tuple[str, str, int, int, int, int]] = set()
    try:
        with index.open(encoding="utf-8") as f:
            for raw in f:
                try:
                    row = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                keys.add(_reconciled_marker_index_key(row))
    except OSError:
        pass
    return keys


def _reconciled_marker_index_has_key(
    index: Path,
    key: tuple[str, str, int, int, int, int],
) -> bool:
    return key in _reconciled_marker_index_keys(index)


def _run_state_ledger_keys(ledger: Path) -> set[tuple[str, str, str]]:
    keys: set[tuple[str, str, str]] = set()
    try:
        with ledger.open(encoding="utf-8") as f:
            for raw in f:
                try:
                    row = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                state_path = row.get("state_path")
                session_id = row.get("session_id")
                jsonl_path = row.get("jsonl_path")
                if state_path and session_id and jsonl_path:
                    keys.add((str(state_path), str(session_id), str(jsonl_path)))
    except OSError:
        pass
    return keys


def _run_state_ledger_has_key(ledger: Path, key: tuple[str, str, str]) -> bool:
    try:
        with ledger.open(encoding="utf-8") as f:
            for raw in f:
                try:
                    row = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                existing = (
                    str(row.get("state_path") or ""),
                    str(row.get("session_id") or ""),
                    str(row.get("jsonl_path") or ""),
                )
                if existing == key:
                    return True
    except OSError:
        return False
    return False


def iter_run_dirs(run_id_filter: Optional[set[str]] = None):
    root = runs_root()
    if not root.exists():
        return
    if run_id_filter is not None:
        for run_id in run_id_filter:
            child = root / run_id
            if child.is_dir():
                yield child
        return
    for child in root.iterdir():
        if child.is_dir():
            yield child


def prune_old_completed_runs(max_age_days: int = 7) -> int:
    root = runs_root()
    if not root.exists():
        return 0
    cutoff = time.time() - (max_age_days * 24 * 60 * 60)
    removed = 0
    with os.scandir(root) as entries:
        for entry in entries:
            try:
                if not entry.is_dir():
                    continue
                complete_path = Path(entry.path) / "complete.json"
                if complete_path.stat().st_mtime >= cutoff:
                    continue
            except OSError:
                continue
            if reap_run_dir(Path(entry.path)):
                removed += 1
    return removed


# In-process CLI timer tools stripped on EVERY claude spawn (replaced by
# the backend-owned scheduler). Single source of truth for both sides of
# the contract: provider_claude appends them to input.json's
# disallowed_tools; runner.py refuses to spawn if any are missing.
TIMER_TOOLS = (
    "CronCreate",
    "CronDelete",
    "CronList",
    "ScheduleWakeup",
)


def turn_dir(run_dir: Path, turn_id: str) -> Path:
    """Per-turn artifact directory under a runner's run_dir.

    Each run serves exactly one turn; `turns/<turn_id>/{start.json,
    complete.json}` is written alongside the run-level files so
    `read_best_complete` can salvage a turn whose runner died before the
    run-level complete.json landed.
    """
    return run_dir / "turns" / turn_id


def runner_alive_path(run_dir: Path) -> Path:
    """Heartbeat sentinel file refreshed by the runner every ~5s for its
    whole lifetime — including a babysitter linger, so the backend can
    tell a live babysitter from a dead orphan.
    """
    return run_dir / "runner_alive"


def read_best_complete(run_dir: Path) -> Optional[dict]:
    """Best available completion payload for a run, or None.

    The runner writes the per-turn ``turns/<turn_id>/complete.json``
    (with the turn's real success/error/output) BEFORE the run-level
    ``complete.json`` (runner.py:1659 then :2070). A runner that dies in
    that gap — e.g. SIGKILLed by the stuck-runner watchdog right after a
    turn succeeded — leaves a valid per-turn payload but no run-level
    file. Callers that would otherwise synthesize a "no complete.json"
    error must fall back here so the real output isn't discarded.

    Preference order:
      1. run-level ``complete.json`` (authoritative).
      2. most-recent ``turns/*/complete.json`` by mtime.
    Returns the parsed dict, or None if neither exists/parses.
    """
    run_level = run_dir / "complete.json"
    if run_level.exists():
        try:
            return json.loads(run_level.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.exception("read_best_complete: bad run-level complete.json %s", run_dir)
    turns = run_dir / "turns"
    if not turns.is_dir():
        return None
    candidates = []
    for child in turns.iterdir():
        cj = child / "complete.json"
        try:
            candidates.append((cj.stat().st_mtime, cj))
        except OSError:
            continue
    for _, cj in sorted(candidates, reverse=True):
        try:
            return json.loads(cj.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    return None


def salvage_complete_payload(run_id: str) -> Optional[dict]:
    """On-disk authority for the dead-runner synthesis path.

    `turn_manager`'s wait loop can see the runner process as dead before
    the provider's in-memory `complete` event wins the race onto the
    queue (event-loop lag, or the runner exiting in the same window it
    wrote complete.json). Rather than fabricate a failure, trust the
    complete.json the runner already wrote — it records the turn's real
    outcome. Returns {success, error, session_id, token_usage}, or None
    when no complete file exists (a genuine no-output death)."""
    data = read_best_complete(runs_root() / run_id)
    if data is None:
        return None
    return {
        "success": bool(data.get("success", False)),
        "error": data.get("error"),
        "session_id": data.get("session_id"),
        "token_usage": data.get("token_usage"),
    }


def delete_runs_for_sessions(sids: set[str]) -> int:
    """Delete every run dir whose messages persist to one of `sids`.

    A run is attributed to `persist_to or app_session_id` — the SAME key
    run-recovery uses to look the session up (`run_recovery._integrate_one`
    keys `session_manager.get` on `persist_to or app_session_id`). Matching
    that exact key means we reap precisely the dirs recovery would later
    orphan-skip, and never a sibling worker run whose persist target is a
    surviving session. Returns the count removed.

    Called when a session tree is deleted so its detached run dirs don't
    outlive it (they'd otherwise linger until the 7-day age-prune and be
    re-scanned + skipped by run-recovery on every backend startup)."""
    if not sids:
        return 0
    root = runs_root()
    if not root.exists():
        return 0
    removed = 0
    for child in root.iterdir():
        if not child.is_dir():
            continue
        try:
            bs = json.loads((child / "backend_state.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        persist_sid = bs.get("persist_to") or bs.get("app_session_id")
        if persist_sid in sids:
            if reap_run_dir(child):
                removed += 1
    if removed:
        logger.info("delete_runs_for_sessions: removed %d run dir(s)", removed)
    return removed


def _harvest_spawn_sid(child: Path) -> None:
    """Record the run dir's provider session_id into the durable spawn
    ledger so BA-spawn provenance survives the dir's removal. Reads the sid
    from whichever run-state file carries it."""
    import spawn_ledger
    spawn_ledger.record_run_dir(child)


def reap_run_dir(child: Path) -> bool:
    """Single owner of run-dir removal: harvest the spawn sid into the
    durable ledger, THEN remove the dir. Every reap site (session-delete and
    the per-provider age-prune) routes through here so no BA-spawned sid is
    lost when its run dir is reaped. Returns True if the dir was removed."""
    _harvest_spawn_sid(child)
    try:
        shutil.rmtree(child)
        return True
    except OSError as e:
        logger.warning("reap_run_dir: failed to rm %s: %s", child, e)
        return False


def atomic_write_json(path: Path, data: dict) -> None:
    """Crash-safe JSON write for run-dir state."""
    write_json(path, data)
    _append_run_state_ledger(path, data)


def pid_alive(pid: Optional[int]) -> bool:
    if not pid or pid <= 0:
        return False
    # Delegate to the platform process-control layer. On POSIX this is the
    # original os.kill(pid, 0) probe; on Windows os.kill(pid, 0) would
    # *terminate* the process, so a Win32 handle probe is used instead.
    from proc_control import process_control

    return process_control().pid_alive(pid)
