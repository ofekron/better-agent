#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

LABEL = "com.betteragent.repository"
UNIT = "better-agent.service"


def _canonical_checkout(raw: str) -> Path:
    checkout = Path(raw).expanduser().resolve()
    if not checkout.is_absolute() or not (checkout / "run.sh").is_file():
        raise ValueError("checkout must contain run.sh")
    return checkout


def _canonical_home(raw: str) -> Path:
    home = Path(raw).expanduser().resolve()
    if not home.is_absolute():
        raise ValueError("state home must be absolute")
    home.mkdir(parents=True, exist_ok=True)
    return home


def launch_agent(checkout: Path, home: Path) -> dict:
    return {
        "Label": LABEL,
        "ProgramArguments": ["/bin/bash", str(checkout / "run.sh"), "--service-child"],
        "WorkingDirectory": str(checkout),
        "EnvironmentVariables": {
            "BETTER_AGENT_HOME": str(home),
            "BETTER_CLAUDE_HOME": str(home),
        },
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 5,
        "ProcessType": "Interactive",
        "StandardOutPath": str(home / "run-service.log"),
        "StandardErrorPath": str(home / "run-service.log"),
    }


def systemd_unit(checkout: Path, home: Path) -> str:
    def quote(value: str) -> str:
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

    return "\n".join((
        "[Unit]",
        "Description=Better Agent repository service",
        "After=network.target",
        "",
        "[Service]",
        "Type=simple",
        f"WorkingDirectory={quote(str(checkout))}",
        f"Environment={quote('BETTER_AGENT_HOME=' + str(home))}",
        f"Environment={quote('BETTER_CLAUDE_HOME=' + str(home))}",
        f"ExecStart=/bin/bash {quote(str(checkout / 'run.sh'))} --service-child",
        "Restart=always",
        "RestartSec=5",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ))


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


def _run(command: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(command, capture_output=True, text=True, timeout=20)


def install_macos(checkout: Path, home: Path) -> None:
    path = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    _atomic_write(path, plistlib.dumps(launch_agent(checkout, home)))
    domain = f"gui/{os.getuid()}"
    _run(["launchctl", "bootout", f"{domain}/{LABEL}"])
    result = _run(["launchctl", "bootstrap", domain, str(path)])
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "launchctl bootstrap failed")


def uninstall_macos() -> None:
    path = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    _run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"])
    path.unlink(missing_ok=True)


def status_macos() -> bool:
    result = _run(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"])
    return result.returncode == 0 and "state = running" in result.stdout


def install_linux(checkout: Path, home: Path) -> None:
    path = Path.home() / ".config" / "systemd" / "user" / UNIT
    _atomic_write(path, systemd_unit(checkout, home).encode())
    result = _run(["systemctl", "--user", "daemon-reload"])
    if result.returncode == 0:
        result = _run(["systemctl", "--user", "enable", "--now", UNIT])
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "systemd service installation failed")


def uninstall_linux() -> None:
    _run(["systemctl", "--user", "disable", "--now", UNIT])
    (Path.home() / ".config" / "systemd" / "user" / UNIT).unlink(missing_ok=True)
    _run(["systemctl", "--user", "daemon-reload"])


def status_linux() -> bool:
    return _run(["systemctl", "--user", "is-active", "--quiet", UNIT]).returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("install", "uninstall", "status"))
    parser.add_argument("--checkout", required=True)
    parser.add_argument("--home", required=True)
    args = parser.parse_args()
    checkout = _canonical_checkout(args.checkout)
    home = _canonical_home(args.home)
    if sys.platform == "darwin":
        install, uninstall, status = install_macos, uninstall_macos, status_macos
    elif sys.platform.startswith("linux") and shutil.which("systemctl"):
        install, uninstall, status = install_linux, uninstall_linux, status_linux
    else:
        raise RuntimeError("repository service mode requires macOS launchd or Linux systemd")
    if args.action == "install":
        install(checkout, home)
        print(f"Better Agent service installed for {checkout}")
        return 0
    if args.action == "uninstall":
        uninstall()
        print("Better Agent service removed")
        return 0
    running = status()
    print("Better Agent service is running" if running else "Better Agent service is not running")
    return 0 if running else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
