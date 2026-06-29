"""Provider-aware claude jsonl path helpers.

Strategies need to locate claude session jsonl files on disk by `sid`
and count lines. The path resolution honors the `CLAUDE_CONFIG_DIR`
env var so users with `~/.claude-zai` (or similar) get the right
files. Lives here (under `orchs/`) instead of `orchestrator.py` so
strategy modules don't import a private symbol via a circular-dodge.
"""

from __future__ import annotations

import os
import time
import json
import logging
import shutil
import subprocess
import heapq
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_JSONL_PATH_CACHE: dict[tuple[str, str, str], tuple[float, Optional[Path]]] = {}
_JSONL_PATH_NEGATIVE_TTL_S = 5.0
_JSONL_INDEX_TTL_S = 5.0
_RUN_STATE_RECENT_SCAN_LIMIT = 256
_RUN_STATE_RECENT_INDEX_TTL_S = 1.0
_RUN_STATE_LOOKUP_TIMEOUT_S = 1.5
_CLAUDE_PATH_INDEX: tuple[str, float, dict[str, Path]] | None = None
_RUN_STATE_PATH_CACHE: dict[tuple[str, str], tuple[float, Optional[Path]]] = {}
_RUN_STATE_RECENT_INDEX: tuple[str, float, tuple[tuple[int, int, str], ...], dict[str, list[Path]]] | None = None


def _claude_projects_dir() -> Path:
    """Resolve <CLAUDE_CONFIG_DIR>/projects, defaulting to ~/.claude.

    Computed per-call rather than at import time so test rigs can flip
    the env mid-process.
    """
    raw = os.environ.get("CLAUDE_CONFIG_DIR", "")
    base = Path(os.path.expandvars(raw)).expanduser() if raw else Path.home() / ".claude"
    return base / "projects"


def _cache_key(agent_sid: str) -> tuple[str, str, str]:
    try:
        from runs_dir import runs_root

        runs = str(runs_root())
    except Exception:
        runs = ""
    return (str(_claude_projects_dir()), runs, agent_sid)


def _cached_path(agent_sid: str) -> tuple[bool, Optional[Path]]:
    key = _cache_key(agent_sid)
    cached = _JSONL_PATH_CACHE.get(key)
    if cached is None:
        return False, None
    ts, path = cached
    if path is None:
        if time.monotonic() - ts < _JSONL_PATH_NEGATIVE_TTL_S:
            return True, None
        _JSONL_PATH_CACHE.pop(key, None)
        return False, None
    if path.exists():
        return True, path
    _JSONL_PATH_CACHE.pop(key, None)
    return False, None


def _cache_existing_path(agent_sid: str, path: Path) -> Path:
    _JSONL_PATH_CACHE[_cache_key(agent_sid)] = (time.monotonic(), path)
    return path


def _cache_missing_path(agent_sid: str) -> None:
    _JSONL_PATH_CACHE[_cache_key(agent_sid)] = (time.monotonic(), None)


def _encoded_cwd_path(cwd: str, agent_sid: str) -> Optional[Path]:
    if not cwd:
        return None
    try:
        from paths import encode_cwd
    except Exception:
        return None
    return _claude_projects_dir() / encode_cwd(cwd) / f"{agent_sid}.jsonl"


def _session_encoded_cwd_path(session: dict, cwd: str, agent_sid: str) -> Optional[Path]:
    if not cwd:
        return None
    try:
        from paths import claude_projects_root_for_session, encode_cwd
    except Exception:
        return None
    return claude_projects_root_for_session(session) / encode_cwd(cwd) / f"{agent_sid}.jsonl"


def _claude_path_index() -> dict[str, Path]:
    global _CLAUDE_PATH_INDEX
    projects = _claude_projects_dir()
    key = str(projects)
    now = time.monotonic()
    if _CLAUDE_PATH_INDEX is not None:
        cached_key, ts, cached = _CLAUDE_PATH_INDEX
        if cached_key == key and now - ts < _JSONL_INDEX_TTL_S:
            return cached
    indexed: dict[str, Path] = {}
    try:
        for path in projects.glob("*/*.jsonl"):
            indexed.setdefault(path.stem, path)
    except OSError:
        indexed = {}
    _CLAUDE_PATH_INDEX = (key, now, indexed)
    return indexed


def _run_state_cache_get(root_key: str, agent_sid: str) -> tuple[bool, Optional[Path]]:
    now = time.monotonic()
    cached = _RUN_STATE_PATH_CACHE.get((root_key, agent_sid))
    if cached is None:
        return False, None
    ts, path = cached
    if path is None:
        if now - ts < _JSONL_PATH_NEGATIVE_TTL_S:
            return True, None
        _RUN_STATE_PATH_CACHE.pop((root_key, agent_sid), None)
        return False, None
    if path.exists():
        return True, path
    _RUN_STATE_PATH_CACHE.pop((root_key, agent_sid), None)
    return False, None


def _run_state_cache_put(root_key: str, agent_sid: str, path: Optional[Path]) -> Optional[Path]:
    _RUN_STATE_PATH_CACHE[(root_key, agent_sid)] = (time.monotonic(), path)
    return path


def _recent_state_candidates(root: Path) -> tuple[tuple[int, int, str], ...]:
    candidates: list[tuple[int, int, str]] = []
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                if not entry.is_dir(follow_symlinks=False):
                    continue
                state_path = Path(entry.path) / "state.json"
                try:
                    st = state_path.stat()
                except OSError:
                    continue
                candidates.append((st.st_mtime_ns, st.st_size, str(state_path)))
    except OSError:
        return ()
    return tuple(heapq.nlargest(_RUN_STATE_RECENT_SCAN_LIMIT, candidates))


def _recent_state_index(root: Path) -> dict[str, list[Path]]:
    global _RUN_STATE_RECENT_INDEX
    key = str(root)
    now = time.monotonic()
    if _RUN_STATE_RECENT_INDEX is not None:
        cached_key, ts, _cached_candidates, index = _RUN_STATE_RECENT_INDEX
        if cached_key == key and now - ts < _RUN_STATE_RECENT_INDEX_TTL_S:
            return index
    candidates = _recent_state_candidates(root)
    if not candidates:
        return {}
    if _RUN_STATE_RECENT_INDEX is not None:
        cached_key, ts, cached_candidates, index = _RUN_STATE_RECENT_INDEX
        if cached_key == key and cached_candidates == candidates and now - ts < _JSONL_INDEX_TTL_S:
            return index
    index: dict[str, list[Path]] = {}
    for _, _, state_path in candidates:
        path = Path(state_path)
        try:
            st = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        sid = str(st.get("session_id") or "")
        if sid:
            index.setdefault(sid, []).append(path)
    _RUN_STATE_RECENT_INDEX = (key, now, candidates, index)
    return index


def _state_files_for_sid(root: Path, agent_sid: str) -> list[Path]:
    recent = _recent_state_index(root).get(agent_sid, [])
    if recent:
        return recent
    rg = shutil.which("rg")
    if rg is None:
        matches: list[Path] = []
        for state_path in root.glob("*/state.json"):
            try:
                if agent_sid in state_path.read_text(encoding="utf-8", errors="ignore"):
                    matches.append(state_path)
            except OSError:
                continue
        return matches
    try:
        proc = subprocess.run(
            [
                rg,
                "--files-with-matches",
                "--fixed-strings",
                "--glob",
                "state.json",
                agent_sid,
                str(root),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=_RUN_STATE_LOOKUP_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return []
    if proc.returncode not in (0, 1):
        return []
    return [Path(line) for line in proc.stdout.splitlines() if line]


def _run_state_path_for_sid(agent_sid: str) -> Optional[Path]:
    try:
        from runs_dir import runs_root
    except Exception:
        return None
    root = runs_root()
    key = str(root)
    cached_hit, cached = _run_state_cache_get(key, agent_sid)
    if cached_hit:
        return cached
    if not root.is_dir():
        return _run_state_cache_put(key, agent_sid, None)
    newest: tuple[float, Path] | None = None
    for state_path in _state_files_for_sid(root, agent_sid):
        try:
            st = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if str(st.get("session_id") or "") != agent_sid:
            continue
        jp_str = st.get("jsonl_path")
        candidate = Path(jp_str) if jp_str else state_path.parent / "session_events.jsonl"
        if not candidate.exists():
            continue
        try:
            mt = state_path.stat().st_mtime
        except OSError:
            mt = 0.0
        if newest is None or (mt, str(candidate)) > (newest[0], str(newest[1])):
            newest = (mt, candidate)
    return _run_state_cache_put(key, agent_sid, newest[1] if newest is not None else None)


def compute_jsonl_path(cwd: str, agent_sid: str) -> Optional[Path]:
    """Locate the session jsonl for ANY provider on this process's
    local disk by agent_sid.

    Pure local-disk resolution. INVARIANT: this helper has ONE semantic
    (local file path). Callers that need to read a remote worker's
    jsonl from primary MUST go through `compute_jsonl_read_path`
    (which routes via the session's `node_id`).

    Resolution order:
      1. Claude's projects dir: `<claude config>/projects/*/<sid>.jsonl`
         — claude CLI's encoded-cwd dirname rules don't always match
         worker_store.encode_cwd, and the sid is unique, so the glob
         is the source of truth.
      2. Gemini run-dir scan: `<ba_home>/runs/*/state.json` carrying
         that sid → that run's `session_events.jsonl`. Per-run files,
         so we have to scan; cheap because there are few in-flight
         runs and `state.json` is small.

    Returns the FIRST hit found across providers. Cwd arg is
    informational only.
    """
    _ = cwd
    cached_hit, cached = _cached_path(agent_sid)
    if cached_hit:
        return cached
    encoded_path = _encoded_cwd_path(cwd, agent_sid)
    if encoded_path is not None and encoded_path.exists():
        return _cache_existing_path(agent_sid, encoded_path)
    claude_index = _claude_path_index()
    claude_path = claude_index.get(agent_sid)
    if claude_path is not None and claude_path.exists():
        return _cache_existing_path(agent_sid, claude_path)
    # Gemini path — scan run dirs for one whose state.json discovered
    # this agent_sid; the runner writes the discovered sid into
    # state.json at init time. Resumed turns reuse the same sid across
    # multiple run dirs, so we collect ALL matches and return the
    # newest (by state.json mtime) — that's the most-recent turn whose
    # events.jsonl the supervisor / replay actually wants.
    run_path = _run_state_path_for_sid(agent_sid)
    if run_path is not None and run_path.exists():
        return _cache_existing_path(agent_sid, run_path)
    # Silent ingestion failure made visible: no provider's jsonl could be
    # located for this sid, so the tailer/strategy will read nothing and
    # events for this turn never ingest. Surface it so encoded-cwd mismatches
    # (common on Windows) and missing run-state are findable in the log. The
    # negative cache (5s TTL) keeps this to ~once per sid per window, not spam.
    log.warning(
        "ingestion: no jsonl located for agent_sid=%s cwd=%r — tried "
        "encoded_cwd=%s (exists=%s); claude index=%d entries, run-state path cache=%d "
        "entries under projects=%s. Events for this sid will NOT ingest.",
        agent_sid,
        cwd,
        encoded_path,
        bool(encoded_path is not None and encoded_path.exists()),
        len(claude_index),
        len(_RUN_STATE_PATH_CACHE),
        _claude_projects_dir(),
    )
    _cache_missing_path(agent_sid)
    return None


def compute_jsonl_read_path(
    cwd: str,
    agent_sid: str,
    session: Optional[dict] = None,
) -> Optional[Path]:
    """Resolve the jsonl path a CALLER ON PRIMARY should `Read()`.

    For local-pinned sessions: behaves exactly like `compute_jsonl_path`
    (real claude jsonl on primary's disk).

    For remote-pinned sessions: returns the shadow-jsonl path that
    `shadow_jsonl.append()` writes to as the node streams raw lines
    over WS. Manager's standard `Read` tool sees a regular local file.

    `session` is the Better Agent session record carrying the `node_id` field.
    If omitted, falls back to local resolution — keeps single-machine
    callers unchanged.
    """
    if session is None:
        return compute_jsonl_path(cwd, agent_sid)
    node_id = session.get("node_id") or "primary"
    try:
        from topology import local_node_id
        here = local_node_id()
    except Exception:
        here = "primary"
    if node_id == here:
        encoded_path = _session_encoded_cwd_path(session, cwd, agent_sid)
        if encoded_path is not None and encoded_path.exists():
            return _cache_existing_path(agent_sid, encoded_path)
        return compute_jsonl_path(cwd, agent_sid)
    # Remote: shadow path. Lazy import — shadow_jsonl pulls in
    # asyncio etc. that the headless helpers shouldn't need.
    import shadow_jsonl
    from session_manager import manager as _sm
    root_id = _sm._root_id_for(session["id"]) or session["id"]
    path = shadow_jsonl.shadow_path(root_id, agent_sid)
    return path if path.exists() else None


def count_jsonl_lines(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        with path.open("rb") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def jsonl_byte_size(path: Optional[Path]) -> int:
    """Byte length of a jsonl file (0 if absent). Used for byte-offset deltas:
    the caller samples the fork's new output via `tail -c +<size+1>`, which
    seeks in O(1) instead of Read's line-count scan."""
    if path is None or not path.exists():
        return 0
    try:
        return path.stat().st_size
    except OSError:
        return 0
