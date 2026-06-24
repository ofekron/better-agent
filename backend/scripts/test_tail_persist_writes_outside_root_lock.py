import os
import shutil
import sys
import tempfile
from pathlib import Path

import _test_home
_test_home.isolate("bc_test_tail_persist_")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import session_manager as session_manager_module  # noqa: E402
import session_store  # noqa: E402


def main() -> int:
    manager = session_manager_module.manager
    root_id = "root-lock-test"
    root = {
        "id": root_id,
        "kind": "user",
        "name": "root",
        "created_at": "",
        "updated_at": "",
        "messages": [],
        "forks": [],
    }
    original_write = session_store.write_session_full
    try:
        session_manager_module._persist_pending[root_id] = root
        session_manager_module._persist_timer[root_id] = None

        def assert_unlocked(_sess, *, bump_updated_at=True):
            lock = manager._lock_for_root(root_id)
            acquired = lock.acquire(blocking=False)
            assert acquired, "write_session_full ran while root lock was held"
            lock.release()

        session_store.write_session_full = assert_unlocked
        manager._tail_persist(root_id)
        print("PASS: tail persist writes outside the root lock")
        return 0
    finally:
        session_store.write_session_full = original_write
        session_manager_module._persist_pending.pop(root_id, None)
        session_manager_module._persist_timer.pop(root_id, None)
        shutil.rmtree(os.environ["BETTER_CLAUDE_HOME"], ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
