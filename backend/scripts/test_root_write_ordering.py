#!/usr/bin/env python3
"""Root-file write-ordering regression tests.

Locks the invariant behind the `test_adapter_session_gaps.py` flake: a
debounced tail flush copies the root under `_lock_for_root` but used to
write OUTSIDE it, so a synchronous locked write that landed between the
copy and the write (fork/delete) was silently clobbered by the stale
copy — erasing a just-created fork from disk AND from the in-memory
fork index, or resurrecting a just-deleted root.

Both tests drive the REAL `_tail_persist` path and block its write at
the repository boundary (event-driven, no sleeps) so the dangerous
interleaving is exercised deterministically:

    tail claims + copies (pre-mutation snapshot)
        -> synchronous mutator writes (fork) / deletes the root
            -> tail's stale write is released

Pre-fix: the stale write lands last and wins. Post-fix: every root
write holds the per-root write lock and snapshot flushes CAS on the
root's write seq, so the stale copy either writes strictly BEFORE the
newer locked write or is skipped entirely.

Run:
    PYTHONPATH=. python3 -m pytest backend/scripts/test_root_write_ordering.py -q
"""

from __future__ import annotations

import atexit
import shutil
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

_BACKEND_DIR = str(Path(__file__).resolve().parents[1])
_REPO_ROOT = str(Path(_BACKEND_DIR).parent)
for _p in (_BACKEND_DIR, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import paths  # noqa: E402

_TEST_HOME = tempfile.mkdtemp(prefix="ba-root-write-ordering-test-")
paths.engage_test_home(_TEST_HOME)
atexit.register(shutil.rmtree, _TEST_HOME, ignore_errors=True)

import config_store  # noqa: E402
import session_store  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402

_WAIT_S = 15.0
# Bounded pause that lets a (correctly) blocked mutator thread reach its
# blocking point before the stale write is released; pure liveness bound,
# never an ordering assumption — see each test's comments.
_MUTATOR_HEADSTART_S = 2.0

_FORK_CAPABLE_PROVIDER_ID: str | None = None


def _fork_capable_provider_id() -> str:
    """codex has an empty `runtime_probe_imports` tuple, so provider
    resolution succeeds in this bare test env with no real CLI (same
    recipe as test_adapter_session_gaps.py)."""
    global _FORK_CAPABLE_PROVIDER_ID
    if _FORK_CAPABLE_PROVIDER_ID is None:
        record = config_store.add_provider({
            "name": f"fork-capable {uuid.uuid4().hex}", "kind": "codex",
            "mode": "subscription",
        })
        _FORK_CAPABLE_PROVIDER_ID = record["id"]
    return _FORK_CAPABLE_PROVIDER_ID


def _make_fork_parent(name: str) -> dict:
    root = session_manager.create(
        name=name,
        cwd=tempfile.mkdtemp(prefix="ba-root-write-ordering-cwd-"),
        orchestration_mode="native",
        provider_id=_fork_capable_provider_id(),
    )
    session_manager.set_agent_sid(root["id"], "manager", f"fake-claude-{root['id'][:8]}")
    session_manager.append_user_msg(root["id"], {
        "id": "m1", "role": "user", "content": "hi", "events": [],
        "timestamp": "2026-05-01T00:00:00", "isStreaming": False,
    })
    # Drain the recipe's own debounced persists so the coordinator's
    # scheduler thread has nothing left to flush — the test's proxy must
    # only ever see the tail flush the test itself arms and runs.
    session_manager.flush_root_persist(root["id"])
    return session_manager.get(root["id"])


class _BlockingTailRepository:
    """Delegating proxy around the manager's real repository that stalls
    the FIRST tail-flush write (`already_persistable=True` — only the
    snapshot flush paths pass it) until the test releases it. All
    synchronization is event-driven; timeouts are failure deadlines
    only."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self._arm_lock = threading.Lock()
        self._armed = True
        self.copied = threading.Event()
        self.release_write = threading.Event()

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def copy_persistable_root(self, root: dict) -> dict:
        snapshot = self._inner.copy_persistable_root(root)
        self.copied.set()
        return snapshot

    def write_root(self, root: dict, **kwargs):
        if kwargs.get("already_persistable"):
            with self._arm_lock:
                armed, self._armed = self._armed, False
            if armed:
                assert self.release_write.wait(_WAIT_S), (
                    "test orchestration stalled: stale write never released"
                )
        return self._inner.write_root(root, **kwargs)


def _enqueue_far_future_flush(root_id: str) -> None:
    """Arm a real pending tail flush whose scheduler deadline is far
    away, so ONLY the test's own `_tail_persist` call claims it —
    deterministic, no race against the coordinator's scheduler thread."""
    persist = session_manager._persist_coordinator
    live_root = session_manager._roots.get(root_id)
    assert live_root is not None, "root must be resident after create"
    with persist.lock:
        persist.enqueue_unlocked(
            root_id, live_root, now=time.monotonic(), debounce=3600.0,
        )


def test_tail_flush_does_not_clobber_fork_written_after_its_copy() -> None:
    root = _make_fork_parent(f"ordering root {uuid.uuid4().hex}")
    root_id = root["id"]

    proxy = _BlockingTailRepository(session_manager._root_repository)
    session_manager._root_repository = proxy
    child: dict = {}
    try:
        _enqueue_far_future_flush(root_id)

        tail = threading.Thread(
            target=session_manager._tail_persist, args=(root_id,),
            name="test-tail-persist", daemon=True,
        )
        tail.start()
        assert proxy.copied.wait(_WAIT_S), "tail flush never copied"

        def _fork() -> None:
            child.update(session_manager.fork(root_id, name="child"))

        forker = threading.Thread(target=_fork, name="test-fork", daemon=True)
        forker.start()
        # Pre-fix, fork completes here (nothing orders it against the
        # stalled tail write). Post-fix it MAY block on the per-root
        # write lock the stalled tail holds — the bounded join lets it
        # finish when it can and proceeds when it (correctly) can't.
        forker.join(_MUTATOR_HEADSTART_S)

        proxy.release_write.set()
        forker.join(_WAIT_S)
        assert not forker.is_alive(), "fork never completed"
        tail.join(_WAIT_S)
        assert not tail.is_alive(), "tail flush never completed"
    finally:
        proxy.release_write.set()
        session_manager._root_repository = proxy._inner

    child_id = child.get("id")
    assert child_id, "fork returned no child"
    # The fork must survive the stale tail flush — on disk and via
    # sid->root resolution (both were erased pre-fix).
    assert session_store.get_session(child_id) is not None, (
        "stale tail flush erased the fork from disk/index"
    )
    tree = session_store.get_root_tree(root_id)
    assert tree is not None
    assert any(f["id"] == child_id for f in session_store._walk_forks(tree)), (
        "root tree on disk lost the fork"
    )


def test_tail_flush_does_not_resurrect_deleted_root() -> None:
    root = _make_fork_parent(f"resurrection root {uuid.uuid4().hex}")
    root_id = root["id"]

    proxy = _BlockingTailRepository(session_manager._root_repository)
    session_manager._root_repository = proxy
    deleted: list[bool] = []
    try:
        _enqueue_far_future_flush(root_id)

        tail = threading.Thread(
            target=session_manager._tail_persist, args=(root_id,),
            name="test-tail-persist", daemon=True,
        )
        tail.start()
        assert proxy.copied.wait(_WAIT_S), "tail flush never copied"

        def _delete() -> None:
            deleted.append(session_manager.delete(root_id))

        deleter = threading.Thread(target=_delete, name="test-delete", daemon=True)
        deleter.start()
        # Pre-fix the delete completes here; post-fix it may block on
        # the write lock until the stale write is released.
        deleter.join(_MUTATOR_HEADSTART_S)

        proxy.release_write.set()
        deleter.join(_WAIT_S)
        assert not deleter.is_alive(), "delete never completed"
        tail.join(_WAIT_S)
        assert not tail.is_alive(), "tail flush never completed"
    finally:
        proxy.release_write.set()
        session_manager._root_repository = proxy._inner

    assert deleted == [True], f"delete failed: {deleted!r}"
    # The stale tail flush must not re-create the deleted root's file.
    assert session_store.get_session(root_id) is None, (
        "stale tail flush resurrected a deleted root"
    )
    assert not session_store.root_session_file_path(root_id).exists(), (
        "deleted root file re-appeared on disk"
    )


if __name__ == "__main__":
    failures = 0
    for fn in (
        test_tail_flush_does_not_clobber_fork_written_after_its_copy,
        test_tail_flush_does_not_resurrect_deleted_root,
    ):
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {exc}")
    sys.exit(1 if failures else 0)
