from __future__ import annotations

import os
import shutil
import sys

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-project-update-counts-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import project_update_store  # noqa: E402


def test_counts_are_cached_after_warmup() -> bool:
    project_update_store.append("proj-a", "change A")
    project_update_store.append("proj-b", "change B")
    if project_update_store.unseen_count("proj-a") != 1:
        return False
    original = project_update_store._read_entries_locked

    def fail_read(project_id: str):
        raise AssertionError(f"count hot path read project update log for {project_id}")

    project_update_store._read_entries_locked = fail_read
    try:
        return (
            project_update_store.unseen_count("proj-a") == 1
            and project_update_store.unseen_count("proj-b") == 1
            and project_update_store.total_unseen() == 2
            and project_update_store.peek_total_unseen() == 2
        )
    finally:
        project_update_store._read_entries_locked = original


def test_mark_seen_updates_cached_count() -> bool:
    entry = project_update_store.append("proj-c", "change C")
    if project_update_store.unseen_count("proj-c") != 1:
        return False
    marked = project_update_store.mark_seen("proj-c", [entry["id"]])
    return marked == 1 and project_update_store.unseen_count("proj-c") == 0


def run() -> int:
    tests = [
        ("counts are cached after warmup", test_counts_are_cached_after_warmup),
        ("mark_seen updates cached count", test_mark_seen_updates_cached_count),
    ]
    failures: list[str] = []
    try:
        for name, fn in tests:
            try:
                ok = fn()
            except Exception as exc:
                ok = False
                print(f"FAIL {name}: {exc}")
            else:
                print(("PASS" if ok else "FAIL") + f" {name}")
            if not ok:
                failures.append(name)
        if failures:
            print(f"FAILED: {failures}")
            return 1
        print("ALL PASS")
        return 0
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(run())
