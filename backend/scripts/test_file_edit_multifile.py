"""Backend regression test for the file-edit provisioned-fork model.

Pins:
  1. Every start creates a fresh user-facing session, even for the same cwd.
  2. Each fresh session has an independent file set/baseline.
  3. All sessions for the same cwd/provider/model fork from one warmed base.
  4. Different cwd → different warm base.
  5. Persistent is a per-session flag, not an upgrade on a cwd singleton.
  6. Legacy single-file meta shape raises (no silent mount).
  7. WS: a working_mode_marked change is broadcast as
     session_metadata_updated carrying working_mode_meta.file_paths.
  8. Comment anchoring works for the session's selected file.
  9. cleanup() tears down only that session.
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
import time
import threading

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

_FAKE_BASES: dict[tuple[str, str, str, str], str] = {}
_FAKE_BASES_LOCK = threading.Lock()


async def _fake_ensure_file_edit_base(cfg):
    key = (cfg.cwd, cfg.provider_id, cfg.model, cfg.node_id)
    with _FAKE_BASES_LOCK:
        sid = _FAKE_BASES.get(key)
        if sid and session_manager.get(sid):
            return sid
        base = session_manager.create(
            name="file-editing-base",
            model=cfg.model,
            cwd=cfg.cwd,
            orchestration_mode="native",
            source="internal",
            provider_id=cfg.provider_id,
            reasoning_effort=cfg.reasoning_effort or None,
            node_id=cfg.node_id,
            bare_config=False,
            worker_creation_policy="deny",
        )
        fake_agent_sid = f"fake-multifile-base-sid-{len(_FAKE_BASES)}"
        session_manager._run(
            base["id"],
            lambda s: s.__setitem__("agent_session_id", fake_agent_sid),
            {"kind": "test_agent_sid_set"},
        )
        working_mode.mark_working_mode(
            base["id"],
            mode=file_editor.BASE_MODE,
            meta={
                "cwd": cfg.cwd,
                "provider_id": cfg.provider_id,
                "model": cfg.model,
                "machine_completion": False,
                "version": file_editor.FILE_EDIT_BASE_SPEC.version,
                "node_id": cfg.node_id,
                "provisioned_at": time.time(),
            },
        )
        _FAKE_BASES[key] = base["id"]
        return base["id"]


file_editor._ensure_file_edit_base = _fake_ensure_file_edit_base  # type: ignore[assignment]


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


def test_same_cwd_creates_distinct_sessions_and_shared_base() -> bool:
    d = _project("same_cwd")
    a = _write(d, "a.txt", "AAA\n")
    b = _write(d, "b.txt", "BBB\n")
    r1 = file_editor.start(str(a), cwd=str(d))
    r2 = file_editor.start(str(b), cwd=str(d))
    if r1["session_id"] == r2["session_id"]:
        print("  expected a fresh session per start")
        return False
    if r1.get("resumed") or r2.get("resumed"):
        print(f"  starts should not report resumed: {r1.get('resumed')!r}, {r2.get('resumed')!r}")
        return False
    s1 = session_manager.get(r1["session_id"]) or {}
    s2 = session_manager.get(r2["session_id"]) or {}
    m1 = s1.get("working_mode_meta") or {}
    m2 = s2.get("working_mode_meta") or {}
    if m1.get("file_paths") != [str(a.resolve())]:
        print(f"  first file set not isolated: {m1.get('file_paths')!r}")
        return False
    if m2.get("file_paths") != [str(b.resolve())]:
        print(f"  second file set not isolated: {m2.get('file_paths')!r}")
        return False
    if not m1.get("base_session_id") or m1.get("base_session_id") != m2.get("base_session_id"):
        print(f"  expected shared warm base, got {m1.get('base_session_id')!r} / {m2.get('base_session_id')!r}")
        return False
    if s1.get("forked_from_agent_sid") != s2.get("forked_from_agent_sid"):
        print("  expected both sessions to fork from same base agent sid")
        return False
    return True


def test_concurrent_starts_no_cwd_join() -> bool:
    import threading
    d = _project("concurrent")
    files = [str(_write(d, f"f{i}.txt", f"{i}\n")) for i in range(8)]
    results: list[dict] = []
    lock = threading.Lock()

    def add(fp: str) -> None:
        result = file_editor.start(fp, cwd=str(d))
        with lock:
            results.append(result)

    threads = [threading.Thread(target=add, args=(fp,)) for fp in files]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    ids = [r["session_id"] for r in results]
    if len(set(ids)) != len(files):
        print(f"  expected {len(files)} distinct sessions, got {len(set(ids))}")
        return False
    base_ids = {
        (session_manager.get(sid) or {}).get("working_mode_meta", {}).get("base_session_id")
        for sid in ids
    }
    if len(base_ids) != 1 or not next(iter(base_ids)):
        print(f"  expected one shared base, got {base_ids!r}")
        return False
    for result in results:
        if len(result.get("file_paths") or []) != 1:
            print(f"  result should carry exactly one selected file: {result.get('file_paths')!r}")
            return False
    return True


def test_same_file_twice_creates_fresh_sessions() -> bool:
    d = _project("dup")
    a = _write(d, "a.txt", "AAA\n")
    r1 = file_editor.start(str(a), cwd=str(d))
    r2 = file_editor.start(str(a), cwd=str(d))
    if r1["session_id"] == r2["session_id"]:
        print("  expected fresh sessions for repeated opens")
        return False
    session = session_manager.get(r2["session_id"]) or {}
    wrapped = asyncio.run(file_editor.wrap_user_prompt(session, "second request"))
    if str(a.resolve()) not in wrapped or "second request" not in wrapped:
        print("  fresh repeated open should wrap its first user request with bootstrap")
        return False
    if r2["file_paths"] != [str(a.resolve())]:
        print(f"  set should contain only the selected file, got {r2['file_paths']}")
        return False
    return True


def test_different_cwd_different_base() -> bool:
    d1 = _project("cwd1")
    d2 = _project("cwd2")
    a = _write(d1, "a.txt", "A\n")
    b = _write(d2, "b.txt", "B\n")
    r1 = file_editor.start(str(a), cwd=str(d1))
    r2 = file_editor.start(str(b), cwd=str(d2))
    m1 = (session_manager.get(r1["session_id"]) or {}).get("working_mode_meta") or {}
    m2 = (session_manager.get(r2["session_id"]) or {}).get("working_mode_meta") or {}
    if r1["session_id"] == r2["session_id"]:
        print("  different cwds must not share user sessions")
        return False
    if not m1.get("base_session_id") or m1.get("base_session_id") == m2.get("base_session_id"):
        print(f"  different cwds should have different bases, got {m1.get('base_session_id')!r} / {m2.get('base_session_id')!r}")
        return False
    return True


def test_original_contents_baseline_per_session() -> bool:
    d = _project("baseline")
    a = _write(d, "a.txt", "ORIG-A\n")
    b = _write(d, "b.txt", "ORIG-B\n")
    r1 = file_editor.start(str(a), cwd=str(d))
    r2 = file_editor.start(str(b), cwd=str(d))
    oc1 = r1["original_contents"]
    oc2 = r2["original_contents"]
    if oc1.get(str(a.resolve())) != "ORIG-A\n" or str(b.resolve()) in oc1:
        print(f"  baseline/session A wrong: {oc1!r}")
        return False
    if oc2.get(str(b.resolve())) != "ORIG-B\n" or str(a.resolve()) in oc2:
        print(f"  baseline/session B wrong: {oc2!r}")
        return False
    return True


def test_persistent_then_temporal_are_independent() -> bool:
    d = _project("persistent_independent")
    a = _write(d, "a.txt", "A\n")
    b = _write(d, "b.txt", "B\n")
    r1 = file_editor.start(str(a), cwd=str(d), persistent=True)
    r2 = file_editor.start(str(b), cwd=str(d))
    if r1["session_id"] == r2["session_id"]:
        print("  persistent and temporal opens must not join")
        return False
    meta1 = (session_manager.get(r1["session_id"]) or {}).get("working_mode_meta") or {}
    meta2 = (session_manager.get(r2["session_id"]) or {}).get("working_mode_meta") or {}
    if meta1.get("persistent") is not True:
        print(f"  persistent session lost flag: {meta1.get('persistent')!r}")
        return False
    if meta2.get("persistent"):
        print(f"  temporal session should remain temporal, got {meta2.get('persistent')!r}")
        return False
    if meta1.get("file_paths") != [str(a.resolve())] or meta2.get("file_paths") != [str(b.resolve())]:
        print(f"  file sets not independent: {meta1.get('file_paths')!r} / {meta2.get('file_paths')!r}")
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
        def schedule_global(self, event_type, data, *, loop=None):
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


def test_comment_format_for_selected_file() -> bool:
    d = _project("comment")
    b = _write(d, "b.txt", "B\n")
    file_editor.start(str(b), cwd=str(d))
    msg = working_mode.format_file_comment(
        str(b.resolve()), 2, 2, 1, 5, "fix this"
    )
    if str(b.resolve()) not in msg or "fix this" not in msg:
        print(f"  comment anchor wrong for selected file: {msg!r}")
        return False
    return True


def test_cleanup_tears_down_only_one_session() -> bool:
    d = _project("teardown")
    a = _write(d, "a.txt", "A\n")
    b = _write(d, "b.txt", "B\n")
    r1 = file_editor.start(str(a), cwd=str(d))
    r2 = file_editor.start(str(b), cwd=str(d))
    ok = file_editor.cleanup(r1["session_id"])
    if not ok:
        print("  cleanup returned False")
        return False
    if session_manager.get(r1["session_id"]) is not None:
        print("  cleaned session should be gone")
        return False
    if session_manager.get(r2["session_id"]) is None:
        print("  independent sibling session should remain")
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
    ("same cwd creates distinct sessions + shared base", test_same_cwd_creates_distinct_sessions_and_shared_base),
    ("concurrent starts create distinct sessions", test_concurrent_starts_no_cwd_join),
    ("same file twice → fresh sessions", test_same_file_twice_creates_fresh_sessions),
    ("different cwd → different warm base", test_different_cwd_different_base),
    ("original_contents baseline per session", test_original_contents_baseline_per_session),
    ("persistent then temporal are independent", test_persistent_then_temporal_are_independent),
    ("legacy single-file meta raises", test_legacy_single_file_meta_raises),
    ("WS broadcasts working_mode_meta.file_paths", test_ws_broadcasts_working_mode_meta),
    ("comment anchor works for selected file", test_comment_format_for_selected_file),
    ("cleanup tears down only one session", test_cleanup_tears_down_only_one_session),
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
