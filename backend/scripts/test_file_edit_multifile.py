"""Backend regression test for the multi-file / one-session-per-project-
cwd file-editing model.

Pins:
  1. Join-by-cwd: start(A, cwd C) then start(B, cwd C) → SAME session,
     file_paths grows to [A, B], 2nd call returns an add-file
     meta_prompt (submitted on the same claude session) + resumed=True.
  2. Same file twice → pure resume, meta_prompt is None.
  3. Different cwd → different sessions (no cross-join).
  4. original_contents holds the disk baseline per file.
  5. Modal/persistent path: start(A, persistent=True, cwd C) then
     temporal start(B, cwd C) → SAME session, stays persistent, set
     grows. (The §2 cross-flavor concern: now an intended single join,
     persistent is upgrade-only / never downgraded.)
  6. Legacy single-file meta shape raises (no silent mount).
  7. WS: a working_mode_marked change is broadcast as
     session_metadata_updated carrying working_mode_meta.file_paths
     end-to-end through SessionWSBroadcaster.on_change (the
     _METADATA_KINDS guard is exactly the regression).
  8. Comment anchoring is path-agnostic for a 2nd-added file.
  9. cleanup() tears down the whole set (one session = all files).
 10. cwd is required (no silent fallback).

Run with:
    cd backend && .venv/bin/python scripts/test_file_edit_multifile.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-multifile-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import file_editor  # noqa: E402
import working_mode  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402
from session_ws_broadcaster import SessionWSBroadcaster  # noqa: E402


# file_editor.start became async (file-editing-on-remote-node support).
# All tests here exercise local node, so `asyncio.run(...)` keeps the
# sync-test pattern intact without making every test_* async.
_async_start = file_editor.start


def _sync_start(*args, **kwargs):
    return asyncio.run(_async_start(*args, **kwargs))


# Monkey-patch so test bodies don't need to change.
file_editor.start = _sync_start  # type: ignore[assignment]


PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _project(label: str) -> Path:
    d = Path(_TMP_HOME) / "proj" / label
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write(d: Path, name: str, content: str) -> Path:
    p = d / name
    p.write_text(content)
    return p


def test_join_by_cwd_grows_set() -> bool:
    d = _project("join")
    a = _write(d, "a.txt", "AAA\n")
    b = _write(d, "b.txt", "BBB\n")
    r1 = file_editor.start(str(a), cwd=str(d))
    r2 = file_editor.start(str(b), cwd=str(d))
    if r1["session_id"] != r2["session_id"]:
        print("  expected join (same session id)")
        return False
    if r2.get("resumed") is not True:
        print(f"  expected resumed=True on join, got {r2.get('resumed')!r}")
        return False
    want = [str(a.resolve()), str(b.resolve())]
    if r2["file_paths"] != want:
        print(f"  expected file_paths={want}, got {r2['file_paths']}")
        return False
    mp = r2.get("meta_prompt")
    if not mp or str(b.resolve()) not in mp:
        print(f"  expected add-file meta_prompt mentioning {b.resolve()}, got {mp!r}")
        return False
    if str(a.resolve()) not in mp:
        print("  add-file meta_prompt should list the full set (missing A)")
        return False
    sess = session_manager.get(r1["session_id"]) or {}
    meta = sess.get("working_mode_meta") or {}
    if meta.get("file_paths") != want:
        print(f"  persisted meta.file_paths={meta.get('file_paths')!r}")
        return False
    return True


def test_concurrent_adds_no_lost_file() -> bool:
    """Two+ near-simultaneous opens for the SAME cwd must not drop a
    file. Pins the atomic read-modify-write of working_mode_meta — the
    old find→read→append→write across separate locks lost files under
    last-writer-wins."""
    import threading
    d = _project("concurrent")
    base = file_editor.start(str(_write(d, "base.txt", "B\n")), cwd=str(d))
    sid = base["session_id"]
    extra = [str(_write(d, f"f{i}.txt", f"{i}\n")) for i in range(8)]

    def add(fp: str) -> None:
        file_editor.start(fp, cwd=str(d))

    threads = [threading.Thread(target=add, args=(fp,)) for fp in extra]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    sess = session_manager.get(sid) or {}
    got = set((sess.get("working_mode_meta") or {}).get("file_paths") or [])
    want = {str(Path(p).resolve()) for p in extra} | {
        str((d / "base.txt").resolve())
    }
    missing = want - got
    if missing:
        print(f"  lost {len(missing)} file(s) under concurrency: "
              f"{sorted(p.split('/')[-1] for p in missing)}")
        return False
    return True


def test_same_file_twice_pure_resume() -> bool:
    d = _project("dup")
    a = _write(d, "a.txt", "AAA\n")
    r1 = file_editor.start(str(a), cwd=str(d))
    r2 = file_editor.start(str(a), cwd=str(d))
    if r1["session_id"] != r2["session_id"]:
        print("  expected same session")
        return False
    if r2.get("meta_prompt") is not None:
        print(f"  expected meta_prompt=None on pure resume, got {r2.get('meta_prompt')!r}")
        return False
    if r2["file_paths"] != [str(a.resolve())]:
        print(f"  set should be unchanged, got {r2['file_paths']}")
        return False
    return True


def test_different_cwd_different_session() -> bool:
    d1 = _project("cwd1")
    d2 = _project("cwd2")
    a = _write(d1, "a.txt", "A\n")
    b = _write(d2, "b.txt", "B\n")
    r1 = file_editor.start(str(a), cwd=str(d1))
    r2 = file_editor.start(str(b), cwd=str(d2))
    if r1["session_id"] == r2["session_id"]:
        print("  different cwds must not cross-join")
        return False
    return True


def test_original_contents_baseline_per_file() -> bool:
    d = _project("baseline")
    a = _write(d, "a.txt", "ORIG-A\n")
    b = _write(d, "b.txt", "ORIG-B\n")
    file_editor.start(str(a), cwd=str(d))
    r2 = file_editor.start(str(b), cwd=str(d))
    oc = r2["original_contents"]
    if oc.get(str(a.resolve())) != "ORIG-A\n":
        print(f"  baseline A wrong: {oc.get(str(a.resolve()))!r}")
        return False
    if oc.get(str(b.resolve())) != "ORIG-B\n":
        print(f"  baseline B wrong: {oc.get(str(b.resolve()))!r}")
        return False
    return True


def test_persistent_then_temporal_joins_stays_persistent() -> bool:
    """Modal-persistent session created first; later temporal AI-Edit of
    a 2nd file in the same cwd JOINS it and it stays persistent."""
    d = _project("modal_join")
    a = _write(d, "a.txt", "A\n")
    b = _write(d, "b.txt", "B\n")
    r1 = file_editor.start(str(a), cwd=str(d), persistent=True)
    r2 = file_editor.start(str(b), cwd=str(d))  # temporal AI-Edit
    if r1["session_id"] != r2["session_id"]:
        print("  expected join into the persistent session")
        return False
    sess = session_manager.get(r1["session_id"]) or {}
    meta = sess.get("working_mode_meta") or {}
    if meta.get("persistent") is not True:
        print(f"  persistent must NOT be downgraded, got {meta.get('persistent')!r}")
        return False
    if meta.get("file_paths") != [str(a.resolve()), str(b.resolve())]:
        print(f"  set should be [A,B], got {meta.get('file_paths')!r}")
        return False
    return True


def test_legacy_single_file_meta_raises() -> bool:
    legacy = {"file_path": "/x/y.txt", "original_content": "z"}
    try:
        file_editor._assert_multifile_meta(legacy, "deadbeef")
    except ValueError:
        pass
    else:
        print("  legacy meta shape should raise ValueError")
        return False
    # New shape must NOT raise.
    file_editor._assert_multifile_meta(
        {"file_paths": ["/x/y.txt"], "original_contents": {}}, "deadbeef"
    )
    return True


def test_ws_broadcasts_working_mode_meta() -> bool:
    """SessionWSBroadcaster.on_change('working_mode_marked') must emit a
    session_metadata_updated frame carrying working_mode_meta.file_paths.
    Exercises the _METADATA_KINDS guard end-to-end."""
    d = _project("ws")
    a = _write(d, "a.txt", "A\n")
    r = file_editor.start(str(a), cwd=str(d))
    sid = r["session_id"]

    captured: list = []

    class FakeCoord:
        # _dispatch calls broadcast_global(event_type, data) — mirror
        # the real Coordinator.broadcast_global signature.
        async def broadcast_global(self, event_type, data):
            captured.append({"type": event_type, "data": data})

    b = SessionWSBroadcaster(FakeCoord())

    async def drive():
        # on_change reads the ENRICHED fields the real mutator ships
        # (see working_mode.py: enrich → working_mode + working_mode_meta).
        # Source them from the post-start session so the frame carries
        # the real file set.
        sess = session_manager.get(sid) or {}
        b.on_change(sid, {
            "kind": "working_mode_marked",
            "working_mode": sess.get("working_mode"),
            "working_mode_meta": sess.get("working_mode_meta"),
        })
        await asyncio.sleep(0.05)

    asyncio.run(drive())

    frames = [p for p in captured if p.get("type") == "session_metadata_updated"]
    if not frames:
        print("  no session_metadata_updated frame emitted (METADATA_KINDS guard?)")
        return False
    patch = (frames[0].get("data") or {}).get("patch") or {}
    if patch.get("working_mode") != "file_editing":
        print(f"  patch.working_mode={patch.get('working_mode')!r}")
        return False
    wmm = patch.get("working_mode_meta") or {}
    if str(a.resolve()) not in (wmm.get("file_paths") or []):
        print(f"  patch.working_mode_meta.file_paths missing the file: {wmm.get('file_paths')!r}")
        return False
    return True


def test_comment_format_path_agnostic_for_added_file() -> bool:
    d = _project("comment")
    a = _write(d, "a.txt", "A\n")
    b = _write(d, "b.txt", "B\n")
    file_editor.start(str(a), cwd=str(d))
    file_editor.start(str(b), cwd=str(d))
    msg = working_mode.format_file_comment(
        str(b.resolve()), 2, 2, 1, 5, "fix this"
    )
    if str(b.resolve()) not in msg or "fix this" not in msg:
        print(f"  comment anchor wrong for added file: {msg!r}")
        return False
    return True


def test_cleanup_tears_down_whole_set() -> bool:
    d = _project("teardown")
    a = _write(d, "a.txt", "A\n")
    b = _write(d, "b.txt", "B\n")
    r = file_editor.start(str(a), cwd=str(d))
    file_editor.start(str(b), cwd=str(d))
    sid = r["session_id"]
    ok = file_editor.cleanup(sid)
    if not ok:
        print("  cleanup returned False")
        return False
    if session_manager.get(sid) is not None:
        print("  session (whole set) should be gone after cleanup")
        return False
    return True


def test_cwd_required() -> bool:
    d = _project("nocwd")
    a = _write(d, "a.txt", "A\n")
    try:
        file_editor.start(str(a), cwd="")
    except ValueError:
        return True
    print("  expected ValueError when cwd missing")
    return False


TESTS = [
    ("join-by-cwd grows the set + add-file prompt", test_join_by_cwd_grows_set),
    ("concurrent adds lose no file (atomic meta write)", test_concurrent_adds_no_lost_file),
    ("same file twice → pure resume (no prompt)", test_same_file_twice_pure_resume),
    ("different cwd → different session", test_different_cwd_different_session),
    ("original_contents baseline per file", test_original_contents_baseline_per_file),
    ("persistent then temporal join stays persistent", test_persistent_then_temporal_joins_stays_persistent),
    ("legacy single-file meta raises", test_legacy_single_file_meta_raises),
    ("WS broadcasts working_mode_meta.file_paths", test_ws_broadcasts_working_mode_meta),
    ("comment anchor path-agnostic for added file", test_comment_format_path_agnostic_for_added_file),
    ("cleanup tears down the whole set", test_cleanup_tears_down_whole_set),
    ("cwd is required", test_cwd_required),
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
