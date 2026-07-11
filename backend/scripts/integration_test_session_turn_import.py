from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import uuid


_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home


_IMPORT_HOME = _test_home.TestHome.acquire("ba-test-turn-import-import-")

import portable_lock  # noqa: E402
import session_store  # noqa: E402
from paths import ba_home  # noqa: E402
import stores.session_turn_import as turn_import  # noqa: E402
from stores.session_turn_import import (  # noqa: E402
    CorruptSessionTree,
    CutoverAborted,
    cutover_root,
    import_lock_path,
    import_root_turns,
    revert_cutover,
    verify_root_import,
)
from stores.session_turn_store import AuthorityConflict, SessionTurnStore  # noqa: E402


def _message(content: str, role: str = "user") -> dict:
    return {
        "id": str(uuid.uuid4()),
        "role": role,
        "content": content,
        "events": [],
        "isStreaming": False,
        "timestamp": 1234.5,
    }


def _seed_root_with_fork() -> tuple[str, dict]:
    session = session_store.create_session(name="import-fixture", cwd="/tmp")
    root_id = session["id"]
    root = session_store.get_root_tree(root_id)
    root["messages"] = [_message("first user prompt"), _message("assistant reply", "assistant")]
    root["forks"] = [
        {
            "id": str(uuid.uuid4()),
            "kind": "fork",
            "messages": [_message("fork prompt")],
            "forks": [],
        }
    ]
    session_store.write_session_full(root)
    return root_id, session_store.get_root_tree(root_id)


def _events_path(root_id: str):
    return ba_home() / "sessions" / root_id / "events.jsonl"


def _write_journal_lines(root_id: str, count: int) -> None:
    path = _events_path(root_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for index in range(count):
            handle.write(json.dumps({"seq": index + 1, "sid": root_id, "type": "t", "data": {}}) + "\n")


def test_import_covers_root_and_forks_and_verifies() -> None:
    root_id, _ = _seed_root_with_fork()
    _write_journal_lines(root_id, 3)
    store = SessionTurnStore()
    report = import_root_turns(store, root_id)
    assert report.turns == 3 and report.appended == 3 and report.unchanged == 0
    assert report.contexts == 2
    assert report.journal_cursor == 3
    assert verify_root_import(store, root_id) == []
    checkpoint = store.get_import_checkpoint(root_id)
    assert checkpoint["journal_cursor"] == 3 and checkpoint["turn_count"] == 3

    keys = store.list_turn_keys(root_id)
    assert len(keys) == 3 and all(key["aggregate_version"] == 1 for key in keys)


def test_reimport_is_a_noop() -> None:
    root_id, _ = _seed_root_with_fork()
    store = SessionTurnStore()
    import_root_turns(store, root_id)
    report = import_root_turns(store, root_id)
    assert report.appended == 0 and report.unchanged == 3
    assert all(key["aggregate_version"] == 1 for key in store.list_turn_keys(root_id))
    assert verify_root_import(store, root_id) == []


def test_changed_and_reverted_messages_append_new_versions() -> None:
    root_id, root = _seed_root_with_fork()
    store = SessionTurnStore()
    import_root_turns(store, root_id)

    original = root["messages"][0]["content"]
    root["messages"][0]["content"] = "edited user prompt"
    session_store.write_session_full(root)
    report = import_root_turns(store, root_id)
    assert report.appended == 1 and report.unchanged == 2
    assert verify_root_import(store, root_id) == []

    root = session_store.get_root_tree(root_id)
    root["messages"][0]["content"] = original
    session_store.write_session_full(root)
    report = import_root_turns(store, root_id)
    assert report.appended == 1 and report.unchanged == 2
    assert verify_root_import(store, root_id) == []
    versions = {key["turn_id"]: key["aggregate_version"] for key in store.list_turn_keys(root_id)}
    assert sorted(versions.values()) == [1, 1, 3]


def test_volatile_fields_do_not_affect_import() -> None:
    root_id, root = _seed_root_with_fork()
    store = SessionTurnStore()
    import_root_turns(store, root_id)

    root = session_store.get_root_tree(root_id)
    root["messages"][1]["isStreaming"] = True
    root["messages"][1]["events"] = [{"type": "noise", "uuid": "u-1"}]
    report_states = import_root_turns(store, root_id)
    assert report_states.appended == 0 and report_states.unchanged == 3

    stored = store.get_turn(root_id, root_id, root["messages"][1]["id"])
    assert "events" not in stored["state"]
    assert "isStreaming" not in stored["state"]


def test_verify_detects_drift_missing_and_extras() -> None:
    root_id, root = _seed_root_with_fork()
    store = SessionTurnStore()
    import_root_turns(store, root_id)

    conn = sqlite3.connect(store.path)
    conn.execute(
        "UPDATE turn_aggregates SET state_json='{\"tampered\":true}' "
        "WHERE root_id=? AND turn_id=?",
        (root_id, root["messages"][0]["id"]),
    )
    conn.commit()
    conn.close()
    drift = verify_root_import(store, root_id)
    assert drift == [f"state drift: {root_id}/{root['messages'][0]['id']}"]

    removed = root["messages"].pop()
    session_store.write_session_full(root)
    problems = set(verify_root_import(store, root_id))
    assert f"extra: {root_id}/{removed['id']}" in problems


def test_import_holds_exclusive_per_root_lock() -> None:
    root_id, _ = _seed_root_with_fork()
    store = SessionTurnStore()
    entered = threading.Event()
    release = threading.Event()
    original_load = turn_import._load_root_tree

    def gated_load(target_root_id: str) -> dict:
        entered.set()
        assert release.wait(10), "test release event never fired"
        return original_load(target_root_id)

    turn_import._load_root_tree = gated_load
    try:
        worker = threading.Thread(target=import_root_turns, args=(store, root_id))
        worker.start()
        assert entered.wait(10), "importer never reached the tree read"
        with import_lock_path(store, root_id).open("a+b") as lock_file:
            assert not portable_lock.try_lock_ex(lock_file.fileno()), (
                "import lock was not held across the tree read"
            )
        release.set()
        worker.join(10)
        assert not worker.is_alive()
        with import_lock_path(store, root_id).open("a+b") as lock_file:
            assert portable_lock.try_lock_ex(lock_file.fileno())
            portable_lock.unlock(lock_file.fileno())
    finally:
        turn_import._load_root_tree = original_load
    assert verify_root_import(store, root_id) == []


def test_import_checkpoint_never_regresses() -> None:
    root_id, _ = _seed_root_with_fork()
    store = SessionTurnStore()
    store.record_import_checkpoint(root_id=root_id, journal_cursor=5, turn_count=3)
    store.record_import_checkpoint(root_id=root_id, journal_cursor=3, turn_count=9)
    checkpoint = store.get_import_checkpoint(root_id)
    assert checkpoint["journal_cursor"] == 5 and checkpoint["turn_count"] == 3
    store.record_import_checkpoint(root_id=root_id, journal_cursor=7, turn_count=4)
    assert store.get_import_checkpoint(root_id)["journal_cursor"] == 7


def test_cutover_flips_authority_after_clean_verify() -> None:
    root_id, _ = _seed_root_with_fork()
    store = SessionTurnStore()
    assert store.get_owner_authority(root_id) == "legacy"

    report = cutover_root(store, root_id)
    assert report.verified_turns == 3
    assert store.get_owner_authority(root_id) == "sqlite"

    try:
        cutover_root(store, root_id)
    except CutoverAborted:
        pass
    else:
        raise AssertionError("cutover of an already-flipped root was accepted")

    revert_cutover(store, root_id)
    assert store.get_owner_authority(root_id) == "legacy"
    assert cutover_root(store, root_id).verified_turns == 3


def test_cutover_aborts_on_divergence_and_keeps_legacy() -> None:
    root_id, _ = _seed_root_with_fork()
    store = SessionTurnStore()
    original_load = turn_import._load_root_tree
    calls = {"count": 0}

    def diverging_load(target_root_id: str) -> dict:
        tree = original_load(target_root_id)
        calls["count"] += 1
        if calls["count"] > 1:
            # A legacy writer mutated the tree between the import snapshot
            # and the verify re-read.
            tree["messages"][0]["content"] = "mutated behind the importer"
        return tree

    turn_import._load_root_tree = diverging_load
    try:
        try:
            cutover_root(store, root_id)
        except CutoverAborted as exc:
            assert "semantic compare failed" in str(exc)
        else:
            raise AssertionError("diverging cutover was accepted")
    finally:
        turn_import._load_root_tree = original_load
    assert store.get_owner_authority(root_id) == "legacy"


def test_authority_gate_cas_and_validation() -> None:
    root_id, _ = _seed_root_with_fork()
    store = SessionTurnStore()
    try:
        store.set_owner_authority(root_id, authority="sqlite", expected_authority="sqlite")
    except ValueError:
        pass
    else:
        raise AssertionError("no-op authority flip was accepted")
    for invalid in ("jsonl", "", None):
        try:
            store.set_owner_authority(root_id, authority=invalid, expected_authority="legacy")
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid authority {invalid!r} was accepted")
    try:
        store.set_owner_authority(root_id, authority="legacy", expected_authority="sqlite")
    except AuthorityConflict:
        pass
    else:
        raise AssertionError("stale expected_authority was accepted")


def test_corrupt_trees_fail_closed() -> None:
    root_id, root = _seed_root_with_fork()
    store = SessionTurnStore()

    duplicate = dict(root["messages"][0])
    root["messages"].append(duplicate)
    session_store.write_session_full(root)
    try:
        import_root_turns(store, root_id)
    except CorruptSessionTree:
        pass
    else:
        raise AssertionError("duplicate message id was imported")

    root = session_store.get_root_tree(root_id)
    root["messages"] = [{"role": "user", "content": "no id"}]
    session_store.write_session_full(root)
    try:
        import_root_turns(store, root_id)
    except CorruptSessionTree:
        pass
    else:
        raise AssertionError("id-less message was imported")


def main() -> None:
    tests = [
        test_import_covers_root_and_forks_and_verifies,
        test_reimport_is_a_noop,
        test_changed_and_reverted_messages_append_new_versions,
        test_volatile_fields_do_not_affect_import,
        test_verify_detects_drift_missing_and_extras,
        test_import_holds_exclusive_per_root_lock,
        test_import_checkpoint_never_regresses,
        test_cutover_flips_authority_after_clean_verify,
        test_cutover_aborts_on_divergence_and_keeps_legacy,
        test_authority_gate_cas_and_validation,
        test_corrupt_trees_fail_closed,
    ]
    _IMPORT_HOME.release()
    for test in tests:
        home = _test_home.TestHome.acquire("ba-test-turn-import-")
        try:
            test()
            print(f"PASS {test.__name__}")
        finally:
            home.release()


if __name__ == "__main__":
    main()
