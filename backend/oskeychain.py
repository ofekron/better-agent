"""Read and write one secret through the OS credential store."""

from __future__ import annotations

import subprocess
import sys
from typing import Optional

_TIMEOUT = 5  # seconds — fail loud rather than hang on a locked keychain


def _native_command(op: str, service: str, account: str, reason: str) -> list[str]:
    args = ["--keychain-helper"] if getattr(sys, "frozen", False) else [
        "-m", "oskeychain", "--keychain-helper",
    ]
    return [sys.executable, *args, op, service, account, reason]


def _run_native(
    op: str,
    service: str,
    account: str,
    reason: str,
    *,
    value: str | None = None,
    timeout: float = _TIMEOUT,
) -> Optional[str]:
    try:
        proc = subprocess.run(
            _native_command(op, service, account, reason),
            input=value,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"OS credential {op} timed out") from None
    if proc.returncode == 44:
        return None
    if proc.returncode != 0:
        raise RuntimeError(f"OS credential {op} was denied or unavailable")
    return proc.stdout


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


def store(service: str, account: str, value: str, *, reason: str | None = None) -> None:
    """Write (replacing) one credential entry. Raises on failure."""
    if sys.platform != "darwin":
        import keyring
        from keyring.errors import KeyringError

        try:
            keyring.set_password(service, account, value)
        except KeyringError:
            raise RuntimeError("OS credential write was denied or unavailable") from None
        return
    if reason:
        _run_native("store", service, account, reason, value=value)
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
    reason: str | None = None,
) -> Optional[str]:
    """Read one credential entry. Returns None when the entry is absent."""
    if sys.platform != "darwin":
        import keyring
        from keyring.errors import KeyringError

        try:
            return keyring.get_password(service, account)
        except KeyringError:
            raise RuntimeError("OS credential read was denied or unavailable") from None
    if reason:
        return _run_native(
            "read", service, account, reason, timeout=timeout,
        )
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


def delete(service: str, account: str, *, reason: str | None = None) -> None:
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
    if reason:
        _run_native("delete", service, account, reason)
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


def _native_helper(op: str, service: str, account: str, reason: str) -> int:
    import ctypes
    from keyring.backends.macOS import api

    try:
        query = api.create_query(
            kSecClass=api.k_("kSecClassGenericPassword"),
            kSecAttrService=service,
            kSecAttrAccount=account,
            kSecUseOperationPrompt=reason,
        )
        if op == "read":
            query = api.create_query(
                kSecClass=api.k_("kSecClassGenericPassword"),
                kSecMatchLimit=api.k_("kSecMatchLimitOne"),
                kSecAttrService=service,
                kSecAttrAccount=account,
                kSecReturnData=True,
                kSecUseOperationPrompt=reason,
            )
            data = ctypes.c_void_p()
            status = api.SecItemCopyMatching(query, ctypes.byref(data))
            if status == api.error.item_not_found:
                return 44
            api.Error.raise_for_status(status)
            sys.stdout.write(api.cfstr_to_str(data))
            return 0
        if op == "delete":
            status = api.SecItemDelete(query)
            if status == api.error.item_not_found:
                return 44
            api.Error.raise_for_status(status)
            return 0
        if op != "store":
            return 2
        value = sys.stdin.read()
        _native_store(api, query, service, account, value, reason)
        return 0
    except api.NotFound:
        return 44
    except api.Error:
        return 1


def _native_store(api, query, service: str, account: str, value: str, reason: str) -> None:
    import ctypes

    update_values = api.create_query(kSecValueData=value)
    sec_item_update = api._sec.SecItemUpdate
    sec_item_update.restype = api.OS_status
    sec_item_update.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
    status = sec_item_update(query, update_values)
    if status == api.error.item_not_found:
        add_query = api.create_query(
            kSecClass=api.k_("kSecClassGenericPassword"),
            kSecAttrService=service,
            kSecAttrAccount=account,
            kSecValueData=value,
            kSecUseOperationPrompt=reason,
        )
        status = api.SecItemAdd(add_query, None)
    api.Error.raise_for_status(status)


def _main(argv: list[str]) -> int:
    if len(argv) != 5 or argv[0] != "--keychain-helper":
        return 2
    return _native_helper(argv[1], argv[2], argv[3], argv[4])


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
