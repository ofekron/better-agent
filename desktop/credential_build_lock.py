from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import NoReturn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend import portable_lock  # noqa: E402


def run_locked(lock_path: Path, command: list[str]) -> NoReturn:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        portable_lock.lock_ex(descriptor)
        os.set_inheritable(descriptor, True)
        os.execvpe(command[0], command, os.environ)
    finally:
        os.close(descriptor)


def main() -> int:
    if len(sys.argv) < 3:
        raise SystemExit("usage: credential_build_lock.py LOCK COMMAND [ARG ...]")
    run_locked(Path(sys.argv[1]), sys.argv[2:])


if __name__ == "__main__":
    raise SystemExit(main())
