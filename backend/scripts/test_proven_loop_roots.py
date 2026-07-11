#!/usr/bin/env python3
from __future__ import annotations

import os
from pathlib import Path
import shutil
import tempfile

HOME = tempfile.mkdtemp(prefix="ba-proven-loop-roots-")
os.environ["BETTER_AGENT_HOME"] = HOME
os.environ["BETTER_AGENT_TEST_MODE"] = "1"

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import lag_incident_queue
from session_manager import SessionManager


def test_spool_depth_is_o1() -> None:
    lag_incident_queue._set_depth(17)
    original = lag_incident_queue._pending_files
    lag_incident_queue._pending_files = lambda **kwargs: (_ for _ in ()).throw(
        AssertionError("depth scanned spool")
    )
    try:
        assert lag_incident_queue.depth() == 17
    finally:
        lag_incident_queue._pending_files = original
    root = Path(HOME) / "lag-incidents"
    root.mkdir(parents=True, exist_ok=True)
    (root / "0123456789abcdef.json").write_text("{}", encoding="utf-8")
    assert lag_incident_queue._reconcile_depth_projection() == 1
    metadata = root / lag_incident_queue._DEPTH_META_NAME
    assert metadata.stat().st_mode & 0o777 == 0o600
    metadata.write_text("corrupt", encoding="utf-8")
    assert lag_incident_queue._reconcile_depth_projection() == 1


def test_file_context_projection_never_waits_for_root_lock() -> None:
    manager = SessionManager()
    manager._ensure_home_current()
    manager._project_key_cache["sid"] = ("/tmp/project", "node-a")
    lock = manager._lock_for_root("sid")
    lock.acquire()
    try:
        assert manager.get_file_ref_context("sid") == ("/tmp/project", "node-a")
    finally:
        lock.release()
    manager._project_key_cache.pop("sid")
    assert manager.get_file_ref_context("sid") == (None, "primary")


def test_search_validation_reuses_snapshot_off_loop() -> None:
    source = (Path(__file__).resolve().parents[1] / "session_search.py").read_text(
        encoding="utf-8"
    )
    validation = source[source.index("def validate_proposed("):source.index("def _resolve_proposed_project(")]
    assert "candidate_stubs" in validation
    assert "_build_index()" not in validation


def main() -> None:
    test_spool_depth_is_o1()
    test_file_context_projection_never_waits_for_root_lock()
    source = (Path(__file__).resolve().parents[1] / "session_search.py").read_text(encoding="utf-8")
    validation = source[source.index("def validate_proposed("):source.index("def _resolve_proposed_project(")]
    assert "candidate_stubs" in validation
    assert "await asyncio.to_thread(\n        validate_proposed" in source
    print("PASS proven loop roots avoid synchronous scans and locks")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(HOME)
