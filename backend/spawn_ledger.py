"""Durable, append-only ledger of provider session_ids Better Agent spawned.

A run dir's `state.json`/`backend_state.json` is the only structured record
that a given provider native session was spawned BY Better Agent (worker,
fork, delegate, supervisor, adv-sync, or a normal turn). Run dirs are reaped
on session delete and on the 7-day age-prune, so that provenance evaporates
within a week — after which the native-session importer can no longer tell a
BA-spawned session apart from a real user CLI session.

This ledger captures each sid at the reap site BEFORE the dir is removed, so
the provenance survives. It is append-only: one sid per line, deduped on read.
Append is O(1) and line-atomic; reads dedupe into a set. The ledger only ever
grows knowledge — it is never the authority that something IS a user session,
only that something WAS BA-spawned.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from paths import ba_home

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()


def _path() -> Path:
    return ba_home() / "native_spawn_ledger.log"


def add(sid: str) -> None:
    """Append a BA-spawned provider session_id. No-op on empty/non-str."""
    if not isinstance(sid, str) or not sid:
        return
    try:
        with _LOCK:
            p = _path()
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a", encoding="utf-8") as f:
                f.write(sid + "\n")
    except OSError:
        logger.exception("spawn_ledger: append failed")


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
