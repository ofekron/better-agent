"""Test desktop/supervisor.py — backend process lifecycle (GUI-free parts).

Run with:
    backend/.venv/bin/python desktop/test_supervisor.py
"""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_TMP_HOME = tempfile.mkdtemp(prefix="bc-test-supervisor-")
os.environ["BETTER_AGENT_HOME"] = _TMP_HOME
os.environ["BETTER_CLAUDE_HOME"] = _TMP_HOME

_HERE = Path(__file__).resolve().parent
_BACKEND = _HERE.parent / "backend"
for _p in (_HERE, _BACKEND):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from supervisor import (
    BackendSupervisor,
    backend_argv,
    kill_backend_lock_holder,
    kill_port_listeners,
    port_is_free,
)

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def test_backend_argv_dev() -> bool:
    """Dev (not frozen): argv runs `backend/app_entry.py --serve` on the
    current interpreter."""
    argv = backend_argv()
    expected_tail = ["app_entry.py", "--serve"]
    if argv[0] != sys.executable:
        print(f"  argv[0] expected {sys.executable}, got {argv[0]}")
        return False
    if Path(argv[1]).name != "app_entry.py" or argv[2] != "--serve":
        print(f"  expected ...{expected_tail}, got {argv}")
        return False
    if not Path(argv[1]).exists():
        print(f"  app_entry.py path does not exist: {argv[1]}")
        return False
    return True


def test_backend_argv_dev_node() -> bool:
    argv = backend_argv("node")
    if argv[0] != sys.executable:
        print(f"  argv[0] expected {sys.executable}, got {argv[0]}")
        return False
    if Path(argv[1]).name != "app_entry.py" or argv[2] != "--serve-node":
        print(f"  expected app_entry.py --serve-node, got {argv}")
        return False
    return True


def test_port_is_free() -> bool:
    """`port_is_free` reports a held port as not-free."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("0.0.0.0", 0))
        held = s.getsockname()[1]
        if port_is_free(held):
            print(f"  port {held} is held but reported free")
            return False
    return True


def test_kill_port_listeners_terminates_child_listener() -> bool:
    """`kill_port_listeners` terminates a process listening on a port so
    the desktop app can relaunch like run.sh."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        held_port = s.getsockname()[1]
    child = (
        "import socket, time\n"
        f"s=socket.socket(); s.bind(('0.0.0.0',{held_port})); "
        "s.listen(1); time.sleep(30)\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", child])
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and port_is_free(held_port):
            time.sleep(0.1)
        if port_is_free(held_port):
            print("  child listener did not bind in time")
            return False
        if not kill_port_listeners(held_port, timeout=1.0):
            print("  kill_port_listeners returned False")
            return False
        if not port_is_free(held_port):
            print("  port is still held after kill_port_listeners")
            return False
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            print("  child process still alive")
            return False
        return True
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_kill_backend_lock_holder_preserves_child_runners() -> bool:
    import supervisor as _sup
    from paths import ba_home  # noqa: E402

    child = (
        "import os, signal, subprocess, sys, time\n"
        "signal.signal(signal.SIGTERM, lambda *a: raise_system_exit())\n"
        "def raise_system_exit(): raise SystemExit(0)\n"
        "runner = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "print(runner.pid, flush=True)\n"
        "time.sleep(30)\n"
    )
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            child,
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    runner_pid = int(proc.stdout.readline().strip())
    orig_process_command = _sup._process_command
    lock_path = ba_home() / "backend.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(f"pid={proc.pid}\nhost=test\nba_home={ba_home()}\n", encoding="utf-8")
    try:
        _sup._process_command = lambda pid: f"{sys.executable} {_BACKEND / 'app_entry.py'} --serve"
        if not kill_backend_lock_holder(timeout=1.0):
            print("  kill_backend_lock_holder returned False")
            return False
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            print("  previous backend process still alive")
            return False
        try:
            os.kill(runner_pid, 0)
        except ProcessLookupError:
            print("  child runner was killed by backend lock cleanup")
            return False
        except OSError:
            pass
        return True
    finally:
        _sup._process_command = orig_process_command
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        try:
            os.kill(runner_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            pass


def _held_port() -> tuple[socket.socket, int]:
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("0.0.0.0", 0))
    holder.listen(1)
    return holder, holder.getsockname()[1]


def test_start_raises_on_held_port_without_prompt_handler() -> bool:
    holder, held_port = _held_port()
    try:
        sup = BackendSupervisor(port=held_port)
        try:
            sup.start()
        except RuntimeError as e:
            if str(held_port) not in str(e):
                print(f"  error should mention port {held_port}: {e}")
                return False
            return True
        print("  expected RuntimeError")
        return False
    finally:
        holder.close()


def test_start_uses_prompt_handler_alternate_port() -> bool:
    holder, held_port = _held_port()
    free_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    free_socket.bind(("0.0.0.0", 0))
    alternate_port = free_socket.getsockname()[1]
    free_socket.close()
    calls = []
    sup = BackendSupervisor(port=held_port)
    orig_spawn = sup._spawn_backend
    sup._spawn_backend = lambda: calls.append("spawned") or subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
    )
    try:
        sup.start(
            on_port_conflict=lambda port, listeners: {
                "action": "use_port",
                "port": alternate_port,
            }
        )
        if sup.port != alternate_port:
            print(f"  expected alternate port {alternate_port}, got {sup.port}")
            return False
        if sup.health_url != f"http://127.0.0.1:{alternate_port}/healthz":
            print(f"  health_url did not update: {sup.health_url}")
            return False
        if sup._env.get("BETTER_CLAUDE_BACKEND_PORT") != str(alternate_port):
            print("  backend port env was not updated")
            return False
        if sup._env.get("BETTER_CLAUDE_BACKEND_URL") != f"http://127.0.0.1:{alternate_port}":
            print("  backend URL env was not updated")
            return False
        if calls != ["spawned"]:
            print(f"  expected one spawn, got {calls}")
            return False
        return True
    finally:
        holder.close()
        sup._spawn_backend = orig_spawn
        if sup._proc is not None and sup._proc.poll() is None:
            sup._proc.kill()
            sup._proc.wait()


def test_restart_flag_detected_and_consumed() -> bool:
    """`restart_was_requested` returns True once when the flag file
    exists, deletes it, and returns False afterward."""
    from paths import ba_home  # noqa: E402  (BETTER_CLAUDE_HOME set above)
    flag = ba_home() / "restart_requested"
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.write_text("1")
    sup = BackendSupervisor()
    if not sup.restart_was_requested():
        print("  flag present but restart_was_requested() returned False")
        return False
    if flag.exists():
        print("  flag was not consumed")
        return False
    if sup.restart_was_requested():
        print("  restart_was_requested() returned True with no flag")
        return False
    return True


def test_wait_exit_returns_exit_code() -> bool:
    """`wait_exit` blocks until the backend process exits and returns its
    exit code."""
    sup = BackendSupervisor()
    sup._proc = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.exit(7)"],
    )
    code = sup.wait_exit()
    if code != 7:
        print(f"  wait_exit expected 7, got {code}")
        return False
    return True


def test_shutdown_signal_choice() -> bool:
    """`shutdown(kill_runners=True)` delivers SIGINT with an explicit
    kill flag; `False` delivers SIGTERM and clears stale kill flags."""
    child = (
        "import signal, sys, time\n"
        "signal.signal(signal.SIGINT, lambda *a: sys.exit(10))\n"
        "signal.signal(signal.SIGTERM, lambda *a: sys.exit(20))\n"
        "time.sleep(30)\n"
    )
    flag = Path(_TMP_HOME) / "kill_runners_requested"
    for kill_runners, expected in ((True, 10), (False, 20)):
        if kill_runners:
            try:
                flag.unlink()
            except FileNotFoundError:
                pass
        else:
            flag.write_text("stale", encoding="utf-8")
        sup = BackendSupervisor()
        sup._proc = subprocess.Popen([sys.executable, "-c", child])
        time.sleep(0.5)  # let the child install its signal handlers
        sup.shutdown(kill_runners=kill_runners)
        if sup._proc.returncode != expected:
            print(
                f"  kill_runners={kill_runners}: expected exit {expected}, "
                f"got {sup._proc.returncode}"
            )
            return False
        if kill_runners and not flag.exists():
            print("  kill_runners=True did not write kill flag")
            return False
        if not kill_runners and flag.exists():
            print("  kill_runners=False did not clear stale kill flag")
            return False
    return True


def test_restart_aborts_when_port_held() -> bool:
    """`restart()` returns False after a brief wait if `BACKEND_PORT` is
    held — and does NOT silently Popen a backend that would die on bind."""
    import supervisor as _sup
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("0.0.0.0", 0))
    held_port = holder.getsockname()[1]
    holder.listen(1)
    orig_port = _sup.BACKEND_PORT
    _sup.BACKEND_PORT = held_port
    try:
        sup = BackendSupervisor()
        started = time.monotonic()
        result = sup.restart()
        elapsed = time.monotonic() - started
    finally:
        _sup.BACKEND_PORT = orig_port
        holder.close()
    if result is not False:
        print(f"  expected False, got {result}")
        return False
    if elapsed > 5.0:
        print(f"  took too long ({elapsed:.1f}s)")
        return False
    if sup._proc is not None:
        print("  restart should not spawn a backend when the port is held")
        return False
    return True


def test_backend_argv_uses_target_checkout_interpreter() -> bool:
    root = Path(_TMP_HOME) / "target-checkout"
    posix_python = root / "backend" / ".venv" / "bin" / "python"
    app_entry = root / "backend" / "app_entry.py"
    posix_python.parent.mkdir(parents=True)
    posix_python.write_text("", encoding="utf-8")
    app_entry.write_text("", encoding="utf-8")
    argv = backend_argv(checkout=root)
    if argv[:2] != [str(posix_python.resolve()), str(app_entry.resolve())]:
        print(f"  target POSIX argv mismatch: {argv}")
        return False
    posix_python.unlink()
    windows_python = root / "backend" / ".venv" / "Scripts" / "python.exe"
    windows_python.parent.mkdir(parents=True)
    windows_python.write_text("", encoding="utf-8")
    argv = backend_argv(checkout=root)
    if argv[:2] != [str(windows_python.resolve()), str(app_entry.resolve())]:
        print(f"  target Windows argv mismatch: {argv}")
        return False
    return True


def test_source_switch_rejects_missing_frontend() -> bool:
    from daemonhost import pointer

    root = Path(_TMP_HOME) / "missing-dist-checkout"
    python = root / "backend" / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    (root / "backend" / "main.py").write_text("", encoding="utf-8")
    pointer.set_active(str(root), "missing-dist")
    sup = BackendSupervisor()
    try:
        sup._resolved_checkout()
    except RuntimeError as exc:
        if "no built frontend" not in str(exc):
            print(f"  unexpected rejection: {exc}")
            return False
    else:
        print("  checkout without frontend dist was accepted")
        return False
    pointer.revert("expected test rejection", "missing-dist")
    return True


TESTS = [
    ("backend_argv dev form runs app_entry.py --serve", test_backend_argv_dev),
    ("backend_argv dev node form runs app_entry.py --serve-node", test_backend_argv_dev_node),
    ("port_is_free reports a held port as not free", test_port_is_free),
    ("kill_port_listeners terminates a child listener",
     test_kill_port_listeners_terminates_child_listener),
    ("kill_backend_lock_holder preserves child runners",
     test_kill_backend_lock_holder_preserves_child_runners),
    ("start refuses a held port without a prompt handler",
     test_start_raises_on_held_port_without_prompt_handler),
    ("start can use a prompt-selected alternate port",
     test_start_uses_prompt_handler_alternate_port),
    ("restart flag is detected once then consumed",
     test_restart_flag_detected_and_consumed),
    ("wait_exit returns the backend exit code", test_wait_exit_returns_exit_code),
    ("shutdown sends SIGINT to kill runners, SIGTERM to keep them",
     test_shutdown_signal_choice),
    ("restart aborts when port is held instead of spawning a dead backend",
     test_restart_aborts_when_port_held),
    ("target checkout argv uses POSIX and Windows interpreters",
     test_backend_argv_uses_target_checkout_interpreter),
    ("source switch rejects a target without a built frontend",
     test_source_switch_rejects_missing_frontend),
]


def main_run() -> int:
    failed = 0
    try:
        for name, fn in TESTS:
            try:
                ok = fn()
            except Exception as e:
                ok = False
                import traceback
                traceback.print_exc()
                print(f"  exception: {e}")
            print(f"{PASS if ok else FAIL}  {name}")
            if not ok:
                failed += 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
    print()
    print(f"{failed} of {len(TESTS)} test(s) FAILED" if failed
          else f"all {len(TESTS)} tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main_run())
