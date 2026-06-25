from __future__ import annotations

import asyncio
import hashlib
import shutil
import sys
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ProviderInstaller:
    kind: str
    label: str
    command: str
    install_argv: tuple[str, ...]
    verify_argv: tuple[str, ...]
    prerequisite_argv: tuple[str, ...]
    install_script_url: str = ""
    install_script_sha256: str = ""


_INSTALLER_SCRIPT_ARG = "<downloaded-installer>"
_AGY_INSTALL_SH = "https://antigravity.google/cli/install.sh"
_AGY_INSTALL_PS1 = "https://antigravity.google/cli/install.ps1"
_AGY_INSTALL_SH_SHA256 = "ee1ea43ce4e9e56356c4ab6dad907ef357ae4bdfcaadb682735909fb57c9c640"
_AGY_INSTALL_PS1_SHA256 = "51c2cb4fada22ce0228da71b9506370383d6544bfebcec85fe7616a52b805344"


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


INSTALLERS: dict[str, ProviderInstaller] = {
    "claude": ProviderInstaller(
        kind="claude",
        label="Claude Code",
        command="claude",
        install_argv=("npm", "install", "-g", "@anthropic-ai/claude-code"),
        verify_argv=("claude", "--version"),
        prerequisite_argv=("npm", "--version"),
    ),
    "codex": ProviderInstaller(
        kind="codex",
        label="Codex CLI",
        command="codex",
        install_argv=("npm", "install", "-g", "@openai/codex"),
        verify_argv=("codex", "--version"),
        prerequisite_argv=("npm", "--version"),
    ),
    "agy": _agy_installer(),
    "copilot": ProviderInstaller(
        kind="copilot",
        label="GitHub Copilot CLI",
        command="copilot",
        # copilot-cli ships as a Homebrew cask (`brew install copilot-cli`).
        # `gh copilot` is the alternate managed-download path; both rely on
        # `gh auth login` for OAuth, done outside the installer.
        install_argv=("brew", "install", "copilot-cli"),
        verify_argv=("copilot", "--version"),
        prerequisite_argv=("brew", "--version"),
    ),
}


def supported_provider_kinds() -> list[str]:
    return sorted(INSTALLERS)


def installer_for(kind: str) -> ProviderInstaller:
    installer = INSTALLERS.get(str(kind or "").strip())
    if installer is None:
        raise ValueError("unsupported provider kind")
    return installer


async def provider_setup_status(kind: str) -> dict[str, Any]:
    installer = installer_for(kind)
    prerequisite = await _check_argv(installer.prerequisite_argv)
    cli = await _check_argv(installer.verify_argv)
    return _public_status(installer, prerequisite, cli)


async def install_provider_cli(kind: str) -> dict[str, Any]:
    installer = installer_for(kind)
    prerequisite = await _check_argv(installer.prerequisite_argv)
    if not prerequisite["ok"]:
        return _public_status(
            installer,
            prerequisite,
            await _check_argv(installer.verify_argv),
            install={
                "ok": False,
                "stdout": "",
                "stderr": f"Missing prerequisite: {installer.prerequisite_argv[0]}",
                "returncode": 127,
            },
        )
    install = (
        await _run_installer_script(installer, timeout=300)
        if installer.install_script_url
        else await _run_argv(installer.install_argv, timeout=300)
    )
    cli = await _check_argv(installer.verify_argv)
    return _public_status(installer, prerequisite, cli, install=install)


async def _check_argv(argv: tuple[str, ...]) -> dict[str, Any]:
    if not shutil.which(argv[0]):
        return {"ok": False, "stdout": "", "stderr": f"{argv[0]} not found", "returncode": 127}
    return await _run_argv(argv, timeout=10)


async def _run_argv(argv: tuple[str, ...], timeout: int) -> dict[str, Any]:
    # Resolve argv[0] to its full path so Windows picks up `.exe`/`.cmd`
    # shims that a bare-name exec misses. A missing or unlaunchable binary
    # must degrade to "not available", never raise and 500 the caller
    # (e.g. provider-setup/status) — that was a Windows-only crash because
    # create_subprocess_exec can't launch a bare CLI name there.
    resolved = shutil.which(argv[0]) or argv[0]
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
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        return {
            "ok": False,
            "stdout": "",
            "stderr": f"{argv[0]} timed out after {timeout}s",
            "returncode": -1,
        }
    return {
        "ok": proc.returncode == 0,
        "stdout": _scrub(stdout.decode(errors="replace")),
        "stderr": _scrub(stderr.decode(errors="replace")),
        "returncode": proc.returncode,
    }


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


def _download_installer_script(url: str, expected_sha256: str, path: Path) -> None:
    with urllib.request.urlopen(url, timeout=30) as response:
        body = response.read()
    actual_sha256 = hashlib.sha256(body).hexdigest()
    if not expected_sha256 or actual_sha256.lower() != expected_sha256.lower():
        raise RuntimeError("installer hash mismatch")
    path.write_bytes(body)
    path.chmod(0o700)


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
