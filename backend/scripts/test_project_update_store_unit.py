"""Unit coverage for project_update_store.py.

Append-only per-project JSONL store of captured project-structure updates, with
an in-memory unseen-count cache (lazy-loaded from disk, then maintained on
append / mark_seen). Pure filesystem store + lock + cache; no pydantic, no
async, no event bus.

Home is engaged to an isolated tempdir at import time so the store writes under
a throwaway project_updates/ tree; each test wipes that tree and resets the
module cache so on-disk + in-memory state is exactly what the test installed.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_BACKEND = _HERE.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import _test_home

_test_home.isolate("project-update-store-test-")

import project_update_store as store  # noqa: E402


def _write_raw(project_id: str, lines: list[str]) -> Path:
    """Write raw JSONL lines (already serialized) straight to a project log."""
    path = store._project_path(project_id)
    with open(path, "w", encoding="utf-8") as f:
        for line in lines:
            f.write(line + "\n")
    return path


def _entry(eid: str, *, seen: bool = False, text: str = "x") -> str:
    return json.dumps({"id": eid, "text": text, "seen": seen})


@pytest.fixture
def fresh():
    """Wipe the project_updates tree + reset the in-memory cache so each test
    starts cold (lazy-load reproducible, no count leakage between tests)."""
    d = store._updates_dir()
    if d.exists():
        shutil.rmtree(d)
    store._counts_loaded = False
    store._unseen_counts.clear()
    store._total_unseen_count = 0
    store._counts_version = 0
    yield store


# --------------------------------------------------------------------------- #
# append + list_unseen
# --------------------------------------------------------------------------- #
def test_append_creates_unseen_entry(fresh) -> None:
    entry = store.append("p1", "structure changed")
    assert entry["seen"] is False
    assert entry["text"] == "structure changed"
    assert len(entry["id"]) == 12
    assert "created_at" in entry

    unseen = store.list_unseen("p1")
    assert len(unseen) == 1
    assert unseen[0]["id"] == entry["id"]


def test_list_unseen_empty_for_unknown_project(fresh) -> None:
    assert store.list_unseen("never") == []


def test_list_unseen_skips_seen_entries(fresh) -> None:
    e1 = store.append("p1", "a")
    store.append("p1", "b")
    marked = store.mark_seen("p1", [e1["id"]])
    assert marked == 1
    unseen = store.list_unseen("p1")
    assert [e["text"] for e in unseen] == ["b"]


# --------------------------------------------------------------------------- #
# _read_entries_path_locked resilience
# --------------------------------------------------------------------------- #
def test_read_skips_blank_and_malformed_lines(fresh) -> None:
    path = _write_raw(
        "p1",
        [_entry("a"), "", "  ", "{not json", _entry("b", seen=True)],
    )
    entries = store._read_entries_path_locked(path)
    assert [e["id"] for e in entries] == ["a", "b"]


def test_read_returns_empty_when_log_absent(fresh) -> None:
    assert store._read_entries_path_locked(fresh._updates_dir() / "nope.jsonl") == []


# --------------------------------------------------------------------------- #
# unseen-count cache: lazy load from disk
# --------------------------------------------------------------------------- #
def test_lazy_load_counts_from_disk(fresh) -> None:
    _write_raw("p1", [_entry("a"), _entry("b", seen=True), _entry("c")])
    _write_raw("p2", [_entry("d")])
    # Cache is cold: peek must report not-loaded before any count op.
    assert store.peek_total_unseen() is None
    assert store.peek_unseen_counts(["p1"]) is None

    assert store.total_unseen() == 3  # p1 has 2 unseen, p2 has 1
    assert store.unseen_count("p1") == 2
    assert store.unseen_count("p2") == 1
    assert store.unseen_count("absent") == 0
    assert fresh._unseen_counts["p1"] == 2


def test_lazy_load_skips_projects_with_zero_unseen(fresh) -> None:
    _write_raw("allseen", [_entry("a", seen=True), _entry("b", seen=True)])
    assert store.total_unseen() == 0
    assert "allseen" not in fresh._unseen_counts


def test_lazy_load_when_dir_absent(fresh) -> None:
    # Fixture removed the tree entirely; total_unseen loads against no dir.
    assert not fresh._updates_dir().exists()
    assert store.total_unseen() == 0
    assert fresh._counts_loaded is True


def test_unseen_counts_for_many_projects(fresh) -> None:
    store.append("p1", "a")
    store.append("p2", "b")
    assert store.unseen_counts(["p1", "p2", "p3"]) == {"p1": 1, "p2": 1, "p3": 0}


# --------------------------------------------------------------------------- #
# peek (no-load) accessors
# --------------------------------------------------------------------------- #
def test_peek_returns_none_until_loaded(fresh) -> None:
    assert store.peek_total_unseen() is None
    assert store.peek_unseen_counts(["p1"]) is None


def test_peek_returns_data_after_warm(fresh) -> None:
    store.append("p1", "a")
    store.warm_counts()
    assert store.peek_total_unseen() == 1
    assert store.peek_unseen_counts(["p1", "p2"]) == {"p1": 1, "p2": 0}


# --------------------------------------------------------------------------- #
# version_token + count transitions (_set_count_locked branches)
# --------------------------------------------------------------------------- #
def test_version_increments_on_count_change_not_on_noop(fresh) -> None:
    base = store.version_token()
    store.append("p1", "a")
    assert store.version_token() == base + 1
    store.append("p1", "b")
    assert store.version_token() == base + 2

    mid = store.version_token()
    # Marking already-seen entries is a no-op: no version bump, no double count.
    e1 = store.list_unseen("p1")[0]
    store.mark_seen("p1", [e1["id"]])
    assert store.version_token() == mid + 1
    store.mark_seen("p1", [e1["id"]])  # already seen -> nothing changes
    assert store.version_token() == mid + 1


def test_count_drops_to_zero_removes_project_from_cache(fresh) -> None:
    e1 = store.append("p1", "a")
    store.append("p1", "b")
    assert fresh._unseen_counts.get("p1") == 2
    store.mark_seen("p1", [e1["id"]])
    assert fresh._unseen_counts.get("p1") == 1
    # Mark the remaining one -> count hits zero -> project evicted from cache.
    remaining = [e["id"] for e in store.list_unseen("p1")]
    store.mark_seen("p1", remaining)
    assert "p1" not in fresh._unseen_counts
    assert store.unseen_count("p1") == 0
    assert store.total_unseen() == 0


# --------------------------------------------------------------------------- #
# mark_seen
# --------------------------------------------------------------------------- #
def test_mark_seen_returns_zero_when_log_absent(fresh) -> None:
    assert store.mark_seen("never", ["x"]) == 0


def test_mark_seen_rewrites_file_with_seen_flags(fresh) -> None:
    e1 = store.append("p1", "a")
    e2 = store.append("p1", "b")
    assert store.mark_seen("p1", [e1["id"]]) == 1

    path = store._project_path("p1", create_dir=False)
    rewritten = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    by_id = {e["id"]: e for e in rewritten}
    assert by_id[e1["id"]]["seen"] is True
    assert by_id[e2["id"]]["seen"] is False
    assert store.unseen_count("p1") == 1


def test_mark_seen_ignores_unknown_ids(fresh) -> None:
    store.append("p1", "a")
    assert store.mark_seen("p1", ["not-a-real-id"]) == 0
    assert store.unseen_count("p1") == 1


# --------------------------------------------------------------------------- #
# append maintains counts incrementally after a cold load
# --------------------------------------------------------------------------- #
def test_append_increments_count_after_load(fresh) -> None:
    _write_raw("p1", [_entry("a")])
    store.warm_counts()
    assert store.unseen_count("p1") == 1
    store.append("p1", "b")
    assert store.unseen_count("p1") == 2
    assert store.total_unseen() == 2


def test_set_count_is_noop_when_unchanged(fresh) -> None:
    # Unreachable via the public API (append always +1, mark_seen only fires on
    # count>0), so exercise the guard directly: a no-op set must not bump the
    # version token nor create a cache entry.
    store.warm_counts()
    base = store.version_token()
    store._set_count_locked("p1", 0)  # 0 == previous 0
    assert store.version_token() == base
    assert "p1" not in fresh._unseen_counts
    assert store.total_unseen() == 0
