from __future__ import annotations

import json
import os
import stat
import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_MAX_BYTES = 2 * 1024 * 1024
_BACKUPS = 3


def _rotate(path: Path) -> None:
    try:
        details = path.lstat()
        if not stat.S_ISREG(details.st_mode):
            raise OSError("backend exit journal must be a regular file")
        if details.st_size < _MAX_BYTES:
            return
    except FileNotFoundError:
        return
    oldest = path.with_name(f"{path.name}.{_BACKUPS}")
    try:
        oldest.unlink()
    except FileNotFoundError:
        pass
    for index in range(_BACKUPS - 1, 0, -1):
        source = path.with_name(f"{path.name}.{index}")
        if source.exists():
            source.replace(path.with_name(f"{path.name}.{index + 1}"))
    path.replace(path.with_name(f"{path.name}.1"))


def append_backend_exit(
    state_root: Path,
    *,
    source: str,
    exit_code: int,
    classification: str,
    decision: str,
    restart_attempt: int | None = None,
    generation_id: str = "",
    pid: int | None = None,
    checkout: str = "",
    request_id: str = "",
) -> None:
    state_root.mkdir(parents=True, exist_ok=True)
    path = state_root / "backend-exits.jsonl"
    _rotate(path)
    record: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "exit_code": exit_code,
        "classification": classification,
        "decision": decision,
    }
    if restart_attempt is not None:
        record["restart_attempt"] = restart_attempt
    if generation_id:
        record["generation_id"] = generation_id
    if pid is not None:
        record["pid"] = pid
    if checkout:
        record["checkout"] = checkout
    if request_id:
        record["request_id"] = request_id
    flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            raise OSError("backend exit journal must be a regular file")
        os.fchmod(descriptor, 0o600)
        payload = (
            json.dumps(record, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        written = 0
        while written < len(payload):
            written += os.write(descriptor, payload[written:])
    finally:
        os.close(descriptor)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-root", required=True, type=Path)
    parser.add_argument("--source", required=True)
    parser.add_argument("--exit-code", required=True, type=int)
    parser.add_argument("--classification", required=True)
    parser.add_argument("--decision", required=True)
    parser.add_argument("--restart-attempt", type=int)
    parser.add_argument("--generation-id", default="")
    parser.add_argument("--pid", type=int)
    parser.add_argument("--checkout", default="")
    parser.add_argument("--request-id", default="")
    args = parser.parse_args(argv)
    append_backend_exit(
        args.state_root,
        source=args.source,
        exit_code=args.exit_code,
        classification=args.classification,
        decision=args.decision,
        restart_attempt=args.restart_attempt,
        generation_id=args.generation_id,
        pid=args.pid,
        checkout=args.checkout,
        request_id=args.request_id,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
