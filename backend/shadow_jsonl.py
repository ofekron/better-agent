"""Primary-side shadow of remote workers' claude jsonls.

When a worker runs on a remote node, the manager (on primary) needs
to read the worker's claude jsonl via the standard `Read` tool — but
the file lives on the node's disk. The node tails the file locally
and ships each raw line to primary; primary appends to a shadow file
at `ba_home() / "shadow_claude_jsonls" / <root_id> / <fork_agent_sid>.jsonl`.

Two correctness nets stacked:

  - `file_version`: claude rewrites its jsonl on compaction. Node
    detects via tail-F file rotation (offset reset backward) and bumps
    `file_version`. Every `jsonl_line` carries `(file_version,
    line_offset_in_version)`. Primary truncates the shadow when it
    sees a higher `file_version`.
  - per-file lock: `_locks` is a `WeakValueDictionary` keyed by
    `(root_id, fork_agent_sid)` so concurrent inbound jsonl_line
    messages for the same shadow file serialize (e.g. reconnect-replay
    overlapping with live tail). Different files run unblocked. GC
    drops the lock entry when no handler holds it.

INVARIANT: every append goes through `append()` — there is no
"fast path" writer for restore vs live. Single-code-path applies.
"""

from __future__ import annotations

import asyncio
import logging
import os
import weakref
from pathlib import Path
from typing import Optional

from paths import ba_home

logger = logging.getLogger(__name__)


_locks: weakref.WeakValueDictionary = weakref.WeakValueDictionary()
# Per-(root_id, fork_agent_sid) current file_version on disk. Tracked
# in-memory; on primary restart we rediscover lazily — `snapshot_cursors_for`
# returns `{}` for a node we haven't heard from since boot, and the
# node interprets that as "send everything from current file_version
# offset 0". Correctness is preserved by `_append_sync`'s truncate-on-
# version-bump + truncate-on-size-mismatch logic; the only cost is
# bandwidth (re-shipping the current file_version's contents until
# the node has acked a new line). Unlike `node_store.last_acked_offset`,
# we deliberately DO NOT persist these cursors — the savings would be
# marginal and the existing self-healing semantics absorb the loss.
_current_version: dict[tuple[str, str], int] = {}
# Per-node, the set of (root_id, fork_agent_sid) we've heard about so
# resume_stream can ship cursors back. Populated lazily by `append`.
_per_node: dict[str, set[tuple[str, str]]] = {}


def _shadow_dir() -> Path:
    return ba_home() / "shadow_claude_jsonls"


def shadow_path(root_id: str, fork_agent_sid: str) -> Path:
    """Returns the canonical shadow file path. Used by both this module
    and by `compute_jsonl_read_path` in paths.py."""
    return _shadow_dir() / root_id / f"{fork_agent_sid}.jsonl"


def _get_lock(key: tuple[str, str]) -> asyncio.Lock:
    lock = _locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _locks[key] = lock
    return lock


async def append(
    *,
    node_id: str,
    root_id: str,
    fork_agent_sid: str,
    file_version: int,
    line_offset_in_version: int,
    line: str,
) -> None:
    """Append (or version-bump-and-rewrite) one shadow-jsonl line.

    Receive logic under per-file lock:

      - incoming.file_version > current → truncate file to 0, set
        current = incoming, append (asserts the line is at offset 0
        for the new version).
      - incoming.file_version < current → drop (stale; node sent
        before observing rotation).
      - incoming.file_version == current:
          * file size == line_offset_in_version → append.
          * file size  > line_offset_in_version → truncate to
            line_offset_in_version then append (partial-line recovery).
          * file size  < line_offset_in_version → drop (we missed a
            prefix; UUID dedup will catch the events anyway).
    """
    key = (root_id, fork_agent_sid)
    lock = _get_lock(key)
    path = shadow_path(root_id, fork_agent_sid)

    async with lock:
        await asyncio.to_thread(
            _append_sync,
            path=path,
            key=key,
            file_version=file_version,
            line_offset_in_version=line_offset_in_version,
            line=line,
        )

    _per_node.setdefault(node_id, set()).add(key)


async def rebuild(root_id: str, fork_agent_sid: str, content: str) -> Path:
    """Overwrite the shadow file with authoritative content pulled from
    the node (recovery path — the node's push watcher is dead, so the
    shadow may be incomplete). The shadow is a rebuildable projection;
    on conflict the node-side source wins. Resets the in-memory version
    cursor so a later push restart re-asserts its own version cleanly."""
    key = (root_id, fork_agent_sid)
    lock = _get_lock(key)
    path = shadow_path(root_id, fork_agent_sid)

    def _write() -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(path)

    async with lock:
        await asyncio.to_thread(_write)
        _current_version.pop(key, None)
    return path


def _append_sync(
    *,
    path: Path,
    key: tuple[str, str],
    file_version: int,
    line_offset_in_version: int,
    line: str,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    current = _current_version.get(key, 0)

    if file_version < current:
        # Stale message from an older file_version — drop.
        return

    if file_version > current:
        # Compaction or rotation observed on node; rewrite from scratch.
        with open(path, "wb") as f:
            f.truncate(0)
        _current_version[key] = file_version
        # New version's first line must start at offset 0.
        if line_offset_in_version != 0:
            logger.warning(
                "shadow_jsonl: version bump v%d→v%d but first line offset=%d "
                "(expected 0) for %s — accepting anyway",
                current, file_version, line_offset_in_version, path,
            )
        # fall through to append

    # Same version: line_offset_in_version is the expected file size.
    size = path.stat().st_size if path.exists() else 0
    if line_offset_in_version < size:
        # We already have at least up to this offset → truncate then
        # rewrite this line. Idempotent because UUID dedup catches dups
        # in events.jsonl; shadow file just needs to converge on the
        # node's current content.
        with open(path, "r+b") as f:
            f.truncate(line_offset_in_version)
    elif line_offset_in_version > size:
        # We missed a prefix. Don't write at the wrong offset — drop
        # this line. UUID dedup ensures correctness of the events
        # stream; the shadow file will resync next time the node sends
        # a contiguous prefix (resume_stream covers this).
        logger.warning(
            "shadow_jsonl: gap detected for %s (size=%d, line_offset=%d) — "
            "dropping line, awaiting prefix",
            path, size, line_offset_in_version,
        )
        return

    encoded = line if line.endswith("\n") else line + "\n"
    with open(path, "ab") as f:
        f.write(encoded.encode("utf-8"))


def snapshot_cursors_for(node_id: str) -> dict[str, dict]:
    """Build the `shadow_jsonls` map for a `resume_stream` to `node_id`.

    Keyed by `"{root_id}:{fork_agent_sid}"` → `{file_version, shadow_size}`.
    Tracks every shadow file we've heard about from THIS node so the
    node can decide whether to replay from a contiguous offset or bump
    `file_version` (compaction-during-gap)."""
    out: dict[str, dict] = {}
    for key in _per_node.get(node_id, set()):
        root_id, fork_agent_sid = key
        path = shadow_path(root_id, fork_agent_sid)
        size = path.stat().st_size if path.exists() else 0
        version = _current_version.get(key, 0)
        out[f"{root_id}:{fork_agent_sid}"] = {
            "file_version": version,
            "shadow_size": size,
        }
    return out


def reset_for_tests() -> None:
    """Test hook only — wipes in-memory state. Files on disk are left
    alone (test fixtures set BETTER_CLAUDE_HOME to a tempdir)."""
    _locks.clear()
    _current_version.clear()
    _per_node.clear()
