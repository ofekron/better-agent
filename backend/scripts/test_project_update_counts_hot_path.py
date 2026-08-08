from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-project-update-counts-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import project_update_store  # noqa: E402
import extension_store  # noqa: E402
from project_match import worker as project_match_worker  # noqa: E402


def test_counts_are_cached_after_warmup() -> None:
    project_update_store.append("proj-a", "change A")
    project_update_store.append("proj-b", "change B")
    assert project_update_store.unseen_count("proj-a") == 1
    original = project_update_store._read_entries_locked

    def fail_read(project_id: str):
        raise AssertionError(f"count hot path read project update log for {project_id}")

    project_update_store._read_entries_locked = fail_read
    try:
        assert project_update_store.unseen_count("proj-a") == 1
        assert project_update_store.unseen_count("proj-b") == 1
        assert project_update_store.total_unseen() == 2
        assert project_update_store.peek_total_unseen() == 2
    finally:
        project_update_store._read_entries_locked = original


def test_mark_seen_updates_cached_count() -> None:
    entry = project_update_store.append("proj-c", "change C")
    assert project_update_store.unseen_count("proj-c") == 1
    marked = project_update_store.mark_seen("proj-c", [entry["id"]])
    assert marked == 1
    assert project_update_store.unseen_count("proj-c") == 0


def test_builtin_enabled_state_is_fingerprint_cached() -> None:
    extension_store._ENABLED_CACHE.clear()
    calls = 0
    fingerprint = (1, 1)
    original_fingerprint = extension_store.store_fingerprint
    original_get = extension_store.get_extension

    def fake_fingerprint():
        return fingerprint

    def fake_get(extension_id: str):
        nonlocal calls
        calls += 1
        return {"enabled": True}

    extension_store.store_fingerprint = fake_fingerprint
    extension_store.get_extension = fake_get
    try:
        assert extension_store.is_extension_enabled_cached("ext-a")
        assert extension_store.is_extension_enabled_cached("ext-a")
        assert calls == 1
        fingerprint = (2, 1)
        assert extension_store.is_extension_enabled_cached("ext-a")
        assert calls == 2
    finally:
        extension_store.store_fingerprint = original_fingerprint
        extension_store.get_extension = original_get
        extension_store._ENABLED_CACHE.clear()


def test_project_match_rebuild_skips_unchanged_sessions() -> None:
    sessions = Path(_TMP_HOME) / "sessions"
    sessions.mkdir(exist_ok=True)
    (sessions / "a.json").write_text('{"id":"a","cwd":"/a","messages":[]}', encoding="utf-8")
    (sessions / "a.summary.json").write_text("{}", encoding="utf-8")
    fingerprint = project_match_worker.sessions_fingerprint()
    for name in (
        "a.opened.json",
        "a.seen.json",
        "a.drafts.json",
        ".fork-index.json",
        ".summary-index.json",
    ):
        (sessions / name).write_text("{}", encoding="utf-8")
    assert project_match_worker.sessions_fingerprint() == fingerprint
    result = project_match_worker.rebuild_index(fingerprint)
    assert result == {"rebuilt": False, "fingerprint": fingerprint}


def run() -> int:
    tests = [
        ("counts are cached after warmup", test_counts_are_cached_after_warmup),
        ("mark_seen updates cached count", test_mark_seen_updates_cached_count),
        ("builtin enabled state is fingerprint cached", test_builtin_enabled_state_is_fingerprint_cached),
        ("project match rebuild skips unchanged sessions", test_project_match_rebuild_skips_unchanged_sessions),
    ]
    failures: list[str] = []
    try:
        for name, fn in tests:
            try:
                fn()
            except Exception as exc:
                failures.append(name)
                print(f"FAIL {name}: {exc}")
            else:
                print(f"PASS {name}")
        if failures:
            print(f"FAILED: {failures}")
            return 1
        print("ALL PASS")
        return 0
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(run())
