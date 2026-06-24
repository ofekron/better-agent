#!/usr/bin/env python3

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import file_ref_resolver as resolver


def main() -> None:
    text = "a" * 500_000 + "."
    started = time.monotonic()
    assert resolver.rewrite_text(text, "/tmp") == text
    elapsed = time.monotonic() - started
    assert elapsed < 1.0, f"file-reference scan took {elapsed:.2f}s"
    print(f"PASS: large non-path scanned in {elapsed:.3f}s")


if __name__ == "__main__":
    main()
