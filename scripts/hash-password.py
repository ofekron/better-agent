#!/usr/bin/env python3
"""Print the argon2id hash of a password, for BETTER_AGENT_PASSWORD_HASH_FILE.

Headless container mode (BETTER_AGENT_HEADLESS_AUTH=1, see
backend/auth_secrets.py and docker/README.md) reads a pre-computed argon2
hash from a file rather than ever handling a plaintext password itself.
This script produces that hash using the same argon2 parameters the
backend verifies against.

Usage:
    ./scripts/hash-password.py
    ./scripts/hash-password.py --password-file /path/to/plaintext   # non-interactive

Never pass the password as a positional/flag argument — that leaks it via
shell history and `ps`. Prompts securely (no echo) by default.
"""
from __future__ import annotations

import argparse
import getpass
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

    print(auth_secrets.make_password_hash(password))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
