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
from pathlib import Path
from typing import Optional

from paths import ba_home

logger = logging.getLogger(__name__)


def runs_root() -> Path:
    return ba_home() / "runs"


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
            try:
                shutil.rmtree(child)
                removed += 1
            except OSError as e:
                logger.warning("delete_runs_for_sessions: failed to rm %s: %s", child, e)
    if removed:
        logger.info("delete_runs_for_sessions: removed %d run dir(s)", removed)
    return removed


def atomic_write_json(path: Path, data: dict) -> None:
    """Crash-safe JSON write for run-dir state. Thin alias over the single
    canonical writer in `json_store` so run-dir and store writes share one
    atomicity recipe."""
    from json_store import write_json

    write_json(path, data)


def pid_alive(pid: Optional[int]) -> bool:
    if not pid or pid <= 0:
        return False
    # Delegate to the platform process-control layer. On POSIX this is the
    # original os.kill(pid, 0) probe; on Windows os.kill(pid, 0) would
    # *terminate* the process, so a Win32 handle probe is used instead.
    from proc_control import process_control

    return process_control().pid_alive(pid)
