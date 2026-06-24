"""Regression: folder_view drives folderized-first sort in the session list.

With folder_view=True, a folderized session older than the newest unfiled
session still sorts first (so the folder tree renders on the initial view).
With folder_view=False (flat list), folder_id is ignored and recency alone
orders the sessions.

Run with:
    cd backend && .venv/bin/python scripts/test_session_list_folder_sort.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-session-folder-sort-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import main as app_main  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def main() -> int:
    try:
        # Folderized session is OLDER than the unfiled one; recency alone would
        # put the unfiled session first.
        folderized_old = {
            "id": "folderized-old",
            "folder_id": "folder-1",
            "updated_at": "2026-05-01T00:00:00",
            "pinned": False,
        }
        unfiled_new = {
            "id": "unfiled-new",
            "updated_at": "2026-06-16T00:00:00",
            "pinned": False,
        }

        order = [
            s["id"]
            for s in sorted(
                [folderized_old, unfiled_new],
                key=lambda s: app_main._session_list_sort_key(s, True, "updated_at"),
                reverse=True,
            )
        ]
        ok = order == ["folderized-old", "unfiled-new"]
        print(
            f"{PASS if ok else FAIL} folderized session sorts before newer "
            f"unfiled session{'' if ok else f' — got {order}'}"
        )

        filtered_order = [
            s["id"]
            for s in sorted(
                [folderized_old, unfiled_new],
                key=lambda s: app_main._session_filtered_sort_key(
                    s, folder_view=True, search="x", content_scores={}
                ),
                reverse=True,
            )
        ]
        fok = filtered_order == ["folderized-old", "unfiled-new"]
        print(
            f"{PASS if fok else FAIL} search-active sort also puts folderized "
            f"first{'' if fok else f' — got {filtered_order}'}"
        )

        # Flat view: folder_id must NOT influence order — recency wins.
        flat_order = [
            s["id"]
            for s in sorted(
                [folderized_old, unfiled_new],
                key=lambda s: app_main._session_list_sort_key(s, False, "updated_at"),
                reverse=True,
            )
        ]
        flat_ok = flat_order == ["unfiled-new", "folderized-old"]
        print(
            f"{PASS if flat_ok else FAIL} flat view ignores folder_id and sorts "
            f"by recency{'' if flat_ok else f' — got {flat_order}'}"
        )
        return 0 if ok and fok and flat_ok else 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
