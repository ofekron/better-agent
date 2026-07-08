"""Maps native transcript paths -> session turn-source for analytics.

A native transcript's `user_prompt` elements are all role=user, but BA injects
many of them — for delegations (fork / team_ask / mssg / delegate_task),
scheduled runs, and internal working-mode sessions. The provider transcript
alone cannot tell a direct-human prompt from a BA-injected one, so we cross-
reference BA's per-run records:

  - `runs/<id>/state.json`  -> jsonl_path (the transcript) + started_at
  - `runs/<id>/input.json`  -> source / fork / working_mode

Per-session (per-transcript-path) classification: a transcript is a direct-user
session if ANY of its runs is a direct run (no working_mode, not a fork, no
source tag). Only sessions whose runs are ALL non-direct (fork / team /
internal) are non-user. This avoids penalizing primary sessions that also
received a delegation — their direct turns still count as user.

Native transcripts with no BA run record are external CLI usage (direct human)
and count as user. The map is TTL-cached; analytics is a usage overview that
tolerates a few minutes of staleness.
"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, List, Optional

import runs_dir

# turn_source values stored on native_file_state.
DIRECT_USER = "direct_user"
EXTERNAL = "external"
INTERNAL = "internal"
FORK = "fork"
TEAM = "team"

_TTL_SECONDS = 300
_LOCK = threading.Lock()
_CACHE: Dict[str, object] = {"built_at": 0.0, "data": None}


def _run_kind(inp: dict) -> str:
    if inp.get("working_mode"):
        return INTERNAL
    if inp.get("fork"):
        return FORK
    source = str(inp.get("source") or "").strip()
    if source and source != "direct":
        return TEAM
    return DIRECT_USER


def _classify(kinds: List[str]) -> str:
    # Any direct run => the session has human input => direct_user. Only
    # sessions whose every run is BA-injected are non-user.
    if DIRECT_USER in kinds:
        return DIRECT_USER
    if INTERNAL in kinds:
        return INTERNAL
    if FORK in kinds:
        return FORK
    return TEAM


def _read_one(run_dir: Path) -> Optional[tuple[str, str]]:
    """Return (jsonl_path, run_kind) for a run, or None if unusable."""
    try:
        inp = json.loads((run_dir / "input.json").read_text(encoding="utf-8"))
        st = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    except Exception:
        return None
    path = st.get("jsonl_path")
    if not path:
        return None
    return (str(path), _run_kind(inp))


def _build() -> Dict[str, str]:
    root = runs_dir.runs_root()
    if not root.is_dir():
        return {}
    run_dirs = [d for d in root.iterdir() if d.is_dir()]
    by_path: Dict[str, List[str]] = {}
    with ThreadPoolExecutor(max_workers=16) as ex:
        for rec in ex.map(_read_one, run_dirs):
            if rec is None:
                continue
            path, kind = rec
            by_path.setdefault(path, []).append(kind)
    return {path: _classify(kinds) for path, kinds in by_path.items()}


def path_source_map() -> Dict[str, str]:
    """Cached transcript-path -> turn_source. Built on first call (or after
    _TTL_SECONDS); reused across calls within the process."""
    now = time.monotonic()
    with _LOCK:
        data = _CACHE["data"]
        if data is None or now - _CACHE["built_at"] > _TTL_SECONDS:
            data = _build()
            _CACHE["built_at"] = now
            _CACHE["data"] = data
        return data  # type: ignore[return-value]


def classify_path(path: Optional[str], source_map: Optional[Dict[str, str]] = None) -> str:
    """turn_source for a transcript path. Absent paths are external (user)."""
    if not path:
        return EXTERNAL
    return (source_map or path_source_map()).get(path, EXTERNAL)


def is_user_source(turn_source: Optional[str]) -> bool:
    """A native session's turns count as user only for direct-user / external
    sessions. Fork / team / internal sessions are BA-injected — non-user."""
    return turn_source in (None, "", DIRECT_USER, EXTERNAL)
