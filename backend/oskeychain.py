"""Read and write one secret through the OS credential store."""

from __future__ import annotations

import subprocess
import sys
from typing import Optional

_TIMEOUT = 5  # seconds — fail loud rather than hang on a locked keychain


def disable_native_user_interaction() -> None:
    if sys.platform != "darwin":
        return
    import ctypes

    security = ctypes.CDLL(
        "/System/Library/Frameworks/Security.framework/Security"
    )
    set_interaction_allowed = security.SecKeychainSetUserInteractionAllowed
    set_interaction_allowed.argtypes = [ctypes.c_ubyte]
    set_interaction_allowed.restype = ctypes.c_int32
    if set_interaction_allowed(0) != 0:
        raise RuntimeError("failed to disable OS credential interaction")


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
        from keyring.errors import KeyringError

        try:
            keyring.set_password(service, account, value)
        except KeyringError:
            raise RuntimeError("OS credential write was denied or unavailable") from None
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


def get(
    service: str,
    account: str,
    *,
    timeout: float = _TIMEOUT,
) -> Optional[str]:
    """Read one credential entry. Returns None when the entry is absent."""
    if sys.platform != "darwin":
        import keyring
        from keyring.errors import KeyringError

        try:
            return keyring.get_password(service, account)
        except KeyringError:
            raise RuntimeError("OS credential read was denied or unavailable") from None
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
        from keyring.errors import KeyringError, PasswordDeleteError

        try:
            keyring.delete_password(service, account)
        except PasswordDeleteError:
            pass
        except KeyringError:
            raise RuntimeError("OS credential delete was denied or unavailable") from None
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


def native_get(service: str, account: str) -> Optional[str]:
    if sys.platform != "darwin":
        return get(service, account)
    from keyring.backends.macOS import api

    try:
        return api.find_generic_password(None, service, account, not_found_ok=True)
    except api.Error:
        raise RuntimeError("native OS credential read was denied or unavailable") from None


def native_store(service: str, account: str, value: str) -> None:
    if sys.platform != "darwin":
        store(service, account, value)
        return
    from keyring.backends.macOS import api

    try:
        api.set_generic_password(None, service, account, value)
    except api.Error:
        raise RuntimeError("native OS credential write was denied or unavailable") from None


def native_delete(service: str, account: str) -> None:
    if sys.platform != "darwin":
        delete(service, account)
        return
    from keyring.backends.macOS import api

    try:
        api.delete_generic_password(None, service, account)
    except api.NotFound:
        pass
    except api.Error:
        raise RuntimeError("native OS credential delete was denied or unavailable") from None
