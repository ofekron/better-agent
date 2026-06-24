"""Write one secret to the OS credential store, per platform.

macOS MUST shell out to /usr/bin/security; do NOT use the python
`keyring` package there. The keychain ACL is bound to the caller
binary — entries written by `security` are readable by `security`
without a GUI prompt, while keyring-written entries bind the ACL to
the python interpreter and prompt on cross-binary reads (see
auth_secrets.py module docstring). Non-macOS uses `keyring`, which
maps to Windows Credential Manager / Linux Secret Service.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Optional

_TIMEOUT = 5  # seconds — fail loud rather than hang on a locked keychain


def store(service: str, account: str, value: str) -> None:
    """Write (replacing) one credential entry. Raises on failure."""
    if sys.platform != "darwin":
        import keyring

        keyring.set_password(service, account, value)
        return
    subprocess.run(
        ["/usr/bin/security", "add-generic-password", "-U",
         "-s", service, "-a", account, "-w", value],
        check=True, timeout=_TIMEOUT,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def get(service: str, account: str) -> Optional[str]:
    """Read one credential entry. Returns None when the entry is absent."""
    if sys.platform != "darwin":
        import keyring

        return keyring.get_password(service, account)
    try:
        return subprocess.check_output(
            ["/usr/bin/security", "find-generic-password",
             "-s", service, "-a", account, "-w"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=_TIMEOUT,
        )
    except subprocess.CalledProcessError:
        return None


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
