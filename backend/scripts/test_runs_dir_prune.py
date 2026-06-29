from __future__ import annotations

import os
import shutil
import sys
import time

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-runs-prune-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import runs_dir  # noqa: E402


def _touch(path, age_days: float = 0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")
    when = time.time() - (age_days * 24 * 60 * 60)
    os.utime(path, (when, when))


def main() -> int:
    root = runs_dir.runs_root()
    old_complete = root / "old-complete"
    fresh_complete = root / "fresh-complete"
    old_incomplete = root / "old-incomplete"
    old_complete.mkdir(parents=True)
    fresh_complete.mkdir(parents=True)
    old_incomplete.mkdir(parents=True)
    _touch(old_complete / "complete.json", age_days=8)
    _touch(fresh_complete / "complete.json", age_days=1)
    _touch(old_incomplete / "state.json", age_days=8)

    removed = runs_dir.prune_old_completed_runs(max_age_days=7)
    checks = [
        (removed == 1, "removed one old completed run"),
        (not old_complete.exists(), "old completed run removed"),
        (fresh_complete.exists(), "fresh completed run kept"),
        (old_incomplete.exists(), "old incomplete run kept"),
    ]
    failed = [msg for ok, msg in checks if not ok]
    for ok, msg in checks:
        print(("PASS" if ok else "FAIL") + f": {msg}")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
