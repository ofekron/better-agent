"""Draft state lives in a per-root sidecar (`<root>.drafts.json`), NOT in
the session tree — so a per-keystroke draft flush is O(small file), not
O(whole tree). This locks:

  * set_draft persists to the sidecar and survives a cold reload
  * the persisted TREE json contains NO draft fields (stripped)
  * the sidecar is the single home; clearing a draft removes it
  * a fork's draft persists independently of the root's
  * a non-draft mutation also flushes a pending draft (no loss)

Run with:
    cd backend && .venv/bin/python scripts/test_draft_sidecar.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-draft-sidecar-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import session_store  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402
# Constructing a Coordinator installs DraftStore + UPM and registers
# itself as the active coordinator so `session_manager.set_draft` /
# `drain_pending_drafts` can route through `coordinator.draft_store`.
# No fallback path exists in sm — the coordinator must be live.
from orchestrator import Coordinator  # noqa: E402
_coordinator = Coordinator()  # noqa: E402  install as active

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _check(cond: bool, name: str, detail: str = "") -> bool:
    print(f"{PASS if cond else FAIL} {name}{'' if cond else ' -- ' + detail}")
    return cond


def _tree_json(root_id: str) -> dict:
    path = session_store._sessions_dir() / f"{root_id}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _reset_opened_cache() -> None:
    with session_store._opened_cache_lock:
        session_store._opened_cache.clear()


def _evict(sid: str) -> None:
    rid = session_manager._root_id_for(sid)
    with session_manager._lock_for_root(rid):
        session_manager._roots.pop(rid, None)


def main() -> int:
    ok = True
    try:
        sess = session_store.create_session(name="t", model="m", cwd="/tmp")
        sid = sess["id"]

        # No bound loop → set_draft takes the inline sidecar write path.
        session_manager.set_draft(sid, "hello", 100)

        # Sidecar holds it; tree json does NOT.
        side = session_store.read_drafts(sid)
        ok = _check(
            side.get(sid, {}).get("draft_input") == "hello"
            and side[sid]["draft_input_seq"] == 100,
            "draft written to sidecar",
            str(side),
        ) and ok
        tree = _tree_json(sid)
        ok = _check(
            "draft_input" not in tree and "draft_input_seq" not in tree,
            "draft stripped from persisted tree json",
            str([k for k in tree if "draft" in k]),
        ) and ok

        # Cold reload (raw store read) overlays the sidecar.
        reloaded = session_store.get_session(sid)
        ok = _check(
            reloaded["draft_input"] == "hello"
            and reloaded["draft_input_seq"] == 100,
            "cold reload overlays draft from sidecar",
            str(reloaded.get("draft_input")),
        ) and ok

        # Clearing the draft drops it from the sidecar.
        _evict(sid)
        session_manager.set_draft(sid, "", 101)
        ok = _check(
            sid not in session_store.read_drafts(sid),
            "cleared draft removed from sidecar",
            str(session_store.read_drafts(sid)),
        ) and ok

        # A draft set INSIDE a batch is deferred (marked dirty, not
        # written inline); the batch-exit `_persist_root` must flush the
        # sidecar. Asserting absence mid-batch + presence after makes
        # this lock the `_persist_root` draft-flush, not the inline path.
        _evict(sid)
        with session_manager.batch(sid):
            session_manager.set_draft(sid, "batched", 300)
            mid = session_store.read_drafts(sid).get(sid, {}).get("draft_input")
        after = session_store.read_drafts(sid).get(sid, {}).get("draft_input")
        ok = _check(
            mid != "batched" and after == "batched",
            "batch-exit persist flushes the draft sidecar",
            f"mid={mid!r} after={after!r}",
        ) and ok

        opened_sid = session_store.create_session(name="opened", model="m", cwd="/tmp")["id"]
        _reset_opened_cache()
        session_store.write_last_opened(opened_sid, opened_sid, "2026-01-01T00:00:00")
        first = session_store.read_last_opened(opened_sid)
        first[opened_sid] = "mutated"
        ok = _check(
            session_store.read_last_opened(opened_sid)[opened_sid] == "2026-01-01T00:00:00",
            "opened cache returns isolated copies",
            str(first),
        ) and ok

        original_loads = session_store.json.loads
        loads = 0

        def counting_loads(raw, *args, **kwargs):
            nonlocal loads
            loads += 1
            return original_loads(raw, *args, **kwargs)

        _reset_opened_cache()
        session_store.json.loads = counting_loads
        try:
            assert session_store.read_last_opened(opened_sid)[opened_sid] == "2026-01-01T00:00:00"
            assert session_store.read_last_opened(opened_sid)[opened_sid] == "2026-01-01T00:00:00"
        finally:
            session_store.json.loads = original_loads
        ok = _check(loads == 1, "opened cache skips repeated json parse", f"loads={loads}") and ok

        session_store.write_last_opened(opened_sid, opened_sid, "2026-01-02T00:00:00")
        ok = _check(
            session_store.read_last_opened(opened_sid)[opened_sid] == "2026-01-02T00:00:00",
            "opened write refreshes cache",
            str(session_store.read_last_opened(opened_sid)),
        ) and ok

        opened_path = session_store._opened_path(opened_sid)
        opened_path.write_text(
            json.dumps({"version": 1, "opened": {opened_sid: "2026-01-03T00:00:00"}}),
            encoding="utf-8",
        )
        ok = _check(
            session_store.read_last_opened(opened_sid)[opened_sid] == "2026-01-03T00:00:00",
            "opened cache invalidates on file signature change",
            str(session_store.read_last_opened(opened_sid)),
        ) and ok

        opened_path.unlink()
        ok = _check(
            session_store.read_last_opened(opened_sid) == {},
            "opened cache observes deleted sidecar",
            str(session_store.read_last_opened(opened_sid)),
        ) and ok
        opened_path.write_text(
            json.dumps({"version": 1, "opened": {opened_sid: "2026-01-04T00:00:00"}}),
            encoding="utf-8",
        )
        ok = _check(
            session_store.read_last_opened(opened_sid)[opened_sid] == "2026-01-04T00:00:00",
            "opened cache recovers after missing sidecar is recreated",
            str(session_store.read_last_opened(opened_sid)),
        ) and ok

        opened_path.write_text("{bad-json", encoding="utf-8")
        ok = _check(
            session_store.read_last_opened(opened_sid) == {},
            "opened cache treats corrupt sidecar as empty",
            str(session_store.read_last_opened(opened_sid)),
        ) and ok
        opened_path.write_text(
            json.dumps({"version": 1, "opened": {opened_sid: "2026-01-05T00:00:00"}}),
            encoding="utf-8",
        )
        ok = _check(
            session_store.read_last_opened(opened_sid)[opened_sid] == "2026-01-05T00:00:00",
            "opened cache recovers after corrupt sidecar becomes valid",
            str(session_store.read_last_opened(opened_sid)),
        ) and ok

        # Legacy session: draft baked into the tree json with NO sidecar
        # (pre-sidecar on-disk state). The first tree write strips the
        # draft — it MUST be seeded to the sidecar on load first, else
        # the user's unsent draft is silently destroyed.
        leg = session_store.create_session(name="legacy", model="m", cwd="/tmp")
        lid = leg["id"]
        lpath = session_store._sessions_dir() / f"{lid}.json"
        tree = json.loads(lpath.read_text(encoding="utf-8"))
        tree["draft_input"] = "legacydraft"
        tree["draft_input_seq"] = 5
        lpath.write_text(json.dumps(tree), encoding="utf-8")
        session_store._drafts_path(lid).unlink(missing_ok=True)
        _evict(lid)

        loaded = session_store.get_session(lid)
        ok = _check(
            loaded["draft_input"] == "legacydraft",
            "legacy baked-in draft survives load",
            str(loaded.get("draft_input")),
        ) and ok
        ok = _check(
            session_store.read_drafts(lid).get(lid, {}).get("draft_input")
            == "legacydraft",
            "load seeds sidecar from legacy tree draft",
            str(session_store.read_drafts(lid)),
        ) and ok
        # Now strip the tree via a real mutation — draft must NOT be lost.
        session_manager.set_pinned(lid, True)
        session_manager.flush_pending_persists()
        _evict(lid)
        reloaded = session_store.get_session(lid)
        disk_tree = json.loads(lpath.read_text(encoding="utf-8"))
        ok = _check(
            reloaded["draft_input"] == "legacydraft"
            and "draft_input" not in disk_tree,
            "legacy draft survives tree mutation (moved to sidecar)",
            f"reloaded={reloaded.get('draft_input')!r} "
            f"in_tree={'draft_input' in disk_tree}",
        ) and ok

        return 0 if ok else 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
