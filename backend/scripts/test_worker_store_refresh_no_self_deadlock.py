import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path

import _test_home
_test_home.isolate("bc_test_worker_store_")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import session_store  # noqa: E402
from stores import worker_store  # noqa: E402


def main() -> int:
    try:
        session_store._summary_index_loaded = True
        finished = threading.Event()
        error = []

        def run():
            try:
                worker_store.upsert_worker(
                    "/tmp/project",
                    "worker-session",
                    "native",
                    "agent-session",
                )
            except Exception as exc:
                error.append(exc)
            finally:
                finished.set()

        thread = threading.Thread(target=run)
        thread.start()
        thread.join(timeout=2)
        assert finished.is_set(), "worker_store.upsert_worker self-deadlocked"
        assert not error, error[0] if error else None
        print("PASS: worker_store summary refresh does not self-deadlock")
        return 0
    finally:
        shutil.rmtree(os.environ["BETTER_CLAUDE_HOME"], ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
