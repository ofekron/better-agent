from __future__ import annotations

from dataclasses import dataclass

import psutil


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    create_time: float


def capture_process_identity(pid: int) -> ProcessIdentity | None:
    if pid <= 0:
        return None
    try:
        process = psutil.Process(pid)
        return ProcessIdentity(pid=pid, create_time=float(process.create_time()))
    except (psutil.Error, OSError, ValueError):
        return None


def process_identity_is_live(identity: ProcessIdentity) -> bool:
    current = capture_process_identity(identity.pid)
    return current == identity


def process_identity_is_proven_dead(identity: ProcessIdentity) -> bool:
    try:
        process = psutil.Process(identity.pid)
        current_create_time = float(process.create_time())
    except psutil.NoSuchProcess:
        return True
    except (psutil.AccessDenied, psutil.Error, OSError, ValueError):
        return False
    return current_create_time != identity.create_time
