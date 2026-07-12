import os
import shutil
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _test_home
_test_home.isolate("bc_test_hydrate_bulk_")

from event_ingester import event_ingester  # noqa: E402
from orchs import get_strategy  # noqa: E402
import render_tree_hydrate  # noqa: E402
import hydration_index_store  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402


def _topology(tree: dict) -> tuple:
    return (
        tree["id"],
        tuple(m["id"] for m in tree.get("messages", [])),
        tuple(_topology(child) for child in tree.get("forks", [])),
    )


def test_live_tree_lease_serializes_fork_and_survives_reload() -> None:
    session = session_manager.create(name="lease", cwd="/tmp", orchestration_mode="native")
    sid = session["id"]
    session_manager.set_agent_sid(sid, "native", "agent-lease")
    entered = threading.Event()
    release = threading.Event()
    fork_done = threading.Event()
    errors: list[BaseException] = []

    def holder() -> None:
        with session_manager.live_tree(sid) as root:
            assert root is not None
            root.setdefault("lease_race_mapping", {})["started"] = True
            entered.set()
            assert release.wait(5)
            root["lease_race_mapping"]["finished"] = True

    def forker() -> None:
        try:
            session_manager.fork(sid, name="barrier-fork")
        except BaseException as exc:
            errors.append(exc)
        finally:
            fork_done.set()

    holder_thread = threading.Thread(target=holder)
    fork_thread = threading.Thread(target=forker)
    holder_thread.start()
    assert entered.wait(5)
    fork_thread.start()
    time.sleep(0.05)
    assert not fork_done.is_set(), "fork escaped the live-tree owner lock"
    release.set()
    holder_thread.join(5)
    fork_thread.join(5)
    assert not holder_thread.is_alive() and not fork_thread.is_alive()
    assert not errors, errors

    def hydrate_and_fork(index: int) -> None:
        try:
            assert session_manager.hydrate_root_prepared(sid)
            session_manager.fork(sid, name=f"lease-fork-{index}")
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=hydrate_and_fork, args=(index,)) for index in range(100)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(15)
    assert not any(thread.is_alive() for thread in threads), "hydrate/fork deadlock"
    assert not errors, errors
    before = session_manager.get_root_tree(sid)
    assert before is not None and len(before.get("forks", [])) == 101
    before_topology = _topology(before)
    session_manager.reload_root_from_disk(sid)
    after = session_manager.get_root_tree(sid)
    assert after is not None
    assert _topology(after) == before_topology


def main() -> int:
    try:
        session = session_manager.create(
            name="bulk", cwd="/tmp", orchestration_mode="native",
        )
        sid = session["id"]
        session_manager.get_ref(sid)["agent_session_id"] = "agent-root"
        msg_id = "msg-bulk"
        session_manager.append_assistant_msg(
            sid,
            {
                "id": msg_id,
                "role": "assistant",
                "content": "",
                "events": [],
                "timestamp": "2026-06-19T00:00:00",
                "isStreaming": False,
                "workers": [],
            },
        )
        for i in range(1000):
            event_ingester.ingest(
                sid,
                sid=sid,
                event_type="agent_message",
                data={
                    "uuid": str(uuid.uuid4()),
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": f"chunk {i}"}],
                    },
                },
                source="bulk-test",
                msg_id=msg_id,
            )
        event_ingester.close_all()

        original_prepare = render_tree_hydrate.prepare_hydration
        prepare_calls = 0

        def topology_racing_prepare(*args, **kwargs):
            nonlocal prepare_calls
            prepared = original_prepare(*args, **kwargs)
            prepare_calls += 1
            if prepare_calls == 1:
                session_manager.fork(sid, name="prepare-topology-race")
            return prepared

        render_tree_hydrate.prepare_hydration = topology_racing_prepare
        try:
            assert session_manager.hydrate_root_prepared(sid)
        finally:
            render_tree_hydrate.prepare_hydration = original_prepare
        assert prepare_calls >= 2, prepare_calls

        original_decode = render_tree_hydrate.decode_prepared_hydration
        decode_calls = 0

        def journal_racing_decode(prepared):
            nonlocal decode_calls
            decoded = original_decode(prepared)
            decode_calls += 1
            if decode_calls == 1:
                event_ingester.ingest(
                    sid,
                    sid=sid,
                    event_type="ai-title",
                    data={"uuid": str(uuid.uuid4()), "title": "race"},
                    source="decode-race-test",
                    msg_id=msg_id,
                )
            return decoded

        render_tree_hydrate.decode_prepared_hydration = journal_racing_decode
        session_manager._event_hydrated_roots.discard(sid)
        try:
            assert session_manager.hydrate_root_prepared(sid)
        finally:
            render_tree_hydrate.decode_prepared_hydration = original_decode
        assert decode_calls >= 2, decode_calls

        backlog_decode_calls = 0

        def backlog_counted_decode(prepared):
            nonlocal backlog_decode_calls
            backlog_decode_calls += 1
            return original_decode(prepared)

        render_tree_hydrate.decode_prepared_hydration = backlog_counted_decode
        try:
            started = time.perf_counter()
            assert all(
                session_manager.hydrate_root_prepared(sid)
                for _ in range(2_000)
            )
            elapsed = time.perf_counter() - started
        finally:
            render_tree_hydrate.decode_prepared_hydration = original_decode
        assert backlog_decode_calls == 0, backlog_decode_calls
        assert elapsed < 0.25, elapsed

        appended_data = {
            "uuid": str(uuid.uuid4()),
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "post-hydration append"}],
            },
        }
        appended_seq = event_ingester.ingest(
            sid,
            sid=sid,
            event_type="agent_message",
            data=appended_data,
            source="bulk-test",
            msg_id=msg_id,
        )
        assert session_manager.apply_written_journal_event(
            sid, sid, msg_id, "agent_message", appended_data, appended_seq,
        )
        live = session_manager.get_root_tree(sid)
        live_msg = next(m for m in live["messages"] if m["id"] == msg_id)
        assert any(
            (event.get("data") or {}).get("uuid") == appended_data["uuid"]
            for event in live_msg["events"]
        )
        session_manager.reload_root_from_disk(sid)
        restored = session_manager.get_root_tree(sid)
        restored_msg = next(m for m in restored["messages"] if m["id"] == msg_id)
        assert any(
            (event.get("data") or {}).get("uuid") == appended_data["uuid"]
            for event in restored_msg["events"]
        )

        strategy = get_strategy("native")
        original_apply = strategy.apply_event
        calls = 0

        def counted_apply(*args, **kwargs):
            nonlocal calls
            calls += 1
            return original_apply(*args, **kwargs)

        strategy.apply_event = counted_apply
        try:
            with session_manager.live_tree(sid) as root:
                assert root is not None
                msg = next(m for m in root["messages"] if m["id"] == msg_id)
                msg["events"] = []
                msg.pop("_uid_idx", None)
            session_manager._event_hydrated_roots.discard(sid)
            assert session_manager.hydrate_root_prepared(sid)
            fork = session_manager.fork(sid, name="fork")
        finally:
            strategy.apply_event = original_apply

        assert len(msg["events"]) == 1001, len(msg["events"])
        assert calls == 0, calls

        fork_sid = fork["id"]
        fork_msg_id = "msg-fork"
        session_manager.append_assistant_msg(
            fork_sid,
            {
                "id": fork_msg_id,
                "role": "assistant",
                "content": "",
                "events": [],
                "timestamp": "2026-06-19T00:00:01",
                "isStreaming": False,
                "workers": [],
            },
        )
        event_ingester.ingest(
            sid,
            sid=fork_sid,
            event_type="agent_message",
            data={
                "uuid": str(uuid.uuid4()),
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "fork chunk"}],
                },
            },
            source="bulk-test",
            msg_id=fork_msg_id,
        )
        event_ingester.close_all()
        original_build_index = render_tree_hydrate._build_hydration_index
        read_calls = 0

        def counted_build_index(*args, **kwargs):
            nonlocal read_calls
            read_calls += 1
            return original_build_index(*args, **kwargs)

        render_tree_hydrate._build_hydration_index = counted_build_index
        try:
            with session_manager.live_tree(sid) as root:
                assert root is not None
                fork_node = next(
                    node for node in root["forks"] if node.get("id") == fork_sid
                )
                msg = next(m for m in root["messages"] if m["id"] == msg_id)
                msg["events"] = []
                msg.pop("_uid_idx", None)
                fork_msg = next(m for m in fork_node["messages"] if m["id"] == fork_msg_id)
                fork_msg["events"] = []
                fork_msg.pop("_uid_idx", None)
            session_manager._event_hydrated_roots.discard(sid)
            assert session_manager.hydrate_root_prepared(sid)
        finally:
            render_tree_hydrate._build_hydration_index = original_build_index

        assert read_calls == 1, read_calls
        assert fork_msg["events"], fork_msg

        cursor = event_ingester.current_seq(sid) or 0
        tail_msg_id = "msg-tail"
        session_manager.append_assistant_msg(
            sid,
            {
                "id": tail_msg_id,
                "role": "assistant",
                "content": "",
                "events": [],
                "timestamp": "2026-06-19T00:00:02",
                "isStreaming": False,
                "workers": [],
            },
        )
        event_ingester.ingest(
            sid,
            sid=sid,
            event_type="agent_message",
            data={
                "uuid": str(uuid.uuid4()),
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "tail chunk"}],
                },
            },
            source="bulk-test",
            msg_id=tail_msg_id,
        )
        event_ingester.close_all()

        read_calls = 0
        scan_starts: list[int] = []
        project_calls = 0
        original_project_content_snapshot = render_tree_hydrate.project_content_snapshot
        original_scan = hydration_index_store._scan

        def counted_project_content_snapshot(*args, **kwargs):
            nonlocal project_calls
            project_calls += 1
            return original_project_content_snapshot(*args, **kwargs)

        def counted_scan(conn, journal, start, digest):
            scan_starts.append(start)
            return original_scan(conn, journal, start, digest)

        render_tree_hydrate._build_hydration_index = counted_build_index
        render_tree_hydrate.project_content_snapshot = counted_project_content_snapshot
        hydration_index_store._scan = counted_scan
        try:
            with session_manager.live_tree(sid) as root:
                assert root is not None
                tail_msg = next(m for m in root["messages"] if m["id"] == tail_msg_id)
                for node in (root, *root.get("forks", [])):
                    for existing_msg in node.get("messages", []):
                        if existing_msg.get("id") != tail_msg_id:
                            existing_msg["content"] = "already projected"
            assert session_manager.hydrate_root_prepared(sid, after_seq=cursor)
        finally:
            render_tree_hydrate._build_hydration_index = original_build_index
            render_tree_hydrate.project_content_snapshot = original_project_content_snapshot
            hydration_index_store._scan = original_scan

        assert read_calls == 1, read_calls
        assert scan_starts and scan_starts[0] > 0, scan_starts
        assert project_calls == 1, project_calls
        assert len(tail_msg["events"]) == 1, tail_msg
        test_live_tree_lease_serializes_fork_and_survives_reload()
        print("PASS: live hydrate bulk-loads ordinary render events")
        return 0
    finally:
        shutil.rmtree(os.environ["BETTER_CLAUDE_HOME"], ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
