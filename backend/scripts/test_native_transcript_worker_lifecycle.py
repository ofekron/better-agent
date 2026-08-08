from __future__ import annotations

import json
import os
import shutil
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
    if "--reader" in sys.argv:
        result = index.run_readonly_sql(
            "SELECT COUNT(*) AS count FROM native_element_fts"
        )
        if result.get("error") is None and result.get("rows") == [[1]]:
            return 0
        print(json.dumps(result, sort_keys=True), file=sys.stderr)
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


def test_supervised_reader_uses_foreign_owned_worker() -> None:
    home = _test_home.TestHome.acquire("ba-test-native-worker-reader-")
    state_root = Path(home.path)
    env = os.environ.copy()
    launcher = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--launcher"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    worker_pid = 0
    try:
        ready_path = state_root / "launcher-ready.json"
        assert _wait_until(ready_path.exists), "launcher did not publish worker identity"
        worker_pid = int(json.loads(ready_path.read_text(encoding="utf-8"))["pid"])
        assert _wait_until(lambda: _worker_indexed_fixture(state_root), 20.0), (
            "worker did not index the reader fixture"
        )

        contender = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--launcher", "--contender"],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        reader = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--launcher", "--reader"],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

        assert contender.returncode == 0, "second launcher adopted another owner's worker"
        assert reader.returncode == 0, reader.stderr
        assert psutil.pid_exists(worker_pid), "reader stopped the backend-owned worker"
        record_path = state_root / "native_transcript_index.sqlite3.worker.pid"
        record = json.loads(
            record_path.read_text(encoding="utf-8")
        )
        assert int(record["owner"]["pid"]) == launcher.pid
        assert int(record["worker"]["pid"]) == worker_pid
    finally:
        if launcher.poll() is None:
            launcher.kill()
            launcher.wait(timeout=5)
        if worker_pid:
            try:
                process = psutil.Process(worker_pid)
            except psutil.NoSuchProcess:
                pass
            else:
                process.kill()
                process.wait(timeout=5)
        home.release()


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


# ─── worker root isolation (fail-closed) ───────────────────────────────────
# Each named test owns a fresh isolated BETTER_AGENT_HOME and cleans it up.
# `_configure_worker_roots` is the exact gate the detached worker runs at
# startup, so exercising it directly also proves subprocess-side rejection.

def _acquire_index():
    import native_session_prompt_search as search
    import native_transcript_index as index
    return search, index


def _write_user_transcript(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "type": "user",
            "uuid": "u-" + path.stem,
            "timestamp": "2026-01-01T00:00:00Z",
            "message": {"role": "user", "content": content},
        })
        + "\n",
        encoding="utf-8",
    )


def _test_in_root_symlink_escape_not_indexed() -> int:
    """Headline: a symlink planted inside a native root must never let the
    full-scan discovery walk (``_scan_full_batch``'s ``os.scandir`` traversal)
    reach a foreign file outside that root. A directory symlink is never
    descended into (``entry.is_dir(follow_symlinks=False)`` is False for it);
    a FILE symlink is caught by the walk's own resolve()-under-root guard."""
    home = _test_home.TestHome.acquire("ba-test-root-symlink-escape-")
    state_root = Path(os.environ["BETTER_AGENT_HOME"]).resolve()
    try:
        search, index = _acquire_index()
        transcript_root = state_root / "transcripts"
        transcript_root.mkdir(parents=True, exist_ok=True)
        _write_user_transcript(transcript_root / "good.jsonl", "good-needle")

        outside = Path(tempfile.mkdtemp(prefix="ba-root-escape-out-"))
        try:
            escaped = outside / "escaped.jsonl"
            _write_user_transcript(escaped, "escaped-needle")
            (transcript_root / "link.jsonl").symlink_to(escaped)
            (transcript_root / "dirlink").symlink_to(outside)

            index.set_roots_resolver(lambda: (
                lambda: [(transcript_root, "claude")],
                search._classify_root,
                search._candidate_from_match,
                search._is_native_transcript_path,
            ))
            try:
                index.refresh_once(full=True)
                escaped_hits = index.search_rows(["escaped-needle"], limit=10)
                good_hits = index.search_rows(["good-needle"], limit=10)
            finally:
                index.set_roots_resolver(None)
        finally:
            shutil.rmtree(outside, ignore_errors=True)
    finally:
        home.release()
    if not escaped_hits and good_hits:
        print("in_root_symlink_escape_not_indexed: PASS")
        return 0
    print(f"in_root_symlink_escape_not_indexed: FAIL "
          f"(escaped_hits={escaped_hits}, good_hits={good_hits})")
    return 1


def _test_escape_root_outside_home_rejected_at_subprocess() -> int:
    home = _test_home.TestHome.acquire("ba-test-root-outside-home-")
    try:
        _search, index = _acquire_index()
        outside = Path(tempfile.mkdtemp(prefix="ba-root-out-"))
        try:
            raw = json.dumps([{"path": str(outside), "tag": "claude"}])
            try:
                index._configure_worker_roots(raw)
            except ValueError:
                print("escape_root_outside_home_rejected_at_subprocess: PASS")
                return 0
        finally:
            shutil.rmtree(outside, ignore_errors=True)
    finally:
        home.release()
    print("escape_root_outside_home_rejected_at_subprocess: FAIL (outside root accepted)")
    return 1


def _test_parent_traversal_root_rejected() -> int:
    home = _test_home.TestHome.acquire("ba-test-root-traversal-")
    state_root = Path(os.environ["BETTER_AGENT_HOME"]).resolve()
    try:
        _search, index = _acquire_index()
        outside = Path(tempfile.mkdtemp(prefix="ba-root-trav-out-"))
        try:
            kids = state_root / "kids"
            kids.mkdir(parents=True, exist_ok=True)
            # absolute path with '..' that resolves to a sibling dir outside home
            if outside.resolve().parent != state_root.parent:
                print("parent_traversal_root_rejected: FAIL (fixture layout)")
                return 1
            traversal = kids / ".." / ".." / outside.name
            raw = json.dumps([{"path": str(traversal), "tag": "claude"}])
            try:
                index._configure_worker_roots(raw)
            except ValueError:
                print("parent_traversal_root_rejected: PASS")
                return 0
        finally:
            shutil.rmtree(outside, ignore_errors=True)
    finally:
        home.release()
    print("parent_traversal_root_rejected: FAIL (traversal root accepted)")
    return 1


def _test_symlink_root_escape_rejected() -> int:
    home = _test_home.TestHome.acquire("ba-test-root-symlink-")
    state_root = Path(os.environ["BETTER_AGENT_HOME"]).resolve()
    try:
        _search, index = _acquire_index()
        outside = Path(tempfile.mkdtemp(prefix="ba-root-sym-out-"))
        try:
            link = state_root / "escape-link"
            link.symlink_to(outside)
            raw = json.dumps([{"path": str(link), "tag": "claude"}])
            try:
                index._configure_worker_roots(raw)
            except ValueError:
                print("symlink_root_escape_rejected: PASS")
                return 0
        finally:
            shutil.rmtree(outside, ignore_errors=True)
    finally:
        home.release()
    print("symlink_root_escape_rejected: FAIL (symlink root accepted)")
    return 1


def _test_serialize_rejects_escaped_root() -> int:
    """Plan B defense-in-depth: `_serialize_worker_roots` must refuse to emit a
    root the worker would reject, so an escaped root can never reach argv."""
    home = _test_home.TestHome.acquire("ba-test-root-serialize-")
    try:
        search, index = _acquire_index()
        outside = Path(tempfile.mkdtemp(prefix="ba-root-ser-out-"))
        try:
            index.set_roots_resolver(lambda: (
                lambda: [(outside, "claude")],
                search._classify_root,
                search._candidate_from_match,
                search._is_native_transcript_path,
            ))
            try:
                try:
                    index._serialize_worker_roots()
                except ValueError:
                    print("serialize_rejects_escaped_root: PASS")
                    return 0
            finally:
                index.set_roots_resolver(None)
        finally:
            shutil.rmtree(outside, ignore_errors=True)
    finally:
        home.release()
    print("serialize_rejects_escaped_root: FAIL (escaped root serialized)")
    return 1


def _test_cleanup_owns_worker_and_temp_home() -> int:
    home = _test_home.TestHome.acquire("ba-test-cleanup-owns-")
    state_root = Path(os.environ["BETTER_AGENT_HOME"]).resolve()
    home_path = home.path
    worker_pid = 0
    ok_record = ok_live = ok_dead = False
    try:
        search, index = _acquire_index()
        transcript_root = state_root / "transcripts"
        transcript_root.mkdir(parents=True, exist_ok=True)
        _write_user_transcript(transcript_root / "s.jsonl", "cleanup-needle")
        index.set_roots_resolver(lambda: (
            lambda: [(transcript_root, "claude")],
            search._classify_root,
            search._candidate_from_match,
            search._is_native_transcript_path,
        ))
        try:
            index.ensure_started()
            record_path = state_root / "native_transcript_index.sqlite3.worker.pid"
            ok_record = _wait_until(record_path.exists, 10.0)
            if ok_record:
                worker_pid = int(
                    json.loads(record_path.read_text(encoding="utf-8"))["worker"]["pid"]
                )
            ok_live = bool(worker_pid) and psutil.pid_exists(worker_pid)
            index.shutdown()
            index.set_roots_resolver(None)
            ok_dead = (
                _wait_until(lambda: not psutil.pid_exists(worker_pid), 5.0)
                if worker_pid else True
            )
        finally:
            if worker_pid and psutil.pid_exists(worker_pid):
                psutil.Process(worker_pid).kill()
    finally:
        home.release()
    ok_removed = not Path(home_path).exists()
    if ok_record and ok_live and ok_dead and ok_removed:
        print("cleanup_owns_worker_and_temp_home: PASS")
        return 0
    print(f"cleanup_owns_worker_and_temp_home: FAIL "
          f"(record={ok_record}, live={ok_live}, dead={ok_dead}, removed={ok_removed})")
    return 1


_ROOT_ISOLATION_TESTS = {
    "in_root_symlink_escape_not_indexed": _test_in_root_symlink_escape_not_indexed,
    "escape_root_outside_home_rejected_at_subprocess": _test_escape_root_outside_home_rejected_at_subprocess,
    "parent_traversal_root_rejected": _test_parent_traversal_root_rejected,
    "symlink_root_escape_rejected": _test_symlink_root_escape_rejected,
    "serialize_rejects_escaped_root": _test_serialize_rejects_escaped_root,
    "cleanup_owns_worker_and_temp_home": _test_cleanup_owns_worker_and_temp_home,
}


def _run_root_isolation() -> int:
    failures: list[str] = []
    for name, fn in _ROOT_ISOLATION_TESTS.items():
        if fn():
            failures.append(name)
    if failures:
        print(f"\nroot isolation: FAIL ({len(failures)} failed: {failures})")
        return 1
    print("\nroot isolation: PASS (all)")
    return 0


if __name__ == "__main__":
    if "--launcher" in sys.argv:
        raise SystemExit(_launcher())
    if "--test" in sys.argv:
        name = sys.argv[sys.argv.index("--test") + 1]
        raise SystemExit(_ROOT_ISOLATION_TESTS[name]())
    if "--root-isolation" in sys.argv:
        raise SystemExit(_run_root_isolation())
    raise SystemExit(_run())
