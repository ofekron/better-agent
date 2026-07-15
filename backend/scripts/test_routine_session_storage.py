import asyncio
import os
import sys
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home  # noqa: E402

_TMP_HOME = _test_home.isolate("bc-test-routine-session-storage-")

import runtime_ownership  # noqa: E402
runtime_ownership.register_current_process_writer()

import event_ingester  # noqa: E402
import native_files_manager  # noqa: E402
import session_manager as session_manager_mod  # noqa: E402
import session_miner  # noqa: E402
import session_queue_projection  # noqa: E402
import session_store  # noqa: E402
import task_runner  # noqa: E402
import user_msg_lifecycle  # noqa: E402
from paths import ba_home  # noqa: E402

failures: list[str] = []


def check(cond, msg):
    print(("  PASS" if cond else "  FAIL") + f": {msg}")
    if not cond:
        failures.append(msg)


async def _create_routine_session() -> dict:
    return (
        await task_runner._resolve_launch_session(
            {
                "id": "routine-alpha",
                "name": "Routine Alpha",
                "cwd": "/tmp/routine-alpha",
                "orchestration_mode": "native",
                "worker_creation_policy": "approve",
                "session_type": "normal",
            },
            model="model-a",
            provider_id="provider-a",
            reasoning_effort=None,
        )
    )[0]


def test_routine_session_uses_routine_directory():
    print("T1 routine launch stores root outside sessions dir")
    session = asyncio.run(_create_routine_session())
    sid = session["id"]
    expected = ba_home() / "routine-sessions" / "routine-alpha" / f"{sid}.json"
    flat = ba_home() / "sessions" / f"{sid}.json"
    check(Path(session_store.session_file_path(sid)) == expected, "session_file_path resolves routine path")
    check(expected.exists(), "routine session root written under routine directory")
    check(not flat.exists(), "routine session root not written under sessions directory")
    check(session_store.get_session(sid)["id"] == sid, "get_session reads scoped root")
    check(session_store.get_root_tree(sid)["id"] == sid, "get_root_tree reads scoped root")
    listed_ids = {item["id"] for item in session_store.list_sessions()}
    check(sid in listed_ids, "list_sessions includes scoped routine root")
    check(event_ingester.event_ingester._events_path(sid) == expected.parent / sid / "events.jsonl",
          "event ingester stores events beside scoped root")
    seq = event_ingester.event_ingester.ingest(
        sid,
        sid,
        "user_message_done",
        {"uuid": "done-1", "lifecycle_msg_id": "life-1"},
        source="test",
    )
    check(seq == 1, "event ingester writes scoped events jsonl")
    terminal = user_msg_lifecycle.terminal_event_for_lifecycle(sid, "life-1")
    check(terminal is not None and terminal["type"] == "user_message_done",
          "lifecycle lookup reads scoped events jsonl")
    native_path = native_files_manager.native_files._native_paths_path(sid)
    check(native_path == expected.parent / sid / "native_paths",
          "native path sidecar lives beside scoped root")


def test_scoped_roots_feed_projections_and_mining():
    print("T2 scoped roots feed queue projection and session miner")
    session = session_manager_mod.manager.create(
        name="Scoped Direct",
        cwd="/tmp/scoped-direct",
        model="model-b",
        provider_id="provider-b",
        storage_scope={"kind": "routine", "routine_id": "routine-beta"},
    )
    sid = session["id"]
    fingerprint = session_queue_projection._session_files_fingerprint()
    check(
        any(Path(relative).stem == sid for relative in fingerprint),
        "queue projection fingerprints scoped root",
    )
    rebuilt = session_queue_projection.rebuild_from_disk()
    check(rebuilt >= 1, "queue projection rebuild sees scoped root")
    visits = list(session_miner.SessionMiner({}))
    check(sid in {visit.sid for visit in visits}, "session miner visits scoped root")


def test_delete_cleans_scoped_sidecars():
    print("T3 delete removes scoped sidecars")
    session = session_manager_mod.manager.create(
        name="Scoped Delete",
        cwd="/tmp/scoped-delete",
        model="model-c",
        provider_id="provider-c",
        storage_scope={"kind": "routine", "routine_id": "routine-delete"},
    )
    sid = session["id"]
    root_path = Path(session_store.session_file_path(sid))
    session_store.write_seen_cursor(sid, sid, "uid-1")
    session_store.write_last_opened(sid, sid, "2026-01-01T00:00:00")
    sidecars = [
        root_path.with_name(f"{sid}.seen.json"),
        root_path.with_name(f"{sid}.opened.json"),
    ]
    check(all(path.exists() for path in sidecars), "scoped sidecars created")
    check(session_store.delete_session(sid), "scoped root deleted")
    check(not root_path.exists(), "scoped root file removed")
    check(all(not path.exists() for path in sidecars), "scoped sidecars removed")


def test_reuse_paths_require_matching_scope():
    print("T4 routine reuse paths require scoped sessions")
    flat = session_manager_mod.manager.create(
        name="Flat Singleton",
        cwd="/tmp/flat-singleton",
        model="model-d",
        provider_id="provider-d",
    )
    task = {"id": "routine-singleton", "singleton_session_id": flat["id"]}
    check(task_runner._resolve_singleton_session(task) is None,
          "flat singleton is not reused for routine")
    spec = task_runner._provisioned_task_spec(
        {"id": "routine-provisioned", "name": "Provisioned", "cwd": "/tmp/prov"},
        model="model-e",
        provider_id="provider-e",
        reasoning_effort=None,
    )
    check(spec.storage_scope == {"kind": "routine", "routine_id": "routine-provisioned"},
          "routine provisioned spec carries routine storage scope")


def main() -> int:
    test_routine_session_uses_routine_directory()
    test_scoped_roots_feed_projections_and_mining()
    test_delete_cleans_scoped_sidecars()
    test_reuse_paths_require_matching_scope()
    print()
    if failures:
        print(f"{len(failures)} FAILURES")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
