"""Regression test for the single-writer session.json refactor.

session_store.fork_session / create_delegate_fork / splice_fork are now
PURE tree-transformers (they mutate a passed root, never touch disk);
session_manager owns the persist. This locks that:
  - fork and delegate-fork land in BOTH the live _roots tree AND on disk
    after the explicit pending-persist durability barrier,
  - fork-delete splices from live + disk and unindexes,
  - root-delete unlinks the file.

These paths are otherwise only covered by test_fork_split.py, which is
red on main (unbound reconcile fn), so this self-contained test is the
working proof.

Run with:
    cd backend && .venv/bin/python scripts/test_session_writer_single_persist.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-session-writer-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from session_manager import manager as sm  # noqa: E402
import session_store  # noqa: E402
from paths import ba_home  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _disk(rid: str) -> dict:
    return json.loads((ba_home() / "sessions" / f"{rid}.json").read_text())


def _mk_root() -> str:
    root = sm.create(name="r", model="gpt-5.5", cwd="/tmp", orchestration_mode="native")
    rid = root["id"]
    # fork_session requires the parent to have a claude/agent sid.
    sm.set_agent_sid(rid, "native", "sid-parent")
    return rid


def test_fork_persists_to_live_and_disk() -> None:
    rid = _mk_root()
    cid = sm.fork(rid)["id"]
    assert sm._roots[rid]["forks"][0]["id"] == cid, "fork missing from live _roots"
    sm.flush_pending_persists()
    assert any(f["id"] == cid for f in _disk(rid)["forks"]), \
        "fork not synchronously persisted to disk"


def test_delegate_fork_persists() -> None:
    rid = _mk_root()
    did = sm.create_delegate_fork(
        parent_agent_session_id=rid, caller_agent_session_id=rid,
        parent_agent_sid_at_fork="sid-parent", parent_line_count_at_fork=0,
        orchestration_mode="native",
    )["id"]
    sm.flush_pending_persists()
    assert any(f["id"] == did for f in _disk(rid)["forks"]), "delegate fork not persisted"


def test_fork_delete_splices_live_and_disk() -> None:
    rid = _mk_root()
    cid = sm.fork(rid)["id"]
    assert sm.delete(cid) is True, "delete returned non-True"
    assert not any(f["id"] == cid for f in sm._roots[rid]["forks"]), \
        "fork still in live _roots after delete"
    sm.flush_pending_persists()
    assert not any(f["id"] == cid for f in _disk(rid)["forks"]), \
        "fork still on disk after delete"
    assert sm._root_id_for(cid) is None, "deleted fork still indexed"


def test_root_delete_unlinks_file() -> None:
    rid = _mk_root()
    assert sm.delete(rid) is True, "root delete returned non-True"
    sm.flush_pending_persists()
    assert not (ba_home() / "sessions" / f"{rid}.json").exists(), \
        "root file still exists after delete"


TESTS = [
    ("fork persists to live + disk (sync)", test_fork_persists_to_live_and_disk),
    ("delegate fork persists", test_delegate_fork_persists),
    ("fork-delete splices live + disk", test_fork_delete_splices_live_and_disk),
    ("root-delete unlinks file", test_root_delete_unlinks_file),
]


def main_run() -> int:
    failed = 0
    try:
        for name, fn in TESTS:
            try:
                fn()
            except Exception:
                import traceback
                traceback.print_exc()
                print(f"{FAIL}  {name}")
                failed += 1
                continue
            print(f"{PASS}  {name}")
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
    print()
    print(f"all {len(TESTS)} tests passed" if not failed
          else f"{failed} of {len(TESTS)} test(s) FAILED")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main_run())
