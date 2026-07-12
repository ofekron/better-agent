from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading

HOME = tempfile.mkdtemp(prefix="ba-queue-loader-")
os.environ["BETTER_AGENT_HOME"] = HOME

import perf  # noqa: E402
import session_queue_projection as projection  # noqa: E402


def main() -> int:
    def seed() -> None:
        with projection._connect() as connection:
            connection.executemany(
                "INSERT INTO records(id, payload, sequence) VALUES(?, ?, ?)",
                [
                    (f"session-{index}", json.dumps({"id": f"session-{index}", "value": index}), index)
                    for index in range(200)
                ],
            )
            connection.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES('sequence', '200')"
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
            projection._ensure_loaded()
        except BaseException as exc:
            failures.append(exc)

    threads = [threading.Thread(target=load) for _ in range(8)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert len(failures) == 8, failures
    assert not projection._loaded
    assert not projection._loading
    assert projection._records == {}
    assert projection._load_future is None

    database.unlink()
    database.with_name(database.name + "-wal").unlink(missing_ok=True)
    database.with_name(database.name + "-shm").unlink(missing_ok=True)
    seed()
    projection._ensure_loaded()
    assert len(projection._records) == 200
    assert projection._records["session-199"]["value"] == 199
    with perf._lock:
        assert perf._counts["queue_projection.load.owner_started"]["total"] == 1
    assert projection._load_future is None
    projection.shutdown_loader()
    assert projection._load_executor is None
    print("PASS: queue projection cold decode has one isolated owner")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        projection.shutdown_loader()
        shutil.rmtree(HOME, ignore_errors=True)
