from __future__ import annotations

import os
import shutil
import sys
import threading
import time
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_BACKEND = _HERE.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import _test_home

_TMP_HOME = _test_home.isolate("bc-wsb-")
os.environ["BETTER_CLAUDE_API_ONLY"] = "1"

from fastapi.testclient import TestClient  # noqa: E402

import auth  # noqa: E402
import main  # noqa: E402
import ws_snapshot_transport  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402


def test_replay_precedes_buffered_live_frames() -> None:
    session = session_manager.create(
        name="subscribe-barrier",
        model="m",
        cwd="/tmp",
        orchestration_mode="native",
    )
    sid = session["id"]
    session_manager.append_user_msg(sid, {
        "id": "before",
        "role": "user",
        "content": "before",
        "events": [],
        "isStreaming": False,
    })

    replay_entered = threading.Event()
    release_replay = threading.Event()
    original_build = main._build_messages_replay_delta

    def blocked_build(*args, **kwargs):
        replay_entered.set()
        if not release_replay.wait(5):
            raise TimeoutError("test did not release replay")
        return original_build(*args, **kwargs)

    main._build_messages_replay_delta = blocked_build
    token = auth.create_token("subscribe-barrier-test")
    outcome: dict[str, object] = {}

    def websocket_client() -> None:
        try:
            with TestClient(main.app, client=("127.0.0.1", 50000)) as client:
                with client.websocket_connect(f"/ws/chat?token={token}") as ws:
                    ws.send_json({
                        "type": "subscribe",
                        "app_session_id": sid,
                        "since_seq": 0,
                        "events_from_seq": 0,
                        "events_cursor_known": True,
                        "generation": 1,
                    })
                    relevant: list[dict] = []
                    while len(relevant) < 3:
                        frame = ws.receive_json()
                        if frame.get("type") not in {
                            "messages_replay",
                            "messages_delta",
                            "subscription_ready",
                            "subscription_failed",
                        }:
                            continue
                        relevant.append(frame)
                        if len(relevant) == 1 and frame.get("type") != "messages_replay":
                            break
                        if frame.get("type") == "subscription_failed":
                            break
                    outcome["frames"] = relevant
        except BaseException as exc:
            outcome["error"] = exc

    try:
        thread = threading.Thread(target=websocket_client, daemon=True)
        thread.start()
        if not replay_entered.wait(10):
            raise AssertionError(
                f"subscribe never entered replay construction; outcome={outcome!r}"
            )
        session_manager.append_assistant_msg(sid, {
            "id": "during",
            "role": "assistant",
            "content": "during",
            "events": [],
            "isStreaming": False,
        })
        time.sleep(0.1)
        release_replay.set()
        thread.join(10)
        assert not thread.is_alive(), "websocket test did not terminate"
        if "error" in outcome:
            raise outcome["error"]  # type: ignore[misc]
    finally:
        release_replay.set()
        main._build_messages_replay_delta = original_build

    frames = outcome.get("frames")
    assert isinstance(frames, list)
    assert [frame["type"] for frame in frames] == [
        "messages_replay",
        "messages_delta",
        "subscription_ready",
    ], frames
    assert all(frame.get("subscription_generation") == 1 for frame in frames)
    replay_ids = {
        message.get("id")
        for message in frames[0].get("data", {}).get("messages", [])
    }
    delta_ids = {
        message.get("id")
        for message in frames[1].get("data", {}).get("messages", [])
    }
    assert "during" in replay_ids or "during" in delta_ids


def test_chunked_replay_finishes_before_buffered_frames() -> None:
    session = session_manager.create(
        name="chunked-subscribe-barrier",
        model="m",
        cwd="/tmp",
        orchestration_mode="native",
    )
    sid = session["id"]
    session_manager.append_user_msg(sid, {
        "id": "large",
        "role": "user",
        "content": "x" * (300 * 1024),
        "events": [],
        "isStreaming": False,
    })
    begin_seen = threading.Event()
    release_acks = threading.Event()
    outcome: dict[str, object] = {}
    token = auth.create_token("chunked-barrier-test")

    def websocket_client() -> None:
        try:
            with TestClient(main.app, client=("127.0.0.1", 50001)) as client:
                with client.websocket_connect(f"/ws/chat?token={token}") as ws:
                    ws.send_json({
                        "type": "subscribe",
                        "app_session_id": sid,
                        "since_seq": 0,
                        "events_from_seq": 0,
                        "events_cursor_known": True,
                        "generation": 1,
                    })
                    ordered: list[str] = []
                    begin: dict | None = None
                    next_chunk = 0
                    while "subscription_ready" not in ordered:
                        frame = ws.receive_json()
                        frame_type = frame.get("type")
                        if frame_type == "snapshot_begin":
                            begin = frame
                            ordered.append(frame_type)
                            begin_seen.set()
                            if not release_acks.wait(5):
                                raise TimeoutError("test did not release snapshot acknowledgements")
                            ws.send_json({
                                "type": "snapshot_ack",
                                "data": {
                                    "snapshot_id": begin["data"]["snapshot_id"],
                                    "revision": begin["data"]["revision"],
                                    "next_chunk": 0,
                                },
                            })
                            continue
                        if frame_type == "snapshot_chunk":
                            ordered.append(frame_type)
                            next_chunk = max(next_chunk, int(frame["data"]["index"]) + 1)
                            assert begin is not None
                            ws.send_json({
                                "type": "snapshot_ack",
                                "data": {
                                    "snapshot_id": begin["data"]["snapshot_id"],
                                    "revision": begin["data"]["revision"],
                                    "next_chunk": next_chunk,
                                },
                            })
                            continue
                        if frame_type in {
                            "snapshot_end",
                            "messages_delta",
                            "subscription_ready",
                            "subscription_failed",
                        }:
                            ordered.append(frame_type)
                    outcome["ordered"] = ordered
        except BaseException as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=websocket_client, daemon=True)
    thread.start()
    assert begin_seen.wait(10), "chunked replay never began"
    session_manager.append_assistant_msg(sid, {
        "id": "chunk-during",
        "role": "assistant",
        "content": "during",
        "events": [],
        "isStreaming": False,
    })
    time.sleep(0.1)
    release_acks.set()
    thread.join(10)
    assert not thread.is_alive(), "chunked websocket test did not terminate"
    if "error" in outcome:
        raise outcome["error"]  # type: ignore[misc]
    ordered = outcome.get("ordered")
    assert isinstance(ordered, list), outcome
    assert ordered.index("snapshot_end") < ordered.index("messages_delta"), ordered
    assert ordered.index("messages_delta") < ordered.index("subscription_ready"), ordered


def test_resubscribe_survives_prior_generation_build_failure() -> None:
    session = session_manager.create(
        name="resubscribe-failure-barrier",
        model="m",
        cwd="/tmp",
        orchestration_mode="native",
    )
    sid = session["id"]
    first_entered = threading.Event()
    release_first = threading.Event()
    second_subscribed = threading.Event()
    counter_lock = threading.Lock()
    calls = 0
    active = 0
    max_active = 0
    original_build = main._build_messages_replay_delta

    def raced_build(*args, **kwargs):
        nonlocal calls, active, max_active
        with counter_lock:
            calls += 1
            call = calls
            active += 1
            max_active = max(max_active, active)
        try:
            if call == 1:
                first_entered.set()
                if not release_first.wait(5):
                    raise TimeoutError("test did not release first replay")
                raise RuntimeError("generation one replay failed")
            return original_build(*args, **kwargs)
        finally:
            with counter_lock:
                active -= 1

    main._build_messages_replay_delta = raced_build
    outcome: dict[str, object] = {}
    token = auth.create_token("resubscribe-failure-test")

    def websocket_client() -> None:
        try:
            with TestClient(main.app, client=("127.0.0.1", 50002)) as client:
                with client.websocket_connect(f"/ws/chat?token={token}") as ws:
                    base = {
                        "type": "subscribe",
                        "app_session_id": sid,
                        "since_seq": 0,
                        "events_from_seq": 0,
                        "events_cursor_known": True,
                    }
                    ws.send_json({**base, "generation": 1})
                    if not first_entered.wait(5):
                        raise TimeoutError("first replay did not start")
                    ws.send_json({**base, "generation": 2})
                    second_subscribed.set()
                    frames: list[dict] = []
                    while True:
                        frame = ws.receive_json()
                        if frame.get("type") in {
                            "messages_replay",
                            "subscription_ready",
                            "subscription_failed",
                        }:
                            frames.append(frame)
                        if (
                            frame.get("type") == "subscription_ready"
                            and frame.get("subscription_generation") == 2
                        ):
                            break
                    outcome["frames"] = frames
        except BaseException as exc:
            outcome["error"] = exc

    try:
        thread = threading.Thread(target=websocket_client, daemon=True)
        thread.start()
        assert second_subscribed.wait(10), "generation two subscribe was not sent"
        release_first.set()
        thread.join(10)
        assert not thread.is_alive(), "resubscribe websocket test did not terminate"
        if "error" in outcome:
            raise outcome["error"]  # type: ignore[misc]
    finally:
        release_first.set()
        main._build_messages_replay_delta = original_build
    frames = outcome.get("frames")
    assert isinstance(frames, list), outcome
    assert max_active == 1
    assert not any(frame.get("type") == "subscription_failed" for frame in frames), frames
    assert [frame.get("subscription_generation") for frame in frames[-2:]] == [2, 2]


def test_rapid_generations_keep_one_bootstrap_waiter() -> None:
    session = session_manager.create(
        name="rapid-generation-barrier",
        model="m",
        cwd="/tmp",
        orchestration_mode="native",
    )
    sid = session["id"]
    first_entered = threading.Event()
    release_first = threading.Event()
    generations_sent = threading.Event()
    original_build = main._build_messages_replay_delta
    original_create_task = main.asyncio.create_task
    build_lock = threading.Lock()
    build_calls = 0
    active_builds = 0
    max_active_builds = 0
    bootstrap_tasks: list[object] = []
    max_live_bootstraps = 0

    def blocked_build(*args, **kwargs):
        nonlocal build_calls, active_builds, max_active_builds
        with build_lock:
            build_calls += 1
            call = build_calls
            active_builds += 1
            max_active_builds = max(max_active_builds, active_builds)
        try:
            if call == 1:
                first_entered.set()
                if not release_first.wait(5):
                    raise TimeoutError("test did not release rapid replay")
            return original_build(*args, **kwargs)
        finally:
            with build_lock:
                active_builds -= 1

    def tracking_create_task(coro, *args, **kwargs):
        nonlocal max_live_bootstraps
        task = original_create_task(coro, *args, **kwargs)
        name = kwargs.get("name")
        if isinstance(name, str) and name.startswith("ws-bootstrap-"):
            bootstrap_tasks.append(task)
            live = sum(not tracked.done() for tracked in bootstrap_tasks)
            max_live_bootstraps = max(max_live_bootstraps, live)
        return task

    main._build_messages_replay_delta = blocked_build
    main.asyncio.create_task = tracking_create_task
    outcome: dict[str, object] = {}
    token = auth.create_token("rapid-generation-test")

    def websocket_client() -> None:
        try:
            with TestClient(main.app, client=("127.0.0.1", 50004)) as client:
                with client.websocket_connect(f"/ws/chat?token={token}") as ws:
                    base = {
                        "type": "subscribe",
                        "app_session_id": sid,
                        "since_seq": 0,
                        "events_from_seq": 0,
                        "events_cursor_known": True,
                    }
                    ws.send_json({**base, "generation": 1})
                    if not first_entered.wait(5):
                        raise TimeoutError("rapid replay did not start")
                    for generation in range(2, 26):
                        ws.send_json({**base, "generation": generation})
                    generations_sent.set()
                    while True:
                        frame = ws.receive_json()
                        if (
                            frame.get("type") == "subscription_ready"
                            and frame.get("subscription_generation") == 25
                        ):
                            outcome["ready"] = True
                            break
        except BaseException as exc:
            outcome["error"] = exc

    try:
        thread = threading.Thread(target=websocket_client, daemon=True)
        thread.start()
        assert generations_sent.wait(10), "rapid generations were not sent"
        time.sleep(0.5)
        release_first.set()
        thread.join(10)
        assert not thread.is_alive(), "rapid generation test did not terminate"
        if "error" in outcome:
            raise outcome["error"]  # type: ignore[misc]
    finally:
        release_first.set()
        main._build_messages_replay_delta = original_build
        main.asyncio.create_task = original_create_task
    assert outcome.get("ready") is True
    assert max_active_builds == 1
    assert build_calls == 2
    assert max_live_bootstraps == 1
    assert all(task.done() for task in bootstrap_tasks)


def test_resubscribe_replaces_events_cursor_without_gap() -> None:
    session = session_manager.create(
        name="resubscribe-events-cursor",
        model="m",
        cwd="/tmp",
        orchestration_mode="native",
    )
    sid = session["id"]
    token = auth.create_token("resubscribe-events-cursor-test")

    def current_subscriber(expected_next_seq: int):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            matches = [
                subscriber
                for (subscriber_sid, _), subscriber
                in list(main.coordinator._subscriber_index.items())
                if subscriber_sid == sid
            ]
            if len(matches) == 1 and matches[0].next_seq == expected_next_seq:
                return matches[0]
            time.sleep(0.01)
        actual = [
            subscriber.next_seq
            for (subscriber_sid, _), subscriber
            in list(main.coordinator._subscriber_index.items())
            if subscriber_sid == sid
        ]
        raise AssertionError(
            f"subscriber cursor did not reach {expected_next_seq}; actual={actual}"
        )

    def subscribe_and_wait(ws, generation: int, events_from_seq: int) -> None:
        ws.send_json({
            "type": "subscribe",
            "app_session_id": sid,
            "since_seq": 0,
            "events_from_seq": events_from_seq,
            "events_cursor_known": True,
            "generation": generation,
        })
        while True:
            frame = ws.receive_json()
            if (
                frame.get("type") == "subscription_ready"
                and frame.get("subscription_generation") == generation
            ):
                return
            assert frame.get("type") != "subscription_failed", frame

    with mock.patch.object(
        main,
        "_floor_events_from_seq",
        side_effect=lambda _sid, seq, *, cursor_known: seq,
    ):
        with TestClient(main.app, client=("127.0.0.1", 50005)) as client:
            with client.websocket_connect(f"/ws/chat?token={token}") as ws:
                subscribe_and_wait(ws, 1, 1000)
                first = current_subscriber(1001)
                subscribe_and_wait(ws, 2, 2000)
                second = current_subscriber(2001)
                assert second is not first


def test_unsubscribed_replay_builds_self_evict_after_completion() -> None:
    sessions = [
        session_manager.create(
            name=f"unsubscribe-replay-build-{index}",
            model="m",
            cwd="/tmp",
            orchestration_mode="native",
        )
        for index in range(2)
    ]
    sids = [session["id"] for session in sessions]
    token = auth.create_token("unsubscribe-replay-build-test")
    original_build = main._build_messages_replay_delta
    original_create_task = main.asyncio.create_task
    release_builds = threading.Event()
    both_started = threading.Event()
    builds_finished = threading.Event()
    unsubscribed = threading.Event()
    close_socket = threading.Event()
    count_lock = threading.Lock()
    started = 0
    finished = 0
    replay_build_registries: list[dict] = []
    outcome: dict[str, object] = {}

    def blocked_build(*args, **kwargs):
        nonlocal started, finished
        with count_lock:
            started += 1
            if started == len(sids):
                both_started.set()
        if not release_builds.wait(5):
            raise TimeoutError("test did not release replay builds")
        try:
            return original_build(*args, **kwargs)
        finally:
            with count_lock:
                finished += 1
                if finished == len(sids):
                    builds_finished.set()

    def tracking_create_task(coro, *args, **kwargs):
        task = original_create_task(coro, *args, **kwargs)
        name = kwargs.get("name")
        if isinstance(name, str) and name.startswith("ws-replay-build-"):
            def capture_registry() -> None:
                for callback_entry in task._callbacks or []:
                    callback = (
                        callback_entry[0]
                        if isinstance(callback_entry, tuple)
                        else callback_entry
                    )
                    if getattr(callback, "__name__", "") != "_evict_completed_replay_build":
                        continue
                    for cell in callback.__closure__ or ():
                        value = cell.cell_contents
                        if isinstance(value, dict) and value not in replay_build_registries:
                            replay_build_registries.append(value)

            task.get_loop().call_soon(capture_registry)
        return task

    def websocket_client() -> None:
        try:
            with TestClient(main.app, client=("127.0.0.1", 50006)) as client:
                with client.websocket_connect(f"/ws/chat?token={token}") as ws:
                    for sid in sids:
                        ws.send_json({
                            "type": "subscribe",
                            "app_session_id": sid,
                            "since_seq": 0,
                            "events_from_seq": 0,
                            "events_cursor_known": True,
                            "generation": 1,
                        })
                    if not both_started.wait(5):
                        raise TimeoutError("replay builds did not start")
                    for sid in sids:
                        ws.send_json({
                            "type": "unsubscribe",
                            "app_session_id": sid,
                            "generation": 1,
                        })
                    unsubscribed.set()
                    if not close_socket.wait(10):
                        raise TimeoutError("test did not close websocket")
        except BaseException as exc:
            outcome["error"] = exc

    main._build_messages_replay_delta = blocked_build
    main.asyncio.create_task = tracking_create_task
    thread = threading.Thread(target=websocket_client, daemon=True)
    try:
        thread.start()
        assert unsubscribed.wait(10), "unsubscribe frames were not sent"
        release_builds.set()
        assert builds_finished.wait(10), "shielded replay builds did not finish"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and (
            len(replay_build_registries) != 1 or replay_build_registries[0]
        ):
            time.sleep(0.02)
        assert len(replay_build_registries) == 1
        assert replay_build_registries[0] == {}, "completed build results remain retained"
    finally:
        release_builds.set()
        close_socket.set()
        thread.join(10)
        main._build_messages_replay_delta = original_build
        main.asyncio.create_task = original_create_task
    assert not thread.is_alive(), "unsubscribe cleanup websocket did not terminate"
    if "error" in outcome:
        raise outcome["error"]  # type: ignore[misc]


def test_rejected_replay_fails_subscription_without_ready() -> None:
    session = session_manager.create(
        name="rejected-replay-barrier",
        model="m",
        cwd="/tmp",
        orchestration_mode="native",
    )
    sid = session["id"]
    session_manager.append_user_msg(sid, {
        "id": "oversized",
        "role": "user",
        "content": "x" * (300 * 1024),
        "events": [],
        "isStreaming": False,
    })
    token = auth.create_token("rejected-replay-test")
    frames: list[dict] = []
    with mock.patch.object(
        ws_snapshot_transport,
        "SNAPSHOT_MAX_PAYLOAD_BYTES",
        ws_snapshot_transport.SNAPSHOT_THRESHOLD_BYTES,
    ):
        with TestClient(main.app, client=("127.0.0.1", 50003)) as client:
            with client.websocket_connect(f"/ws/chat?token={token}") as ws:
                ws.send_json({
                    "type": "subscribe",
                    "app_session_id": sid,
                    "since_seq": 0,
                    "events_from_seq": 0,
                    "events_cursor_known": True,
                    "generation": 1,
                })
                while True:
                    frame = ws.receive_json()
                    if frame.get("type") in {
                        "snapshot_refresh_required",
                        "messages_delta",
                        "subscription_ready",
                        "subscription_failed",
                    }:
                        frames.append(frame)
                    if frame.get("type") in {"subscription_ready", "subscription_failed"}:
                        break
                assert not main.coordinator.ws_callbacks.get(sid)
    assert [frame["type"] for frame in frames] == [
        "snapshot_refresh_required",
        "subscription_failed",
    ], frames
    assert frames[-1].get("subscription_generation") == 1


if __name__ == "__main__":
    try:
        test_replay_precedes_buffered_live_frames()
        test_chunked_replay_finishes_before_buffered_frames()
        test_resubscribe_survives_prior_generation_build_failure()
        test_rapid_generations_keep_one_bootstrap_waiter()
        test_resubscribe_replaces_events_cursor_without_gap()
        test_unsubscribed_replay_builds_self_evict_after_completion()
        test_rejected_replay_fails_subscription_without_ready()
        print("PASS: websocket replay barrier preserves bootstrap ordering")
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
