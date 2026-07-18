"""Read and write one secret through the OS credential store."""

from __future__ import annotations

import subprocess
import sys
from typing import Optional

_TIMEOUT = 5  # seconds — fail loud rather than hang on a locked keychain


def _interactive_arg(value: str) -> str:
    if any(character in value for character in "\0\r\n"):
        raise ValueError("OS credential identifiers cannot contain control characters")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _store_input(service: str, account: str, value: str) -> bytes:
    args = (
        "add-generic-password",
        "-U",
        "-s",
        service,
        "-a",
        account,
        "-X",
        value.encode("utf-8").hex(),
    )
    return (" ".join(_interactive_arg(arg) for arg in args) + "\n").encode("utf-8")


def store(service: str, account: str, value: str) -> None:
    """Write (replacing) one credential entry. Raises on failure."""
    if sys.platform != "darwin":
        import keyring

        keyring.set_password(service, account, value)
        return
    timed_out = False
    try:
        proc = subprocess.run(
            ["/usr/bin/security", "-q", "-i"],
            input=_store_input(service, account, value),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        timed_out = True
    if timed_out:
        raise RuntimeError("OS credential write timed out") from None
    if proc.returncode != 0:
        raise RuntimeError("OS credential write was denied or unavailable") from None


def get(service: str, account: str, *, timeout: float = _TIMEOUT) -> Optional[str]:
    """Read one credential entry. Returns None when the entry is absent."""
    if sys.platform != "darwin":
        import keyring

        return keyring.get_password(service, account)
    timed_out = False
    try:
        proc = subprocess.run(
            ["/usr/bin/security", "find-generic-password",
             "-s", service, "-a", account, "-w"],
            stderr=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        timed_out = True
    if timed_out:
        raise RuntimeError("OS credential read timed out")
    if proc.returncode == 44:
        return None
    if proc.returncode != 0:
        raise RuntimeError("OS credential read was denied or unavailable")
    return proc.stdout


def delete(service: str, account: str) -> None:
    """Delete one credential entry. Missing entries are already absent."""
    if sys.platform != "darwin":
        import keyring
        from keyring.errors import PasswordDeleteError

        try:
            keyring.delete_password(service, account)
        except PasswordDeleteError:
            pass
        return
    try:
        proc = subprocess.run(
            ["/usr/bin/security", "delete-generic-password",
             "-s", service, "-a", account],
            check=False, timeout=_TIMEOUT,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("OS credential delete timed out") from None
    if proc.returncode not in {0, 44}:
        raise RuntimeError("OS credential delete was denied or unavailable")
