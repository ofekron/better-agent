#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Callable

import psutil
from itsdangerous import URLSafeTimedSerializer


ROOT = Path(__file__).resolve().parents[2]


def _copy_ignore(directory: str, names: list[str]) -> set[str]:
    relative = Path(directory).resolve().relative_to(ROOT)
    ignored = {"__pycache__"}
    if relative == Path("."):
        ignored.update({".git", "AGENTS.md", "CLAUDE.md", "GEMINI.md"})
    elif relative == Path("backend"):
        ignored.update({".active-venv", ".dependency-plan.lock", ".venvs"})
    elif relative == Path("frontend"):
        ignored.update({"dist", "node_modules"})
    return ignored.intersection(names)


def _stage_checkout(temp_root: Path) -> Path:
    checkout = temp_root / "checkout"
    subprocess.run(
        ["git", "clone", "--local", "--no-hardlinks", str(ROOT), str(checkout)],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    shutil.copytree(
        ROOT,
        checkout,
        dirs_exist_ok=True,
        ignore=_copy_ignore,
        symlinks=True,
    )
    retired_restart_request = checkout / "desktop" / "restart_request.py"
    if not (ROOT / "desktop" / "restart_request.py").exists():
        retired_restart_request.unlink(missing_ok=True)
    return checkout


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _log_tail(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-12000:]
    except OSError:
        return "<launcher log unavailable>"


def _launcher_ready_count(path: Path) -> int:
    try:
        return path.read_text(encoding="utf-8", errors="replace").count(
            "Backend is ready."
        )
    except OSError:
        return 0


def _wait_until(
    predicate: Callable[[], bool],
    *,
    description: str,
    launcher: subprocess.Popen[bytes],
    log_path: Path,
    timeout: float,
    require_launcher_running: bool = True,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        if require_launcher_running and launcher.poll() is not None:
            raise AssertionError(
                f"run.sh exited while waiting for {description}: "
                f"{launcher.returncode}\n{_log_tail(log_path)}"
            )
        time.sleep(0.05)
    raise AssertionError(
        f"timed out waiting for {description}\n{_log_tail(log_path)}"
    )


def _ready(port: int) -> bool:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/readyz",
            timeout=0.3,
        ) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def _backend_children(launcher_pid: int, port: int) -> list[psutil.Process]:
    try:
        descendants = psutil.Process(launcher_pid).children(recursive=True)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return []
    expected_port = str(port)
    matches: list[psutil.Process] = []
    for process in descendants:
        try:
            command = process.cmdline()
            if (
                "uvicorn" in command
                and "main:app" in command
                and "--port" in command
                and expected_port in command
                and process.status() != psutil.STATUS_ZOMBIE
            ):
                matches.append(process)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return matches


def _wait_generation(
    launcher: subprocess.Popen[bytes],
    port: int,
    *,
    generation_number: int,
    previous_pids: set[int],
    log_path: Path,
    timeout: float,
) -> psutil.Process:
    observed: psutil.Process | None = None

    def current() -> bool:
        nonlocal observed
        candidates = [
            process
            for process in _backend_children(launcher.pid, port)
            if process.pid not in previous_pids
        ]
        if (
            len(candidates) != 1
            or not _ready(port)
            or _launcher_ready_count(log_path) < generation_number
        ):
            return False
        observed = candidates[0]
        return True

    _wait_until(
        current,
        description="a healthy replacement backend generation",
        launcher=launcher,
        log_path=log_path,
        timeout=timeout,
    )
    assert observed is not None
    return observed


def _post_restart(port: int, request_id: str, bearer_token: str) -> dict:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/admin/restart",
        data=json.dumps({"request_id": request_id, "mode": "now"}).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {bearer_token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read())


def _journal_rows(state: Path) -> list[dict]:
    try:
        lines = (state / "backend-exits.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
    except FileNotFoundError:
        return []
    return [json.loads(line) for line in lines]


def _wait_journal_rows(
    state: Path,
    count: int,
    *,
    launcher: subprocess.Popen[bytes],
    log_path: Path,
) -> list[dict]:
    rows: list[dict] = []

    def observed() -> bool:
        nonlocal rows
        rows = _journal_rows(state)
        return len(rows) >= count

    _wait_until(
        observed,
        description=f"{count} backend exit journal rows",
        launcher=launcher,
        log_path=log_path,
        timeout=20,
    )
    return rows


def _stage_ui_only_profile(state: Path, checkout: Path) -> None:
    provider = state / "claude"
    provider.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    provider.chmod(0o700)
    env = {
        **os.environ,
        "BETTER_AGENT_HOME": str(state),
        "BETTER_CLAUDE_HOME": str(state),
        "PYTHONPATH": str(checkout / "backend"),
    }
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import os;"
            " import installation_profile as p;"
            " import provider_setup as s;"
            " x=os.path.join(os.environ['BETTER_AGENT_HOME'], 'claude');"
            " p.stage_activation(p.new_active_profile("
            "mode=p.DESKTOP_UI_ONLY, provider='claude',"
            " provider_identity=s.executable_identity(x)))",
        ],
        cwd=checkout,
        env=env,
        check=True,
    )


def _stage_headless_auth(state: Path) -> tuple[Path, Path, str]:
    password_hash = state / "password-hash"
    password_hash.write_text("unused-test-hash", encoding="utf-8")
    session_secret = state / "session-secret"
    secret = "11" * 32
    session_secret.write_text(secret, encoding="utf-8")
    bearer_token = URLSafeTimedSerializer(
        secret,
        salt="bc-bearer-v1",
    ).dumps({"username": "integration-test"})
    return password_hash, session_secret, bearer_token


def _write_command_wrappers(directory: Path) -> None:
    directory.mkdir()
    uname = directory / "uname"
    uname.write_text(
        "#!/bin/sh\n"
        "if [ \"$#\" -eq 0 ] || [ \"$1\" = \"-s\" ]; then\n"
        "  printf 'Linux\\n'\n"
        "else\n"
        "  exec /usr/bin/uname \"$@\"\n"
        "fi\n",
        encoding="utf-8",
    )
    uname.chmod(0o700)
    bash = directory / "bash"
    bash.write_text(
        "#!/bin/sh\n"
        "case \"$1\" in\n"
        "  */scripts/install-bagent.sh) exit 0 ;;\n"
        "esac\n"
        "exec /bin/bash \"$@\"\n",
        encoding="utf-8",
    )
    bash.chmod(0o700)


def _terminate_process_group(group_id: int) -> None:
    try:
        os.killpg(group_id, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 20
    while _group_members(group_id) and time.monotonic() < deadline:
        time.sleep(0.05)
    if _group_members(group_id):
        try:
            os.killpg(group_id, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _terminate_owned_group(launcher: subprocess.Popen[bytes]) -> None:
    _terminate_process_group(launcher.pid)
    if launcher.poll() is None:
        launcher.wait(timeout=20)


def _group_members(group_id: int) -> list[int]:
    members: list[int] = []
    for process in psutil.process_iter(["pid"]):
        try:
            if os.getpgid(process.pid) == group_id:
                members.append(process.pid)
        except (ProcessLookupError, PermissionError):
            continue
    return members


def _scoped_processes(
    checkout: Path,
    state: Path,
    *,
    created_after: float,
) -> list[psutil.Process]:
    roots = (str(checkout.resolve()), str(state.resolve()))
    matches: list[psutil.Process] = []
    for process in psutil.process_iter(["cmdline", "create_time", "exe", "pid"]):
        try:
            created_at = float(process.info["create_time"] or 0)
            if created_at < created_after:
                continue
            values = [
                str(process.info["exe"] or ""),
                str(process.cwd()),
                *(str(value) for value in (process.info["cmdline"] or [])),
            ]
            environment = process.environ()
            scoped_environment = (
                environment.get("BETTER_AGENT_HOME") == roots[1]
                or environment.get("BETTER_CLAUDE_HOME") == roots[1]
                or environment.get("BETTER_AGENT_ACTIVE_CHECKOUT") == roots[0]
            )
            if scoped_environment or any(
                root in value
                for root in roots
                for value in values
            ):
                matches.append(process)
        except (
            psutil.AccessDenied,
            psutil.NoSuchProcess,
            psutil.ZombieProcess,
        ):
            continue
    return matches


def _terminate_scoped_processes(
    checkout: Path,
    state: Path,
    *,
    created_after: float,
) -> None:
    processes = _scoped_processes(
        checkout,
        state,
        created_after=created_after,
    )
    for process in processes:
        try:
            process.terminate()
        except psutil.NoSuchProcess:
            pass
    _gone, alive = psutil.wait_procs(processes, timeout=10)
    for process in alive:
        try:
            process.kill()
        except psutil.NoSuchProcess:
            pass
    psutil.wait_procs(alive, timeout=10)


def _run(temp_root: Path) -> None:
    checkout = _stage_checkout(temp_root)
    state = temp_root / "state"
    state.mkdir()
    wrappers = temp_root / "bin"
    _write_command_wrappers(wrappers)
    _stage_ui_only_profile(state, checkout)
    password_hash, session_secret, bearer_token = _stage_headless_auth(state)
    port = _free_port()
    log_path = temp_root / "run-sh.log"
    control_dirs_before = set(Path("/tmp").glob("ba-bs.*"))
    index = checkout / "frontend" / "dist" / "index.html"
    index.parent.mkdir(parents=True)
    index.write_text("<!doctype html><title>test</title>", encoding="utf-8")

    env = {
        **os.environ,
        "PATH": f"{wrappers}{os.pathsep}{os.environ.get('PATH', '')}",
        "BETTER_AGENT_HOME": str(state),
        "BETTER_CLAUDE_HOME": str(state),
        "BETTER_AGENT_BACKEND_PORT": str(port),
        "BETTER_AGENT_RUN_SH_ASSUME_NO_BAS": "1",
        "BETTER_AGENT_RUN_SH_SERVICE_CHILD": "1",
        "BETTER_AGENT_NO_BROWSER": "1",
        "BETTER_AGENT_TEST_MODE": "1",
        "BETTER_AGENT_BACKEND_CRASH_LIMIT": "2",
        "BETTER_AGENT_BACKEND_STABILITY_SECONDS": "3600",
        "BETTER_AGENT_GRACEFUL_RESTART_TIMEOUT_SECONDS": "2",
        "BETTER_AGENT_HEADLESS_AUTH": "1",
        "BETTER_AGENT_USERNAME": "integration-test",
        "BETTER_AGENT_PASSWORD_HASH_FILE": str(password_hash),
        "BETTER_AGENT_SESSION_SECRET_FILE": str(session_secret),
        "PYTHONUNBUFFERED": "1",
    }
    launcher: subprocess.Popen[bytes] | None = None
    generation_pids: list[int] = []
    process_boundary = time.time()
    try:
        with log_path.open("wb") as output:
            launcher = subprocess.Popen(
                ["/bin/bash", str(checkout / "run.sh")],
                cwd=checkout,
                env=env,
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        first = _wait_generation(
            launcher,
            port,
            generation_number=1,
            previous_pids=set(),
            log_path=log_path,
            timeout=300,
        )
        generation_pids.append(first.pid)

        request_id = str(uuid.uuid4())
        response = _post_restart(port, request_id, bearer_token)
        assert response["request_id"] == request_id
        second = _wait_generation(
            launcher,
            port,
            generation_number=2,
            previous_pids=set(generation_pids),
            log_path=log_path,
            timeout=60,
        )
        generation_pids.append(second.pid)
        rows = _wait_journal_rows(
            state,
            1,
            launcher=launcher,
            log_path=log_path,
        )
        assert rows[0]["classification"] == "refresh"
        assert rows[0]["decision"] == "restart"
        assert rows[0]["request_id"] == request_id
        assert rows[0]["pid"] == first.pid

        second.kill()
        second.wait(timeout=10)
        third = _wait_generation(
            launcher,
            port,
            generation_number=3,
            previous_pids=set(generation_pids),
            log_path=log_path,
            timeout=30,
        )
        generation_pids.append(third.pid)
        rows = _wait_journal_rows(
            state,
            2,
            launcher=launcher,
            log_path=log_path,
        )
        assert rows[1]["classification"] == "unexpected"
        assert rows[1]["decision"] == "restart"
        assert rows[1]["restart_attempt"] == 1
        assert rows[1]["pid"] == second.pid

        third.kill()
        third.wait(timeout=10)
        fourth = _wait_generation(
            launcher,
            port,
            generation_number=4,
            previous_pids=set(generation_pids),
            log_path=log_path,
            timeout=30,
        )
        generation_pids.append(fourth.pid)
        rows = _wait_journal_rows(
            state,
            3,
            launcher=launcher,
            log_path=log_path,
        )
        assert rows[2]["decision"] == "restart"
        assert rows[2]["restart_attempt"] == 2
        assert rows[2]["pid"] == third.pid

        fourth.kill()
        fourth.wait(timeout=10)
        launcher.wait(timeout=30)
        rows = _journal_rows(state)
        assert launcher.returncode == 0, _log_tail(log_path)
        assert [row["decision"] for row in rows] == [
            "restart",
            "restart",
            "restart",
            "circuit_open",
        ]
        assert rows[-1]["classification"] == "unexpected"
        assert rows[-1]["restart_attempt"] == 2
        assert rows[-1]["pid"] == fourth.pid
        assert len({row["generation_id"] for row in rows}) == 4
        _wait_until(
            lambda: all(not _group_members(pid) for pid in generation_pids),
            description="backend generation process trees to exit",
            launcher=launcher,
            log_path=log_path,
            timeout=20,
            require_launcher_running=False,
        )
        _wait_until(
            lambda: not _scoped_processes(
                checkout,
                state,
                created_after=process_boundary,
            ),
            description="all checkout- and state-scoped processes to exit",
            launcher=launcher,
            log_path=log_path,
            timeout=20,
            require_launcher_running=False,
        )
    finally:
        for generation_pid in generation_pids:
            _terminate_process_group(generation_pid)
        if launcher is not None:
            _terminate_owned_group(launcher)
            _wait_until(
                lambda: not _group_members(launcher.pid),
                description="all run.sh process-group members to exit",
                launcher=launcher,
                log_path=log_path,
                timeout=20,
                require_launcher_running=False,
            )
        _terminate_scoped_processes(
            checkout,
            state,
            created_after=process_boundary,
        )

    assert not _ready(port)
    assert not any(psutil.pid_exists(pid) for pid in generation_pids)
    assert not _scoped_processes(
        checkout,
        state,
        created_after=process_boundary,
    )
    assert set(Path("/tmp").glob("ba-bs.*")) == control_dirs_before


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="ba-run-sh-recovery-") as raw_root:
        _run(Path(raw_root))
    print("PASS: real run.sh refresh-to-crash recovery and bounded circuit")


if __name__ == "__main__":
    main()
