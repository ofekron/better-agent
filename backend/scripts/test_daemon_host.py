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
import types
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
from daemonhost.paths import registry_path, state_path, switch_journal_path

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

# Recovery is independent of the source checkout still being present.
recovery_daemon_root = daemon.root
shutil.rmtree(dh_install.previous_dir(recovery_daemon_root), ignore_errors=True)
dh_install.current_dir(recovery_daemon_root).rename(dh_install.previous_dir(recovery_daemon_root))
daemon.spec = {**daemon.spec, "source_root": str(Path(_TMP) / "missing-source")}
assert host._ensure_installed(daemon) is True
assert dh_install.current_dir(recovery_daemon_root).is_dir()
with (dh_install.current_dir(recovery_daemon_root) / "corrupt").open("w", encoding="utf-8") as handle:
    handle.write("corrupt")
assert host._ensure_installed(daemon) is False, "corrupt current must not spawn without source"

# Interrupted install swaps recover the predecessor before any new install.
recovery_root = Path(_TMP) / "install-recovery"
(recovery_root / "previous").mkdir(parents=True)
(recovery_root / "previous" / "marker").write_text("previous", encoding="utf-8")
dh_install.seal_copy(recovery_root / "previous")
assert dh_install.recover_current(recovery_root) is True
assert (recovery_root / "current" / "marker").read_text(encoding="utf-8") == "previous"
shutil.rmtree(recovery_root / "current")
(recovery_root / "last_good").mkdir()
(recovery_root / "last_good" / "marker").write_text("last-good", encoding="utf-8")
dh_install.seal_copy(recovery_root / "last_good")
assert dh_install.recover_current(recovery_root) is True
assert (recovery_root / "current" / "marker").read_text(encoding="utf-8") == "last-good"

atomic_root = Path(_TMP) / "atomic-last-good"
(atomic_root / "current").mkdir(parents=True)
(atomic_root / "current" / "marker").write_text("good", encoding="utf-8")
dh_install.seal_copy(atomic_root / "current")
write_json(atomic_root / "install.json", {"promoted": False})
dh_install.promote_last_good(atomic_root)
(atomic_root / "current" / "marker").write_text("bad", encoding="utf-8")
assert dh_install.rollback_to_last_good(atomic_root) is True
assert (atomic_root / "current" / "marker").read_text(encoding="utf-8") == "good"
assert not (atomic_root / "rollback_staging").exists()
assert not dh_install.previous_dir(atomic_root).exists()

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
pointer.confirm_healthy(dev)
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
pointer.confirm_healthy(main)
assert pointer.read()["status"] == "active"
pointer.confirm_healthy(main)  # idempotent
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

# --- RC2: a failed switch must not strand every launch on the broken target ---
dev2 = _make_checkout("co-dev2")
main2 = _make_checkout("co-main2")

# First-ever switch dev2->main2 fails to start. With no runnable previous the
# launcher marks it failed (the exact state that trapped the running stack on a
# 1428-commit-stale line). resolve() must NOT keep returning the broken target.
pointer.set_active(main2, "req-fail")
pointer.revert("backend failed to become healthy")  # empty previous -> mark failed
assert pointer.read()["status"] == "failed", pointer.read()
assert pointer.read()["active"] == str(Path(main2).resolve())
assert pointer.resolve(dev2) == dev2, "failed switch must fall back to the launcher default, not the broken target"

# The launcher then comes up on dev2 (the default) and reconciles the pointer to
# reality so resolve()/UI stop reflecting the dead switch — no manual repair.
pointer.confirm_healthy(dev2)
data = pointer.read()
assert data["status"] == "active" and data["active"] == str(Path(dev2).resolve()), data
assert pointer.resolve("x") == str(Path(dev2).resolve())

# confirm_healthy also reconciles a pointer whose active disagrees with what the
# backend actually came up from, even when status was not 'failed'.
pointer.set_active(main2, "req-mismatch")
pointer.mark_result("active")  # pretend main2 came up, pointer says active=main2
pointer.confirm_healthy(dev2)  # but the backend is really on dev2
assert pointer.read()["active"] == str(Path(dev2).resolve()), pointer.read()

# A 'reverted' pointer already matching the running checkout is preserved so the
# UI can still show the revert note.
pointer.set_active(main2, "req-rev")
pointer.revert("boom")  # previous is dev2 (runnable) -> reverted, active=dev2
assert pointer.read()["status"] == "reverted"
pointer.confirm_healthy(dev2)
assert pointer.read()["status"] == "reverted", "confirm_healthy must not clobber a matching revert"

# One request owns the switching state. Exact retries are idempotent; another
# request and stale launcher outcomes cannot alter it.
first = pointer.set_active(main2, "req-serialized")
assert pointer.set_active(main2, "req-serialized") == first
try:
    pointer.set_active(dev2, "req-concurrent")
    raise AssertionError("a second in-flight request must be rejected")
except ValueError as exc:
    assert "already in flight" in str(exc)
try:
    pointer.confirm_healthy(main2, "req-stale")
    raise AssertionError("stale health confirmation must be rejected")
except ValueError:
    pass
assert pointer.revert_if_switching("stale crash", "req-stale") is False
assert pointer.read()["request_id"] == "req-serialized"
pointer.confirm_healthy(main2, "req-serialized")

# Root validation rejects traversal and a symlink target even when both resolve
# to an otherwise runnable checkout.
traversal = str(Path(dev2).parent / "ignored" / ".." / Path(dev2).name)
for unsafe in (traversal, str(Path(_TMP) / "checkout-link")):
    if unsafe.endswith("checkout-link"):
        Path(unsafe).symlink_to(dev2, target_is_directory=True)
    try:
        pointer.set_active(unsafe, "req-unsafe")
        raise AssertionError(f"unsafe checkout path accepted: {unsafe}")
    except ValueError:
        pass

# A crash after the durable intent write is recovered synchronously when a new
# daemonhost starts. The exact request is rolled back and journaled before any
# extension daemons are reconciled.
pointer.set_active(dev2, "req-crash-window")
assert pointer.read()["status"] == "switching"
restarted_host = DaemonHost(poll_interval=0)
assert pointer.read()["status"] == "reverted"
assert pointer.read()["active"] == str(Path(main2).resolve())
journal = switch_journal_path().read_text(encoding="utf-8")
assert '"event": "switch_requested"' in journal
assert '"event": "startup_reconciled"' in journal
assert '"request_id": "req-crash-window"' in journal
restarted_host.stop()

# Windows parity is covered without mutating os.name: exercise the msvcrt lock
# protocol with a deterministic module fake and accept a Scripts/python.exe
# checkout layout.
calls: list[tuple[int, int, int]] = []
fake_msvcrt = types.SimpleNamespace(
    LK_LOCK=1,
    LK_UNLCK=2,
    locking=lambda fd, mode, length: calls.append((fd, mode, length)),
)
prior_msvcrt = sys.modules.get("msvcrt")
sys.modules["msvcrt"] = fake_msvcrt
try:
    class FakeHandle:
        def seek(self, _offset: int) -> None:
            pass

        def fileno(self) -> int:
            return 17

    with pointer._platform_lock(FakeHandle(), "nt"):
        assert calls == [(17, fake_msvcrt.LK_LOCK, 1)]
finally:
    if prior_msvcrt is None:
        sys.modules.pop("msvcrt", None)
    else:
        sys.modules["msvcrt"] = prior_msvcrt
assert calls[-1] == (17, fake_msvcrt.LK_UNLCK, 1)

windows_checkout = Path(_TMP) / "co-windows"
(windows_checkout / "backend" / ".venv" / "Scripts").mkdir(parents=True)
(windows_checkout / "backend" / "main.py").write_text("", encoding="utf-8")
(windows_checkout / "backend" / ".venv" / "Scripts" / "python.exe").write_text("", encoding="utf-8")
assert pointer._is_runnable_checkout(str(windows_checkout))

print("OK test_daemon_host")
