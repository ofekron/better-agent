"""Regression tests for the run-state ledger app-session index.

Covers:
  * backfill stamps written_at in the past (state.json mtime), so a later
    live append for the same app_session_id always wins.
  * backfill is key-deduped — running it twice appends no duplicate rows.
  * run_dirs_by_app_session serves from the in-memory ledger cache when the
    sqlite cache write silently failed (never degrades to {}).

Run with:
    cd backend && .venv/bin/python scripts/test_runs_dir_ledger_stamps.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-runs-ledger-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import runs_dir  # noqa: E402

OK = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"


def _write_state(root: Path, run_id: str, sid: str, app_sid: str, *, age_days: float = 0.0) -> Path:
    state_path = root / run_id / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({
        "session_id": sid,
        "jsonl_path": f"/tmp/{sid}.jsonl",
        "app_session_id": app_sid,
    }), encoding="utf-8")
    when = time.time() - (age_days * 24 * 60 * 60)
    os.utime(state_path, (when, when))
    return state_path


def _ledger_rows(root: Path) -> list[dict]:
    rows = []
    with runs_dir.run_state_ledger_path(root).open(encoding="utf-8") as f:
        for raw in f:
            rows.append(json.loads(raw))
    return rows


def test_backfill_past_stamps_and_live_append_wins() -> bool:
    root = runs_dir.runs_root() / "ledger-stamps"
    root.mkdir(parents=True, exist_ok=True)
    _write_state(root, "run-old", "sid-old", "app-A", age_days=5)
    _write_state(root, "run-mid", "sid-mid", "app-A", age_days=1)
    before = time.time()
    mapping = runs_dir.run_dirs_by_app_session(root)
    rows = _ledger_rows(root)
    no_future = all(row["written_at"] <= before for row in rows)
    backfill_ok = mapping.get("app-A") == root / "run-mid"
    runs_dir.atomic_write_json(root / "run-live" / "state.json", {
        "session_id": "sid-live",
        "jsonl_path": "/tmp/sid-live.jsonl",
        "app_session_id": "app-A",
    })
    live_mapping = runs_dir.run_dirs_by_app_session(root)
    live_wins = live_mapping.get("app-A") == root / "run-live"
    ok = no_future and backfill_ok and live_wins
    print(f"{OK if ok else FAIL} backfill stamps past + live append wins "
          f"(no_future={no_future}, backfill={mapping.get('app-A')}, "
          f"live={live_mapping.get('app-A')})")
    return ok


def test_backfill_twice_appends_no_duplicates() -> bool:
    root = runs_dir.runs_root() / "ledger-dedup"
    root.mkdir(parents=True, exist_ok=True)
    _write_state(root, "run-1", "sid-1", "app-B", age_days=2)
    runs_dir.run_dirs_by_app_session(root)
    first = len(_ledger_rows(root))
    runs_dir.run_state_app_index_backfill_marker_path(root).unlink()
    runs_dir.run_dirs_by_app_session(root)
    second = len(_ledger_rows(root))
    ok = first == second == 1
    print(f"{OK if ok else FAIL} backfill twice appends no duplicates "
          f"(first={first}, second={second})")
    return ok


def test_app_index_served_from_memory_when_sqlite_write_fails() -> bool:
    root = runs_dir.runs_root() / "ledger-memfallback"
    root.mkdir(parents=True, exist_ok=True)
    _write_state(root, "run-a", "sid-a", "app-C", age_days=1)
    original = runs_dir._write_run_state_cache

    def silent_failure(*args, **kwargs):
        # Mirrors the real function's swallow-on-error behavior: no sqlite
        # cache lands, no exception escapes.
        return None

    runs_dir._write_run_state_cache = silent_failure
    try:
        mapping = runs_dir.run_dirs_by_app_session(root)
        again = runs_dir.run_dirs_by_app_session(root)
    finally:
        runs_dir._write_run_state_cache = original
    sqlite_missing = not runs_dir.run_state_ledger_cache_path(root).exists()
    ok = (
        mapping.get("app-C") == root / "run-a"
        and again.get("app-C") == root / "run-a"
        and sqlite_missing
    )
    print(f"{OK if ok else FAIL} app index served from memory on sqlite write failure "
          f"(mapping={mapping.get('app-C')}, again={again.get('app-C')}, "
          f"sqlite_missing={sqlite_missing})")
    return ok


def test_live_extend_keeps_memory_app_index_current() -> bool:
    root = runs_dir.runs_root() / "ledger-extend"
    root.mkdir(parents=True, exist_ok=True)
    _write_state(root, "run-a", "sid-a", "app-D", age_days=1)
    runs_dir.run_dirs_by_app_session(root)
    runs_dir.atomic_write_json(root / "run-b" / "state.json", {
        "session_id": "sid-b",
        "jsonl_path": "/tmp/sid-b.jsonl",
        "app_session_id": "app-D",
    })
    mapping = runs_dir.run_dirs_by_app_session(root)
    ok = mapping.get("app-D") == root / "run-b"
    print(f"{OK if ok else FAIL} live append extends in-memory app index "
          f"(mapping={mapping.get('app-D')})")
    return ok


def main_run() -> int:
    tests = [
        test_backfill_past_stamps_and_live_append_wins,
        test_backfill_twice_appends_no_duplicates,
        test_app_index_served_from_memory_when_sqlite_write_fails,
        test_live_extend_keeps_memory_app_index_current,
    ]
    results = []
    for fn in tests:
        try:
            results.append(fn())
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"{FAIL} {fn.__name__} raised: {e}")
            results.append(False)
    n_pass = sum(1 for r in results if r)
    print(f"\n{n_pass}/{len(results)} runs_dir ledger tests passed")
    return 0 if n_pass == len(results) else 1


if __name__ == "__main__":
    try:
        sys.exit(main_run())
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
