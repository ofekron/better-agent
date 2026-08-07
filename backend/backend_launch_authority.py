from __future__ import annotations

import json
import os
import re
import stat
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend_instance_lock import PrimaryBackendInstanceReservation
from primary_launcher_lease import PrimaryLauncherLease

from env_compat import get_env
from paths import ba_home, make_private_file

_AUTHORITY_FILE = "backend_launch_authority.json"
_GENERATION_ENV = "BETTER_AGENT_BACKEND_LAUNCH_GENERATION"
_ACTIVE_CHECKOUT_ENV = "BETTER_AGENT_ACTIVE_CHECKOUT"
_LAUNCHER_LEASE_ID_ENV = "BETTER_AGENT_PRIMARY_LAUNCHER_LEASE_ID"
_BACKEND_RESERVATION_ID_ENV = "BETTER_AGENT_BACKEND_RESERVATION_ID"
_VERSION = 2
_POINTER_STATES = frozenset({"active", "switching", "reverted"})
_RECORD_KEYS = frozenset({
    "version",
    "generation",
    "checkout",
    "state_root",
    "role",
    "issuer_pid",
    "issued_at",
    "launcher_lease_id",
    "backend_reservation_id",
    "backend_lock_identity",
})
_HEX_32_RE = re.compile(r"[0-9a-f]{32}")


@dataclass(frozen=True)
class LaunchAuthority:
    generation: str
    checkout: Path
    state_root: Path
    launcher_lease_id: str
    backend_reservation_id: str


def launch_env_keys() -> tuple[str, ...]:
    return (
        _GENERATION_ENV,
        _ACTIVE_CHECKOUT_ENV,
        _LAUNCHER_LEASE_ID_ENV,
        _BACKEND_RESERVATION_ID_ENV,
        "BETTER_AGENT_BACKEND_INSTANCE_LOCK",
        "BETTER_AGENT_BACKEND_INSTANCE_ACK",
        "BETTER_CLAUDE_ACTIVE_CHECKOUT",
    )


def _canonical(path: Path | str) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute() or ".." in candidate.parts:
        raise RuntimeError("backend launch authority paths must be absolute")
    return candidate.resolve()


def _authority_path(root: Path) -> Path:
    return root / _AUTHORITY_FILE


def _write_private_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    fd = -1
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        offset = 0
        while offset < len(raw):
            written = os.write(fd, raw[offset:])
            if written <= 0:
                raise RuntimeError("backend launch authority write failed")
            offset += written
        os.fsync(fd)
        os.close(fd)
        fd = -1
        make_private_file(temporary)
        os.replace(temporary, path)
        make_private_file(path)
        # Directory fsync is POSIX-only; the Windows arm is exercised on Windows.
        # No single platform can run both arms, so exclude the branch decision.
        if os.name != "nt":  # pragma: no branch
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _read_object(path: Path, label: str) -> dict[str, Any]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        before = path.lstat()
        reparse = bool(
            getattr(before, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        )
        if stat.S_ISLNK(before.st_mode) or reparse or not stat.S_ISREG(before.st_mode):
            raise RuntimeError(f"backend launch authority {label} is invalid")
        fd = os.open(path, flags)
        try:
            opened = os.fstat(fd)
            raw = os.read(fd, 64 * 1024 + 1)
        finally:
            os.close(fd)
        after = path.lstat()
    except FileNotFoundError as exc:
        raise RuntimeError(f"backend launch authority {label} is missing") from exc
    except OSError as exc:
        raise RuntimeError(f"backend launch authority {label} is invalid") from exc
    if len(raw) > 64 * 1024:
        raise RuntimeError(f"backend launch authority {label} is invalid")
    if (
        not os.path.samestat(before, opened)
        or not os.path.samestat(opened, after)
        or opened.st_nlink != 1
        or after.st_nlink != 1
    ):
        raise RuntimeError(f"backend launch authority {label} changed during validation")
    try:
        value = json.loads(raw.decode())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"backend launch authority {label} is invalid") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"backend launch authority {label} is invalid")
    return value


def _validate_pointer(root: Path, checkout: Path) -> None:
    path = root / "active_checkout.json"
    try:
        path.lstat()
    except FileNotFoundError:
        return
    pointer = _read_object(path, "active checkout pointer")
    status = pointer.get("status")
    active = pointer.get("active")
    if status == "failed":
        return
    if status not in _POINTER_STATES or not isinstance(active, str) or not active:
        raise RuntimeError("backend launch authority active checkout pointer is invalid")
    if _canonical(active) != checkout:
        raise RuntimeError("backend launch authority does not match active checkout pointer")


def _record_path(record: dict[str, Any], key: str) -> Path:
    value = record.get(key)
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"backend launch authority record has invalid {key}")
    return _canonical(value)


def issue_primary_backend_launch(
    *,
    checkout: Path | str,
    launcher_lease: PrimaryLauncherLease,
    backend_reservation: PrimaryBackendInstanceReservation,
    state_root: Path | str | None = None,
    generation: str | None = None,
) -> dict[str, str]:
    if type(launcher_lease) is not PrimaryLauncherLease:
        raise RuntimeError("backend launch requires a concrete launcher lease")
    if type(backend_reservation) is not PrimaryBackendInstanceReservation:
        raise RuntimeError("backend launch requires a concrete backend reservation")
    root = _canonical(state_root or launcher_lease.canonical_home)
    selected_checkout = _canonical(checkout)
    generation = generation or uuid.uuid4().hex
    if _HEX_32_RE.fullmatch(generation) is None:
        raise ValueError("backend launch generation must be 32 lowercase hex characters")
    launcher_lease.assert_owner(root)
    backend_reservation.assert_owner(launcher_lease)
    # Defense-in-depth invariant, unreachable via the public API: a reservation is
    # always co-bound to its launcher lease's canonical home, and assert_owner(root)
    # above already proved root == lease home == reservation.canonical_home.
    if backend_reservation.canonical_home != os.path.normcase(str(root)):  # pragma: no cover
        raise RuntimeError("backend reservation state root does not match")
    _write_private_json(
        _authority_path(root),
        {
            "version": _VERSION,
            "generation": generation,
            "checkout": str(selected_checkout),
            "state_root": str(root),
            "role": "primary",
            "issuer_pid": os.getpid(),
            "issued_at": time.time(),
            "launcher_lease_id": launcher_lease.lease_id,
            "backend_reservation_id": backend_reservation.reservation_id,
            "backend_lock_identity": backend_reservation.lock_identity,
        },
    )
    return {
        _GENERATION_ENV: generation,
        _ACTIVE_CHECKOUT_ENV: str(selected_checkout),
        "BETTER_CLAUDE_ACTIVE_CHECKOUT": str(selected_checkout),
        **backend_reservation.child_env(),
    }


def consume_primary_backend_launch_authority(
    *,
    launcher_lease_id: str,
    backend_reservation_id: str,
    backend_lock_identity: str,
    executing_checkout: Path | str | None = None,
) -> LaunchAuthority:
    root = _canonical(ba_home())
    checkout = _canonical(executing_checkout or Path(__file__).resolve().parents[1])
    generation = os.environ.get(_GENERATION_ENV, "")
    active_checkout = get_env("BETTER_CLAUDE_ACTIVE_CHECKOUT") or ""
    if _HEX_32_RE.fullmatch(generation) is None or not active_checkout:
        raise RuntimeError("backend launch authority environment is missing")
    if _canonical(active_checkout) != checkout:
        raise RuntimeError("backend launch authority checkout does not match executable")
    path = _authority_path(root)
    record = _read_object(path, "record")
    if set(record) != _RECORD_KEYS:
        raise RuntimeError("backend launch authority record has unexpected fields")
    if (
        record.get("version") != _VERSION
        or record.get("role") != "primary"
        or not isinstance(record.get("issuer_pid"), int)
        or isinstance(record.get("issuer_pid"), bool)
        or record["issuer_pid"] <= 0
        or not isinstance(record.get("issued_at"), (int, float))
        or isinstance(record.get("issued_at"), bool)
        or record.get("generation") != generation
        or record.get("launcher_lease_id") != launcher_lease_id
        or record.get("backend_reservation_id") != backend_reservation_id
        or record.get("backend_lock_identity") != backend_lock_identity
        or _record_path(record, "checkout") != checkout
        or _record_path(record, "state_root") != root
    ):
        raise RuntimeError("backend launch authority binding does not match")
    _validate_pointer(root, checkout)
    authority = LaunchAuthority(
        generation,
        checkout,
        root,
        launcher_lease_id,
        backend_reservation_id,
    )
    path.unlink()
    # Directory fsync is POSIX-only; the Windows arm is exercised on Windows.
    if os.name != "nt":  # pragma: no branch
        directory_fd = os.open(root, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    for key in (_GENERATION_ENV, _LAUNCHER_LEASE_ID_ENV, _BACKEND_RESERVATION_ID_ENV):
        os.environ.pop(key, None)
    return authority
