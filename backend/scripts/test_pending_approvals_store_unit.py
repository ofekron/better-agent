"""Unit coverage for stores/pending_approvals.py.

Disk-backed approval queue for fresh-worker delegation requests. One JSON
file per delegation under <home>/pending_approvals/<id>.json, serialized by
fcntl locking. This is a security-critical durable store: a malformed record
or a path-traversal id must never escape the directory or silently produce a
worker spawn the user never approved.

The API layer (test_pending_approvals_api.py) fakes this store, so the store's
own branches — create validation, the idempotent/missing/expired transition
state machine, path-injection rejection, cache invalidation, and prune — have
no direct coverage. This file covers every branch directly.

Home is engaged to an isolated tempdir at import time. An autouse fixture
clears the directory and drops the pending cache before each test so no record
leaks across tests.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_BACKEND = _HERE.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import _test_home  # noqa: E402

_test_home.isolate("pending-approvals-store-test-")

from stores import pending_approvals as store  # noqa: E402


@pytest.fixture(autouse=True)
def fresh():
    """Start each test cold: empty directory, cache invalidated."""
    d = store._dir()
    if d.exists():
        for p in d.glob("*.json"):
            p.unlink()
    store._invalidate_pending_cache()
    yield


def _make(delegation_id="del_1", **overrides) -> dict:
    base = dict(
        delegation_id=delegation_id,
        app_session_id="app_1",
        cwd="/repo",
        justification="need a worker",
        proposed_description="researcher",
        proposed_orchestration_mode="team",
        instructions_preview="do the thing",
        model="claude-sonnet",
    )
    base.update(overrides)
    return store.create(**base)


def _write_raw(delegation_id: str, payload: dict) -> Path:
    path = store._path(delegation_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    store._invalidate_pending_cache()
    return path


# ── _path / path-injection guard ──────────────────────────────────────────── #


@pytest.mark.parametrize("bad", ["../evil", "a/b", "a b", "a.b", "a" * 65, "", "café"])
def test_path_rejects_invalid_delegation_id(bad):
    # The strict regex is the only thing keeping a caller-controlled id inside
    # the approvals directory. It MUST raise rather than build an escape path.
    with pytest.raises(ValueError):
        store._path(bad)


# ── create ─────────────────────────────────────────────────────────────────── #


def test_create_persists_record_with_expected_fields():
    rec = _make()
    assert rec["status"] == "pending"
    assert rec["resolved_at"] is None
    assert rec["approved_description"] is None
    assert rec["node_id"] == "primary"
    assert rec["harness_profile_id"] == ""
    # expiry is exactly 24h after creation.
    created = datetime.fromisoformat(rec["created_at"])
    expires = datetime.fromisoformat(rec["expires_at"])
    assert expires - created == timedelta(hours=store.EXPIRY_HOURS)
    # round-trips through disk.
    assert store.get("del_1") == rec


def test_create_normalizes_manager_to_team():
    rec = _make(proposed_orchestration_mode="manager")
    assert rec["proposed_orchestration_mode"] == "team"


def test_create_rejects_invalid_orchestration_mode():
    with pytest.raises(ValueError):
        _make(proposed_orchestration_mode="bogus")


def test_create_rejects_duplicate_delegation_id():
    _make()
    with pytest.raises(ValueError):
        _make()


def test_create_truncates_instructions_preview():
    long = "x" * 5000
    rec = _make(instructions_preview=long)
    assert len(rec["instructions_preview"]) == 2000


def test_create_carries_node_id_and_profile():
    rec = _make(node_id="linux-box", harness_profile_id="prof_1")
    assert rec["node_id"] == "linux-box"
    assert rec["harness_profile_id"] == "prof_1"


# ── get ────────────────────────────────────────────────────────────────────── #


def test_get_missing_returns_none():
    assert store.get("absent") is None


def test_get_invalid_id_returns_none():
    # Invalid ids must never raise out of get(); callers are not trusted.
    assert store.get("../escape") is None


def test_get_corrupt_json_returns_none():
    path = store._path("del_1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert store.get("del_1") is None


# ── list_pending ──────────────────────────────────────────────────────────── #


def test_list_pending_returns_only_pending():
    _make("del_a", cwd="/a")
    _make("del_b", cwd="/b")
    store.deny("del_b")
    ids = sorted(r["delegation_id"] for r in store.list_pending())
    assert ids == ["del_a"]


def test_list_pending_filters_by_cwd():
    _make("del_a", cwd="/a")
    _make("del_b", cwd="/b")
    only_a = store.list_pending(cwd="/a")
    assert [r["delegation_id"] for r in only_a] == ["del_a"]


def test_list_pending_empty_when_no_dir():
    # Directory absent -> snapshot reads no records, not an error.
    for p in store._dir().glob("*.json"):
        p.unlink()
    store._dir().rmdir()
    store._invalidate_pending_cache()
    assert store.list_pending() == []


def test_list_pending_serves_second_call_from_cache():
    # A repeat call with no intervening mutation must hit the populated cache.
    _make("del_a")
    first = store.list_pending()
    second = store.list_pending()
    assert first == second
    assert [r["delegation_id"] for r in second] == ["del_a"]


def test_list_pending_skips_corrupt_file_in_directory():
    _make("del_a")
    # A corrupt sibling must be skipped, not crash the listing.
    bad = store._path("del_bad")
    bad.write_text("{not json", encoding="utf-8")
    store._invalidate_pending_cache()
    ids = [r["delegation_id"] for r in store.list_pending()]
    assert ids == ["del_a"]


# ── approve ───────────────────────────────────────────────────────────────── #


def test_approve_defaults_description_and_mode_from_proposed():
    _make(proposed_description="researcher", proposed_orchestration_mode="native")
    rec, reason = store.approve("del_1")
    assert reason == "ok"
    assert rec["status"] == "approved"
    assert rec["resolved_at"] is not None
    assert rec["approved_description"] == "researcher"
    assert rec["approved_orchestration_mode"] == "native"


def test_approve_uses_explicit_description():
    _make(proposed_description="researcher")
    rec, _ = store.approve("del_1", description="reviewer")
    assert rec["approved_description"] == "reviewer"


def test_approve_normalizes_manager_mode():
    _make(proposed_orchestration_mode="team")
    rec, _ = store.approve("del_1", orchestration_mode="manager")
    assert rec["approved_orchestration_mode"] == "team"


def test_approve_rejects_invalid_mode_and_leaves_record_pending():
    _make()
    # Raised inside the lock, before the write — record must stay pending.
    with pytest.raises(ValueError):
        store.approve("del_1", orchestration_mode="bogus")
    assert store.get("del_1")["status"] == "pending"


def test_approve_is_idempotent_already_resolved():
    _make()
    first, r1 = store.approve("del_1")
    assert r1 == "ok"
    second, r2 = store.approve("del_1")
    assert r2 == "already_resolved"
    assert second["resolved_at"] == first["resolved_at"]


def test_approve_missing_record():
    assert store.approve("absent") == (None, "missing")


def test_approve_invalid_id_is_missing():
    assert store.approve("../escape") == (None, "missing")


def test_approve_expired_record():
    _make()
    rec = store.get("del_1")
    rec["expires_at"] = (datetime.now() - timedelta(hours=1)).isoformat()
    _write_raw("del_1", rec)
    result, reason = store.approve("del_1")
    assert reason == "expired"
    assert result is not None
    assert result["status"] == "pending"  # unchanged


def test_approve_unparseable_expires_at_is_treated_as_no_expiry():
    # A malformed expires_at is ignored (not crashed on); approval proceeds.
    _make()
    rec = store.get("del_1")
    rec["expires_at"] = "not-a-date"
    _write_raw("del_1", rec)
    result, reason = store.approve("del_1")
    assert reason == "ok"
    assert result["status"] == "approved"


def test_approve_missing_expires_at_field_proceeds():
    # A record without expires_at (legacy shape) is treated as never-expiring.
    _make()
    rec = store.get("del_1")
    del rec["expires_at"]
    _write_raw("del_1", rec)
    result, reason = store.approve("del_1")
    assert reason == "ok"
    assert result["status"] == "approved"


def test_transition_corrupt_json_is_missing():
    path = store._path("del_1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert store.approve("del_1") == (None, "missing")


def test_transition_rejects_invalid_new_status():
    with pytest.raises(ValueError):
        store._transition_locked("del_1", "bogus")


# ── deny ──────────────────────────────────────────────────────────────────── #


def test_deny_marks_denied():
    _make()
    rec, reason = store.deny("del_1")
    assert reason == "ok"
    assert rec["status"] == "denied"
    assert rec["resolved_at"] is not None


def test_deny_after_approve_is_already_resolved():
    _make()
    store.approve("del_1")
    _, reason = store.deny("del_1")
    assert reason == "already_resolved"


def test_deny_missing():
    assert store.deny("absent") == (None, "missing")


# ── delete ────────────────────────────────────────────────────────────────── #


def test_delete_existing_returns_true_and_removes():
    _make()
    assert store.delete("del_1") is True
    assert store.get("del_1") is None


def test_delete_missing_returns_false():
    assert store.delete("absent") is False


def test_delete_invalid_id_returns_false():
    assert store.delete("../escape") is False


# ── prune_old ─────────────────────────────────────────────────────────────── #


def test_prune_old_removes_files_older_than_cutoff():
    _make("del_a")
    _make("del_b")
    # Push del_a's mtime a week into the past.
    old = store._path("del_a")
    old_ts = time.time() - (8 * 24 * 3600)
    os.utime(old, (old_ts, old_ts))
    deleted = store.prune_old(max_age_days=7)
    assert deleted == 1
    assert store.get("del_a") is None
    assert store.get("del_b") is not None


def test_prune_old_noop_when_nothing_old():
    _make()
    assert store.prune_old(max_age_days=7) == 0


def test_prune_old_no_dir_returns_zero():
    for p in store._dir().glob("*.json"):
        p.unlink()
    store._dir().rmdir()
    store._invalidate_pending_cache()
    assert store.prune_old() == 0


# ── cache invalidation ────────────────────────────────────────────────────── #


def test_cache_invalidated_on_transition_and_delete():
    _make("del_a")
    assert len(store.list_pending()) == 1
    store.approve("del_a")  # invalidates cache
    assert store.list_pending() == []
    _make("del_b")
    assert len(store.list_pending()) == 1
    store.delete("del_b")  # invalidates cache
    assert store.list_pending() == []
