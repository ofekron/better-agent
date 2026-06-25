from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path

os.environ["BETTER_AGENT_HOME"] = tempfile.mkdtemp(prefix="session-store-durability-")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from grouped_durability_writer import BatchSnapshot, GroupedDurabilityWriter
import session_store
from paths import ba_home


class AckBarrier:
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.snapshots: list[BatchSnapshot] = []

    def hook(self, phase: str, snapshot: BatchSnapshot) -> None:
        if phase != "before_ack":
            return
        self.snapshots.append(snapshot)
        self.entered.set()
        if not self.release.wait(3):
            raise TimeoutError("test did not release durability acknowledgement")

    def reset(self) -> None:
        self.entered.clear()
        self.release.clear()


def _run_blocked(barrier: AckBarrier, operation) -> threading.Thread:
    failure: list[BaseException] = []

    def run() -> None:
        try:
            operation()
        except BaseException as exc:
            failure.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    assert barrier.entered.wait(3), "operation did not reach acknowledgement barrier"
    assert thread.is_alive(), "operation returned before durability acknowledgement"
    barrier.release.set()
    thread.join(3)
    assert not thread.is_alive(), "operation did not finish after durability acknowledgement"
    assert not failure, failure
    barrier.reset()
    return thread


def main() -> None:
    home = Path(os.environ["BETTER_AGENT_HOME"])
    barrier = AckBarrier()
    writer = GroupedDurabilityWriter(max_batch_age_s=0, crash_hook=barrier.hook)
    session_store._durability_writer = writer
    root_id = "durable-root"
    root = {
        "id": root_id,
        "_schema_version": session_store.SCHEMA_VERSION,
        "parent_session_id": None,
        "name": "durability",
        "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00",
        "forks": [],
        "messages": [],
        "queued_prompts": [],
    }

    _run_blocked(
        barrier,
        lambda: session_store.write_session_full(root, bump_updated_at=False),
    )
    root_path = ba_home() / "sessions" / f"{root_id}.json"
    assert json.loads(root_path.read_text())["id"] == root_id
    assert barrier.snapshots[-1].targets == (root_path,)

    fingerprint = (1, 0, 0, root_path.stat().st_size, 7)
    _run_blocked(
        barrier,
        lambda: session_store._write_index_sidecar(
            fingerprint,
            {"fork": root_id},
            {root_id: {"fork"}},
            {root_id: session_store._session_file_signature(root_path)},
        ),
    )
    sidecar = ba_home() / "sessions" / ".fork-index.json"
    assert json.loads(sidecar.read_text())["fork_index"] == {"fork": root_id}
    assert barrier.snapshots[-1].targets == (sidecar,)

    _run_blocked(barrier, lambda: session_store.delete_session(root_id))
    assert not root_path.exists()

    barrier.release.set()
    session_store._schedule_index_sidecar_write(
        (0, 0, 0, 0, 0),
        {"shutdown-fork": "shutdown-root"},
        {"shutdown-root": {"shutdown-fork"}},
        {},
    )
    session_store.shutdown_durability_writer()
    assert session_store._durability_writer is None
    assert json.loads(sidecar.read_text())["fork_index"] == {
        "shutdown-fork": "shutdown-root"
    }
    replacement = session_store._get_durability_writer()
    assert replacement is not writer
    session_store.shutdown_durability_writer()
    shutil.rmtree(home, ignore_errors=True)
    print("session store grouped durability: ok")


if __name__ == "__main__":
    main()
