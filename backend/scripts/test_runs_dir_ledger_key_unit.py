"""Hermetic unit tests for the ledger-key parse and marker-match branches in
``runs_dir`` that the happy-path tests leave out:

  * ``_run_state_ledger_keys`` skips a ledger line whose ``state_path`` /
    ``session_id`` / ``jsonl_path`` is missing or falsy (branch 1141->1133).
    Without the guard the malformed line would add a bogus ``('None', ...)`` key.
  * ``reconciled_marker_index_row_matches`` returns ``False`` when the on-disk
    ``reconciled.marker`` cannot be ``lstat``-ed (it is absent) — lines 1066-1067.
    Without the ``except OSError`` the fault would propagate.

NOTE on line 1031 (``if key in existing: continue`` in the reconciled-marker
backfill): that dedup is behaviorally REDUNDANT — ``ReconciledMarkerIndex``
already dedupes by key inside ``_append_many_locked`` (line 118 of
``reconciled_marker_index.py``). Removing 1030-1031 therefore changes nothing
observable, so no fail-on-bug test can cover it without being a "line-toucher".
It is excluded with this written justification (confirms defect 464eba2811e7:
the dedup is redundant, not merely unreachable). The proper fix is to delete the
redundant check; that belongs to the defect, not to this test commit.

All filesystem state lives under an isolated ``BETTER_AGENT_HOME`` tempdir via
``_test_home.isolate``; nothing touches a real home. Module-level caches are
reset between tests so order never matters. Every assertion is fail-on-bug.

Run with:
    cd backend && .venv/bin/python -m pytest scripts/test_runs_dir_ledger_key_unit.py -v
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-runs-dir-ledger-key-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest  # noqa: E402

import runs_dir  # noqa: E402
from runs_dir import (  # noqa: E402
    _run_state_ledger_keys,
    reconciled_marker_index_row_matches,
)


@pytest.fixture(autouse=True)
def _reset_module_caches():
    runs_dir._RUN_STATE_LEDGER_SEEN.clear()
    runs_dir._RUN_STATE_RECENT_INDEX_CACHE.clear()
    runs_dir._RUN_STATE_LEDGER_CACHE.clear()
    runs_dir._RUN_STATE_RECENT_INDEX_INFLIGHT.clear()
    runs_dir._RUN_STATE_LEDGER_CACHE_REBUILD_INFLIGHT.clear()
    yield
    runs_dir._RUN_STATE_LEDGER_SEEN.clear()
    runs_dir._RUN_STATE_RECENT_INDEX_CACHE.clear()
    runs_dir._RUN_STATE_LEDGER_CACHE.clear()
    runs_dir._RUN_STATE_RECENT_INDEX_INFLIGHT.clear()
    runs_dir._RUN_STATE_LEDGER_CACHE_REBUILD_INFLIGHT.clear()


@pytest.fixture
def fresh_root():
    path = Path(tempfile.mkdtemp(prefix="bc-rd-ledger-key-"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


def _write_lines(path: Path, rows: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, separators=(",", ":")) + "\n")
    return path


# --------------------------------------------------------------------------- #
# _run_state_ledger_keys: skip a line whose key fields are falsy (1141->1133)
# --------------------------------------------------------------------------- #


def test_run_state_ledger_keys_skips_falsy_key_line(fresh_root):
    """Branch 1141->1133: a ledger line missing one of state_path / session_id
    / jsonl_path is skipped (the ``if`` is False, control returns to the loop)
    and only the well-formed line contributes a key. Removing the guard makes
    the malformed line add a bogus ``('None', sid, 'None')`` key, so the
    assertion of exactly one key proves the guard."""
    ledger = fresh_root / "ledger.jsonl"
    sp = str(fresh_root / "run-1" / "state.json")
    _write_lines(
        ledger,
        [
            {"state_path": sp, "session_id": "s1", "jsonl_path": "j1"},  # valid
            {"state_path": sp, "session_id": "s2"},  # missing jsonl_path -> falsy
        ],
    )
    keys = _run_state_ledger_keys(ledger)
    assert keys == {(sp, "s1", "j1")}


# --------------------------------------------------------------------------- #
# reconciled_marker_index_row_matches: lstat OSError on missing marker (1066-1067)
# --------------------------------------------------------------------------- #


def test_reconciled_marker_row_match_missing_marker_returns_false(fresh_root):
    """Lines 1066-1067: when the ``reconciled.marker`` file does not exist,
    ``marker_path.lstat()`` raises ``OSError`` and the matcher fails closed to
    ``False``. The row is built to pass the earlier run_id / marker_path /
    symlink guards (run_dir is a real dir, marker path string matches), so
    execution reaches the lstat; removing the ``except OSError`` lets the fault
    escape the function."""
    run_dir = fresh_root / "run-1"
    run_dir.mkdir(parents=True)
    marker_path = run_dir / "reconciled.marker"
    assert not marker_path.exists()  # lstat will raise
    row = {
        "run_id": run_dir.name,
        "marker_path": str(marker_path),
        "marker_size": 1,
        "marker_mtime_ns": 2,
        "marker_inode": 3,
    }
    assert reconciled_marker_index_row_matches(run_dir, row) is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
