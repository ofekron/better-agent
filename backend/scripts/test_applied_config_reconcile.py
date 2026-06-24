"""Locks the declarative applied_config reconcile lifecycle:
enable -> tag registered; disable -> empty; uninstall -> empty.

Run with:
    cd backend && .venv/bin/python scripts/test_applied_config_reconcile.py
"""
from __future__ import annotations

import os
import shutil
import sys

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-applied-config-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import extension_applied_config  # noqa: E402
import file_ref_resolver  # noqa: E402


def _record(enabled: bool) -> dict:
    return {
        "enabled": enabled,
        "manifest": {
            "id": "ofek-dev.needs-user-decision",
            "entrypoints": {
                "applied_config": {
                    "tag_rules": [
                        {
                            "tag": "NEEDS_USER_DECISION",
                            "strip_wrapper": True,
                            "bold": True,
                            "font_scale": 1.3,
                            "marker": {"color": "#ff8c00", "tooltip": "Needs your decision"},
                            "clear_on": "view",
                        }
                    ]
                }
            },
        },
    }


def main() -> int:
    try:
        # The registry rebuild walks extension_store's persisted records.
        # Patch that single list source so the test is hermetic.
        records = [_record(True)]
        extension_applied_config._all_enabled_records = lambda: records  # type: ignore

        extension_applied_config.reconcile(records[0])
        assert "NEEDS_USER_DECISION" in file_ref_resolver.tag_names(), \
            f"expected tag registered, got {file_ref_resolver.tag_names()}"

        watch = extension_applied_config.tag_watch_rules()
        assert "NEEDS_USER_DECISION" in watch, watch
        assert watch["NEEDS_USER_DECISION"]["clear_on"] == "view"
        assert watch["NEEDS_USER_DECISION"]["marker"]["color"] == "#ff8c00"

        # Disable -> registry empties (record now disabled).
        records[0]["enabled"] = False
        extension_applied_config.reconcile(records[0])
        assert file_ref_resolver.tag_names() == frozenset(), \
            f"expected empty after disable, got {file_ref_resolver.tag_names()}"

        # Re-enable then uninstall (record removed from the source).
        records[0]["enabled"] = True
        extension_applied_config.reconcile_all()
        assert "NEEDS_USER_DECISION" in file_ref_resolver.tag_names()

        removed = _record(True)
        records.clear()
        extension_applied_config.clear_for_uninstall(removed)
        assert file_ref_resolver.tag_names() == frozenset(), \
            f"expected empty after uninstall, got {file_ref_resolver.tag_names()}"

        print("PASS test_applied_config_reconcile")
        return 0
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
