from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import psutil

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import _test_home


def _wait_until(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _launcher() -> int:
    import native_session_prompt_search as search
    import native_transcript_index as index

    state_root = Path(os.environ["BETTER_AGENT_HOME"]).resolve()
    if "--contender" in sys.argv:
        try:
            index.ensure_started()
        except RuntimeError:
            return 0
        return 1
    if "--gated" in sys.argv:
        os.environ[index._WORKER_TEST_START_GATE_ENV] = str(
            state_root / "release-worker-start"
        )
    transcript_root = state_root / "fixture-transcripts"
    transcript = transcript_root / "project" / "session.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    event = json.dumps({
            "type": "user",
            "uuid": "fixture-user",
            "timestamp": "2026-01-01T00:00:00Z",
            "message": {"role": "user", "content": "ownership-fixture-needle"},
        })
    transcript.write_text(
        ((event + "\n") * 50_000) if "--large" in sys.argv else event + "\n",
        encoding="utf-8",
    )
    index.set_roots_resolver(lambda: (
        lambda: [(transcript_root, "claude")],
        search._classify_root,
        search._candidate_from_match,
        search._is_native_transcript_path,
    ))
    index.ensure_started()
    identity_path = state_root / "native_transcript_index.sqlite3.worker.pid"
    ready_path = state_root / "launcher-ready.json"
    record = json.loads(identity_path.read_text(encoding="utf-8"))
    ready_path.write_text(
        json.dumps(record["worker"]),
        encoding="utf-8",
    )
    while True:
        time.sleep(1)


def _worker_indexed_fixture(state_root: Path) -> bool:
    import sqlite3

    db_path = state_root / "native_transcript_index.sqlite3"
    if not db_path.exists():
        return False
    try:
        connection = sqlite3.connect(
            f"file:{db_path}?mode=ro",
            uri=True,
            timeout=0.1,
        )
        try:
            row = connection.execute(
                "SELECT COUNT(*) FROM native_element_meta "
                "WHERE text_sha256 != ''"
            ).fetchone()
            roots = connection.execute(
                "SELECT DISTINCT path FROM native_file_state"
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error:
        return False
    fixture_root = state_root / "fixture-transcripts"
    return bool(
        row
        and row[0] == 1
        and roots
        and all(fixture_root in Path(path).parents for (path,) in roots)
    )


def _worker_cpu_seconds(pid: int) -> float:
    try:
        times = psutil.Process(pid).cpu_times()
        return float(times.user + times.system)
    except psutil.Error:
        return 0.0


def _run() -> int:
    home = _test_home.TestHome.acquire("ba-test-native-worker-lifecycle-")
    state_root = Path(home.path)
    env = os.environ.copy()
    launcher = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--launcher"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    worker_pid = 0
    owned_worker_pids: set[int] = set()
    try:
        ready_path = state_root / "launcher-ready.json"
        assert _wait_until(ready_path.exists), "launcher did not publish worker identity"
        worker_pid = int(json.loads(ready_path.read_text(encoding="utf-8"))["pid"])
        owned_worker_pids.add(worker_pid)
        assert psutil.pid_exists(worker_pid), "detached worker never started"
        contender = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--launcher", "--contender"],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert contender.returncode == 0, "second launcher adopted another owner's worker"
        assert _wait_until(lambda: _worker_indexed_fixture(state_root), 20.0), (
            "worker did not index only the explicit fixture root"
        )

        launcher.kill()
        launcher.wait(timeout=5)
        assert _wait_until(lambda: not psutil.pid_exists(worker_pid), 5.0), (
            f"detached worker {worker_pid} survived launcher death"
        )
        assert _wait_until(
            lambda: not (
                state_root / "native_transcript_index.sqlite3.worker.pid"
            ).exists()
        ), "owner-death cleanup left a stale worker record"

        ready_path = state_root / "launcher-ready.json"
        ready_path.unlink(missing_ok=True)
        gated_launcher = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--launcher",
                "--gated",
            ],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        launcher = gated_launcher
        assert _wait_until(ready_path.exists), "gated launcher did not publish worker"
        gated_pid = int(json.loads(ready_path.read_text(encoding="utf-8"))["pid"])
        owned_worker_pids.add(gated_pid)
        assert psutil.pid_exists(gated_pid), "gated worker never spawned"
        database = state_root / "native_transcript_index.sqlite3"
        database.unlink(missing_ok=True)
        gated_launcher.kill()
        gated_launcher.wait(timeout=5)
        assert _wait_until(lambda: not psutil.pid_exists(gated_pid), 5.0), (
            "delayed worker survived owner death before initialization"
        )
        assert not database.exists(), "delayed worker opened index after owner death"
        assert _wait_until(
            lambda: not (
                state_root / "native_transcript_index.sqlite3.worker.pid"
            ).exists()
        ), (
            "delayed owner-death cleanup left a stale worker record: "
            + (state_root / "native_transcript_index.sqlite3.worker.pid").read_text(
                encoding="utf-8"
            )
        )
        worker_pid = 0

        for suffix in ("", "-wal", "-shm"):
            (state_root / f"native_transcript_index.sqlite3{suffix}").unlink(
                missing_ok=True
            )
        ready_path.unlink(missing_ok=True)
        active_launcher = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--launcher",
                "--large",
            ],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        launcher = active_launcher
        assert _wait_until(ready_path.exists), "active launcher did not publish worker"
        active_pid = int(json.loads(ready_path.read_text(encoding="utf-8"))["pid"])
        owned_worker_pids.add(active_pid)
        assert _wait_until(
            lambda: database.exists() and _worker_cpu_seconds(active_pid) >= 0.25,
            10.0,
        ), "worker never entered sustained index work"
        assert not _worker_indexed_fixture(state_root), (
            "large index completed before active-work termination proof"
        )
        active_launcher.kill()
        active_launcher.wait(timeout=5)
        assert _wait_until(lambda: not psutil.pid_exists(active_pid), 5.0), (
            "worker survived owner death during active index work"
        )
        import sqlite3

        connection = sqlite3.connect(str(database))
        try:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
        finally:
            connection.close()
        assert integrity == ("ok",), "worker exit left the index database corrupt"

        import native_transcript_index as index

        dead_owner = index.ProcessIdentity(2_147_483_647, 0.0)
        assert index._run_worker_process(dead_owner, "not-json") == 0, (
            "dead-before-start owner did not short-circuit worker startup"
        )
        outside = Path(tempfile.mkdtemp(prefix="ba-worker-root-escape-"))
        try:
            try:
                index._configure_worker_roots(json.dumps([
                    {"path": str(outside), "tag": "claude"},
                ]))
            except ValueError:
                pass
            else:
                raise AssertionError("test worker accepted a root outside BETTER_AGENT_HOME")
        finally:
            outside.rmdir()
    finally:
        if launcher.poll() is None:
            launcher.kill()
            launcher.wait(timeout=5)
        for pid in owned_worker_pids:
            if not psutil.pid_exists(pid):
                continue
            process = psutil.Process(pid)
            process.kill()
            process.wait(timeout=5)
        home.release()
    print("native transcript worker lifecycle: PASS")
    return 0


if __name__ == "__main__":
    if "--launcher" in sys.argv:
        raise SystemExit(_launcher())
    raise SystemExit(_run())
