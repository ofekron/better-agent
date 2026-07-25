#!/usr/bin/env python3
"""Print the argon2id hash of a password, for BETTER_AGENT_PASSWORD_HASH_FILE.

Headless container mode (BETTER_AGENT_HEADLESS_AUTH=1, see
backend/auth_secrets.py and docker/README.md) reads a pre-computed argon2
hash from a file rather than ever handling a plaintext password itself.
This script produces that hash using the same argon2 parameters the
backend verifies against.

Usage:
    ./scripts/hash-password.py --out docker/secrets/password_hash
    ./scripts/hash-password.py --password-file /path/to/plaintext --out docker/secrets/password_hash
    ./scripts/hash-password.py                                          # prints to stdout

Never pass the password as a positional/flag argument — that leaks it via
shell history and `ps`. Prompts securely (no echo) by default.

Prefer --out over shell redirection (`... > secrets/password_hash`):
redirection truncates the destination file the instant the shell opens it,
before this script runs, so a mismatched confirmation or Ctrl-C leaves an
EMPTY file behind. `auth_secrets`'s headless mode then fails to load
credentials at boot — see backend/auth.py's `_load()` — which used to
degrade silently into an uncompletable first-run setup screen; it now
fails loud instead, but an empty hash file is still a footgun redirection
makes easy and --out avoids by writing atomically only on success.
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import auth_secrets  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--password-file",
        type=Path,
        help="read the plaintext password from this file instead of prompting "
        "(the file itself is never written to; only read)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        help="write the hash here atomically (temp file + rename) instead of stdout; "
        "nothing is written if hashing fails or is aborted",
    )
    args = parser.parse_args()

    if args.password_file:
        password = args.password_file.read_text(encoding="utf-8").strip()
    else:
        password = getpass.getpass("Password to hash: ")
        confirm = getpass.getpass("Confirm password: ")
        if password != confirm:
            print("hash-password: passwords did not match", file=sys.stderr)
            return 1

    if not password:
        print("hash-password: password must be non-empty", file=sys.stderr)
        return 1

    password_hash = auth_secrets.make_password_hash(password)

    if not args.out:
        print(password_hash)
        return 0

    tmp_path = args.out.with_name(f".{args.out.name}.tmp{os.getpid()}")
    # 0o600 from creation, not a chmod afterward: an argon2 hash is an
    # offline-crack target, and the file briefly exists at whatever the
    # umask would otherwise give it (often world-readable) between
    # os.open and any later chmod.
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, (password_hash + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    os.replace(tmp_path, args.out)
    print(f"hash-password: wrote {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
