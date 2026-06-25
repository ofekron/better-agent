"""Physical render-tree migration for move-to-project.

`move_session_to_project` (in `main.py`) creates a NEW session in the target
project's cwd. By default that new session is blank until the user sends a
prompt; the history lives only in the archived source. `migrate_session_content`
copies the source's render tree into the destination so the moved session shows
its conversation immediately.

What migrates (the BA-owned render tree):
  - events.jsonl         (root sid rewritten source -> dest)
  - event_summaries.json (root_id / sid rewritten source -> dest)
  - event_meta.json      (root sid keys rewritten source -> dest)
  - message_frontend_cache/  (content-addressed, copied verbatim)

What does NOT migrate:
  - native_paths      (the new session spawns a fresh provider sid)
  - workers / forks   (bound to the source project's files)
  - project-scoped tags

The rewrite is a full-UUID string substitution of the SOURCE ROOT sid only.
Fork/worker sids are distinct UUIDs and are intentionally left untouched, so
historical worker-panel events still render under the moved root.
"""

from __future__ import annotations

import shutil
from pathlib import Path

_RENDER_FILES = ("events.jsonl", "event_summaries.json", "event_meta.json")
_FRONTEND_CACHE_DIR = "message_frontend_cache"


def _session_dir(sid: str) -> Path:
    import session_store
    return Path(session_store.session_file_path(sid)).parent / sid


def _rewrite_root_sid(text: str, src_sid: str, dst_sid: str) -> str:
    # Full-UUID substitution. A UUID never appears as a substring of a
    # different UUID, so this swaps the source root identity everywhere —
    # top-level `sid`, nested `data.sid`, path strings, summary root_id —
    # without touching fork/worker sids (distinct UUIDs).
    return text.replace(src_sid, dst_sid)


def migrate_session_content(src_sid: str, dst_sid: str) -> None:
    """Copy src's render tree into dst, rewriting the src root sid to dst.

    Overwrites dst render files. Files missing on src are skipped (a brand-new
    source has none yet). Idempotent: re-running reproduces the same dst state.
    """
    if not src_sid or not dst_sid or src_sid == dst_sid:
        raise ValueError("migrate_session_content requires distinct non-empty sids")

    src_dir = _session_dir(src_sid)
    dst_dir = _session_dir(dst_sid)
    dst_dir.mkdir(parents=True, exist_ok=True)

    for name in _RENDER_FILES:
        src_path = src_dir / name
        if not src_path.is_file():
            continue
        text = src_path.read_text(encoding="utf-8")
        dst_dir.joinpath(name).write_text(
            _rewrite_root_sid(text, src_sid, dst_sid), encoding="utf-8"
        )

    cache_src = src_dir / _FRONTEND_CACHE_DIR
    if cache_src.is_dir():
        cache_dst = dst_dir / _FRONTEND_CACHE_DIR
        if cache_dst.exists():
            shutil.rmtree(cache_dst)
        shutil.copytree(cache_src, cache_dst)
