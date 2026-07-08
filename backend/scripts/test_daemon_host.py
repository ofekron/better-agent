"""Locks the platform daemon host protocol: selftest-gated install, spawn,
crash restarts with cap, last-known-good rollback, retire-on-registry-removal,
and the active-checkout pointer semantics (resolve/revert/confirm) that the
launchers rely on for line switching.

Run: backend/.venv/bin/python backend/scripts/test_daemon_host.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(BACKEND.parent))

import paths

_TMP = tempfile.mkdtemp(prefix="ba-daemon-host-")
paths.engage_test_home(_TMP)

from daemonhost import install as dh_install
from daemonhost import pointer
from daemonhost.host import DaemonHost
from daemonhost.jsonio import read_json, write_json
from daemonhost.paths import registry_path, state_path

GOOD_DAEMON = '''
import sys, time
if "--selftest" in sys.argv[1:]:
    sys.exit(0)
while True:
    time.sleep(1)
'''

BAD_SELFTEST_DAEMON = '''
import sys
sys.exit(1)
'''

CRASH_DAEMON = '''
import sys
if "--selftest" in sys.argv[1:]:
    sys.exit(0)
sys.exit(3)
'''


def _write_source(source: Path, body: str) -> None:
    (source / "daemon").mkdir(parents=True, exist_ok=True)
    (source / "daemon" / "worker.py").write_text(body, encoding="utf-8")


def _registry(source: Path, max_restarts: int = 1) -> None:
    write_json(
        registry_path(),
        {
            "daemons": {
                "test.ext:worker": {
                    "extension_id": "test.ext",
                    "name": "worker",
                    "module": "daemon.worker",
                    "lifecycle": "supervisor",
                    "restart_policy": {"max_restarts": max_restarts, "backoff_seconds": 1},
                    "env_allowlist": [],
                    "ports": [],
                    "source_root": str(source),
                }
            }
        },
    )


source = Path(_TMP) / "src"
_write_source(source, GOOD_DAEMON)
_registry(source)

host = DaemonHost(poll_interval=0.1)

# Install + spawn.
host.reconcile_once()
state = read_json(state_path())["daemons"]["test.ext:worker"]
assert state["status"] == "running" and state["pid"], state
daemon = host._daemons["test.ext:worker"]
assert dh_install.current_dir(daemon.root).is_dir()

# Selftest-rejected update: current copy keeps running untouched.
old_hash = dh_install.install_meta(daemon.root)["source_hash"]
_write_source(source, BAD_SELFTEST_DAEMON)
running_pid = state["pid"]
host._stop_proc(daemon)  # quiescent window
host.reconcile_once()
state = read_json(state_path())["daemons"]["test.ext:worker"]
assert dh_install.install_meta(daemon.root)["source_hash"] == old_hash, "rejected install must not replace current"
assert state["install_error"].startswith("install rejected"), state
assert state["status"] == "running" and state["pid"] != running_pid, "old copy must respawn"

# Healthy-cycle promotion to last_good (forced clock: pretend it ran long).
daemon = host._daemons["test.ext:worker"]
daemon.started_at = time.time() - 3600
host.reconcile_once()
assert dh_install.last_good_dir(daemon.root).is_dir(), "healthy cycle must promote last_good"
assert dh_install.install_meta(daemon.root)["promoted"] is True

# Crash loop: new (unpromoted) bad copy rolls back to last_good.
_write_source(source, CRASH_DAEMON)
host._stop_proc(daemon)
host.reconcile_once()  # installs crash daemon (selftest passes), spawns it
meta = dh_install.install_meta(daemon.root)
assert meta["promoted"] is False, "fresh install must not be promoted yet"
deadline = time.time() + 10
while time.time() < deadline:
    host.reconcile_once()
    state = read_json(state_path())["daemons"]["test.ext:worker"]
    if state["status"] == "running" and dh_install.install_meta(daemon.root).get("rolled_back_at"):
        break
    time.sleep(0.1)
assert dh_install.install_meta(daemon.root).get("rolled_back_at"), "crash must roll back to last_good"

# Registry entry removed -> process stopped and state cleared.
write_json(registry_path(), {"daemons": {}})
host.reconcile_once()
assert "test.ext:worker" not in read_json(state_path())["daemons"]

host.stop()
host._shutdown_all()

# --- pointer semantics ------------------------------------------------------
def _make_checkout(name: str) -> str:
    root = Path(_TMP) / name
    (root / "backend" / ".venv" / "bin").mkdir(parents=True, exist_ok=True)
    (root / "backend" / "main.py").write_text("", encoding="utf-8")
    (root / "backend" / ".venv" / "bin" / "python").write_text("", encoding="utf-8")
    return str(root)


dev = _make_checkout("co-dev")
main = _make_checkout("co-main")

# No pointer: resolve falls back to the launcher default.
assert pointer.resolve(dev) == dev

# Switch intent: resolve returns the target; revert-if-switching flips back.
pointer.set_active(dev, "req-0")
pointer.confirm_healthy()
pointer.set_active(main, "req-1")
assert pointer.resolve(dev) == str(Path(main).resolve())
assert pointer.read()["status"] == "switching"
assert pointer.revert_if_switching("backend failed to become healthy") is True
data = pointer.read()
assert data["status"] == "reverted", data
assert data["active"] == str(Path(dev).resolve()), "revert must flip active back to previous"
assert pointer.resolve("fallback") == str(Path(dev).resolve())

# Revert fires at most once (status is no longer 'switching').
assert pointer.revert_if_switching("again") is False

# confirm_healthy completes only an in-flight switch.
pointer.set_active(main, "req-2")
pointer.confirm_healthy()
assert pointer.read()["status"] == "active"
pointer.confirm_healthy()  # idempotent
assert pointer.read()["status"] == "active"

# Non-runnable target is rejected (fail closed).
try:
    pointer.set_active(str(Path(_TMP) / "nope"), "req-3")
    raise AssertionError("set_active must reject a non-runnable checkout")
except ValueError:
    pass

# Broken active checkout: resolve falls back to default.
shutil.rmtree(Path(main) / "backend")
assert pointer.resolve(dev) == dev

print("OK test_daemon_host")
