#!/usr/bin/env python3

from __future__ import annotations

import sys
import time
from pathlib import Path

import _test_home

_test_home.isolate("bc-test-file-ref-perf-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import file_ref_resolver as resolver  # noqa: E402


def test_file_ref_scan_performance():
    text = "a" * 500_000 + "."
    started = time.monotonic()
    assert resolver.rewrite_text(text, "/tmp") == text
    elapsed = time.monotonic() - started
    assert elapsed < 1.0, f"file-reference scan took {elapsed:.2f}s"
    assert resolver.rewrite_text("see missing.py", "/tmp") == "see missing.py"
    cache_size = len(resolver._cwd_path_cache._d)
    assert resolver.rewrite_text("see also missing.py", "/tmp") == "see also missing.py"
    assert len(resolver._cwd_path_cache._d) == cache_size
