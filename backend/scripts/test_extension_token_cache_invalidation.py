#!/usr/bin/env python3
"""Regression lock: the per-extension token registry cache must reflect
out-of-process writes to extension_tokens.json.

The cache previously keyed only on the file PATH, so once loaded it never
re-read the file within a process lifetime. A token minted/rotated by another
writer (reinstall rotation, another node/process, recovery) was invisible until
restart — resolve() returned None and the loopback call 403'd with no self-heal.

The cache now keys on (path, mtime_ns, size), mirroring the spec cache's
store_fingerprint, so an external write invalidates it.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import sys
from pathlib import Path

TMP_HOME = Path(tempfile.mkdtemp(prefix="bc-test-token-cache-"))
import _test_home
_test_home.isolate("ba-test-")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import extension_token_registry as reg  # noqa: E402


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"PASS {message}")


def test_external_write_invalidates_cache() -> None:
    token_a = reg.mint("ext.a")
    check(reg.resolve(token_a) == "ext.a", "freshly minted token resolves")

    # Simulate an out-of-process writer adding ext.b without touching this
    # process's in-memory cache (different content => different size + mtime).
    path = reg._path()
    data = json.loads(path.read_text(encoding="utf-8"))
    data["ext.b"] = "externally-minted-token-value-1234567890"
    path.write_text(json.dumps(data), encoding="utf-8")

    check(reg.resolve("externally-minted-token-value-1234567890") == "ext.b",
          "externally-written token resolves (cache invalidated on file change)")
    check(reg.resolve(token_a) == "ext.a", "original token still resolves after reload")


if __name__ == "__main__":
    try:
        test_external_write_invalidates_cache()
        print("ALL PASS")
    finally:
        shutil.rmtree(TMP_HOME, ignore_errors=True)
