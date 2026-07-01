"""Durable, append-only ledger of provider session_ids Better Agent spawned.

A run dir's `state.json`/`backend_state.json` is the only structured record
that a given provider native session was spawned BY Better Agent (worker,
fork, delegate, supervisor, adv-sync, or a normal turn). Run dirs are reaped
on session delete and on the 7-day age-prune, so that provenance evaporates
within a week — after which the native-session importer can no longer tell a
BA-spawned session apart from a real user CLI session.

This ledger captures each sid before the run dir is removed or when a provider
discovers the native session id. It is append-only: one sid per line, deduped on read.
Append is O(1) and line-atomic; reads dedupe into a set. The ledger only ever
grows knowledge — it is never the authority that something IS a user session,
only that something WAS BA-spawned.
"""

from __future__ import annotations

import logging
import threading
import json
from pathlib import Path

from paths import ba_home

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()


def _path() -> Path:
    return ba_home() / "native_spawn_ledger.log"


def _bootstrap_marker_path() -> Path:
    return ba_home() / "native_spawn_ledger.bootstrapped"


def add(sid: str) -> bool:
    """Append a BA-spawned provider session_id. No-op on empty/non-str."""
    if not isinstance(sid, str) or not sid:
        return True
    return add_many([sid])


def add_many(sids: list[str]) -> bool:
    clean = [sid for sid in sids if isinstance(sid, str) and sid]
    if not clean:
        return True
    try:
        with _LOCK:
            p = _path()
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a", encoding="utf-8") as f:
                f.write("".join(f"{sid}\n" for sid in clean))
        return True
    except OSError:
        logger.exception("spawn_ledger: append failed")
        return False


def all_sids() -> set[str]:
    """Every BA-spawned sid recorded so far (deduped)."""
    p = _path()
    if not p.exists():
        return set()
    try:
        with _LOCK:
            text = p.read_text(encoding="utf-8")
    except OSError:
        logger.exception("spawn_ledger: read failed")
        return set()
    return {ln.strip() for ln in text.splitlines() if ln.strip()}


def record_discovered(sid: str) -> None:
    add(sid)


def _sid_from_run_dir(child: Path) -> str:
    for name in ("state.json", "backend_state.json", "complete.json"):
        try:
            o = json.loads((child / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        sid = o.get("session_id") if isinstance(o, dict) else None
        if isinstance(sid, str) and sid:
            return sid
    return ""


def record_run_dir(child: Path) -> None:
    add(_sid_from_run_dir(child))


def bootstrap_from_run_dirs_once() -> None:
    marker = _bootstrap_marker_path()
    if marker.exists():
        return
    try:
        from runs_dir import runs_root
        root = runs_root()
        sids: list[str] = []
        if root.exists():
            for child in root.iterdir():
                if child.is_dir():
                    sid = _sid_from_run_dir(child)
                    if sid:
                        sids.append(sid)
        if sids and not add_many(sids):
            return
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("1\n", encoding="utf-8")
    except OSError:
        logger.exception("spawn_ledger: bootstrap from run dirs failed")
