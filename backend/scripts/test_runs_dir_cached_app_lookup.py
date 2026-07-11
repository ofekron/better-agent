from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import _test_home


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_TMP_HOME = _test_home.isolate("bc-test-cached-app-runs-")

import runs_dir


def _seed(root: Path, run_id: str, app_session_id: str) -> Path:
    run_dir = root / run_id
    run_dir.mkdir(parents=True)
    state_path = run_dir / "state.json"
    state = {
        "session_id": f"provider-{run_id}",
        "jsonl_path": str(run_dir / "events.jsonl"),
        "app_session_id": app_session_id,
    }
    state_path.write_text(json.dumps(state), encoding="utf-8")
    runs_dir._append_run_state_ledger(state_path, state)
    return run_dir


def test_cached_lookup_is_scoped_and_never_backfills() -> None:
    root = runs_dir.runs_root()
    root.mkdir(parents=True)
    target = "target-session"
    expected = {_seed(root, f"target-{index}", target) for index in range(3)}
    for index in range(2_000):
        _seed(root, f"decoy-{index}", f"other-{index}")
    runs_dir.run_dirs_by_app_session(root)

    original_backfill = runs_dir._backfill_run_state_app_index
    original_iterdir = runs_dir.Path.iterdir
    original_rebuild = runs_dir.ledger_state_files_for_sid

    def forbidden_backfill(_root: Path) -> None:
        raise AssertionError("request-path cached lookup must never backfill")

    def forbidden_iterdir(path: Path):
        if path == root:
            raise AssertionError("request-path cached lookup must never enumerate runs")
        return original_iterdir(path)

    def forbidden_rebuild(*_args, **_kwargs):
        raise AssertionError("request-path cached lookup must never rebuild")

    runs_dir._backfill_run_state_app_index = forbidden_backfill
    runs_dir.Path.iterdir = forbidden_iterdir
    runs_dir.ledger_state_files_for_sid = forbidden_rebuild
    try:
        actual = set(runs_dir.cached_run_dirs_for_app_session(root, target))
    finally:
        runs_dir._backfill_run_state_app_index = original_backfill
        runs_dir.Path.iterdir = original_iterdir
        runs_dir.ledger_state_files_for_sid = original_rebuild
    assert actual == expected


def test_cached_lookup_fails_closed_when_cache_is_unavailable() -> None:
    root = runs_dir.runs_root()
    cache = runs_dir.run_state_ledger_cache_path(root)
    cache.unlink(missing_ok=True)
    assert runs_dir.cached_run_dirs_for_app_session(root, "target-session") == []
    cache.write_bytes(b"not sqlite")
    assert runs_dir.cached_run_dirs_for_app_session(root, "target-session") == []


if __name__ == "__main__":
    try:
        test_cached_lookup_is_scoped_and_never_backfills()
        test_cached_lookup_fails_closed_when_cache_is_unavailable()
        print("PASS: ask run lookup is cached, scoped, and bounded")
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
