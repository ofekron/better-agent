from __future__ import annotations

import argparse
import os
import stat
from pathlib import Path


def consume_restart_request(
    path: Path,
    *,
    not_before: float | None = None,
) -> str | None:
    try:
        path_details = path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(path_details.st_mode):
        raise OSError("restart request must not be a symlink")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None
    try:
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_size > 64
            or (details.st_dev, details.st_ino)
            != (path_details.st_dev, path_details.st_ino)
        ):
            raise OSError("restart request must be a small regular file")
        request_id = os.read(descriptor, 65).decode("ascii")
    except (UnicodeDecodeError, OSError):
        raise OSError("restart request is invalid") from None
    finally:
        os.close(descriptor)
    path.unlink()
    if not_before is not None and details.st_mtime < not_before:
        return None
    if (
        len(request_id) != 32
        or any(character not in "0123456789abcdef" for character in request_id)
    ):
        raise OSError("restart request id is invalid")
    return request_id


def clear_restart_request(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    parser.add_argument("--not-before", type=float)
    parser.add_argument("--clear", action="store_true")
    args = parser.parse_args(argv)
    if args.clear:
        if args.not_before is not None:
            parser.error("--clear cannot be combined with --not-before")
        clear_restart_request(args.path)
        return 0
    request_id = consume_restart_request(
        args.path,
        not_before=args.not_before,
    )
    if request_id is not None:
        print(request_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
