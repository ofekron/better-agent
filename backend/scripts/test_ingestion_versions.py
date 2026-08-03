#!/usr/bin/env python3

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import sys

import _test_home
_test_home.isolate("bc-test-ingestion-versions-")

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from ingestion_versions import (  # noqa: E402
    AGY_INGESTION_VERSION,
    CLAUDE_INGESTION_VERSION,
    CODEX_INGESTION_VERSION,
    COPILOT_INGESTION_VERSION,
    OPENAI_INGESTION_VERSION,
    current_ingestion_version,
    marker_data_matches_current,
    marker_matches_current,
    write_marker,
)
from run_recovery import (  # noqa: E402
    _ingestion_version_current,
    _mark_reconciled_terminal,
    _native_source_exists,
    _runs_root,
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


def test_old_version_missing_source_tombstones_marker() -> None:
    # Version-stale + native-source-missing can never be re-digested by
    # any pipeline version; a terminal mark tombstones it so startup
    # recovery stops re-queueing the run forever.
    run_id = "old-version-missing-source"
    run_dir = _runs_root() / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    desc = {
        "provider_kind": "codex",
        "ingestion_version": CODEX_INGESTION_VERSION - 1,
        "jsonl_path": str(run_dir / "missing.jsonl"),
    }
    assert _mark_reconciled_terminal(
        run_id,
        desc,
        "test old-version missing-source",
    )
    assert (run_dir / "reconciled.marker").exists()


def test_current_ingestion_version_per_provider() -> None:
    assert current_ingestion_version("codex") == CODEX_INGESTION_VERSION
    assert current_ingestion_version("claude") == CLAUDE_INGESTION_VERSION
    assert current_ingestion_version("agy") == AGY_INGESTION_VERSION
    assert current_ingestion_version("copilot") == COPILOT_INGESTION_VERSION
    assert current_ingestion_version("openai") == OPENAI_INGESTION_VERSION
    # Unknown / unset provider kinds never re-digest: they stay at version 1.
    assert current_ingestion_version("unknown-provider") == 1
    assert current_ingestion_version(None) == 1


def test_marker_data_matches_current_truth_table() -> None:
    assert marker_data_matches_current(
        {"provider_kind": "claude", "ingestion_version": CLAUDE_INGESTION_VERSION},
        "claude",
    )
    # Provider mismatch fails even at the right version.
    assert not marker_data_matches_current(
        {"provider_kind": "claude", "ingestion_version": CLAUDE_INGESTION_VERSION},
        "codex",
    )
    # Stale version for the right provider fails.
    assert not marker_data_matches_current(
        {"provider_kind": "claude", "ingestion_version": 1}, "claude"
    )


def test_marker_matches_current_rejects_non_file() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp) / "isdir"
        directory.mkdir()
        # A directory fails the file guard before any JSON is parsed.
        assert not marker_matches_current(directory, "codex")


def test_marker_matches_current_oserror_is_a_mismatch() -> None:
    # The stat guard exists for platforms where is_symlink/is_file raises
    # OSError (ELOOP, EACCES). That OS condition is not reproducible on the
    # macOS/Docker test host (pathlib turns it into False), so simulate the
    # exact OSError the guard is written for and assert it is treated as a
    # mismatch rather than propagating out of marker_matches_current.
    marker = Path("irrelevant")
    with patch.object(Path, "is_symlink", side_effect=OSError("simulated ELOOP")):
        assert not marker_matches_current(marker, "codex")


def main() -> int:
    test_marker_requires_current_version()
    test_current_ingestion_version_per_provider()
    test_marker_data_matches_current_truth_table()
    test_marker_matches_current_rejects_non_file()
    test_marker_matches_current_oserror_is_a_mismatch()
    test_old_version_requires_native_source_before_reingest()
    test_old_version_missing_source_tombstones_marker()
    print("PASS: ingestion version markers")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
