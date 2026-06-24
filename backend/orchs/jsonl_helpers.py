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
from pathlib import Path
from typing import Optional

_JSONL_PATH_CACHE: dict[tuple[str, str, str], tuple[float, Optional[Path]]] = {}
_JSONL_PATH_NEGATIVE_TTL_S = 5.0
_JSONL_INDEX_TTL_S = 5.0
_CLAUDE_PATH_INDEX: tuple[str, float, dict[str, Path]] | None = None
_RUN_STATE_INDEX: tuple[str, float, dict[str, Path]] | None = None


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


def _run_state_index() -> dict[str, Path]:
    global _RUN_STATE_INDEX
    try:
        from runs_dir import runs_root
    except Exception:
        return {}
    root = runs_root()
    key = str(root)
    now = time.monotonic()
    if _RUN_STATE_INDEX is not None:
        cached_key, ts, cached = _RUN_STATE_INDEX
        if cached_key == key and now - ts < _JSONL_INDEX_TTL_S:
            return cached
    if not root.exists():
        _RUN_STATE_INDEX = (key, now, {})
        return {}
    newest: dict[str, tuple[float, Path]] = {}
    try:
        for child in root.iterdir():
            if not child.is_dir():
                continue
            state_path = child / "state.json"
            if not state_path.exists():
                continue
            try:
                st = json.loads(state_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            sid = st.get("session_id")
            if not sid:
                continue
            jp_str = st.get("jsonl_path")
            candidate = Path(jp_str) if jp_str else child / "session_events.jsonl"
            if not candidate.exists():
                continue
            try:
                mt = state_path.stat().st_mtime
            except OSError:
                mt = 0.0
            current = newest.get(str(sid))
            if current is None or (mt, str(candidate)) > (current[0], str(current[1])):
                newest[str(sid)] = (mt, candidate)
    except OSError:
        newest = {}
    indexed = {sid: path for sid, (_, path) in newest.items()}
    _RUN_STATE_INDEX = (key, now, indexed)
    return indexed


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
    claude_path = _claude_path_index().get(agent_sid)
    if claude_path is not None and claude_path.exists():
        return _cache_existing_path(agent_sid, claude_path)
    # Gemini path — scan run dirs for one whose state.json discovered
    # this agent_sid; the runner writes the discovered sid into
    # state.json at init time. Resumed turns reuse the same sid across
    # multiple run dirs, so we collect ALL matches and return the
    # newest (by state.json mtime) — that's the most-recent turn whose
    # events.jsonl the supervisor / replay actually wants.
    run_path = _run_state_index().get(agent_sid)
    if run_path is not None and run_path.exists():
        return _cache_existing_path(agent_sid, run_path)
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
