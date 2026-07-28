from __future__ import annotations

import ast
import inspect
import shutil
from pathlib import Path

import _test_home

_TEST_HOME = _test_home.isolate("ba-test-session-persist-coordinator-")

import session_manager as session_manager_module  # noqa: E402


def _session_manager_legacy_state_reads() -> set[str]:
    tree = ast.parse(
        Path(session_manager_module.__file__).read_text(encoding="utf-8")
    )
    legacy_names = {
        "_persist_pending",
        "_persist_deadlines",
        "_persist_deadline_heap",
        "_persist_last_at",
        "_persist_inflight",
        "_persist_state_lock",
        "_persist_state_changed",
        "_arm_persist_deadline_unlocked",
        "_cancel_persist_deadline_unlocked",
    }
    manager_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "SessionManager"
    )
    return {
        node.id
        for node in ast.walk(manager_class)
        if isinstance(node, ast.Name) and node.id in legacy_names
    }


def main() -> int:
    try:
        manager = session_manager_module.manager
        coordinator = manager._persist_coordinator

        assert session_manager_module._production_persist_coordinator is coordinator
        assert session_manager_module._persist_pending is coordinator.pending
        assert session_manager_module._persist_deadlines is coordinator.deadlines
        assert (
            session_manager_module._persist_deadline_heap
            is coordinator.deadline_heap
        )
        assert session_manager_module._persist_last_at is coordinator.last_at
        assert session_manager_module._persist_inflight is coordinator.inflight
        assert session_manager_module._persist_state_lock is coordinator.lock
        assert session_manager_module._persist_state_changed is coordinator.changed
        assert not hasattr(session_manager_module, "_persist_scheduler_started")

        marker = "persist-coordinator-alias-proof"
        with session_manager_module._persist_state_lock:
            session_manager_module._persist_pending[marker] = {"id": marker}
            assert coordinator.pending[marker]["id"] == marker
            coordinator.pending.pop(marker)

        callback = coordinator._flush_root
        assert callback.__self__ is manager
        assert callback.__func__ is manager._tail_persist.__func__
        scheduler_source = inspect.getsource(
            session_manager_module._SessionPersistCoordinator.scheduler_loop
        )
        assert "manager._tail_persist" not in scheduler_source
        wrapper_source = inspect.getsource(
            session_manager_module._persist_scheduler_loop
        )
        assert "_production_persist_coordinator.scheduler_loop()" in wrapper_source
        assert not _session_manager_legacy_state_reads()

        print("PASS session persist coordinator ownership")
        return 0
    finally:
        shutil.rmtree(_TEST_HOME, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
