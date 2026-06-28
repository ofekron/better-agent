from __future__ import annotations

import json
import os
import shutil
import sys

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-remote-recovery-scan-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import run_recovery  # noqa: E402
from ingestion_versions import write_marker  # noqa: E402
from runs_dir import runs_root  # noqa: E402


def test_pending_remote_scan_filters_on_disk_state() -> bool:
    root = runs_root()
    root.mkdir(parents=True, exist_ok=True)
    pending = root / "run-pending"
    pending.mkdir()
    (pending / "backend_state.json").write_text(
        json.dumps({"node_id": "node-a", "provider_kind": "claude"}),
        encoding="utf-8",
    )
    reconciled = root / "run-reconciled"
    reconciled.mkdir()
    (reconciled / "backend_state.json").write_text(
        json.dumps({"node_id": "node-a", "provider_kind": "claude"}),
        encoding="utf-8",
    )
    write_marker(reconciled / "reconciled.marker", "claude")
    other_node = root / "run-other-node"
    other_node.mkdir()
    (other_node / "backend_state.json").write_text(
        json.dumps({"node_id": "node-b", "provider_kind": "claude"}),
        encoding="utf-8",
    )

    found = run_recovery._pending_remote_runs_for_node("node-a")
    return [path.name for path, _ in found] == ["run-pending"]


def run() -> int:
    try:
        ok = test_pending_remote_scan_filters_on_disk_state()
        print(("PASS" if ok else "FAIL") + " pending remote scan filters on disk state")
        return 0 if ok else 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(run())
