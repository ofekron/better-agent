from __future__ import annotations

import asyncio
import datetime as _dt
import hashlib
import logging
import os
import sys
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from cli_paths import resolve_cli_binary
import perf

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProviderInstaller:
    kind: str
    label: str
    command: str
    install_argv: tuple[str, ...]
    verify_argv: tuple[str, ...]
    prerequisite_argv: tuple[str, ...]
    prerequisite_install_argv: tuple[str, ...] = ()
    install_script_url: str = ""
    install_script_sha256: str = ""


_INSTALLER_SCRIPT_ARG = "<downloaded-installer>"
_AGY_INSTALL_SH = "https://antigravity.google/cli/install.sh"
_AGY_INSTALL_PS1 = "https://antigravity.google/cli/install.ps1"
_AGY_INSTALL_SH_SHA256 = "ee1ea43ce4e9e56356c4ab6dad907ef357ae4bdfcaadb682735909fb57c9c640"
_AGY_INSTALL_PS1_SHA256 = "51c2cb4fada22ce0228da71b9506370383d6544bfebcec85fe7616a52b805344"


def _node_prerequisite_install_argv() -> tuple[str, ...]:
    if sys.platform == "win32":
        return (
            "winget",
            "install",
            "--id",
            "OpenJS.NodeJS.LTS",
            "--source",
            "winget",
            "--accept-package-agreements",
            "--accept-source-agreements",
            "--scope",
            "user",
            "--silent",
        )
    return ()


def _agy_installer() -> ProviderInstaller:
    if sys.platform == "win32":
        return ProviderInstaller(
            kind="agy",
            label="Antigravity CLI",
            command="agy",
            install_argv=(
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                _INSTALLER_SCRIPT_ARG,
            ),
            verify_argv=("agy", "--version"),
            prerequisite_argv=("powershell", "-NoProfile", "-Command", "$PSVersionTable.PSVersion"),
            install_script_url=_AGY_INSTALL_PS1,
            install_script_sha256=_AGY_INSTALL_PS1_SHA256,
        )
    return ProviderInstaller(
        kind="agy",
        label="Antigravity CLI",
        command="agy",
        install_argv=("bash", _INSTALLER_SCRIPT_ARG),
        verify_argv=("agy", "--version"),
        prerequisite_argv=("bash", "--version"),
        install_script_url=_AGY_INSTALL_SH,
        install_script_sha256=_AGY_INSTALL_SH_SHA256,
    )


def _copilot_installer() -> ProviderInstaller:
    if sys.platform == "win32":
        return ProviderInstaller(
            kind="copilot",
            label="GitHub Copilot CLI",
            command="copilot",
            install_argv=(
                "winget",
                "install",
                "--id",
                "GitHub.Copilot",
                "--source",
                "winget",
                "--accept-package-agreements",
                "--accept-source-agreements",
                "--silent",
            ),
            verify_argv=("copilot", "--version"),
            prerequisite_argv=("winget", "--version"),
        )
    return ProviderInstaller(
        kind="copilot",
        label="GitHub Copilot CLI",
        command="copilot",
        # copilot-cli ships as a Homebrew cask (`brew install copilot-cli`).
        # `gh copilot` is the alternate managed-download path; both rely on
        # `gh auth login` for OAuth, done outside the installer.
        install_argv=("brew", "install", "copilot-cli"),
        verify_argv=("copilot", "--version"),
        prerequisite_argv=("brew", "--version"),
    )


INSTALLERS: dict[str, ProviderInstaller] = {
    "claude": ProviderInstaller(
        kind="claude",
        label="Claude Code",
        command="claude",
        install_argv=("npm", "install", "-g", "@anthropic-ai/claude-code"),
        verify_argv=("claude", "--version"),
        prerequisite_argv=("npm", "--version"),
        prerequisite_install_argv=_node_prerequisite_install_argv(),
    ),
    "codex": ProviderInstaller(
        kind="codex",
        label="Codex CLI",
        command="codex",
        install_argv=("npm", "install", "-g", "@openai/codex"),
        verify_argv=("codex", "--version"),
        prerequisite_argv=("npm", "--version"),
        prerequisite_install_argv=_node_prerequisite_install_argv(),
    ),
    "agy": _agy_installer(),
    "copilot": _copilot_installer(),
    "pi": ProviderInstaller(
        kind="pi",
        label="pi",
        command="pi",
        install_argv=("npm", "install", "-g", "@mariozechner/pi-coding-agent"),
        verify_argv=("pi", "--version"),
        prerequisite_argv=("npm", "--version"),
        prerequisite_install_argv=_node_prerequisite_install_argv(),
    ),
    "qwen": ProviderInstaller(
        kind="qwen",
        label="Qwen Code",
        command="qwen",
        install_argv=("npm", "install", "-g", "@qwen-code/qwen-code"),
        verify_argv=("qwen", "--version"),
        prerequisite_argv=("npm", "--version"),
        prerequisite_install_argv=_node_prerequisite_install_argv(),
    ),
    "amp": ProviderInstaller(
        kind="amp",
        label="Amp",
        command="amp",
        install_argv=("npm", "install", "-g", "@sourcegraph/amp"),
        verify_argv=("amp", "--version"),
        prerequisite_argv=("npm", "--version"),
        prerequisite_install_argv=_node_prerequisite_install_argv(),
    ),
    "opencode": ProviderInstaller(
        kind="opencode",
        label="OpenCode",
        command="opencode",
        install_argv=("npm", "install", "-g", "opencode-ai"),
        verify_argv=("opencode", "--version"),
        prerequisite_argv=("npm", "--version"),
        prerequisite_install_argv=_node_prerequisite_install_argv(),
    ),
}


def supported_provider_kinds() -> list[str]:
    import provider_manifest
    return provider_manifest.installable_kinds()


def installer_for(kind: str) -> ProviderInstaller:
    installer = INSTALLERS.get(str(kind or "").strip())
    if installer is None:
        raise ValueError("unsupported provider kind")
    return installer


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def executable_identity(launcher_path: str) -> dict[str, Any]:
    launcher = Path(launcher_path)
    if not launcher.is_absolute() or not launcher.is_file():
        raise ValueError("provider executable launcher is unavailable")
    target = launcher.resolve(strict=True)
    if not target.is_file():
        raise ValueError("provider executable target is unavailable")
    stat = target.stat()
    return {
        "command": launcher.stem if launcher.suffix.lower() in (".cmd", ".exe", ".bat") else launcher.name,
        "launcher_path": str(launcher),
        "launcher_sha256": _file_sha256(launcher),
        "target_path": str(target),
        "target_sha256": _file_sha256(target),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


async def verified_provider_identity(kind: str) -> dict[str, Any] | None:
    installer = installer_for(kind)
    launcher = resolve_cli_binary(
        installer.command,
        respect_installation_profile=False,
    )
    if not launcher:
        return None
    before = executable_identity(str(Path(launcher).absolute()))
    if before["command"] != installer.command:
        raise RuntimeError("provider executable identity does not match selected provider")
    result = await _run_argv(
        (before["launcher_path"], *installer.verify_argv[1:]),
        timeout=10,
    )
    after = executable_identity(before["launcher_path"])
    if before != after:
        raise RuntimeError("provider executable changed during verification")
    if not result["ok"]:
        return None
    return after


async def provider_setup_status(kind: str, *, wait_for_cold: bool = False) -> dict[str, Any]:
    if not _SETUP_ACCEPTING:
        raise RuntimeError("provider setup is shutting down")
    cached = _STATUS_CACHE.get(kind)
    now = time.monotonic()
    if cached and now - cached[0] <= _STATUS_TTL_SECONDS:
        return _copy_status(cached[1])
    task = _STATUS_INFLIGHT.get(kind)
    if cached:
        if task is None:
            task = asyncio.create_task(_refresh_status_cache(kind))
            _STATUS_INFLIGHT[kind] = task
            task.add_done_callback(_provider_setup_task_done)
        return _copy_status(cached[1])
    if task is None:
        task = asyncio.create_task(_refresh_status_cache(kind))
        _STATUS_INFLIGHT[kind] = task
        task.add_done_callback(_provider_setup_task_done)
    if wait_for_cold:
        return _copy_status(await task)
    return _pending_status(installer_for(kind))


async def _refresh_status_cache(kind: str) -> dict[str, Any]:
    try:
        installer = installer_for(kind)
        prerequisite, cli = await asyncio.gather(
            _check_argv(installer.prerequisite_argv),
            _check_argv(installer.verify_argv),
        )
        status = _public_status(installer, prerequisite, cli)
        _STATUS_CACHE[kind] = (time.monotonic(), status)
        return status
    finally:
        if _STATUS_INFLIGHT.get(kind) is asyncio.current_task():
            _STATUS_INFLIGHT.pop(kind, None)


def clear_status_cache(kind: str | None = None) -> None:
    if kind is None:
        _STATUS_CACHE.clear()
        _STATUS_INFLIGHT.clear()
        return
    _STATUS_CACHE.pop(kind, None)
    _STATUS_INFLIGHT.pop(kind, None)


def _copy_status(status: dict[str, Any]) -> dict[str, Any]:
    copied = dict(status)
    for key in ("prerequisite", "verify", "install"):
        value = copied.get(key)
        if isinstance(value, dict):
            copied[key] = dict(value)
    command = copied.get("install_command")
    if isinstance(command, list):
        copied["install_command"] = list(command)
    return copied


def _pending_status(installer: ProviderInstaller) -> dict[str, Any]:
    pending = {"ok": False, "stdout": "", "stderr": "", "returncode": -1, "checking": True}
    status = _public_status(installer, dict(pending), dict(pending))
    status["checking"] = True
    return status


# ---- Streaming install registry ----------------------------------------
# An install run is a background asyncio task that streams the installer
# subprocess stdout/stderr line-by-line to the frontend via global WS
# events (`provider_install_progress` / `provider_install_finished`).
# One run per kind; multiple kinds run concurrently. Authoritative state
# is this in-memory registry — `GET /api/provider-setup/installs` returns
# the snapshot for first paint, WS pings carry the live deltas.

BroadcastFn = Callable[[str, dict], Awaitable[None]]
LineFn = Callable[[str, str], Awaitable[None]]

_INSTALL_RUNS: dict[str, dict[str, Any]] = {}
_INSTALL_TASKS: dict[str, asyncio.Task] = {}
_MAX_LINES = 1000
_STATUS_TTL_SECONDS = 60.0
_STATUS_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_STATUS_INFLIGHT: dict[str, asyncio.Task] = {}
_ACTIVE_PROCESSES: set[asyncio.subprocess.Process] = set()
_SETUP_ACCEPTING = True


def reopen_provider_setup() -> None:
    global _SETUP_ACCEPTING
    if _SETUP_ACCEPTING:
        return
    _SETUP_ACCEPTING = True


async def shutdown_provider_setup() -> None:
    global _SETUP_ACCEPTING
    started = time.perf_counter()
    _SETUP_ACCEPTING = False
    tasks = tuple({*_STATUS_INFLIGHT.values(), *_INSTALL_TASKS.values()})
    for task in tasks:
        task.cancel()
    results = await asyncio.gather(*tasks, return_exceptions=True) if tasks else []
    processes = tuple(_ACTIVE_PROCESSES)
    if processes:
        await asyncio.gather(
            *(_terminate_process(proc) for proc in processes),
            return_exceptions=True,
        )
    perf.record("shutdown.provider_setup", (time.perf_counter() - started) * 1000)
    perf.record_count("shutdown.provider_setup.tasks", len(tasks))
    perf.record_count("shutdown.provider_setup.processes", len(processes))
    perf.record_count(
        "shutdown.provider_setup.failed",
        sum(isinstance(result, Exception) for result in results),
    )


def _provider_setup_task_done(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        logger.error("provider setup task failed: %s", error, exc_info=error)


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _new_run(installer: ProviderInstaller) -> dict[str, Any]:
    return {
        "kind": installer.kind,
        "label": installer.label,
        "command": installer.command,
        "state": "running",
        "lines": [],
        "started_at": _now_iso(),
        "finished_at": None,
        "returncode": None,
        "installed": None,
        "message": None,
    }


def _snapshot(run: dict[str, Any]) -> dict[str, Any]:
    return {**run, "lines": list(run["lines"])}


def get_install_runs() -> dict[str, dict[str, Any]]:
    return {kind: _snapshot(run) for kind, run in _INSTALL_RUNS.items()}


async def start_install(kind: str, broadcast: BroadcastFn) -> dict[str, Any]:
    """Start (or no-op) a streaming background install for `kind`.

    Returns the current run snapshot immediately; the background task
    keeps streaming lines via `broadcast`. Concurrent calls for the same
    kind collapse to the already-running task."""
    if not _SETUP_ACCEPTING:
        raise RuntimeError("provider setup is shutting down")
    installer = installer_for(kind)
    existing = _INSTALL_RUNS.get(kind)
    if existing and existing["state"] == "running":
        return _snapshot(existing)

    prerequisite = await _check_argv(installer.prerequisite_argv)
    if not prerequisite["ok"]:
        if not installer.prerequisite_install_argv:
            run = _new_run(installer)
            run["state"] = "failed"
            run["message"] = f"Missing prerequisite: {installer.prerequisite_argv[0]}"
            run["returncode"] = 127
            run["finished_at"] = _now_iso()
            _INSTALL_RUNS[kind] = run
            await broadcast("provider_install_finished", _snapshot(run))
            return _snapshot(run)

    if not _SETUP_ACCEPTING:
        raise RuntimeError("provider setup is shutting down")
    run = _new_run(installer)
    _INSTALL_RUNS[kind] = run
    await broadcast("provider_install_progress", {"kind": kind, "phase": "started"})
    if not _SETUP_ACCEPTING:
        if _INSTALL_RUNS.get(kind) is run:
            _INSTALL_RUNS.pop(kind, None)
        raise RuntimeError("provider setup is shutting down")
    task = asyncio.create_task(_run_install(installer, run, broadcast))
    _INSTALL_TASKS[kind] = task
    def _done(done: asyncio.Task, key: str = kind) -> None:
        if _INSTALL_TASKS.get(key) is done:
            _INSTALL_TASKS.pop(key, None)
        _provider_setup_task_done(done)
    task.add_done_callback(_done)
    return _snapshot(run)


async def install_if_missing(kind: str, broadcast: BroadcastFn) -> dict[str, Any]:
    status = await provider_setup_status(kind, wait_for_cold=True)
    if bool((status.get("verify") or {}).get("ok")):
        return {
            "kind": kind,
            "state": "already_installed",
            "installed": True,
            "lines": [],
        }
    await start_install(kind, broadcast)
    task = _INSTALL_TASKS.get(kind)
    if task is not None:
        await task
    return _snapshot(_INSTALL_RUNS[kind])


async def _run_install(
    installer: ProviderInstaller,
    run: dict[str, Any],
    broadcast: BroadcastFn,
) -> None:
    kind = installer.kind

    async def on_line(stream: str, text: str) -> None:
        run["lines"].append({"s": stream, "t": text})
        if len(run["lines"]) > _MAX_LINES:
            del run["lines"][: len(run["lines"]) - _MAX_LINES]
        await broadcast(
            "provider_install_progress",
            {"kind": kind, "stream": stream, "text": text},
        )

    try:
        prerequisite = await _check_argv(installer.prerequisite_argv)
        if not prerequisite["ok"] and installer.prerequisite_install_argv:
            await on_line(
                "stdout",
                "Installing prerequisite "
                f"{installer.prerequisite_argv[0]}: {' '.join(installer.prerequisite_install_argv)}",
            )
            prereq_result = await _run_argv_streaming(
                installer.prerequisite_install_argv,
                900,
                on_line,
            )
            if sys.platform == "win32":
                _refresh_windows_path()
            prerequisite = await _check_argv(installer.prerequisite_argv)
            if not prereq_result.get("ok") or not prerequisite["ok"]:
                run["state"] = "failed"
                run["returncode"] = prereq_result.get("returncode", 1)
                run["message"] = f"Failed to install prerequisite: {installer.prerequisite_argv[0]}"
                run["finished_at"] = _now_iso()
                if prereq_result.get("stderr"):
                    await on_line("stderr", prereq_result["stderr"])
                await broadcast("provider_install_finished", _snapshot(run))
                return

        if installer.install_script_url:
            result = await _run_installer_script_streaming(installer, 300, on_line)
        else:
            result = await _run_argv_streaming(installer.install_argv, 300, on_line)
        if sys.platform == "win32":
            _refresh_windows_path()
    except Exception as exc:  # pragma: no cover — defensive, surfaced to UI
        run["state"] = "failed"
        run["returncode"] = 1
        run["message"] = _scrub(str(exc))
        run["finished_at"] = _now_iso()
        await broadcast("provider_install_finished", _snapshot(run))
        return

    cli = await _check_argv(installer.verify_argv)
    clear_status_cache(kind)
    run["returncode"] = result.get("returncode")
    run["installed"] = bool(cli["ok"])
    run["state"] = "succeeded" if (result.get("ok") and cli["ok"]) else "failed"
    run["finished_at"] = _now_iso()
    if not result.get("ok") and result.get("stderr"):
        await on_line("stderr", result["stderr"])
    await broadcast("provider_install_finished", _snapshot(run))


async def _check_argv(argv: tuple[str, ...]) -> dict[str, Any]:
    if not resolve_cli_binary(argv[0], respect_installation_profile=False):
        return {"ok": False, "stdout": "", "stderr": f"{argv[0]} not found", "returncode": 127}
    return await _run_argv(argv, timeout=10)


async def _run_argv(argv: tuple[str, ...], timeout: int) -> dict[str, Any]:
    # Resolve argv[0] to its full path so Windows picks up `.exe`/`.cmd`
    # shims that a bare-name exec misses. A missing or unlaunchable binary
    # must degrade to "not available", never raise and 500 the caller
    # (e.g. provider-setup/status) — that was a Windows-only crash because
    # create_subprocess_exec can't launch a bare CLI name there.
    resolved = (
        resolve_cli_binary(argv[0], respect_installation_profile=False)
        or argv[0]
    )
    try:
        proc = await asyncio.create_subprocess_exec(
            resolved, *argv[1:],
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, OSError) as e:
        return {
            "ok": False,
            "stdout": "",
            "stderr": f"{argv[0]} could not be launched: {e}",
            "returncode": 127,
        }
    _ACTIVE_PROCESSES.add(proc)
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        await _terminate_process(proc)
        return {
            "ok": False,
            "stdout": "",
            "stderr": f"{argv[0]} timed out after {timeout}s",
            "returncode": -1,
        }
    except asyncio.CancelledError:
        await _terminate_process(proc)
        raise
    finally:
        _ACTIVE_PROCESSES.discard(proc)
    return {
        "ok": proc.returncode == 0,
        "stdout": _scrub(stdout.decode(errors="replace")),
        "stderr": _scrub(stderr.decode(errors="replace")),
        "returncode": proc.returncode,
    }


async def _run_argv_streaming(
    argv: tuple[str, ...],
    timeout: int,
    on_line: LineFn,
) -> dict[str, Any]:
    """Run `argv` streaming stdout/stderr line-by-line through `on_line`.
    Returns {ok, returncode, stderr?}. `on_line` is awaited per line so it
    can broadcast to WS clients."""
    resolved = (
        resolve_cli_binary(argv[0], respect_installation_profile=False)
        or argv[0]
    )
    try:
        proc = await asyncio.create_subprocess_exec(
            resolved, *argv[1:],
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, OSError) as e:
        return {
            "ok": False,
            "returncode": 127,
            "stderr": f"{argv[0]} could not be launched: {e}",
        }

    _ACTIVE_PROCESSES.add(proc)

    async def drain(stream: asyncio.StreamReader, name: str) -> None:
        while True:
            line = await stream.readline()
            if not line:
                break
            await on_line(name, _scrub(line.decode(errors="replace").rstrip("\r\n")))

    try:
        await asyncio.wait_for(
            asyncio.gather(drain(proc.stdout, "stdout"), drain(proc.stderr, "stderr")),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        await _terminate_process(proc)
        return {
            "ok": False,
            "returncode": -1,
            "stderr": f"{argv[0]} timed out after {timeout}s",
        }
    except asyncio.CancelledError:
        await _terminate_process(proc)
        raise
    finally:
        _ACTIVE_PROCESSES.discard(proc)
    await proc.wait()
    return {"ok": proc.returncode == 0, "returncode": proc.returncode}


async def _terminate_process(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        await proc.wait()
        return
    try:
        proc.terminate()
    except ProcessLookupError:
        await proc.wait()
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=2)
    except asyncio.TimeoutError:
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        await proc.wait()


async def _run_installer_script(installer: ProviderInstaller, timeout: int) -> dict[str, Any]:
    if installer.install_script_url not in {_AGY_INSTALL_SH, _AGY_INSTALL_PS1}:
        return {
            "ok": False,
            "stdout": "",
            "stderr": "installer URL is not allowlisted",
            "returncode": 126,
        }
    if not installer.install_script_sha256:
        return {
            "ok": False,
            "stdout": "",
            "stderr": "installer hash is not pinned",
            "returncode": 126,
        }
    suffix = ".ps1" if installer.install_script_url.endswith(".ps1") else ".sh"
    try:
        with tempfile.TemporaryDirectory(prefix="bc-provider-install-") as tmp:
            path = Path(tmp) / f"install{suffix}"
            await asyncio.to_thread(
                _download_installer_script,
                installer.install_script_url,
                installer.install_script_sha256,
                path,
            )
            argv = tuple(str(path) if arg == _INSTALLER_SCRIPT_ARG else arg for arg in installer.install_argv)
            return await _run_argv(argv, timeout=timeout)
    except Exception as exc:
        return {
            "ok": False,
            "stdout": "",
            "stderr": _scrub(str(exc)),
            "returncode": 1,
        }


async def _run_installer_script_streaming(
    installer: ProviderInstaller,
    timeout: int,
    on_line: LineFn,
) -> dict[str, Any]:
    """Streaming counterpart of `_run_installer_script`: downloads the
    allowlisted, hash-pinned script, then streams its output."""
    if installer.install_script_url not in {_AGY_INSTALL_SH, _AGY_INSTALL_PS1}:
        return {"ok": False, "returncode": 126, "stderr": "installer URL is not allowlisted"}
    if not installer.install_script_sha256:
        return {"ok": False, "returncode": 126, "stderr": "installer hash is not pinned"}
    suffix = ".ps1" if installer.install_script_url.endswith(".ps1") else ".sh"
    try:
        with tempfile.TemporaryDirectory(prefix="bc-provider-install-") as tmp:
            path = Path(tmp) / f"install{suffix}"
            await asyncio.to_thread(
                _download_installer_script,
                installer.install_script_url,
                installer.install_script_sha256,
                path,
            )
            argv = tuple(
                str(path) if arg == _INSTALLER_SCRIPT_ARG else arg
                for arg in installer.install_argv
            )
            return await _run_argv_streaming(argv, timeout, on_line)
    except Exception as exc:
        return {"ok": False, "returncode": 1, "stderr": _scrub(str(exc))}


def _download_installer_script(url: str, expected_sha256: str, path: Path) -> None:
    with urllib.request.urlopen(url, timeout=30) as response:
        body = response.read()
    actual_sha256 = hashlib.sha256(body).hexdigest()
    if not expected_sha256 or actual_sha256.lower() != expected_sha256.lower():
        raise RuntimeError("installer hash mismatch")
    path.write_bytes(body)
    path.chmod(0o700)


def _refresh_windows_path() -> None:
    """Refresh PATH after installers like winget update user/machine env.

    The running backend process does not receive Windows environment broadcasts,
    so npm installed by Node's MSI can be invisible until restart unless we
    re-read registry PATH values here.
    """
    if sys.platform != "win32":
        return
    try:
        import winreg
    except Exception:
        return

    paths: list[str] = []
    paths.extend(_windows_winget_node_paths())
    npm_global = _windows_npm_global_path()
    if npm_global:
        paths.append(npm_global)
    # User PATH first: user-scope winget installs are intentionally meant to
    # override stale machine-wide tools, e.g. old C:\Program Files\nodejs.
    entries = (
        (winreg.HKEY_CURRENT_USER, r"Environment"),
        (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
    )
    for root, subkey in entries:
        try:
            with winreg.OpenKey(root, subkey) as key:
                value, _ = winreg.QueryValueEx(key, "Path")
        except OSError:
            continue
        if value:
            paths.append(os.path.expandvars(str(value)))
    current = os.environ.get("PATH", "")
    if current:
        paths.append(current)
    if paths:
        os.environ["PATH"] = os.pathsep.join(paths)


def _windows_winget_node_paths() -> list[str]:
    if sys.platform != "win32":
        return []
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return []
    packages = Path(local) / "Microsoft" / "WinGet" / "Packages"
    if not packages.exists():
        return []
    matches = sorted(
        packages.glob("OpenJS.NodeJS.LTS_*/*"),
        key=lambda p: p.stat().st_mtime if p.exists() else 0,
        reverse=True,
    )
    return [str(p) for p in matches if (p / "node.exe").exists()]


def _windows_npm_global_path() -> str:
    if sys.platform != "win32":
        return ""
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return ""
    return str(Path(appdata) / "npm")


def _public_status(
    installer: ProviderInstaller,
    prerequisite: dict[str, Any],
    cli: dict[str, Any],
    install: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "kind": installer.kind,
        "label": installer.label,
        "command": installer.command,
        "install_command": list(installer.install_argv),
        "prerequisite_command": installer.prerequisite_argv[0],
        "prerequisite_install_command": list(installer.prerequisite_install_argv),
        "prerequisite_installable": bool(installer.prerequisite_install_argv),
        "prerequisite": prerequisite,
        "installed": bool(cli["ok"]),
        "verify": cli,
        "install": install,
    }


def _scrub(text: str) -> str:
    out = []
    for line in text.splitlines()[-80:]:
        if any(token in line.lower() for token in ("api_key", "apikey", "token=", "secret=")):
            out.append("[redacted]")
        else:
            out.append(line)
    return "\n".join(out)
