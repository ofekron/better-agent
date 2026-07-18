#!/usr/bin/env python3
"""A keychain timeout on one entry must not disable keyring for other entries.
Before the fix a single ACL-locked item flipped a process-wide flag, so every
later read (e.g. a perfectly readable provider key) silently returned empty."""
from __future__ import annotations

import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import paths  # noqa: E402

paths.engage_test_home(tempfile.mkdtemp(prefix="ba-keyring-scope-"))

import config_store  # noqa: E402


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"PASS {message}")


def hang_forever(service: str, account: str) -> str:
    time.sleep(60)
    return "never"


def fast_read(service: str, account: str) -> str:
    return f"value-for-{account}"


def main() -> None:
    config_store._KEYRING_TIMEOUT = 0.2

    flag: list[bool] = []
    value = config_store._keyring_call(
        hang_forever, "svc", "locked-item", default="", failure_flag=flag,
    )
    check(value == "" and flag, "ACL-locked entry times out and reports failure")

    value = config_store._keyring_call(fast_read, "svc", "readable-key", default="")
    check(value == "value-for-readable-key", "other entries stay readable after a timeout")

    flag2: list[bool] = []
    started = time.monotonic()
    value = config_store._keyring_call(
        hang_forever, "svc", "locked-item", default="", failure_flag=flag2,
    )
    elapsed = time.monotonic() - started
    check(value == "" and flag2, "blocked entry keeps failing")
    check(elapsed < 0.1, "blocked entry short-circuits without burning another timeout")

    leftover = [t for t in threading.enumerate() if t.name.startswith("keyring-")]
    check(len(leftover) <= 1, "only the hung worker thread remains")
    print("OK")


if __name__ == "__main__":
    main()
