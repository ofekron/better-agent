"""Test desktop/supervisor.py — backend process lifecycle (GUI-free parts).

Run with:
    backend/.venv/bin/python desktop/test_supervisor.py
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
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

import supervisor as supervisor_module
from supervisor import (
    BackendSupervisor,
    backend_argv,
    kill_backend_lock_holder,
    kill_port_listeners,
    port_is_free,
)
import dependency_plan
import backend.dependency_plan as backend_dependency_plan

dependency_plan.verified_active_env = dependency_plan.active_env
backend_dependency_plan.verified_active_env = backend_dependency_plan.active_env

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _source_checkout(name: str, *, interpreter: Path | None = None) -> tuple[Path, Path]:
    root = Path(_TMP_HOME) / name
    python = root / "backend" / ".venvs" / "test" / "bin" / "python"
    python.parent.mkdir(parents=True, exist_ok=True)
    if interpreter is None:
        python.write_text("", encoding="utf-8")
    else:
        python.symlink_to(interpreter)
    (root / "backend" / ".active-venv").write_text(".venvs/test", encoding="utf-8")
    (root / "backend" / "app_entry.py").write_text("", encoding="utf-8")
    (root / "backend" / "main.py").write_text("", encoding="utf-8")
    return root, python


def test_backend_argv_dev() -> bool:
    """Dev (not frozen): argv runs `backend/app_entry.py --serve` on the
    current interpreter."""
    checkout, python = _source_checkout("argv-dev", interpreter=Path(sys.executable))
    argv = backend_argv(checkout=checkout)
    expected_tail = ["app_entry.py", "--serve"]
    if Path(argv[0]).resolve() != python.resolve():
        print(f"  argv[0] expected {python}, got {argv[0]}")
        return False
    if Path(argv[1]).name != "app_entry.py" or argv[2] != "--serve":
        print(f"  expected ...{expected_tail}, got {argv}")
        return False
    if not Path(argv[1]).exists():
        print(f"  app_entry.py path does not exist: {argv[1]}")
        return False
    return True


def test_backend_argv_dev_node() -> bool:
    checkout, python = _source_checkout("argv-node", interpreter=Path(sys.executable))
    argv = backend_argv("node", checkout=checkout)
    if Path(argv[0]).resolve() != python.resolve():
        print(f"  argv[0] expected {python}, got {argv[0]}")
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
        if sup.health_url != f"http://127.0.0.1:{alternate_port}/readyz":
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
    flag.write_text("1" * 32)
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


def test_stale_restart_flag_is_consumed_without_refresh() -> bool:
    from paths import ba_home

    flag = ba_home() / "restart_requested"
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.write_text("a" * 32, encoding="utf-8")
    sup = BackendSupervisor()
    sup._generation_started_wall_time = time.time() + 1
    if sup.restart_was_requested():
        print("  stale restart flag was classified as refresh")
        return False
    if flag.exists():
        print("  stale restart flag was not consumed")
        return False
    return True


def test_restart_flag_symlink_is_rejected() -> bool:
    from paths import ba_home

    target = ba_home() / "restart-target"
    target.write_text("b" * 32, encoding="utf-8")
    flag = ba_home() / "restart_requested"
    flag.symlink_to(target)
    sup = BackendSupervisor()
    try:
        sup.restart_was_requested()
    except OSError:
        pass
    else:
        print("  restart symlink was accepted")
        return False
    finally:
        flag.unlink(missing_ok=True)
    if target.read_text(encoding="utf-8") != "b" * 32:
        print("  restart symlink target was modified")
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


def test_unexpected_exit_restarts_with_bounded_circuit() -> bool:
    sup = BackendSupervisor()
    sup.restart = lambda: False
    sup._generation_started_at = sup._monotonic()
    sup._wait_for_stop = lambda _seconds: False
    if sup.recover_unexpected_exit(-15):
        print("  crash recovery exceeded the configured limit")
        return False
    rows = [
        json.loads(line)
        for line in (Path(_TMP_HOME) / "backend-exits.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    decisions = [row["decision"] for row in rows[-6:]]
    if decisions != ["restart"] * 5 + ["circuit_open"]:
        print(f"  unexpected exit journal decisions: {decisions}")
        return False
    return True


def test_stable_generation_resets_crash_circuit() -> bool:
    import supervisor as supervisor_module

    sup = BackendSupervisor()
    sup._consecutive_crashes = 5
    sup._generation_healthy_at = (
        sup._monotonic() - supervisor_module._CRASH_STABILITY_SECONDS
    )
    sup.restart = lambda: True
    sup._wait_for_stop = lambda _seconds: False
    if not sup.recover_unexpected_exit(7):
        print("  stable generation did not reset crash circuit")
        return False
    if sup._consecutive_crashes != 1:
        print(f"  expected reset attempt 1, got {sup._consecutive_crashes}")
        return False
    return True


def test_unexpected_exit_respawns_real_process() -> bool:
    holder, port = _held_port()
    holder.close()
    sup = BackendSupervisor(port=port)
    first = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.exit(7)"],
    )
    sup._set_generation(first)
    first_pid = first.pid
    assert sup.wait_exit() == 7
    spawned: list[subprocess.Popen] = []

    def spawn():
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
        )
        spawned.append(proc)
        return proc

    sup._spawn_backend = spawn
    sup.wait_healthy = lambda timeout=30.0: True
    sup._wait_for_stop = lambda _seconds: False
    try:
        if not sup.recover_unexpected_exit(7):
            print("  real process was not respawned")
            return False
        if not spawned or spawned[0].pid == first_pid:
            print("  recovery did not create a new backend PID")
            return False
        return True
    finally:
        sup.shutdown(kill_runners=False)


def test_quit_during_backoff_prevents_respawn() -> bool:
    sup = BackendSupervisor()
    entered_backoff = threading.Event()
    restarted = threading.Event()

    def wait_for_stop(_seconds: float) -> bool:
        entered_backoff.set()
        return sup._stopping.wait(5)

    sup._wait_for_stop = wait_for_stop
    sup.restart = lambda: restarted.set() or True
    result: list[bool] = []
    thread = threading.Thread(
        target=lambda: result.append(sup.recover_unexpected_exit(7))
    )
    thread.start()
    if not entered_backoff.wait(2):
        print("  recovery never entered backoff")
        return False
    sup.shutdown(kill_runners=False)
    thread.join(timeout=2)
    if thread.is_alive():
        print("  recovery thread did not stop during quit")
        return False
    if restarted.is_set() or result != [False]:
        print("  quit allowed a backend respawn")
        return False
    return True


def test_shutdown_reaps_spawn_crossing_lifecycle_boundary() -> bool:
    holder, port = _held_port()
    holder.close()
    sup = BackendSupervisor(port=port)
    entered_spawn = threading.Event()
    release_spawn = threading.Event()
    spawned: list[subprocess.Popen] = []

    def spawn():
        entered_spawn.set()
        release_spawn.wait(5)
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
        )
        spawned.append(proc)
        return proc

    sup._spawn_backend = spawn
    sup.wait_healthy = lambda timeout=30.0: True
    recovery = threading.Thread(target=sup.restart)
    recovery.start()
    if not entered_spawn.wait(2):
        print("  restart never reached spawn boundary")
        return False
    shutdown = threading.Thread(
        target=lambda: sup.shutdown(kill_runners=False)
    )
    shutdown.start()
    release_spawn.set()
    recovery.join(timeout=5)
    shutdown.join(timeout=5)
    if recovery.is_alive() or shutdown.is_alive():
        print("  lifecycle synchronization deadlocked")
        return False
    if not spawned or spawned[0].poll() is None:
        print("  shutdown left the crossing backend generation alive")
        if spawned and spawned[0].poll() is None:
            spawned[0].kill()
            spawned[0].wait()
        return False
    return True


def test_shutdown_interrupts_health_polling() -> bool:
    holder, port = _held_port()
    holder.close()
    sup = BackendSupervisor(port=port)
    spawned = threading.Event()
    process: list[subprocess.Popen] = []

    def spawn():
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
        )
        process.append(proc)
        spawned.set()
        return proc

    sup._spawn_backend = spawn
    recovery = threading.Thread(target=sup.restart)
    recovery.start()
    if not spawned.wait(2):
        print("  restart never entered health polling")
        return False
    started = time.monotonic()
    shutdown = threading.Thread(
        target=lambda: sup.shutdown(kill_runners=False)
    )
    shutdown.start()
    recovery.join(timeout=3)
    shutdown.join(timeout=3)
    elapsed = time.monotonic() - started
    if recovery.is_alive() or shutdown.is_alive() or elapsed > 3:
        print(f"  shutdown did not interrupt health polling ({elapsed:.1f}s)")
        if process and process[0].poll() is None:
            process[0].kill()
            process[0].wait()
        return False
    if process[0].poll() is None:
        print("  health-polling backend remained alive after shutdown")
        process[0].kill()
        process[0].wait()
        return False
    return True


def test_stop_tracked_backend_force_kills_stubborn_child() -> bool:
    child = (
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "time.sleep(30)\n"
    )
    sup = BackendSupervisor()
    sup._proc = subprocess.Popen([sys.executable, "-c", child])
    time.sleep(0.2)
    try:
        if not sup._stop_tracked_backend(timeout=0.1):
            print("  stubborn backend was not force-killed")
            return False
        if sup._proc.poll() is None:
            print("  stubborn backend remained alive")
            return False
        return True
    finally:
        if sup._proc.poll() is None:
            sup._proc.kill()
            sup._proc.wait()


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
    root, posix_python = _source_checkout("target-checkout")
    app_entry = root / "backend" / "app_entry.py"
    argv = backend_argv(checkout=root)
    if argv[:2] != [str(posix_python.resolve()), str(app_entry.resolve())]:
        print(f"  target POSIX argv mismatch: {argv}")
        return False
    posix_python.unlink()
    windows_python = root / "backend" / ".venvs" / "test" / "Scripts" / "python.exe"
    windows_python.parent.mkdir(parents=True)
    windows_python.write_text("", encoding="utf-8")
    argv = backend_argv(checkout=root)
    if argv[:2] != [str(windows_python.resolve()), str(app_entry.resolve())]:
        print(f"  target Windows argv mismatch: {argv}")
        return False
    return True


def test_packaged_restart_preserves_denial_and_rotates_channel() -> bool:
    import provider_credentials
    import supervisor as supervisor_module

    reads = 0
    spawns: list[dict] = []
    real_get = provider_credentials.oskeychain.native_get
    real_cli_get = provider_credentials.oskeychain.get
    real_popen = supervisor_module.subprocess.Popen
    sup = BackendSupervisor()
    checkout, _ = _source_checkout("packaged-restart")
    (checkout / "frontend" / "dist").mkdir(parents=True)
    sup._resolved_checkout = lambda: checkout

    def denied_get(service: str, account: str, **kwargs):
        nonlocal reads
        reads += 1
        raise RuntimeError("denied")

    class FakeProcess:
        pid = 4242
        stdout: list[str] = []

        def poll(self):
            return None

    def fake_popen(*args, **kwargs):
        spawns.append(kwargs)
        return FakeProcess()

    provider_credentials.oskeychain.native_get = denied_get
    provider_credentials.oskeychain.get = denied_get
    supervisor_module.subprocess.Popen = fake_popen
    try:
        request = {
            "op": "read",
            "provider_id": "provider-denied",
            "request_id": "0" * 32,
        }
        assert sup._credential_broker.handle(request) == {"status": "blocked"}
        assert reads == 1
        first = sup._spawn_backend()
        first_session = sup._credential_session
        second = sup._spawn_backend()
        assert first is not second
        assert first_session is not sup._credential_session
        # The denial survives channel rotation but is re-probed, not cached.
        assert sup._credential_broker.handle(request) == {"status": "blocked"}
        assert reads == 2
        assert len(spawns) == 2
        assert all(
            "BETTER_AGENT_CREDENTIAL_SESSION_FD" in spawn["env"]
            for spawn in spawns
        )
        assert "BETTER_AGENT_CREDENTIAL_SESSION_FD" not in sup._env
        return True
    finally:
        sup._close_credential_session()
        sup._credential_broker.clear()
        provider_credentials.oskeychain.native_get = real_get
        provider_credentials.oskeychain.get = real_cli_get
        supervisor_module.subprocess.Popen = real_popen


def test_backend_contract_resolves_before_credential_session_opens() -> bool:
    calls: list[str] = []
    real_backend_argv = supervisor_module.backend_argv
    real_popen = supervisor_module.subprocess.Popen
    sup = BackendSupervisor(role="node")
    checkout, _ = _source_checkout("node-launch-order")
    sup._resolved_checkout = lambda: checkout
    sup._ensure_backend_logger = lambda: None

    class FakeSession:
        def start(self):
            calls.append("session-start")

        def backend_env(self):
            return {}

        def backend_popen_kwargs(self):
            return {}

        def revoke_backend_inheritance(self):
            pass

        def stop(self):
            pass

    class FakeProcess:
        stdout: list[str] = []

    supervisor_module.backend_argv = (
        lambda *_args, **_kwargs: calls.append("backend-argv") or ["backend"]
    )
    sup._credential_broker.open_session = (
        lambda: calls.append("open-session") or FakeSession()
    )
    supervisor_module.subprocess.Popen = lambda *_args, **_kwargs: FakeProcess()
    try:
        sup._spawn_backend()
        assert calls[:3] == ["backend-argv", "open-session", "session-start"]
        return True
    finally:
        sup._close_credential_session()
        supervisor_module.backend_argv = real_backend_argv
        supervisor_module.subprocess.Popen = real_popen


def test_source_switch_rejects_missing_frontend() -> bool:
    from daemonhost import pointer

    root = Path(_TMP_HOME) / "missing-dist-checkout"
    root, _ = _source_checkout("missing-dist-checkout")
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
    ("stale restart flag is consumed without refresh",
     test_stale_restart_flag_is_consumed_without_refresh),
    ("restart flag symlink is rejected",
     test_restart_flag_symlink_is_rejected),
    ("wait_exit returns the backend exit code", test_wait_exit_returns_exit_code),
    ("unexpected exits restart with a bounded circuit",
     test_unexpected_exit_restarts_with_bounded_circuit),
    ("stable generation resets the crash circuit",
     test_stable_generation_resets_crash_circuit),
    ("unexpected exit respawns a real process",
     test_unexpected_exit_respawns_real_process),
    ("quit during backoff prevents respawn",
     test_quit_during_backoff_prevents_respawn),
    ("shutdown reaps a spawn crossing the lifecycle boundary",
     test_shutdown_reaps_spawn_crossing_lifecycle_boundary),
    ("shutdown interrupts health polling",
     test_shutdown_interrupts_health_polling),
    ("tracked backend shutdown escalates to kill", test_stop_tracked_backend_force_kills_stubborn_child),
    ("shutdown sends SIGINT to kill runners, SIGTERM to keep them",
     test_shutdown_signal_choice),
    ("restart aborts when port is held instead of spawning a dead backend",
     test_restart_aborts_when_port_held),
    ("target checkout argv uses POSIX and Windows interpreters",
     test_backend_argv_uses_target_checkout_interpreter),
    ("source switch rejects a target without a built frontend",
     test_source_switch_rejects_missing_frontend),
    ("packaged restart preserves denial and rotates credential channel",
     test_packaged_restart_preserves_denial_and_rotates_channel),
    ("node resolves dependency contract before credential session opens",
     test_backend_contract_resolves_before_credential_session_opens),
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
