from __future__ import annotations

import asyncio
import json
import sys
import threading
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


import main  # noqa: E402
import ws_serialization  # noqa: E402
from jsonl_tailer import _Subscriber  # noqa: E402
from ws_snapshot_transport import SNAPSHOT_THRESHOLD_BYTES, SnapshotTransport  # noqa: E402


class _BlockingWebSocket:
    def __init__(self) -> None:
        self.closed = False
        self.started = asyncio.Event()

    async def send_text(self, _text: str) -> None:
        self.started.set()
        await asyncio.Event().wait()

    async def close(self) -> None:
        self.closed = True


class _RecordingWebSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed = False

    async def send_text(self, text: str) -> None:
        self.sent.append(text)

    async def close(self) -> None:
        self.closed = True


class _StallingWebSocket(_RecordingWebSocket):
    async def send_text(self, text: str) -> None:
        await asyncio.sleep(0.03)
        await super().send_text(text)


class _PreSerializedEvent(dict):
    pass


class _HangingCloseWebSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, text: str) -> None:
        self.sent.append(text)

    async def close(self) -> None:
        await asyncio.Event().wait()


async def _wait_for(predicate, *, timeout: float = 1.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition timed out")
        await asyncio.sleep(0.01)


def test_ws_outbox_blocked_writer_times_out_without_accepting_frame() -> None:
    async def run() -> None:
        websocket = _BlockingWebSocket()
        closed = asyncio.Event()

        async def on_close() -> None:
            closed.set()

        outbox = main._WebSocketOutbox(
            websocket,
            on_close=on_close,
            send_timeout_s=0.05,
            enqueue_timeout_s=0.02,
            max_items=1,
        )
        assert await asyncio.wait_for(
            outbox.send({"type": "messages_replay", "data": {"messages": []}}),
            timeout=0.02,
        ) is True
        await asyncio.wait_for(websocket.started.wait(), timeout=0.2)
        assert await asyncio.wait_for(
            outbox.send({"type": "session_running_changed", "data": {}}),
            timeout=0.02,
        ) is True
        assert await asyncio.wait_for(
            outbox.send({"type": "agent_message", "data": {}}),
            timeout=0.1,
        ) is False
        await asyncio.wait_for(closed.wait(), timeout=0.5)
        assert websocket.closed is True
        assert await asyncio.wait_for(
            outbox.send({"type": "agent_message", "data": {}}),
            timeout=0.02,
        ) is False
        await outbox.wait_closed()

    asyncio.run(run())


def test_ws_outbox_sends_queued_frames_fifo() -> None:
    async def run() -> None:
        websocket = _RecordingWebSocket()
        closed = asyncio.Event()

        async def on_close() -> None:
            closed.set()

        outbox = main._WebSocketOutbox(websocket, on_close=on_close)
        await outbox.send({"type": "first", "data": {"n": 1}})
        await outbox.send({"type": "second", "data": {"n": 2}})
        await _wait_for(lambda: len(websocket.sent) == 2)
        assert [json.loads(text)["type"] for text in websocket.sent] == [
            "first",
            "second",
        ]
        assert closed.is_set() is False
        await outbox.close()
        await outbox.wait_closed()

    asyncio.run(run())


def test_ws_outbox_records_writer_start_payload_and_wire_without_payload() -> None:
    async def run() -> None:
        websocket = _RecordingWebSocket()

        async def on_close() -> None:
            return None

        records: list[tuple[str, float]] = []
        counts: list[tuple[str, int]] = []
        with (
            mock.patch.object(main.perf, "record", side_effect=lambda name, value: records.append((name, value))),
            mock.patch.object(main.perf, "record_count", side_effect=lambda name, value=1: counts.append((name, value))),
        ):
            outbox = main._WebSocketOutbox(websocket, on_close=on_close)
            assert await outbox.send({"type": "instrumented", "data": {"secret": "not-logged"}})
            await _wait_for(lambda: len(websocket.sent) == 1)
            await outbox.close()
            await outbox.wait_closed()
        names = {name for name, _ in records}
        assert "ws.outbox.writer_start" in names
        assert "ws.send_json.wire" in names
        assert any(name == "ws.serialize.payload_bytes" for name, _ in counts)
        assert all("not-logged" not in name for name, _ in records + counts)
        phases = {name: value for name, value in records if name.startswith("ws.phase.")}
        assert abs(phases["ws.phase.timeline_total"] - phases["ws.phase.timeline_elapsed"]) < 0.1
        assert all(value >= 0 for name, value in records if name.startswith("ws.phase."))

    asyncio.run(run())


def test_ws_outbox_precompleted_serialization_has_disjoint_timeline() -> None:
    async def run() -> None:
        websocket = _RecordingWebSocket()
        event = _PreSerializedEvent(type="instrumented", data={})
        now = main.time.perf_counter()
        frame = ws_serialization.SerializedWebSocketFrame(
            '{"type":"instrumented","data":{}}',
            submit_at=now - 0.003,
            start_at=now - 0.002,
            done_at=now - 0.001,
        )
        event._bc_serialized_json_task = asyncio.create_task(asyncio.sleep(0, result=frame))
        await event._bc_serialized_json_task
        records: list[tuple[str, float]] = []
        with mock.patch.object(
            main.perf, "record", side_effect=lambda name, value: records.append((name, value)),
        ):
            outbox = main._WebSocketOutbox(websocket, on_close=lambda: asyncio.sleep(0))
            assert await outbox.send(event)
            await _wait_for(lambda: len(websocket.sent) == 1)
            await outbox.close()
            await outbox.wait_closed()
        phases = {name: value for name, value in records if name.startswith("ws.phase.")}
        assert "ws.phase.serializer_done_writer_dequeue" in phases
        assert "ws.phase.serializer_await_start_resume" in phases
        assert abs(phases["ws.phase.timeline_total"] - phases["ws.phase.timeline_elapsed"]) < 0.1
        assert all(value >= 0 for value in phases.values())

    asyncio.run(run())


def test_ws_outbox_injected_loop_stall_is_attributed_to_wire_only() -> None:
    async def run() -> None:
        websocket = _StallingWebSocket()
        records: list[tuple[str, float]] = []
        with mock.patch.object(
            main.perf, "record", side_effect=lambda name, value: records.append((name, value)),
        ):
            outbox = main._WebSocketOutbox(websocket, on_close=lambda: asyncio.sleep(0))
            assert await outbox.send({"type": "instrumented", "data": {}})
            await _wait_for(lambda: len(websocket.sent) == 1)
            await outbox.close()
            await outbox.wait_closed()
        phases = {name: value for name, value in records if name.startswith("ws.phase.")}
        assert phases["ws.phase.wire_start_resume"] >= 25
        assert phases["ws.phase.serializer_start_done"] < phases["ws.phase.wire_start_resume"]
        assert abs(phases["ws.phase.timeline_total"] - phases["ws.phase.timeline_elapsed"]) < 0.1

    asyncio.run(run())


def test_ws_serializer_separates_gated_queue_wait_from_encode() -> None:
    async def run() -> None:
        gate = threading.Event()
        executor = ws_serialization._WS_JSON_EXECUTOR
        assert executor is not None
        blockers = [executor.submit(gate.wait) for _ in range(2)]
        records: list[tuple[str, float]] = []
        counts: list[tuple[str, int]] = []
        with (
            mock.patch.object(ws_serialization.perf, "record", side_effect=lambda name, value: records.append((name, value))),
            mock.patch.object(ws_serialization.perf, "record_count", side_effect=lambda name, value=1: counts.append((name, value))),
        ):
            pending = asyncio.create_task(
                ws_serialization.dumps_ws_json({"type": "control", "data": {}}),
            )
            await asyncio.sleep(0)
            assert not pending.done()
            gate.set()
            assert await asyncio.wait_for(pending, timeout=1)
        for blocker in blockers:
            blocker.result(timeout=1)
        names = {name for name, _ in records}
        assert "ws.serialize.queue_wait" in names
        assert "ws.serialize.encode" in names
        assert any(name == "ws.serialize.payload_bytes" for name, _ in counts)

    asyncio.run(run())


def test_ws_metric_type_cardinality_is_registry_bounded() -> None:
    assert ws_serialization.metric_event_type({"type": "messages_delta"}) == "messages_delta"
    assert ws_serialization.metric_event_type({"type": "attacker-value-a"}) == "other"
    assert ws_serialization.metric_event_type({"type": "attacker-value-b"}) == "other"


def test_ws_outbox_backpressures_burst_larger_than_capacity() -> None:
    async def run() -> None:
        websocket = _RecordingWebSocket()

        async def on_close() -> None:
            raise AssertionError("draining websocket must not close")

        outbox = main._WebSocketOutbox(
            websocket,
            on_close=on_close,
            max_items=4,
            enqueue_timeout_s=0.2,
        )
        for n in range(300):
            accepted = await outbox.send({"type": "agent_message", "data": {"n": n}})
            assert accepted is True, n
        await _wait_for(lambda: len(websocket.sent) == 300)
        assert [json.loads(text)["data"]["n"] for text in websocket.sent] == list(range(300))
        assert websocket.closed is False
        await outbox.close()
        await outbox.wait_closed()

    asyncio.run(run())


def test_ws_outbox_close_rejects_waiting_enqueue() -> None:
    async def run() -> None:
        websocket = _BlockingWebSocket()

        async def on_close() -> None:
            return None

        outbox = main._WebSocketOutbox(
            websocket,
            on_close=on_close,
            max_items=1,
            send_timeout_s=1.0,
            enqueue_timeout_s=1.0,
        )
        assert await outbox.send({"type": "first", "data": {}}) is True
        await websocket.started.wait()
        assert await outbox.send({"type": "second", "data": {}}) is True
        waiting = asyncio.create_task(outbox.send({"type": "third", "data": {}}))
        await asyncio.sleep(0)
        await outbox.close()
        assert await asyncio.wait_for(waiting, timeout=0.1) is False
        await outbox.wait_closed()

    asyncio.run(run())


def test_ws_outbox_close_is_timeout_bounded() -> None:
    async def run() -> None:
        websocket = _HangingCloseWebSocket()
        closed = asyncio.Event()

        async def on_close() -> None:
            closed.set()

        outbox = main._WebSocketOutbox(
            websocket,
            on_close=on_close,
            max_items=1,
            enqueue_timeout_s=0.02,
            close_timeout_s=0.02,
        )
        assert await outbox.send({"type": "first", "data": {}}) is True
        await asyncio.wait_for(outbox.close(), timeout=0.05)
        await asyncio.wait_for(closed.wait(), timeout=0.2)
        await outbox.wait_closed()

    asyncio.run(run())


def test_production_callback_rejection_preserves_subscriber_watermark() -> None:
    async def run() -> None:
        websocket = _RecordingWebSocket()

        async def on_close() -> None:
            return None

        outbox: main._WebSocketOutbox | None = main._WebSocketOutbox(
            websocket,
            on_close=on_close,
        )
        await outbox.close()
        await outbox.wait_closed()

        transport = SnapshotTransport(
            principal="user",
            send=lambda frame, serialized=None: outbox.send(frame, serialized),
        )

        async def ws_callback(event_dict):
            return await main._send_ws_callback_event(transport, event_dict)

        sub = _Subscriber(
            app_session_id="sid",
            ws_callback=ws_callback,
            from_seq=0,
            root_id="sid",
        )
        await sub.push_entry(
            {"seq": 1},
            {"type": "agent_message", "data": {}, "seq": 1},
        )
        assert sub.next_seq == 1
        assert await main._send_ws_callback_event(None, {"type": "ignored"}) is False

    asyncio.run(run())


def test_snapshot_transport_preserves_outbox_fifo_with_live_interleave() -> None:
    async def run() -> None:
        websocket = _RecordingWebSocket()

        async def on_close() -> None:
            return None

        outbox = main._WebSocketOutbox(websocket, on_close=on_close)

        async def send(frame, serialized=None) -> bool:
            return await outbox.send(frame, serialized)

        transport = SnapshotTransport(principal="user", send=send)
        assert await transport.send_event({
            "type": "messages_replay",
            "data": {
                "app_session_id": "sid",
                "next_seq": 7,
                "messages": [{"content": "x" * (SNAPSHOT_THRESHOLD_BYTES + 1)}],
            },
        })
        begin = None
        await _wait_for(lambda: bool(websocket.sent))
        begin = json.loads(websocket.sent[0])
        assert await outbox.send({"type": "agent_message", "data": {"seq": 8}})
        assert await transport.acknowledge({
            "type": "snapshot_ack",
            "data": {
                "snapshot_id": begin["data"]["snapshot_id"],
                "revision": begin["data"]["revision"],
                "next_chunk": 0,
            },
        })
        await _wait_for(lambda: len(websocket.sent) >= 4)
        types = [json.loads(text)["type"] for text in websocket.sent[:4]]
        assert types == [
            "snapshot_begin", "agent_message", "snapshot_chunk", "snapshot_chunk",
        ]
        await outbox.close()
        await outbox.wait_closed()

    asyncio.run(run())


def test_snapshot_refresh_roots_have_correlated_terminal_boundary() -> None:
    async def run() -> None:
        frames = []

        async def send(frame) -> bool:
            frames.append(frame)
            return True

        roots = {"sid-a": "root-a", "sid-b": "root-b", "fork": "root-a"}
        scopes = {
            "root-a": {"root-a", "fork"},
            "root-b": {"root-b"},
        }
        with mock.patch.object(
            main.session_manager,
            "_root_id_for",
            side_effect=lambda sid: roots[sid],
        ), mock.patch.object(
            main.session_manager,
            "subtree_ids",
            side_effect=lambda sid: set(scopes[sid]),
        ):
            assert await main._send_snapshot_refresh_roots(
                (("sid-b", "m2"), ("fork", "m3"), ("sid-a", "m1")),
                "f" * 32,
                send,
            )
        assert frames == [
            {
                "type": "session_reconciled",
                "data": {
                    "root_id": "root-a",
                    "scope_sids": ["fork", "root-a"],
                    "snapshot_refresh_id": "f" * 32,
                },
            },
            {
                "type": "session_reconciled",
                "data": {
                    "root_id": "root-b",
                    "scope_sids": ["root-b"],
                    "snapshot_refresh_id": "f" * 32,
                },
            },
            {
                "type": "snapshot_refresh_complete",
                "data": {
                    "refresh_id": "f" * 32,
                    "success": True,
                    "root_ids": ["root-a", "root-b"],
                },
            },
        ]

    asyncio.run(run())


def test_snapshot_refresh_scope_cap_fails_closed_without_partial_authority() -> None:
    async def run() -> None:
        frames = []

        async def send(frame) -> bool:
            frames.append(frame)
            return True

        oversized = {"root", *(
            f"fork-{index}" for index in range(main._SNAPSHOT_REFRESH_MAX_SCOPE_SIDS)
        )}
        with mock.patch.object(
            main.session_manager,
            "_root_id_for",
            return_value="root",
        ), mock.patch.object(
            main.session_manager,
            "subtree_ids",
            return_value=oversized,
        ):
            assert await main._send_snapshot_refresh_roots(
                (("root", None),),
                "e" * 32,
                send,
            )
        assert frames == [{
            "type": "snapshot_refresh_complete",
            "data": {
                "refresh_id": "e" * 32,
                "success": False,
                "root_ids": [],
            },
        }]

    asyncio.run(run())


def test_snapshot_refresh_scope_cap_is_aggregate_across_roots() -> None:
    async def run() -> None:
        frames = []

        async def send(frame) -> bool:
            frames.append(frame)
            return True

        roots = {"sid-a": "root-a", "sid-b": "root-b"}
        scopes = {
            "root-a": {"root-a", *(f"a-{index}" for index in range(299))},
            "root-b": {"root-b", *(f"b-{index}" for index in range(299))},
        }
        with mock.patch.object(
            main.session_manager,
            "_root_id_for",
            side_effect=lambda sid: roots[sid],
        ), mock.patch.object(
            main.session_manager,
            "subtree_ids",
            side_effect=lambda root_id: set(scopes[root_id]),
        ):
            assert await main._send_snapshot_refresh_roots(
                (("sid-a", None), ("sid-b", None)),
                "d" * 32,
                send,
            )
        assert frames == [{
            "type": "snapshot_refresh_complete",
            "data": {
                "refresh_id": "d" * 32,
                "success": False,
                "root_ids": [],
            },
        }]

    asyncio.run(run())
