from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
import asyncio
from unittest import mock
from pathlib import Path


HOME = tempfile.mkdtemp(prefix="ba-owner-lifecycle-")
os.environ["BETTER_AGENT_HOME"] = HOME
BACKEND = Path(__file__).parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import runtime_ownership
from ingestion_versions import current_ingestion_version
from run_recovery import _drain_recovered_live_queue, _mark_reconciled_terminal
from session_manager import SessionManager, manager

runtime_ownership.register_current_process_writer()


def test_create_invalidates_cached_miss_without_unrelated_generation_effect() -> None:
    missing = "created-after-miss"
    assert manager.root_id_for(missing) is None
    stable = manager.create(id="stable-owner")
    stable_token = manager.claim_owner(stable["id"])
    assert stable_token is not None

    created = manager.create(id=missing)
    assert manager.root_id_for(missing) == created["id"]
    assert manager.run_if_owner(stable_token, lambda: "ok") == (True, "ok")


def test_delete_serializes_with_callback_and_revokes_late_callback() -> None:
    sid = manager.create(id="delete-race")["id"]
    token = manager.claim_owner(sid)
    assert token is not None
    entered = threading.Event()
    release = threading.Event()
    callback_done = threading.Event()

    def callback() -> None:
        entered.set()
        release.wait(2)
        callback_done.set()

    worker = threading.Thread(target=lambda: manager.run_if_owner(token, callback))
    worker.start()
    assert entered.wait(1)
    deleted = threading.Event()
    deleter = threading.Thread(target=lambda: (manager.delete(sid), deleted.set()))
    deleter.start()
    assert not deleted.wait(0.05)
    release.set()
    worker.join(2)
    deleter.join(2)
    assert callback_done.is_set() and deleted.is_set()
    assert manager.run_if_owner(token, lambda: None)[0] is False


def test_owner_operation_can_mutate_session_without_opposite_lock_deadlock() -> None:
    sid = manager.create(id="opposite-lock-order")["id"]
    token = manager.claim_owner(sid)
    assert token is not None
    entered = threading.Event()
    release = threading.Event()

    def operation() -> None:
        entered.set()
        release.wait(2)
        manager.advance_processed_lines(sid, "agent", 3)

    worker = threading.Thread(target=lambda: manager.run_if_owner(token, operation))
    worker.start()
    assert entered.wait(1)
    deleter = threading.Thread(target=lambda: manager.delete(sid))
    deleter.start()
    release.set()
    worker.join(2)
    deleter.join(2)
    assert not worker.is_alive() and not deleter.is_alive()


def test_delete_recreate_rejects_old_token_and_accepts_new_owner() -> None:
    sid = manager.create(id="same-sid-recreate")["id"]
    old = manager.claim_owner(sid)
    assert old is not None and manager.delete(sid)
    manager.create(id=sid)
    new = manager.claim_owner(sid)
    assert new is not None and new.generation != old.generation
    assert manager.run_if_owner(old, lambda: None)[0] is False
    assert manager.run_if_owner(new, lambda: "new") == (True, "new")


def test_recreate_crash_boundary_keeps_old_evidence_off_new_incarnation() -> None:
    sid = "same-sid-crash-boundary"
    manager.create(id=sid)
    old = manager.claim_owner(sid)
    assert old is not None and manager.delete(sid)
    evidence_path = manager._deletion_evidence_path(sid)
    assert evidence_path.exists() and manager.owner_deletion_committed(old)

    recreated = manager.create(id=sid)
    assert evidence_path.exists(), "recreate must retain bounded prior-incarnation evidence"
    restarted = SessionManager()
    new = restarted.claim_owner(recreated["id"])
    assert new is not None
    assert new.incarnation != old.incarnation
    assert restarted.owner_deletion_committed(old)
    assert not restarted.owner_deletion_committed(new)


def test_initial_missing_recovery_owner_remains_retryable() -> None:
    run_id = "initial-missing-run"
    run_dir = Path(HOME) / "runs" / run_id
    run_dir.mkdir(parents=True)
    desc = {
        "run_id": run_id,
        "app_session_id": "later-created",
        "provider_kind": "claude",
        "ingestion_version": current_ingestion_version("claude"),
    }
    asyncio.run(_drain_recovered_live_queue(None, None, desc, asyncio.Queue(), None))
    assert not (run_dir / "reconciled.marker").exists()
    manager.create(id="later-created")
    assert manager.claim_owner("later-created") is not None


def _make_three_level_tree(prefix: str) -> tuple[str, str, str]:
    root = manager.create(id=f"{prefix}-root")
    child = manager.create_sub_session(
        parent_session_id=root["id"], name="child", cwd="/tmp",
    )
    grandchild = manager.create_sub_session(
        parent_session_id=child["id"], name="grandchild", cwd="/tmp",
    )
    return root["id"], child["id"], grandchild["id"]


def _assert_delete_waits_for_descendant(target_sid: str, active_sid: str) -> None:
    token = manager.claim_owner(active_sid)
    assert token is not None
    entered = threading.Event()
    release = threading.Event()
    operation = threading.Thread(target=lambda: manager.run_if_owner(
        token, lambda: (entered.set(), release.wait(2)),
    ))
    operation.start()
    assert entered.wait(1)
    deleted = threading.Event()
    deleter = threading.Thread(target=lambda: (manager.delete(target_sid), deleted.set()))
    deleter.start()
    assert not deleted.wait(0.05)
    release.set()
    operation.join(2)
    deleter.join(2)
    assert deleted.is_set()
    assert manager.run_if_owner(token, lambda: None)[0] is False


def test_root_delete_waits_for_active_grandchild() -> None:
    root, _, grandchild = _make_three_level_tree("root-delete")
    _assert_delete_waits_for_descendant(root, grandchild)


def test_fork_delete_waits_for_active_descendant() -> None:
    _, child, grandchild = _make_three_level_tree("fork-delete")
    _assert_delete_waits_for_descendant(child, grandchild)


def test_revocation_callbacks_run_after_root_and_operation_locks_release() -> None:
    sid = manager.create(id="callback-lock-state")["id"]
    token = manager.claim_owner(sid)
    assert token is not None
    observed: list[bool] = []

    def callback() -> None:
        operation_lock = manager._owner_operation_locks[sid]
        root_lock = manager._lock_for_root(token.root_id)
        observed.append(
            not operation_lock._is_owned() and not root_lock._is_owned()
        )

    manager.subscribe_owner_revoked(token, callback)
    assert manager.delete(sid)
    assert observed == [True]


def test_root_delete_false_keeps_owner_live_without_retirement_evidence() -> None:
    sid = manager.create(id="delete-false")["id"]
    from canonical_runtime_journal import canonical_runtime_journal
    canonical_runtime_journal().ensure_cutover(
        sid, rows=[], session=manager.get(sid),
    )
    token = manager.claim_owner(sid)
    assert token is not None
    with mock.patch("session_store.delete_session", return_value=False):
        assert manager.delete(sid) is False
    assert manager.run_if_owner(token, lambda: "live") == (True, "live")
    assert not manager.owner_deletion_committed(token)
    assert manager.get(sid) is not None
    assert canonical_runtime_journal().is_authoritative(sid)


def test_fork_write_failure_rolls_back_tree_and_keeps_tokens_live() -> None:
    root, child, grandchild = _make_three_level_tree("fork-write-failure")
    child_token = manager.claim_owner(child)
    grandchild_token = manager.claim_owner(grandchild)
    assert child_token is not None and grandchild_token is not None
    import session_store
    real_write = session_store.write_session_full
    writes = 0

    def fail_once(*args, **kwargs):
        nonlocal writes
        writes += 1
        if writes == 1:
            raise OSError("disk full")
        return real_write(*args, **kwargs)

    with mock.patch("session_store.write_session_full", side_effect=fail_once):
        assert manager.delete(child) is False
    assert manager.get(child) is not None and manager.get(grandchild) is not None
    assert manager.run_if_owner(child_token, lambda: True) == (True, True)
    assert manager.run_if_owner(grandchild_token, lambda: True) == (True, True)
    assert not manager.owner_deletion_committed(child_token)
    assert not manager.owner_deletion_committed(grandchild_token)


def test_successful_delete_commits_evidence_before_retiring_token() -> None:
    sid = manager.create(id="durable-success")["id"]
    token = manager.claim_owner(sid)
    assert token is not None
    assert manager.delete(sid)
    assert manager.owner_deletion_committed(token)
    assert manager.run_if_owner(token, lambda: None)[0] is False


def test_tombstone_failure_restores_root_and_keeps_recovery_retryable() -> None:
    sid = manager.create(id="tombstone-failure")["id"]
    token = manager.claim_owner(sid)
    assert token is not None
    with mock.patch.object(
        manager, "_commit_deletion_evidence_locked", side_effect=OSError("no tombstone"),
    ):
        assert manager.delete(sid) is False
    assert manager.get(sid) is not None
    assert manager.run_if_owner(token, lambda: "retry") == (True, "retry")
    assert not manager.owner_deletion_committed(token)


def test_revocation_close_is_idempotent_and_retirement_survives_restart_scan() -> None:
    sid = manager.create(id="revocation-close")["id"]
    token = manager.claim_owner(sid)
    assert token is not None
    calls = 0

    def close() -> None:
        nonlocal calls
        calls += 1

    unsubscribe = manager.subscribe_owner_revoked(token, close)
    assert manager.delete(sid)
    unsubscribe()
    unsubscribe()
    assert calls == 1

    run_id = "retired-run"
    desc = {
        "run_id": run_id,
        "provider_kind": "claude",
        "ingestion_version": current_ingestion_version("claude"),
    }
    (Path(HOME) / "runs" / run_id).mkdir(parents=True)
    assert _mark_reconciled_terminal(run_id, desc, "owner revoked")
    assert (Path(HOME) / "runs" / run_id / "reconciled.marker").exists()


if __name__ == "__main__":
    try:
        test_create_invalidates_cached_miss_without_unrelated_generation_effect()
        test_delete_serializes_with_callback_and_revokes_late_callback()
        test_owner_operation_can_mutate_session_without_opposite_lock_deadlock()
        test_delete_recreate_rejects_old_token_and_accepts_new_owner()
        test_recreate_crash_boundary_keeps_old_evidence_off_new_incarnation()
        test_initial_missing_recovery_owner_remains_retryable()
        test_root_delete_waits_for_active_grandchild()
        test_fork_delete_waits_for_active_descendant()
        test_revocation_callbacks_run_after_root_and_operation_locks_release()
        test_root_delete_false_keeps_owner_live_without_retirement_evidence()
        test_fork_write_failure_rolls_back_tree_and_keeps_tokens_live()
        test_successful_delete_commits_evidence_before_retiring_token()
        test_tombstone_failure_restores_root_and_keeps_recovery_retryable()
        test_revocation_close_is_idempotent_and_retirement_survives_restart_scan()
        print("PASS session owner lifecycle")
    finally:
        shutil.rmtree(HOME, ignore_errors=True)
