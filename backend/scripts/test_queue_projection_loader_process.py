from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
from contextlib import closing
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="ba-queue-loader-")
os.environ["BETTER_AGENT_HOME"] = HOME
BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import perf  # noqa: E402
import session_queue_projection as projection  # noqa: E402


def main() -> int:
    runtime = projection.runtime()

    def seed() -> None:
        with closing(runtime._connect()) as connection:
            connection.executemany(
                "INSERT INTO records(id, payload, sequence) VALUES(?, ?, ?)",
                [
                    (
                        f"session-{index}",
                        json.dumps({
                            "id": f"session-{index}",
                            "value": index,
                        }),
                        index,
                    )
                    for index in range(200)
                ],
            )
            connection.execute(
                "INSERT OR REPLACE INTO metadata(key, value) "
                "VALUES('sequence', '200')"
            )
            connection.commit()

    seed()
    database = projection._database_path()
    database.with_name(database.name + "-wal").unlink(missing_ok=True)
    database.with_name(database.name + "-shm").unlink(missing_ok=True)
    database.write_bytes(b"not-a-sqlite-database")

    barrier = threading.Barrier(9)
    failures: list[BaseException] = []

    def load() -> None:
        try:
            barrier.wait()
            runtime.records()
        except BaseException as exc:
            failures.append(exc)

    threads = [threading.Thread(target=load) for _ in range(8)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    snapshot = runtime.debug_snapshot()
    assert len(failures) == 8, (failures, snapshot)
    assert not snapshot["loaded"]
    assert snapshot["records"] == 0
    assert not snapshot["loader_active"]

    database.unlink()
    database.with_name(database.name + "-wal").unlink(missing_ok=True)
    database.with_name(database.name + "-shm").unlink(missing_ok=True)
    seed()
    records = runtime.records()
    assert len(records) == 200
    assert next(
        record for record in records if record["id"] == "session-199"
    )["value"] == 199
    with perf._lock:
        assert perf._counts["queue_projection.load.owner_started"]["total"] == 1
    assert not runtime.debug_snapshot()["loader_active"]
    projection.shutdown(timeout=5.0)
    print("PASS: queue projection cold decode has one restartable owner")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        try:
            projection.shutdown(timeout=5.0)
        except Exception:
            pass
        shutil.rmtree(HOME, ignore_errors=True)
