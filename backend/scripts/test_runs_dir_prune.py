from __future__ import annotations

import os
import shutil
import sys
import threading
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


def _test_scan_does_not_hold_catalog_lock(root) -> bool:
    entered_scan = threading.Event()
    release_scan = threading.Event()
    acquired_locks = threading.Event()
    original_scandir = runs_dir.os.scandir
    thread_errors = []

    class SlowScandir:
        def __enter__(self):
            entered_scan.set()
            release_scan.wait(timeout=2)
            self._inner = original_scandir(root)
            return self._inner.__enter__()

        def __exit__(self, *args):
            return self._inner.__exit__(*args)

    runs_dir.os.scandir = lambda _root: SlowScandir()

    def run_prune() -> None:
        try:
            runs_dir.prune_old_completed_runs()
        except BaseException as exc:
            thread_errors.append(exc)

    worker = threading.Thread(target=run_prune)
    worker.start()
    result = False
    try:
        if not entered_scan.wait(timeout=1):
            result = False
        else:
            def acquire_catalog() -> None:
                try:
                    for _ in range(64):
                        with runs_dir.run_catalog_lock(root):
                            pass
                    acquired_locks.set()
                except BaseException as exc:
                    thread_errors.append(exc)

            contender = threading.Thread(target=acquire_catalog)
            contender.start()
            unlocked = acquired_locks.wait(timeout=0.25)
            release_scan.set()
            contender.join(timeout=2)
            result = unlocked and not contender.is_alive() and not thread_errors
    finally:
        release_scan.set()
        worker.join(timeout=2)
        runs_dir.os.scandir = original_scandir
        if worker.is_alive():
            thread_errors.append(RuntimeError("prune worker did not stop"))
    return result and not thread_errors


def _test_changed_candidate_is_not_reaped(root) -> bool:
    child = root / "changed-after-scan"
    child.mkdir()
    complete = child / "complete.json"
    _touch(complete, age_days=8)
    original_lock = runs_dir.run_catalog_lock

    class MutatingLock:
        def __enter__(self):
            complete.write_text('{"changed":true}', encoding="utf-8")
            when = time.time() - (8 * 24 * 60 * 60)
            os.utime(complete, (when, when))
            return None

        def __exit__(self, *_args):
            return None

    runs_dir.run_catalog_lock = lambda _root: MutatingLock()
    try:
        removed = runs_dir.prune_old_completed_runs(max_age_days=7)
    finally:
        runs_dir.run_catalog_lock = original_lock
    return removed == 0 and child.exists()


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
        (_test_scan_does_not_hold_catalog_lock(root), "catalog remains available during inventory scan"),
        (_test_changed_candidate_is_not_reaped(root), "changed candidate is rejected during locked revalidation"),
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
