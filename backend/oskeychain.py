"""Read and write one secret through the OS credential store.

macOS reads use the stable Apple ``security`` binary identity. Writes use the
native Keyring API because the ``security`` CLI accepts plaintext only in argv.
Other platforms use Keyring for both operations.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Optional

_TIMEOUT = 5  # seconds — fail loud rather than hang on a locked keychain


def store(service: str, account: str, value: str) -> None:
    """Write (replacing) one credential entry. Raises on failure."""
    import keyring

    keyring.set_password(service, account, value)


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
    subprocess.run(
        ["/usr/bin/security", "delete-generic-password",
         "-s", service, "-a", account],
        check=False, timeout=_TIMEOUT,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
