"""Locks re-snapshot-on-drift for private_local extensions.

Regression test: an already-installed private_local extension whose local
repo advanced (recorded commit_sha != current repo HEAD) MUST be re-snapshotted
on the next store reconcile, so manifest/code edits to a local private
extension take effect without a manual reinstall. Before the fix,
_ensure_private_extensions skipped any installed non-required private record
outright, so e.g. a newly-added `backend_routes` permission never reached the
on-disk install and the extension's backend 404'd.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import _test_home
_test_home.isolate("resnapshot-test-")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import extension_store as es  # noqa: E402

PS = es.BUILTIN_PROJECT_STRUCTURE_EXTENSION_ID


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)
    print(f"ok - {msg}")


def _stale_record() -> dict:
    return {
        "manifest": {
            "id": PS,
            "permissions": {"session_state": True, "internal_loopback": True},
        },
        "enabled": True,
        "installed_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "source": {
            "type": "better_agent_local",
            "repo_url": "",
            "extension_path": "extensions/project-structure",
            "ref": "",
            "commit_sha": "STALE_SHA",
            "install_path": "",
        },
        "instructions_enabled": {"global": True, "projects": {}},
    }


def _run(stale: bool, snap_impl) -> tuple[dict, list]:
    """Run _ensure_private_extensions over a single PS record, with the heavy
    snapshot installer stubbed. Returns (data, calls)."""
    calls: list[str] = []
    orig_paths = es._PRIVATE_EXTENSION_PATHS
    orig_snap = es._install_private_package_snapshot
    orig_required = es.REQUIRED_EXTENSION_IDS
    orig_discover = es._discover_private_extensions
    es._PRIVATE_EXTENSION_PATHS = {PS: "extensions/project-structure"}
    es.REQUIRED_EXTENSION_IDS = set()
    # Discovery is now a generic dir-scan; neutralize it so only the patched
    # map's PS is processed (this test is about resnapshot, not discovery).
    es._discover_private_extensions = lambda root: {}

    def tracked(eid, package_dir):
        calls.append(eid)
        return snap_impl(eid, package_dir)

    es._install_private_package_snapshot = tracked
    rec = _stale_record()
    if not stale:
        rec["source"]["commit_sha"] = es._private_extension_commit_sha()
    data = {"extensions": {PS: rec}, "deleted_extensions": {}}
    try:
        es._ensure_private_extensions(data)
    finally:
        es._PRIVATE_EXTENSION_PATHS = orig_paths
        es._install_private_package_snapshot = orig_snap
        es.REQUIRED_EXTENSION_IDS = orig_required
        es._discover_private_extensions = orig_discover
    return data, calls


def _good_snapshot(eid, package_dir):
    return {
        "manifest": {
            "id": eid,
            "permissions": {
                "session_state": True,
                "internal_loopback": True,
                "backend_routes": True,
            },
        },
        "enabled": True,
        "installed_at": "2026-06-23T00:00:00+00:00",
        "updated_at": "2026-06-23T00:00:00+00:00",
        "source": {
            "type": "better_agent_local",
            "commit_sha": es._private_extension_commit_sha(),
            "extension_path": "extensions/project-structure",
            "install_path": "/tmp/x",
        },
        "instructions_enabled": {"global": True, "projects": {}},
    }


def test_stale_private_local_is_resnapshotted() -> None:
    if es._local_private_extension_repo_root() is None:
        print("ok - SKIP (no local private repo in this env)")
        return
    data, calls = _run(stale=True, snap_impl=_good_snapshot)
    rec = data["extensions"][PS]
    check(calls == [PS], "stale private_local extension triggers a re-snapshot")
    check(
        rec["manifest"]["permissions"].get("backend_routes") is True,
        "re-snapshot refreshes the manifest (backend_routes now present)",
    )
    check(rec["source"]["commit_sha"] != "STALE_SHA", "commit_sha is updated past the stale value")
    check(rec["enabled"] is True, "enabled flag is preserved across re-snapshot")
    check(
        rec["installed_at"] == "2026-01-01T00:00:00+00:00",
        "original installed_at is preserved across re-snapshot",
    )


def test_up_to_date_private_local_is_not_resnapshotted() -> None:
    if es._local_private_extension_repo_root() is None:
        print("ok - SKIP (no local private repo in this env)")
        return
    _data, calls = _run(stale=False, snap_impl=_good_snapshot)
    check(calls == [], "up-to-date private_local extension is left untouched (no re-snapshot)")


def test_failed_resnapshot_fails_open() -> None:
    if es._local_private_extension_repo_root() is None:
        print("ok - SKIP (no local private repo in this env)")
        return

    def boom(eid, package_dir):
        raise es.ExtensionError("smoke test failed")

    data, calls = _run(stale=True, snap_impl=boom)
    rec = data["extensions"][PS]
    check(calls == [PS], "re-snapshot was attempted on drift")
    check(rec["source"]["commit_sha"] == "STALE_SHA", "failed re-snapshot leaves the working install untouched")
    check(
        "backend_routes" not in rec["manifest"]["permissions"],
        "failed re-snapshot does not partially mutate the record",
    )


if __name__ == "__main__":
    test_stale_private_local_is_resnapshotted()
    test_up_to_date_private_local_is_not_resnapshotted()
    test_failed_resnapshot_fails_open()
    print("\nALL PASS")
