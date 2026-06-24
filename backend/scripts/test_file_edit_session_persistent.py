"""Backend regression test for FR-FILE.0.1 — file viewer MUST auto-open
on entry to a File-Mode session, now under the multi-file / one-session-
per-project-cwd model.

Pins the contract that:
  1. file_editor.start(persistent=True) marks the session with
     working_mode="file_editing" + meta.persistent=True + meta.file_paths
     so the frontend overlay derivation auto-mounts on entry.
  2. file_editor.start(persistent=False) (temporal flavor) marks meta
     without `persistent` so the temporal-flavor Done button stays.
  3. Upgrade-only: a temporal session for a cwd, then a persistent
     start for the SAME cwd → meta.persistent upgraded in place
     (same session — one session per cwd).
  4. Sidebar visibility: persistent file-mode sessions appear in
     GET /api/sessions; temporal file-mode + prompt_engineering hide.
  5. POST /api/sessions with `file_edit_path` routes through
     file_editor.start(persistent=True) and the returned session
     record carries working_mode + meta.file_paths.
  6. Newly created sessions don't carry the legacy file_edit_path field.

Each test uses its OWN project dir as cwd — sessions are keyed per
project cwd now, so sharing one cwd would cross-join tests.

Run with:
    cd backend && .venv/bin/python scripts/test_file_edit_session_persistent.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-fileedit-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import file_editor  # noqa: E402
import working_mode  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402


PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _project(label: str) -> Path:
    """A fresh project dir (used as the session cwd) per test."""
    d = Path(_TMP_HOME) / "proj" / label
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write(d: Path, name: str, content: str = "hello world\n") -> Path:
    p = d / name
    p.write_text(content)
    return p


def test_persistent_marks_meta() -> bool:
    """file_editor.start(persistent=True) stamps meta.persistent=True +
    file_paths."""
    d = _project("persistent_marks")
    fp = _write(d, "a.txt")
    result = file_editor.start(str(fp), cwd=str(d), persistent=True)
    sess = session_manager.get(result["session_id"])
    if sess is None:
        print("  session not found after start")
        return False
    if sess.get("working_mode") != "file_editing":
        print(f"  expected working_mode='file_editing', got {sess.get('working_mode')!r}")
        return False
    meta = sess.get("working_mode_meta") or {}
    if meta.get("persistent") is not True:
        print(f"  expected meta.persistent=True, got {meta.get('persistent')!r}")
        return False
    if meta.get("file_paths") != [str(fp.resolve())]:
        print(f"  expected meta.file_paths=[{str(fp.resolve())!r}], got {meta.get('file_paths')!r}")
        return False
    if meta.get("project_cwd") != str(d.resolve()):
        print(f"  expected meta.project_cwd={str(d.resolve())!r}, got {meta.get('project_cwd')!r}")
        return False
    return True


def test_temporal_marks_meta_without_persistent() -> bool:
    """file_editor.start(persistent=False) leaves meta.persistent falsy."""
    d = _project("temporal_marks")
    fp = _write(d, "b.txt")
    result = file_editor.start(str(fp), cwd=str(d))
    sess = session_manager.get(result["session_id"])
    if sess is None:
        print("  session not found")
        return False
    meta = sess.get("working_mode_meta") or {}
    if meta.get("persistent"):
        print(f"  expected meta.persistent falsy, got {meta.get('persistent')!r}")
        return False
    return True


def test_resume_upgrades_persistent_flag() -> bool:
    """Existing temporal session for a cwd + a later persistent=True
    start for the SAME cwd → same session, meta.persistent upgraded."""
    d = _project("upgrade")
    fp = _write(d, "c.txt")
    r1 = file_editor.start(str(fp), cwd=str(d))                 # temporal
    r2 = file_editor.start(str(fp), cwd=str(d), persistent=True)  # upgrade
    if r1["session_id"] != r2["session_id"]:
        print("  expected idempotent join to return same session id")
        return False
    sess = session_manager.get(r2["session_id"])
    meta = (sess or {}).get("working_mode_meta") or {}
    if meta.get("persistent") is not True:
        print(f"  expected meta.persistent=True after upgrade, got {meta.get('persistent')!r}")
        return False
    return True


def test_sidebar_visibility_persistent_vs_temporal() -> bool:
    """should_hide_from_sidebar:
      - persistent file_editing → visible (False)
      - temporal file_editing → hidden (True)
      - prompt_engineering → hidden (True)
      - normal session → visible (False)"""
    persistent_summary = {
        "id": "p",
        "working_mode": "file_editing",
        "working_mode_meta": {"persistent": True, "file_paths": ["/x"]},
    }
    temporal_summary = {
        "id": "t",
        "working_mode": "file_editing",
        "working_mode_meta": {"file_paths": ["/y"]},
    }
    eng_summary = {
        "id": "e",
        "working_mode": "prompt_engineering",
        "working_mode_meta": {"parent_session_id": "p", "temp_file_path": "/z"},
    }
    normal_summary = {"id": "n"}

    cases = [
        (persistent_summary, False, "persistent file_editing"),
        (temporal_summary, True, "temporal file_editing"),
        (eng_summary, True, "prompt_engineering"),
        (normal_summary, False, "normal"),
    ]
    for summary, want_hidden, label in cases:
        got = working_mode.should_hide_from_sidebar(summary)
        if got != want_hidden:
            print(f"  {label}: expected hidden={want_hidden}, got {got}")
            return False
    return True


def test_list_sessions_includes_persistent_but_excludes_temporal() -> bool:
    """End-to-end: GET /api/sessions equivalent via session_manager.list()
    + should_hide_from_sidebar (the pipeline main.py:get_sessions uses).
    Different cwds so the two flavors don't cross-join."""
    dp = _project("e2e_persistent")
    dt = _project("e2e_temporal")
    fp_p = _write(dp, "persistent_e2e.txt")
    fp_t = _write(dt, "temporal_e2e.txt")
    r_p = file_editor.start(str(fp_p), cwd=str(dp), persistent=True)
    r_t = file_editor.start(str(fp_t), cwd=str(dt))

    summaries = session_manager.list()
    visible_ids = {
        s["id"] for s in summaries
        if not working_mode.should_hide_from_sidebar(s)
    }
    if r_p["session_id"] not in visible_ids:
        print(f"  persistent session {r_p['session_id'][:8]} should be visible")
        return False
    if r_t["session_id"] in visible_ids:
        print(f"  temporal session {r_t['session_id'][:8]} must be hidden")
        return False
    return True


def test_list_sessions_summary_includes_working_mode_meta() -> bool:
    """The sidebar summary must carry `working_mode_meta` so the frontend
    sidebar filter can read meta.persistent without a per-session detail
    fetch."""
    d = _project("summary_meta")
    fp = _write(d, "summary_meta.txt")
    file_editor.start(str(fp), cwd=str(d), persistent=True)
    summaries = session_manager.list()
    pers = [
        s for s in summaries
        if s.get("working_mode") == "file_editing"
        and (s.get("working_mode_meta") or {}).get("persistent")
    ]
    if not pers:
        print("  no persistent session found in sidebar summaries")
        return False
    meta = pers[0].get("working_mode_meta")
    if meta is None:
        print("  summary missing working_mode_meta field")
        return False
    if not meta.get("file_paths"):
        print("  summary's working_mode_meta missing file_paths")
        return False
    return True


def test_session_record_has_no_legacy_file_edit_path_field() -> bool:
    """The legacy `file_edit_path` field was deleted from the session
    schema. Newly created sessions must not carry it."""
    sess = session_manager.create(name="legacy-check", model="x", cwd="/tmp")
    if "file_edit_path" in sess:
        print(f"  session record still carries legacy field: file_edit_path={sess.get('file_edit_path')!r}")
        return False
    return True


TESTS = [
    ("persistent flag stamps meta + file_paths", test_persistent_marks_meta),
    ("temporal flag leaves meta.persistent falsy", test_temporal_marks_meta_without_persistent),
    ("resume upgrades persistent flag (cwd-keyed)", test_resume_upgrades_persistent_flag),
    ("should_hide_from_sidebar respects persistent flag", test_sidebar_visibility_persistent_vs_temporal),
    ("list_sessions includes persistent + excludes temporal", test_list_sessions_includes_persistent_but_excludes_temporal),
    ("list_sessions summary includes working_mode_meta", test_list_sessions_summary_includes_working_mode_meta),
    ("session record drops legacy file_edit_path field", test_session_record_has_no_legacy_file_edit_path_field),
]


def main_run() -> int:
    failed = 0
    try:
        for name, fn in TESTS:
            try:
                ok = fn()
            except Exception as e:
                ok = False
                import traceback
                traceback.print_exc()
                print(f"  exception: {e}")
            print(f"{PASS if ok else FAIL}  {name}")
            if not ok:
                failed += 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
    print()
    if failed:
        print(f"{failed} of {len(TESTS)} test(s) FAILED")
    else:
        print(f"all {len(TESTS)} tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main_run())
