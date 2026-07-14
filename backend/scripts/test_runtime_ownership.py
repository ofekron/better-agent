import json
import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import _test_home

_TEST_HOME = _test_home.isolate(prefix="ba-runtime-ownership-")

import runtime_ownership
import session_store


def _session_payload(session_id: str) -> dict:
    return {
        "id": session_id,
        "name": "Runtime ownership test",
        "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00",
        "messages": [],
        "forks": [],
        "schema_version": session_store.SCHEMA_VERSION,
    }


def test_session_root_write_requires_runtime_writer_lock():
    script = """
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / "backend"))
import runtime_ownership
import session_store
payload = {
    "id": "without-lock",
    "name": "Runtime ownership test",
    "created_at": "2026-01-01T00:00:00",
    "updated_at": "2026-01-01T00:00:00",
    "messages": [],
    "forks": [],
    "schema_version": session_store.SCHEMA_VERSION,
}
try:
    session_store.write_session_full(payload)
except runtime_ownership.RuntimeOwnershipError:
    raise SystemExit(0)
raise SystemExit(1)
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[2],
        env={**os.environ, "BETTER_AGENT_HOME": tempfile.mkdtemp(prefix="ba-runtime-no-lock-")},
    )
    assert result.returncode == 0


def test_session_root_write_succeeds_with_runtime_writer_lock():
    with runtime_ownership.runtime_writer():
        session_store.write_session_full(_session_payload("with-lock"))
    path = Path(os.environ["BETTER_AGENT_HOME"]) / "sessions" / "with-lock.json"
    assert json.loads(path.read_text(encoding="utf-8"))["id"] == "with-lock"


def test_runtime_writer_lock_is_bound_to_current_home():
    with tempfile.TemporaryDirectory(prefix="ba-runtime-bound-first-") as first_home:
        with tempfile.TemporaryDirectory(prefix="ba-runtime-bound-second-") as second_home:
            script = """
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / "backend"))
import runtime_ownership
import session_store
assert runtime_ownership.acquire_runtime_writer_lock()
os.environ["BETTER_AGENT_HOME"] = os.environ["SECOND_HOME"]
payload = {
    "id": "wrong-home",
    "name": "Runtime ownership test",
    "created_at": "2026-01-01T00:00:00",
    "updated_at": "2026-01-01T00:00:00",
    "messages": [],
    "forks": [],
    "schema_version": session_store.SCHEMA_VERSION,
}
try:
    session_store.write_session_full(payload)
except runtime_ownership.RuntimeOwnershipError:
    raise SystemExit(0)
raise SystemExit(1)
"""
            subprocess.run(
                [sys.executable, "-c", script],
                cwd=Path(__file__).resolve().parents[2],
                env={**os.environ, "BETTER_AGENT_HOME": first_home, "SECOND_HOME": second_home},
                check=True,
            )


def test_runtime_writer_lock_rejects_second_live_holder_and_releases_on_kill():
    with tempfile.TemporaryDirectory(prefix="ba-runtime-crash-lock-") as home:
        script = """
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / "backend"))
import runtime_ownership
assert runtime_ownership.acquire_runtime_writer_lock()
print("ready", flush=True)
sys.stdin.read()
"""
        holder = subprocess.Popen(
            [sys.executable, "-c", script],
            cwd=Path(__file__).resolve().parents[2],
            env={**os.environ, "BETTER_AGENT_HOME": home},
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            assert holder.stdout is not None
            assert holder.stdout.readline().strip() == "ready"
            check_script = """
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / "backend"))
import runtime_ownership
raise SystemExit(0 if runtime_ownership.acquire_runtime_writer_lock() else 1)
"""
            blocked = subprocess.run(
                [sys.executable, "-c", check_script],
                cwd=Path(__file__).resolve().parents[2],
                env={**os.environ, "BETTER_AGENT_HOME": home},
            )
            assert blocked.returncode == 1
            blocking_timeout_script = """
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / "backend"))
import runtime_ownership
try:
    runtime_ownership.acquire_runtime_writer_lock(blocking=True, timeout_seconds=0.2)
except runtime_ownership.RuntimeOwnershipError as exc:
    text = str(exc)
    if "session-root.writer.lock" in text and "pid=" in text:
        raise SystemExit(0)
raise SystemExit(1)
"""
            timed_out = subprocess.run(
                [sys.executable, "-c", blocking_timeout_script],
                cwd=Path(__file__).resolve().parents[2],
                env={**os.environ, "BETTER_AGENT_HOME": home},
            )
            assert timed_out.returncode == 0
            if os.name == "nt":
                holder.terminate()
            else:
                holder.send_signal(signal.SIGKILL)
            holder.wait(timeout=10)
            subprocess.run(
                [sys.executable, "-c", check_script],
                cwd=Path(__file__).resolve().parents[2],
                env={**os.environ, "BETTER_AGENT_HOME": home},
                check=True,
            )
        finally:
            if holder.poll() is None:
                holder.kill()
                holder.wait(timeout=10)
