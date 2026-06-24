#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import sys

import _test_home
_test_home.isolate("bc-test-ingestion-versions-")

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from ingestion_versions import (  # noqa: E402
    CODEX_INGESTION_VERSION,
    CLAUDE_INGESTION_VERSION,
    marker_matches_current,
    write_marker,
)
from run_recovery import (  # noqa: E402
    _ingestion_version_current,
    _mark_reconciled_if_safe,
    _native_source_exists,
)


def test_marker_requires_current_version() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        marker = Path(tmp) / "reconciled.marker"
        marker.touch()
        assert not marker_matches_current(marker, "codex")

        marker.write_text(json.dumps({"provider_kind": "codex", "ingestion_version": 1}))
        assert not marker_matches_current(marker, "codex")

        write_marker(marker, "codex")
        assert marker_matches_current(marker, "codex")
        assert not marker_matches_current(marker, "claude")

        marker.write_text(json.dumps({
            "provider_kind": "codex",
            "ingestion_version": CLAUDE_INGESTION_VERSION,
        }))
        assert not marker_matches_current(marker, "claude")


def test_old_version_requires_native_source_before_reingest() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        native = Path(tmp) / "rollout.jsonl"
        desc = {
            "provider_kind": "codex",
            "ingestion_version": CODEX_INGESTION_VERSION - 1,
            "jsonl_path": str(native),
        }
        assert not _ingestion_version_current(desc)
        assert not _native_source_exists(desc)

        native.write_text("{}\n", encoding="utf-8")
        assert _native_source_exists(desc)


def test_old_version_missing_source_does_not_write_current_marker() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        run_dir = Path(tmp) / "run"
        run_dir.mkdir()
        desc = {
            "provider_kind": "codex",
            "ingestion_version": CODEX_INGESTION_VERSION - 1,
            "jsonl_path": str(run_dir / "missing.jsonl"),
        }
        assert not _mark_reconciled_if_safe(
            run_dir.name,
            desc,
            "test old-version missing-source",
        )
        assert not (run_dir / "reconciled.marker").exists()


def main() -> int:
    test_marker_requires_current_version()
    test_old_version_requires_native_source_before_reingest()
    test_old_version_missing_source_does_not_write_current_marker()
    print("PASS: ingestion version markers")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
