"""Regression test for moved-session content migration.

Pins `session_migrate.migrate_session_content`:
  1. events.jsonl is copied with the source root sid rewritten to dst
     everywhere it appears — top-level `sid`, nested `data.sid`, AND
     inside path strings (the bug that motivated replacing the per-line
     JSON rewriter with full-UUID text substitution).
  2. event_summaries.json root_id/sid rewritten to dst.
  3. message_frontend_cache/ copied verbatim (content-addressed).
  4. Fork/worker sids (distinct UUIDs) are left untouched.
  5. native_paths is NOT carried (new session spawns a fresh provider sid).

Pytest-collectable (each ``test_*`` self-isolates via _wipe_sessions +
_setup_source). Also runnable standalone:

    cd backend && .venv/bin/python scripts/test_session_migrate.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home  # noqa: E402

# Register a per-module home so the shared conftest autouse fixture
# (_ensure_ba_home_dirs) re-engages THIS home per test instead of falling back
# to the shared _SESSION_ROOT. Without registration, sibling tests populate the
# shared sessions/ dir and this module's _wipe_sessions rmtree collides with it.
_TMP = tempfile.mkdtemp(prefix="ba_test_migrate_")
_test_home.engage(_TMP)
import paths  # noqa: E402  (bc_home() returns the engaged home)
import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _engage_migrate_home():
    # Re-engage this module's isolated home before every test. Heavy sibling
    # imports (e.g. orchestrator) can leave bc_home() repointed at the shared
    # root, which would make _wipe_sessions rmtree the shared sessions/ dir and
    # collide with siblings. Pinning the home per test keeps this hermetic.
    _test_home.engage(_TMP)
    yield

import session_migrate  # noqa: E402

SRC = "01b4b55f-00d4-42a6-96f2-2222241770d5"
DST = "55003396-be2d-42a3-9979-739c60a6b209"
FORK = "d78ef486-5494-4bd1-b3ed-0357123444b1"  # a worker fork sid; must survive


def _sessions_dir() -> Path:
    return paths.bc_home() / "sessions"


def _write(sid: str, name: str, text: str) -> None:
    d = _sessions_dir() / sid
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(text, encoding="utf-8")


def _read(sid: str, name: str) -> str:
    return (_sessions_dir() / sid / name).read_text(encoding="utf-8")


def _wipe_sessions() -> None:
    # Remove only this test's own session dirs. A blanket rmtree of the whole
    # sessions/ tree races with background persistence threads (loaded via
    # sibling imports like orchestrator) that concurrently flush into
    # bc_home()/sessions, hitting "Directory not empty" mid-rmtree. The test
    # owns only SRC/DST (fixed UUIDs no background thread writes), so targeted
    # removal is both sufficient for a clean slate and race-free.
    sd = _sessions_dir()
    for sid in (SRC, DST):
        d = sd / sid
        if d.exists():
            shutil.rmtree(d)


def _setup_source() -> None:
    events = [
        {
            "seq": 1,
            "sid": SRC,
            "type": "command_received",
            "data": {
                "method": "POST",
                # path embeds the sid as a substring — must be rewritten too
                "path": f"/api/sessions/{SRC}/opened",
                "sid": SRC,
            },
        },
        {
            "seq": 2,
            "sid": SRC,
            "type": "worker_event",
            "data": {"fork_sid": FORK, "parent_sid": SRC},
        },
    ]
    _write(SRC, "events.jsonl", "\n".join(json.dumps(e) for e in events) + "\n")
    _write(
        SRC,
        "event_summaries.json",
        json.dumps({
            "summary_version": 5,
            "summaries": {
                "m1": {"root_id": SRC, "sid": SRC, "seq_start": 1},
                "m2": {"root_id": SRC, "sid": FORK, "seq_start": 2},
            },
        }),
    )
    _write(SRC, "event_meta.json", json.dumps({
        "max_seq_by_sid": {SRC: 2, FORK: 1},
    }))
    cache_dir = _sessions_dir() / SRC / "message_frontend_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "abc123.json").write_text('{"rendered": true}', encoding="utf-8")
    # native_paths exists on source but must NOT be carried.
    _write(SRC, "native_paths", json.dumps([{"owning_session": SRC}]))


def test_rewrites_root_sid_everywhere_and_preserves_fork_sids() -> None:
    _wipe_sessions()
    _setup_source()
    session_migrate.migrate_session_content(SRC, DST)

    ev_text = _read(DST, "events.jsonl")
    assert SRC not in ev_text, "events.jsonl still contains source root sid"
    assert DST in ev_text, "events.jsonl missing dest root sid"
    assert FORK in ev_text, "events.jsonl dropped the fork sid (should be preserved)"
    assert f"/api/sessions/{DST}/opened" in ev_text, "path substring not rewritten to dest sid"
    ev_lines = [json.loads(line) for line in ev_text.splitlines() if line.strip()]
    assert ev_lines[0]["data"]["sid"] == DST, "nested data.sid was not rewritten"
    assert ev_lines[1]["data"]["fork_sid"] == FORK, "fork sid inside data was corrupted"


def test_rewrites_event_summaries() -> None:
    _wipe_sessions()
    _setup_source()
    session_migrate.migrate_session_content(SRC, DST)

    summ = json.loads(_read(DST, "event_summaries.json"))
    assert summ["summaries"]["m1"]["root_id"] == DST
    assert summ["summaries"]["m1"]["sid"] == DST, "root-owned sid not rewritten"
    assert summ["summaries"]["m2"]["sid"] == FORK, "fork-owned sid corrupted"
    assert summ["summaries"]["m2"]["root_id"] == DST, "fork-owned root_id not rewritten"


def test_copies_frontend_cache_verbatim() -> None:
    _wipe_sessions()
    _setup_source()
    session_migrate.migrate_session_content(SRC, DST)

    cache_file = _sessions_dir() / DST / "message_frontend_cache" / "abc123.json"
    assert cache_file.is_file()
    assert cache_file.read_text(encoding="utf-8") == '{"rendered": true}'


def test_omits_native_paths() -> None:
    _wipe_sessions()
    _setup_source()
    session_migrate.migrate_session_content(SRC, DST)

    assert not (_sessions_dir() / DST / "native_paths").exists(), "native_paths was carried"


def test_rewrites_event_meta() -> None:
    _wipe_sessions()
    _setup_source()
    session_migrate.migrate_session_content(SRC, DST)

    meta = json.loads(_read(DST, "event_meta.json"))
    assert SRC not in meta["max_seq_by_sid"]
    assert DST in meta["max_seq_by_sid"]


def test_idempotent_rerun_reproduces_identical_dst() -> None:
    _wipe_sessions()
    _setup_source()
    session_migrate.migrate_session_content(SRC, DST)
    first = _read(DST, "events.jsonl")
    session_migrate.migrate_session_content(SRC, DST)
    assert _read(DST, "events.jsonl") == first, "re-run is not idempotent"


def test_rejects_equal_or_empty_sids() -> None:
    _wipe_sessions()
    _setup_source()
    for src, dst in [(SRC, SRC), ("", DST), (SRC, "")]:
        try:
            session_migrate.migrate_session_content(src, dst)
        except ValueError:
            continue
        raise AssertionError(f"({src!r}, {dst!r}) should raise ValueError")


def test_skips_missing_render_files() -> None:
    # A source missing one render file migrates the rest and skips the gap.
    _wipe_sessions()
    _setup_source()
    (_sessions_dir() / SRC / "event_meta.json").unlink()
    session_migrate.migrate_session_content(SRC, DST)

    assert (_sessions_dir() / DST / "events.jsonl").is_file()
    assert (_sessions_dir() / DST / "event_summaries.json").is_file()
    assert not (_sessions_dir() / DST / "event_meta.json").exists()


def test_skips_when_source_has_no_frontend_cache_dir() -> None:
    # No message_frontend_cache on source -> dst gets none, no error.
    _wipe_sessions()
    _setup_source()
    shutil.rmtree(_sessions_dir() / SRC / "message_frontend_cache")
    session_migrate.migrate_session_content(SRC, DST)

    assert not (_sessions_dir() / DST / "message_frontend_cache").exists()


_TESTS = [
    test_rewrites_root_sid_everywhere_and_preserves_fork_sids,
    test_rewrites_event_summaries,
    test_copies_frontend_cache_verbatim,
    test_omits_native_paths,
    test_rewrites_event_meta,
    test_idempotent_rerun_reproduces_identical_dst,
    test_rejects_equal_or_empty_sids,
    test_skips_missing_render_files,
    test_skips_when_source_has_no_frontend_cache_dir,
]


def main() -> int:
    failures: list[str] = []
    for test in _TESTS:
        try:
            test()
        except AssertionError as exc:
            failures.append(f"{test.__name__}: {exc}")
    if failures:
        print("FAIL:")
        for f in failures:
            print("  - " + f)
        return 1
    print("OK: migrate_session_content rewrites root sid everywhere, "
          "preserves fork sids, copies cache, omits native_paths; idempotent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
