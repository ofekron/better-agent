"""Locks attention-marker persistence across backend restart.

Markers must survive a restart: they are written atomically to
`attention_markers.json` on every mutation and lazily reloaded after the
home-scoped caches reset (the restart-equivalent code path).

Run with:
    cd backend && .venv/bin/python scripts/test_marker_persistence.py
"""
from __future__ import annotations

import os
import shutil
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-marker-persist-")

import session_store  # noqa: E402
from session_manager import manager  # noqa: E402


def _simulate_restart() -> None:
    session_store._reset_home_scoped_caches()


def main() -> int:
    try:
        sid = manager.create(cwd="/tmp/marker-persist", orchestration_mode="team")["id"]
        marker = {"color": "#ff8c00", "tooltip": "Needs your decision"}

        # Set → survives restart.
        manager.set_marker(sid, "ext.a", marker)
        assert session_store._markers_path().exists(), "marker file not written"
        _simulate_restart()
        got = session_store._markers_for_session(sid)
        assert got == {"ext.a": marker}, f"marker lost across restart: {got}"

        # Restored marker lands in a freshly built summary.
        summary = session_store._build_summary_for_root(manager.get_ref(sid))
        assert summary["markers"] == {"ext.a": marker}, summary["markers"]

        # Clear → stays cleared after restart.
        manager.clear_marker(sid, "ext.a")
        _simulate_restart()
        assert session_store._markers_for_session(sid) == {}, \
            session_store._markers_for_session(sid)

        # Purge-on-uninstall persists too.
        manager.set_marker(sid, "ext.b", marker)
        manager.set_marker(sid, "ext.c", marker)
        session_store.markers_for_extension_purge("ext.b")
        _simulate_restart()
        got = session_store._markers_for_session(sid)
        assert got == {"ext.c": marker}, f"purge not persisted: {got}"

        # Corrupt file fails closed to empty (no crash).
        session_store._markers_path().write_text("{not json", encoding="utf-8")
        _simulate_restart()
        assert session_store._markers_for_session(sid) == {}

        print("PASS test_marker_persistence")
        return 0
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
